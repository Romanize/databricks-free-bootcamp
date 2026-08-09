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
* A tool behind a registered MCP server does NOT run when the agent decides to
  call it. The turn ends with an `mcp_approval_request` item and waits to be
  told yes. The Playground answers that for you; this app has to do it itself,
  which is why `stream()` is a loop and not a single request. Before that, every
  tool-using question stopped at "I'll check your holdings for you." and the
  tracing table stayed empty - the tool had genuinely never run.
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

# How many times one question may come back asking to run more tools. The agent
# is told to call tools one at a time, so a thorough answer legitimately needs
# several rounds; the cap only exists so a loop cannot run forever.
MAX_TOOL_ROUNDS = int(os.environ.get("CAPSTONE_CHAT_TOOL_ROUNDS", 8))

# A tool reached through a registered MCP server is not run on the agent's
# say-so: the turn stops and asks permission. By default that question is put to
# the user in the chat tab. Set CAPSTONE_CHAT_AUTO_APPROVE=1 to answer yes
# automatically, which is defensible here - the MCP server has no dangerous tool
# to approve, since `execute_trade` needs a single-use key only the user can mint
# by clicking Accept on the Trades tab - but it does mean tools run unannounced.
AUTO_APPROVE = os.environ.get("CAPSTONE_CHAT_AUTO_APPROVE", "0").lower() in ("1", "true", "yes")


class ChatError(Exception):
    """A user-facing chat failure."""


class _NotStreamable(Exception):
    """Internal: this endpoint would not stream, so use the blocking path."""


def is_configured() -> bool:
    return bool(ENDPOINT)


def status() -> dict:
    """What the UI shows when the agent is not wired up yet."""
    if is_configured():
        # auto_approve is the STARTING position of the chat tab's toggle, not a
        # rule: the request carries whichever way the user has since set it.
        return {"configured": True, "endpoint": ENDPOINT, "auto_approve": AUTO_APPROVE}
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


def _tools_used(payload) -> list:
    """Tool names in a complete (non-streamed) response, in call order."""
    if not isinstance(payload, dict):
        return []
    return [
        str(item.get("name"))
        for item in payload.get("output") or []
        if isinstance(item, dict) and item.get("type") == "function_call" and item.get("name")
    ]


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


