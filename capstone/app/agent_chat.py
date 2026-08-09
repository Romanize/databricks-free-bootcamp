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

Two things about the wire format, both learned the hard way:

* The request field is `input`, not `messages`. Agent Bricks agents are
  ResponsesAgents and reject anything else outright.
* `WorkspaceClient().serving_endpoints.query()` is not used here. It
  deserializes into a dataclass with no `output` field, so a ResponsesAgent's
  entire answer is dropped in transit and the caller is left holding a response
  id and three empty lists - indistinguishable from an endpoint that returned
  nothing. Posting to `/invocations` directly keeps the whole reply.

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
    """Internal: this endpoint would not stream, so use the blocking path."""


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


def _url() -> tuple[str, dict]:
    """The invocations URL and the headers to call it with."""
    from databricks.sdk import WorkspaceClient

    config = WorkspaceClient().config
    return (
        f"{config.host.rstrip('/')}/serving-endpoints/{ENDPOINT}/invocations",
        {"Content-Type": "application/json", **config.authenticate()},
    )


# ------------------------------------------------------------------ responses


def _sse_events(body: str):
    """Yield (event_name, parsed_data) for each frame of an SSE body."""
    name = ""
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            name = ""  # a blank line ends the frame
            continue
        if line.startswith("event:"):
            name = line[len("event:"):].strip()
            continue
        if not line.startswith("data:"):
            continue
        chunk = line[len("data:"):].strip()
        if not chunk or chunk == "[DONE]":
            continue
        try:
            parsed = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            yield name, parsed


def _failure_in(name: str, event: dict) -> str:
    """
    The agent's own error message, if this frame carries one.

    An agent that cannot reach its MCP tools still answers HTTP 200 and then
    says so inside the stream, so this is the only place that failure is
    visible. It has to be surfaced verbatim - "HTTP 401 registering tools" and
    "the model is overloaded" need completely different fixes.
    """
    if name == "error" or event.get("error_code") or event.get("error"):
        error = event.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error)
        return str(event.get("message") or error or event.get("error_code"))
    return ""


def _looks_like_sse(body: str, content_type: str) -> bool:
    # Content-Type is not reliable here: the endpoint labels an SSE error
    # stream as application/json.
    return (
        "event-stream" in content_type
        or body.startswith("data:")
        or body.startswith("event:")
    )


def _extract_reply(payload) -> str:
    """
    Pull the assistant's text out of the response.

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

    if payload.get("output_text"):
        return str(payload["output_text"]).strip()

    # Walked BACKWARDS on purpose. When the agent used tools, `output` holds the
    # whole turn - function_call and function_call_output items first, the
    # spoken answer last - so the first item with text in it is a tool argument
    # blob, not the reply. Only `message` items are the agent talking.
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

    # Older chat-shaped agents, kept because they cost three lines.
    for choice in payload.get("choices") or []:
        text = ((choice or {}).get("message") or {}).get("content")
        if text:
            return text.strip()

    return ""


def _parse_sse(body: str) -> dict | str:
    """
    Turn a whole SSE body into something `_extract_reply` can read.

    Prefers a final event carrying the complete response object, since that has
    the same shape as a non-streaming reply; falls back to stitching the
    incremental text deltas back together.
    """
    final: dict | None = None
    deltas: list[str] = []

    for name, event in _sse_events(body):
        failure = _failure_in(name, event)
        if failure:
            raise ChatError(failure)

        # A `response` object only counts as the final answer once it actually
        # carries text: the opening `response.created` event contains one that
        # holds nothing but an id, and accepting that would discard every delta
        # that follows it.
        response_obj = event.get("response")
        if isinstance(response_obj, dict) and _extract_reply(response_obj):
            final = response_obj
        elif _extract_reply(event) and (event.get("output") or event.get("choices")):
            final = event
        elif isinstance(event.get("delta"), str):
            deltas.append(event["delta"])
        elif isinstance(event.get("text"), str) and event.get("type", "").endswith("delta"):
            deltas.append(event["text"])

    return final if final is not None else "".join(deltas)


# -------------------------------------------------------------------- calling


def ask(messages: list) -> dict:
    """
    Forward a conversation and wait for the whole answer.

    Used by the trade-approval path, which needs a complete reply rather than
    a stream, and as the fallback when streaming is unavailable.
    """
    if not is_configured():
        raise ChatError(status()["message"])

    history = _clean(messages)
    url, headers = _url()

    try:
        response = requests.post(
            url, headers=headers, json={"input": history}, timeout=TIMEOUT
        )
    except requests.Timeout:
        raise ChatError(f"The agent did not answer within {TIMEOUT}s.") from None
    except requests.RequestException as err:
        raise ChatError(f"Could not reach the agent endpoint: {err}") from err

    body = (response.text or "").strip()
    content_type = response.headers.get("Content-Type", "")

    if _looks_like_sse(body, content_type):
        parsed = _parse_sse(body)  # raises ChatError on an error frame
    elif response.status_code >= 400:
        raise ChatError(f"The agent endpoint returned HTTP {response.status_code}: {body[:400]}")
    elif not body:
        raise ChatError(f"The agent endpoint returned HTTP {response.status_code} with an empty body.")
    else:
        try:
            parsed = response.json()
        except ValueError:
            raise ChatError(f"The agent endpoint did not return JSON: {body[:200]}") from None

    reply = _extract_reply(parsed)
    if not reply:
        raise ChatError("The agent replied, but with no text in it.")
    return {"reply": reply, "endpoint": ENDPOINT}


def _stream_delta(event: dict) -> str:
    """Pull the incremental text out of one streamed event, if it has any."""
    delta = event.get("delta")
    if isinstance(delta, str):
        return delta
    if isinstance(delta, dict):
        if isinstance(delta.get("content"), str):
            return delta["content"]
        for block in delta.get("content") or []:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                return block["text"]

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

    Falls back to the blocking `ask()` when streaming is unavailable. An error
    the agent reports about itself is NOT retried - it would fail identically
    and only delay the message that explains it.
    """
    if not is_configured():
        raise ChatError(status()["message"])

    history = _clean(messages)
    url, headers = _url()
    streamed_any = False

    try:
        response = requests.post(
            url,
            headers={**headers, "Accept": "text/event-stream"},
            json={"input": history, "stream": True},
            stream=True,
            timeout=TIMEOUT,
        )

        if response.status_code >= 400:
            raise _NotStreamable(f"HTTP {response.status_code}: {(response.text or '')[:200]}")

        name = ""
        for raw in response.iter_lines(decode_unicode=True):
            line = (raw or "").strip()
            if not line:
                name = ""
                continue
            if line.startswith("event:"):
                name = line[len("event:"):].strip()
                continue
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

            failure = _failure_in(name, event)
            if failure:
                raise ChatError(failure)

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

    except ChatError:
        raise  # the agent's own words; nothing to retry and nothing to add
    except _NotStreamable as err:
        logger.info("Falling back to a blocking call: %s", err)
    except Exception as err:
        if streamed_any:
            logger.exception("Stream broke after text had been sent")
            yield {"type": "error", "message": f"The reply was cut short: {err}"}
            return
        logger.info("Streaming failed before any text (%s); falling back", err)

    yield {"type": "delta", "text": ask(messages)["reply"]}
    yield {"type": "done"}
