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

import hashlib
import json
import logging
import os
import threading
import time

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


def _blocking(messages: list, *, timeout: int | None = None) -> dict:
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
            url, headers=headers, json={"input": history}, timeout=timeout or TIMEOUT
        )
    except requests.Timeout:
        raise ChatError(f"The agent did not answer within {timeout or TIMEOUT}s.") from None
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


# Tools whose result the browser can draw a chart from. The payload of these -
# and only these - is forwarded to the page alongside the "done" frame, so the
# chart is built from the same numbers the agent was given rather than from
# numbers it retyped into its answer. Everything else stays server-side: a tool
# result is not something the chat window has any business showing.
CHART_TOOLS = {
    "project_scenario",
    "get_investment_plan_projection",
    "get_holdings_breakdown",
    "get_networth_history",
}

# A monthly 80-year projection is a few hundred KB of JSON. Past this something
# is wrong and it is not worth pushing down an SSE stream.
MAX_TOOL_RESULT_CHARS = 400_000


def _tool_result(item: dict) -> dict | None:
    """
    The tool's own JSON payload, when the finished frame carries one.

    The output turns up in three shapes depending on how the tool was reached -
    a JSON string (`function_call_output`), a list of MCP content blocks, or an
    already-parsed object - and none of them is worth guessing between, so all
    three are handled. Returns None whenever anything is off; a chart is a nice
    extra and must never be able to break the answer it sits next to.
    """
    output = item.get("output")

    if isinstance(output, dict):
        structured = output.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        content = output.get("content")
        if isinstance(content, list):
            output = content
        else:
            return output

    if isinstance(output, list):
        output = next(
            (block.get("text") for block in output
             if isinstance(block, dict) and isinstance(block.get("text"), str)),
            None,
        )

    if not isinstance(output, str) or len(output) > MAX_TOOL_RESULT_CHARS:
        return None
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


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
                    finished_event = {"type": "tool",
                                      "id": open_tools.pop(tool_name, identifier),
                                      "name": tool_name, "status": "done"}
                    if tool_name in CHART_TOOLS:
                        result = _tool_result(item)
                        # Only a tool that actually answered: a chart of an
                        # error payload would be an empty axis under a sentence
                        # explaining that there is no data.
                        if result and result.get("status") == "success":
                            finished_event["result"] = result
                    yield finished_event
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


# ---------------------------------------------------------------- suggestions
#
# These are the chips the chat tab shows before the first message, and they are
# on the critical path of opening that tab: nobody waits thirty seconds to be
# told what they could ask. So this is deliberately the CHEAPEST turn in the
# app - no tools, a small model prompt, three questions, a short timeout.
#
# The agent is not asked to look anything up. The app is already holding the
# holdings, the watchlist, the plan and the last report, so it writes them into
# the request as a short fact block. That is the whole latency story: a
# tool-using turn is several round trips through the MCP server before a single
# token is written, and it produced questions no better grounded than the facts
# the app could have handed over for free.

# Three, not five: they arrive sooner and a shorter row is easier to read.
SUGGESTION_COUNT = int(os.environ.get("CAPSTONE_CHAT_SUGGESTION_COUNT", 3))

# The chip row is not worth the full 120s chat budget. If the agent is having a
# slow day the row says so and offers a retry, which is a better outcome than a
# tab that looks stuck.
SUGGESTION_TIMEOUT = int(os.environ.get("CAPSTONE_CHAT_SUGGESTION_TIMEOUT", 25))

# Held between opens of the tab. The key is a hash of the fact block, so a new
# holding or a fresh report invalidates it immediately, while merely switching
# tabs does not.
SUGGESTION_TTL = int(os.environ.get("CAPSTONE_CHAT_SUGGESTION_TTL", 1800))

# A starter chip has to fit on one line next to two others.
SUGGESTION_MAX_CHARS = 90


def _suggestion_request(context: str) -> str:
    """The whole prompt: instructions plus the facts, so no tool call is needed."""
    return (
        f"Write {SUGGESTION_COUNT} short questions the user could ask you next.\n\n"
        "DO NOT CALL ANY TOOLS. Everything you need is in the snapshot below, and "
        "this request is on a timer - answer immediately from it.\n\n"
        f"--- snapshot of their portfolio ---\n{context or 'No data yet.'}\n"
        "--- end of snapshot ---\n\n"
        "Base the questions on that snapshot: name their real tickers where it "
        "helps, and never mention a ticker that is not listed above. If something "
        "is missing (no plan, no report, an empty watchlist), make one question "
        "the one that fixes it.\n\n"
        "Reply with ONLY a JSON array of strings. No prose, no markdown fence, no "
        f"numbering. Each question under {SUGGESTION_MAX_CHARS} characters, first "
        "person, as if the user were typing it to you."
    )