def _blocking(messages: list) -> dict:
    """
    One non-streamed call, used only when the endpoint will not stream.

    Note this path cannot answer an approval request - it has no way to send a
    second turn - so a tool-using question here comes back as whatever the agent
    managed to say before it stopped to ask.
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
    return {"reply": reply, "endpoint": ENDPOINT, "tools": _tools_used(parsed)}


def _stream_delta(event: dict) -> str:
    """Pull the incremental text out of one streamed event, if it has any."""
    # `response.function_call_arguments.delta` carries a `delta` string too, but
    # it is the tool's raw JSON arguments being assembled, not the agent
    # speaking. Without this guard `{"refresh_prices": true}` is typed into the
    # chat window one fragment at a time, in front of the actual answer.
    kind = str(event.get("type") or "")
    if "function_call" in kind or "arguments" in kind:
        return ""

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


# How a turn reports tool use. A locally-defined tool arrives as `function_call`;
# a tool reached through a registered MCP server arrives as `mcp_approval_request`
# (the agent asking to run it) and then `mcp_call` (it running).
_TOOL_STARTS = ("function_call", "mcp_approval_request", "mcp_call")
_TOOL_ENDS = ("function_call_output", "mcp_call_output")


def _item_of(event: dict) -> dict:
    """The output item a frame is about, if it is about one."""
    item = event.get("item")
    if isinstance(item, dict):
        return item
    if event.get("type") in _TOOL_STARTS + _TOOL_ENDS:
        return event
    return {}


def _tool_item(event: dict):
    """
    Recognise tool activity in a streamed event.

    Returns (id, name, finished), or None when the frame is not about a tool.

    This has to exist separately because tool frames carry NO text:
    `_stream_delta` finds nothing in them and `_extract_reply` skips every item
    that is not a `message`. Without this, a turn that called six tools looked
    from the browser exactly like a turn that called none - a long silence, then
    an answer out of nowhere.
    """
    item = _item_of(event)
    kind = item.get("type")
    if kind not in _TOOL_STARTS + _TOOL_ENDS:
        return None

    identifier = str(item.get("call_id") or item.get("id") or item.get("name") or "")
    name = str(item.get("name") or "")
    # An `mcp_call` is both the call and its result: it is finished once it
    # carries an output or an error.
    finished = kind in _TOOL_ENDS or (
        kind == "mcp_call" and (item.get("output") is not None or item.get("error"))
    )
    return identifier, name, finished


def _resume_items(resume: dict) -> list:
    """
    Rebuild a paused turn from the client's decisions.

    The paused conversation travels to the browser and back rather than being
    held in server memory: a Databricks App can be restarted or run more than
    one worker, and a pending approval that only one process knows about
    silently stops working when the next request lands elsewhere.
    """
    items = resume.get("items")
    if not isinstance(items, list) or not items:
        raise ChatError("That approval is no longer valid. Please ask again.")

    decisions = [
        {
            "type": "mcp_approval_response",
            "approval_request_id": str(decision.get("id")),
            "approve": bool(decision.get("approve")),
        }
        for decision in resume.get("approvals") or []
        if isinstance(decision, dict) and decision.get("id")
    ]
    if not decisions:
        raise ChatError("No approval decision was sent.")

    return [item for item in items if isinstance(item, dict)] + decisions


def _post_stream(items: list):
    """POST one turn and yield every parsed SSE frame as (event_name, event)."""
    url, headers = _url()
    response = requests.post(
        url,
        headers={**headers, "Accept": "text/event-stream"},
        json={"input": items, "stream": True},
        stream=True,
        timeout=TIMEOUT,
    )
    if response.status_code >= 400:
        raise _NotStreamable(f"HTTP {response.status_code}: {(response.text or '')[:200]}")

    name = ""
    for raw in response.iter_lines(decode_unicode=True):
        line = (raw or "").strip()
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
            event = json.loads(chunk)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            # Turn this on when the chat tab shows no tool activity: it says
            # whether the endpoint emits tool frames at all, or only text.
            logger.debug("chat stream event: %s", event.get("type"))
            yield name, event


def stream(messages: list | None = None, *, resume: dict | None = None,
           auto_approve: bool | None = None):
    """
    Yield the agent's answer in pieces as it is written.

    Emits {"type": "delta", "text": ...} and {"type": "tool", ...} as they
    happen, then {"type": "done"}.

    ## Why this is a loop and not one request

    A tool reached through a registered MCP server is not run on the agent's
    say-so. The turn ENDS with an `mcp_approval_request` item - "may I run
    get_holdings_breakdown?" - and the caller is expected to send the answer
    back and let the agent carry on. Nothing runs until it does.

    The Playground does this for you, which is exactly why the agent worked
    there and stalled here: this app used to read that turn, find some text in
    it ("I'll check your holdings for you."), and stop. The tool was never run,
    so no result ever existed and the tracing table stayed empty.

