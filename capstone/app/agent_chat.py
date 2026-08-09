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

import json
import logging
import os

import requests

logger = logging.getLogger(__name__)

ENDPOINT = os.environ.get("CAPSTONE_AGENT_ENDPOINT", "").strip()

# Keep the forwarded history bounded: an Agent Bricks endpoint has its own
# context limit, and a long portfolio conversation with tool results in it can
# get there faster than you would expect.
MAX_HISTORY_MESSAGES = int(os.environ.get("CAPSTONE_CHAT_HISTORY", 20))
MAX_MESSAGE_CHARS = 4000

# An agent that calls several MCP tools before answering is not fast.
TIMEOUT = int(os.environ.get("CAPSTONE_CHAT_TIMEOUT", 120))


class ChatError(Exception):
    """A user-facing chat failure."""


class _NotStreamable(Exception):
    """Internal: this endpoint will not stream, so use the blocking path."""


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
# `input` first: Agent Bricks agents are ResponsesAgents, and theirs is the
# shape they accept. The other two stay as fallbacks so a differently-authored
# agent still works, but the common case now costs one request, not three.
REQUEST_SHAPES = ("input", "messages", "dataframe_records")

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


def _parse_sse(body: str) -> dict | str:
    """
    Turn a Server-Sent Events stream into something `_extract_reply` can read.

    A streaming Responses endpoint answers with a sequence of `data: {...}`
    lines rather than one JSON document, which is why plain json.loads() on the
    body fails at character 0. Two things can be recovered from it: a final
    event carrying the whole response object (preferred - it has the same shape
    as the non-streaming reply), or the incremental text deltas, which are
    stitched back together as a fallback.
    """
    final: dict | None = None
    deltas: list[str] = []

    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        chunk = line[len("data:"):].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            event = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        # A `response` object only counts as the final answer once it actually
        # carries text. The opening `response.created` event contains one that
        # holds nothing but an id, and accepting that would discard every delta
        # that follows it.
        response_obj = event.get("response")
        if isinstance(response_obj, dict) and _extract_reply(response_obj):
            final = response_obj
        elif _extract_reply(event) and (
            event.get("output") or event.get("choices") or event.get("messages")
        ):
            final = event
        elif isinstance(event.get("delta"), str):
            deltas.append(event["delta"])
        elif isinstance(event.get("text"), str) and event.get("type", "").endswith("delta"):
            deltas.append(event["text"])

    if final is not None:
        return final
    return "".join(deltas)


def _post(payload: dict) -> tuple[dict | str, str]:
    """
    POST to the endpoint and return (parsed body, error). Exactly one is real.

    Deliberately a plain request rather than serving_endpoints.query(): that
    helper deserializes into a fixed dataclass with no `output` field, so a
    ResponsesAgent's entire answer is dropped on the way in and you are left
    holding an id and three empty lists. Here nothing is discarded, and when
    something does go wrong the status and the body are in the message.
    """
    from databricks.sdk import WorkspaceClient

    config = WorkspaceClient().config
    url = f"{config.host.rstrip('/')}/serving-endpoints/{ENDPOINT}/invocations"
    headers = {"Content-Type": "application/json", **config.authenticate()}

    try:
        response = requests.post(url, headers=headers, json=payload, timeout=TIMEOUT)
    except requests.Timeout:
        return None, f"no response within {TIMEOUT}s"
    except requests.RequestException as err:
        return None, str(err)

    body = (response.text or "").strip()
    if response.status_code >= 400:
        return None, f"HTTP {response.status_code}: {body[:400]}"
    if not body:
        return None, f"HTTP {response.status_code} with an empty body"

    content_type = response.headers.get("Content-Type", "")
    if "event-stream" in content_type or body.startswith("data:"):
        return _parse_sse(body), ""

    try:
        return response.json(), ""
    except ValueError:
        return None, f"body was not JSON (Content-Type {content_type!r}): {body[:200]}"


