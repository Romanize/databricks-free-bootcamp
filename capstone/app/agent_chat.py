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


def ask(messages: list) -> dict:
    """
    Forward a conversation to the agent and return its reply.

    `messages` is the running [{role, content}] history from the browser.
    """
    if not is_configured():
        raise ChatError(status()["message"])

    history = _clean(messages)

    try:
        from databricks.sdk import WorkspaceClient

        response = WorkspaceClient().serving_endpoints.query(
            name=ENDPOINT,
            input=history,
        )
    except Exception as err:
        logger.exception("Agent endpoint %s failed", ENDPOINT)
        raise ChatError(
            f"Could not reach the agent endpoint {ENDPOINT!r}: {err}"
        ) from err

    # Parse the agent response - try multiple formats
    reply = None
    
    # Format 1: Standard chat completion format (choices)
    choices = getattr(response, "choices", None)
    if choices and isinstance(choices, list) and len(choices) > 0:
        message = getattr(choices[0], "message", None)
        if message:
            reply = getattr(message, "content", "") or ""
    
    # Format 2: Data array format
    if not reply:
        data = getattr(response, "data", None)
        if data and isinstance(data, list) and len(data) > 0:
            item = data[0]
            if isinstance(item, dict):
                reply = item.get("content", "") or item.get("response", "")
            else:
                reply = str(item)
    
    # Format 3: Predictions format
    if not reply:
        predictions = getattr(response, "predictions", None)
        if predictions and isinstance(predictions, list) and len(predictions) > 0:
            item = predictions[0]
            if isinstance(item, dict):
                reply = item.get("content", "") or item.get("response", "")
            else:
                reply = str(item)
    
    if not reply:
        response_id = getattr(response, "id", "unknown")
        raise ChatError(
            f"The agent endpoint responded (ID: {response_id}) but returned no content. "
            f"The endpoint may not be fully deployed or configured correctly."
        )

    return {
        "reply": reply,
        "endpoint": ENDPOINT,
    }