By default the question is passed on to the user: the generator emits an
    `approval` event carrying the tool name, its arguments and the paused
    conversation, and stops. The browser shows Accept / Reject and calls back in
    with `resume=`, which continues the same turn. Set AUTO_APPROVE to answer
    yes without asking.

    Falls back to a blocking call when streaming is unavailable. An error the
    agent reports about itself is NOT retried - it would fail identically and
    only delay the message that explains it.
    """
    if not is_configured():
        raise ChatError(status()["message"])
    if auto_approve is None:
        auto_approve = AUTO_APPROVE

    items = _resume_items(resume) if resume else list(_clean(messages or []))
    streamed_any = False
    # The agent often says something before it calls a tool ("I'll check your
    # holdings for you.") and then carries on in the next round. Neither half
    # brings its own spacing, so without this they collide mid-sentence. Only
    # needed within one response: a resumed turn opens a fresh message bubble.
    needs_break = False
    saw_tool = False
    # name -> chip id, so a result frame ticks off the call that opened it
    # instead of adding a second chip next to it.
    open_tools: dict[str, str] = {}
    known: dict[str, str] = {}

    try:
        for round_number in range(1, MAX_TOOL_ROUNDS + 1):
            produced: list = []
            approvals: list = []

            for name, event in _post_stream(items):
                failure = _failure_in(name, event)
                if failure:
                    raise ChatError(failure)

                item = _item_of(event)
                if item and str(event.get("type", "")).endswith(".done"):
                    # Kept so the next round can replay this turn back to a
                    # stateless endpoint, which has no memory of it otherwise.
                    produced.append(item)
                    if item.get("type") == "mcp_approval_request":
                        approvals.append(item)

                tool = _tool_item(event)
                if tool:
                    identifier, tool_name, finished = tool
                    if item.get("type") == "mcp_approval_request" and not auto_approve:
                        continue  # the approval card names it; a chip would double it
                    if not finished:
                        if tool_name and tool_name not in open_tools:
                            open_tools[tool_name] = identifier
                            known[identifier] = tool_name
                            saw_tool = True
                            yield {"type": "tool", "id": identifier,
                                   "name": tool_name, "status": "running"}
                        continue
                    tool_name = tool_name or known.get(identifier) or "tool"
                    yield {"type": "tool",
                           "id": open_tools.pop(tool_name, identifier),
                           "name": tool_name, "status": "done"}
                    continue

                piece = _stream_delta(event)
                if piece:
                    if needs_break:
                        needs_break = False
                        if streamed_any:
                            yield {"type": "delta", "text": "\n\n"}
                    streamed_any = True
                    yield {"type": "delta", "text": piece}
                    continue

                # Some endpoints stream nothing but a single final object.
                final = event.get("response") if isinstance(event.get("response"), dict) else event
                text = _extract_reply(final)
                if text and not streamed_any:
                    streamed_any = True
                    yield {"type": "delta", "text": text}

            if not approvals:
                break

            if not auto_approve:
                logger.info("Pausing for approval of %d tool call(s)", len(approvals))
                yield {
                    "type": "approval",
                    "requests": [
                        {
                            "id": approval.get("id"),
                            "name": approval.get("name"),
                            "arguments": approval.get("arguments"),
                            "server": approval.get("server_label"),
                        }
                        for approval in approvals
                    ],
                    # Everything needed to pick the turn back up, opaque to the
                    # client, which only stores it and sends it back.
                    "state": {"items": items + produced},
                }
                yield {"type": "done"}
                return

            logger.info("Approving %d MCP tool call(s), round %d",
                        len(approvals), round_number)
            needs_break = True
            items = items + produced + [
                {
                    "type": "mcp_approval_response",
                    "approval_request_id": approval.get("id"),
                    "approve": True,
                }
                for approval in approvals
            ]
        else:
            raise ChatError(
                f"The agent asked to run tools more than {MAX_TOOL_ROUNDS} times "
                "without finishing an answer."
            )

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

    answer = _blocking(messages)
    # Only if the stream itself reported none - otherwise the fallback would
    # list the same calls a second time.
    if not saw_tool:
        for index, used in enumerate(answer.get("tools") or []):
            yield {"type": "tool", "id": f"blocking-{index}", "name": used, "status": "done"}
    yield {"type": "delta", "text": answer["reply"]}
    yield {"type": "done"}


def ask(messages: list) -> dict:
    """
    Forward a conversation and wait for the whole answer.

    Used by the trade-approval path, which hands the confirmation key to the
    agent and needs the complete reply. It drains `stream()` rather than making
    its own request, so it gets the approval loop too - without it the key
    handoff would stall at "I'll execute that trade now" and never execute it.
    """
    pieces: list[str] = []
    tools: list[str] = []
    seen: set = set()

    # auto_approve is forced: this call has no user attached to it. It runs when
    # the app itself messages the agent (handing over a trade confirmation key),
    # and there is nobody watching a chat window to click Accept.
    for event in stream(messages, auto_approve=True):
        if event["type"] == "delta":
            pieces.append(event["text"])
        elif event["type"] == "tool" and event["id"] not in seen:
            seen.add(event["id"])
            tools.append(event["name"])
        elif event["type"] == "error":
            raise ChatError(event["message"])

    reply = "".join(pieces).strip()
    if not reply:
        raise ChatError("The agent replied, but with no text in it.")
    return {"reply": reply, "endpoint": ENDPOINT, "tools": tools}
