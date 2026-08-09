"""
Chat proxy to the deployed Agent Bricks agent.

The app does not run its own tool-calling loop. It forwards the conversation to
the agent's serving endpoint, and the agent - which has the MCP server
registered as a tool source - decides which tools to call. That keeps exactly
one agent in existence: the thing answering in this app is the same thing you
test in the Playground, with the same system prompt and the same guardrails.

The endpoint name is configuration, not code, because it does not exist until
the agent has been deployed. Until CAPSTONE_AGENT_ENDPOINT is set the chat tab
renders and explains itself instead of erroring - see `status()`.

Nothing here can execute a trade. The agent's only trade tool is `propose_trade`,
which queues a row for the approval queue in this same app.
"""

import logging
import os

logger = logging.getLogger(__name__)

ENDPOINT = os.environ.get("CAPSTONE_AGENT_ENDPOINT", "").strip()

# Keep the forwarded history bounded: an Agent Bricks endpoint has its own
# context limit, and a long portfolio conversation with tool results in it can
# get there faster than you would expect.
MAX_HISTORY_MESSAGES = int(os.environ.get("CAPSTONE_CHAT_HISTORY", 20))
MAX_MESSAGE_CHARS = 4000


class ChatError(Exception):
    """A user-facing chat failure."""


def is_configured() -> bool:
    return bool(ENDPOINT)


def status() -> dict:
    """What the UI shows when the agent is not wired up yet."""
    if is_configured():
        return {"configured": True, "endpoint": ENDPOINT}
    return {
        "configured": False,
        "message": (
            "The agent is not connected yet. Deploy the Agent Bricks agent with the "
            "MCP server registered as a tool source, then set CAPSTONE_AGENT_ENDPOINT "
            "in app.yaml to its serving endpoint name and redeploy this app."
        ),
    }


def _clean(messages: list) -> list:
    """Validate and trim the client-supplied history before forwarding it."""
    cleaned = []
    for message in messages[-MAX_HISTORY_MESSAGES:]:
        role = (message or {}).get("role")
        content = ((message or {}).get("content") or "").strip()
        if role not in ("user", "assistant") or not content:
            continue
        cleaned.append({"role": role, "content": content[:MAX_MESSAGE_CHARS]})

    if not cleaned or cleaned[-1]["role"] != "user":
        raise ChatError("The conversation must end with a user message.")
    return cleaned


# Databricks serves agents behind several different request/response schemas,
# and which one you get depends on how the agent was authored - not on anything
# visible from here. Rather than hard-code a guess, try them in order and
# remember the one that worked, so only the first call after a restart pays for
# the discovery. `probe_agent_endpoint.py` prints the same answer by hand.
REQUEST_SHAPES = ("messages", "input", "dataframe_records")

_working_shape: str | None = None


def _build(shape: str, history: list) -> dict:
    if shape == "messages":            # chat completions / ChatAgent
        return {"messages": history}
    if shape == "input":               # ResponsesAgent
        return {"input": history}
    return {"dataframe_records": [{"messages": history}]}


def _extract_reply(payload) -> str:
    """
    Pull the assistant's text out of whichever response schema came back.

    Returns "" when the response parsed fine but held no text - the caller
    treats that as an error, because an empty answer in a chat tab is a bug,
    not a reply.
    """
    if payload is None:
        return ""
    if isinstance(payload, str):
        return payload.strip()
    if not isinstance(payload, dict):
        return ""

    # chat completions: {"choices": [{"message": {"content": ...}}]}
    for choice in payload.get("choices") or []:
        text = ((choice or {}).get("message") or {}).get("content")
        if text:
            return text.strip()

    # ChatAgent: {"messages": [{"role": "assistant", "content": ...}]}
    for message in reversed(payload.get("messages") or []):
        if (message or {}).get("role") == "assistant" and message.get("content"):
            return str(message["content"]).strip()

    # ResponsesAgent: {"id": "resp_...", "output": [{"type": "message",
    #                   "content": [{"type": "output_text", "text": ...}]}]}
    #
    # Walked BACKWARDS on purpose. When the agent used tools, `output` holds the
    # whole turn - function_call and function_call_output items first, the
    # spoken answer last - so the first item with text in it is a tool argument
    # blob, not the reply. Only `message` items are the agent talking.
    if payload.get("output_text"):
        return str(payload["output_text"]).strip()

    for item in reversed(payload.get("output") or []):
        item = item or {}
        if item.get("type") not in (None, "message"):
            continue
        if item.get("role") not in (None, "assistant"):
            continue

        content = item.get("content")
        if isinstance(content, str) and content:
            return content.strip()
        texts = [
            str(block["text"])
            for block in (content or [])
            if isinstance(block, dict) and block.get("text")
        ]
        if texts:
            return "\n".join(texts).strip()

    # dataframe serving: {"predictions": [...]}
    for prediction in payload.get("predictions") or []:
        if isinstance(prediction, str) and prediction:
            return prediction.strip()
        if isinstance(prediction, dict):
            nested = _extract_reply(prediction)
            if nested:
                return nested
            for key in ("content", "response", "text", "result"):
                if prediction.get(key):
                    return str(prediction[key]).strip()

    for key in ("content", "response", "text"):
        if payload.get(key):
            return str(payload[key]).strip()

    return ""


def ask(messages: list) -> dict:
    """
    Forward a conversation to the agent and return its reply.

    `messages` is the running [{role, content}] history from the browser.
    """
    global _working_shape

    if not is_configured():
        raise ChatError(status()["message"])

    history = _clean(messages)

    from databricks.sdk import WorkspaceClient

    client = WorkspaceClient().api_client
    path = f"/serving-endpoints/{ENDPOINT}/invocations"
    shapes = [_working_shape] if _working_shape else list(REQUEST_SHAPES)
    failures = []

    for shape in shapes:
        try:
            response = client.do("POST", path, body=_build(shape, history))
        except Exception as err:
            # A permission or missing-endpoint failure will fail every shape
            # identically, so keep going and report them all together below.
            logger.warning("Agent endpoint %s rejected the %r shape: %s", ENDPOINT, shape, err)
            failures.append(f"{shape}: {err}")
            _working_shape = None
            continue

        reply = _extract_reply(response)
        if reply:
            if _working_shape != shape:
                logger.info("Agent endpoint %s speaks the %r shape", ENDPOINT, shape)
                _working_shape = shape
            return {"reply": reply, "endpoint": ENDPOINT, "shape": shape}

        failures.append(f"{shape}: accepted the request but returned no text")
        _working_shape = None

    logger.error("Agent endpoint %s failed every request shape", ENDPOINT)
    raise ChatError(
        f"Could not get a reply from the agent endpoint {ENDPOINT!r}. "
        + " | ".join(failures)
    )