# {"key": fact-block hash, "at": monotonic seconds, "questions": [...]}
_suggestion_cache: dict = {"key": "", "at": 0.0, "questions": []}

# Opening the chat tab and the page-load prefetch can ask at the same moment.
# Without this they would each spend a turn to compute the same answer.
_suggestion_lock = threading.Lock()


def _parse_suggestions(reply: str) -> list:
    """
    Pull a list of questions out of whatever the agent actually sent.

    It is asked for a bare JSON array, and usually obliges, but a model that
    wraps it in a ```json fence or falls back to a numbered list should not cost
    the user their suggestions - the whole point is that nothing here is
    hardcoded, so there is no list to fall back to.
    """
    text = (reply or "").strip()

    # Drop a surrounding markdown fence, if there is one.
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        text = text.rsplit("```", 1)[0]

    questions: list = []

    # Prefer the JSON array, wherever in the reply it starts.
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            questions = [str(item) for item in parsed if isinstance(item, str)]

    if not questions:
        # A plain list, one per line. Numbering, bullets and quotes come off.
        for line in text.splitlines():
            line = line.strip().strip("`").lstrip("-*").strip()
            line = line.lstrip("0123456789").lstrip(".)").strip()
            line = line.strip('"').strip("'").strip()
            if len(line) > 8 and not line.endswith(":"):
                questions.append(line)

    cleaned: list = []
    for question in questions:
        question = " ".join(question.split()).strip()
        # Truncating mid-sentence produces a chip that reads like a bug, so an
        # over-long question is dropped rather than cut. Asking for three of
        # them leaves room for one to be discarded.
        if question and len(question) <= SUGGESTION_MAX_CHARS and question not in cleaned:
            cleaned.append(question)
    return cleaned[:SUGGESTION_COUNT]


def _cached(key: str) -> list:
    """The held questions, if they are for this snapshot and still fresh."""
    if _suggestion_cache["key"] != key or not _suggestion_cache["questions"]:
        return []
    if time.monotonic() - _suggestion_cache["at"] >= SUGGESTION_TTL:
        return []
    return list(_suggestion_cache["questions"])


def _generate(context: str) -> list:
    """One non-streamed call, no tool loop, short timeout."""
    messages = [{"role": "user", "content": _suggestion_request(context)}]

    # `_blocking`, not `ask()`: this turn is not supposed to use tools, so it
    # does not need the approval loop, and one plain request is the shortest
    # path there is. Should the agent ask for a tool anyway, the reply will be
    # its preamble instead of an array, `_parse_suggestions` will find nothing,
    # and the caller reports that rather than showing "I'll check that for you."
    # as a question.
    answer = _blocking(messages, timeout=SUGGESTION_TIMEOUT)
    return _parse_suggestions(answer["reply"])


def suggestions(context: str = "", *, refresh: bool = False) -> dict:
    """
    Starter questions for the chat tab, written by the agent.

    `context` is a short fact block about the portfolio, built by the caller,
    which the agent answers from instead of calling tools.

    Returns {"suggestions": [...], "cached": bool, "ttl": int}. Raises ChatError
    when the agent is not configured or could not answer - the caller shows that
    instead of questions, because there is deliberately no hardcoded list to
    show in its place.
    """
    if not is_configured():
        raise ChatError(status()["message"])

    key = hashlib.sha256((context or "").encode("utf-8")).hexdigest()[:16]
    if not refresh:
        held = _cached(key)
        if held:
            return {"suggestions": held, "cached": True, "ttl": SUGGESTION_TTL}

    with _suggestion_lock:
        # Someone else may have generated them while this call waited.
        if not refresh:
            held = _cached(key)
            if held:
                return {"suggestions": held, "cached": True, "ttl": SUGGESTION_TTL}

        questions = _generate(context)
        if not questions:
            raise ChatError("The agent did not return any questions.")

        _suggestion_cache.update({"key": key, "at": time.monotonic(), "questions": questions})

    logger.info("Generated %d chat suggestions", len(questions))
    return {"suggestions": questions, "cached": False, "ttl": SUGGESTION_TTL}