def ask(messages: list) -> dict:
    """
    Forward a conversation to the agent and return its reply.

    `messages` is the running [{role, content}] history from the browser.
    """
    global _working_shape

    if not is_configured():
        raise ChatError(status()["message"])

    history = _clean(messages)
    shapes = [_working_shape] if _working_shape else list(REQUEST_SHAPES)
    failures = []

    for shape in shapes:
        body, error = _post(_build(shape, history))
        if error:
            logger.warning("Agent endpoint %s rejected the %r shape: %s", ENDPOINT, shape, error)
            failures.append(f"{shape}: {error}")
            _working_shape = None
            continue

        reply = _extract_reply(body)
        if reply:
            if _working_shape != shape:
                logger.info("Agent endpoint %s speaks the %r shape", ENDPOINT, shape)
                _working_shape = shape
            return {"reply": reply, "endpoint": ENDPOINT, "shape": shape}

        failures.append(f"{shape}: replied, but with no text in it")
        _working_shape = None

    logger.error("Agent endpoint %s failed every request shape", ENDPOINT)
    raise ChatError(
        f"Could not get a reply from the agent endpoint {ENDPOINT!r}. "
        + " | ".join(failures)
    )

# ---------------------------------------------------------------- streaming


def _stream_delta(event: dict) -> str:
    """Pull the incremental text out of one streamed event, if it has any."""
    if not isinstance(event, dict):
        return ""

    # Responses API: {"type": "response.output_text.delta", "delta": "..."}
    delta = event.get("delta")
    if isinstance(delta, str):
        return delta
    if isinstance(delta, dict):
        if isinstance(delta.get("content"), str):
            return delta["content"]
        for block in delta.get("content") or []:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                return block["text"]

    # Chat completions: {"choices": [{"delta": {"content": "..."}}]}
    for choice in event.get("choices") or []:
        piece = ((choice or {}).get("delta") or {}).get("content")
        if isinstance(piece, str):
            return piece

    if event.get("type", "").endswith("delta") and isinstance(event.get("text"), str):
        return event["text"]

    return ""


def stream(messages: list):
    """
    Yield the agent's answer in pieces as it is written.

    Emits {"type": "delta", "text": ...} repeatedly, then {"type": "done"}.

    Falls back to the blocking `ask()` and emits the whole answer as one delta
    whenever streaming is not available - a request the endpoint refuses, a
    stream that carries no text, or a transport error before any text arrived.
    Once text HAS been shown, a mid-stream failure is reported as an error
    rather than retried, because replaying would duplicate what the user is
    already reading.
    """
    if not is_configured():
        raise ChatError(status()["message"])

    history = _clean(messages)
    streamed_any = False

    try:
        from databricks.sdk import WorkspaceClient

        config = WorkspaceClient().config
        url = f"{config.host.rstrip('/')}/serving-endpoints/{ENDPOINT}/invocations"
        response = requests.post(
            url,
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                **config.authenticate(),
            },
            json={"input": history, "stream": True},
            stream=True,
            timeout=TIMEOUT,
        )

        if response.status_code >= 400:
            detail = (response.text or "")[:200]
            logger.info("Endpoint %s refused to stream (%s): %s",
                        ENDPOINT, response.status_code, detail)
            raise _NotStreamable(f"HTTP {response.status_code}")

        for raw in response.iter_lines(decode_unicode=True):
            if not raw:
                continue
            line = raw.strip()
            if not line.startswith("data:"):
                continue
            chunk = line[len("data:"):].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                event = json.loads(chunk)
            except json.JSONDecodeError:
                continue

            piece = _stream_delta(event)
            if piece:
                streamed_any = True
                yield {"type": "delta", "text": piece}
                continue

            # Some endpoints stream nothing but a single final object.
            final = event.get("response") if isinstance(event.get("response"), dict) else event
            text = _extract_reply(final)
            if text and not streamed_any:
                streamed_any = True
                yield {"type": "delta", "text": text}

        if not streamed_any:
            raise _NotStreamable("the stream carried no text")

        yield {"type": "done"}
        return

    except _NotStreamable as err:
        logger.info("Falling back to a blocking call: %s", err)
    except Exception as err:
        if streamed_any:
            logger.exception("Stream broke after text had been sent")
            yield {"type": "error", "message": f"The reply was cut short: {err}"}
            return
        logger.info("Streaming failed before any text (%s); falling back", err)

    # Fallback: one blocking call, delivered as a single delta.
    yield {"type": "delta", "text": ask(messages)["reply"]}
    yield {"type": "done"}
