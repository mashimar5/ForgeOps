"""
AI ANALYST -- the assistant behind the dashboard's AI Analyst page

Runs assistant.py's question loop inside the API: the same model, system
prompt, MCP tools and settings, with the MCP server (mcp_server.py)
connected in-process so its tools use the data the API has already loaded.
Each answer streams back as server-sent events: the text as Claude writes
it, every tool call with its input, and every tool result.

Two additions, both for the dashboard's time control:

- A conversation is "as of" the hour the time control was at when it
  started. Before the end of the data, the tools answer as of that hour when
  Claude leaves at_hour out and refuse later hours, and the system prompt
  says so. At the end of the data nothing is added: the page asks exactly
  as the assistant's eval does.
- Conversations live in memory (the last 20), so follow-up questions work
  until the API restarts.

Each question calls the Claude API on the account the API process is
signed in with (`ant auth login`, or ANTHROPIC_API_KEY) and is billed there.
"""

import asyncio
import json
import logging
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field

import anthropic
from anthropic.lib.tools import ToolError, beta_async_tool

import assistant

log = logging.getLogger(__name__)

EFFORT = "medium"        # the command line's default, and what the eval measures
MAX_CONVERSATIONS = 20
MAX_RUNNING = 2          # questions answered at once, across conversations
PREVIEW_CHARS = 6000     # of each tool result, for the page

# USD per million tokens, as in the eval's price table (.claude/hillclimb/assistant/_state.json)
PRICES = {
    "claude-opus-5-5": {"in": 4.0, "out": 20.0, "cache_read": 0.2},
    "claude-sonnet-5-5": {"in": 2.0, "out": 10.0, "cache_read": 0.2},
}

NOTE = (
    "Answers come from Claude using the assistant's read-only tools, the same evidence as the "
    "dashboard's pages, and keep their caveats. Every number should come from a tool result; check "
    "the tool calls under each answer. Each question calls the Claude API on the account the API "
    "server is signed in with and is billed there; the cost shown is an estimate from token counts."
)

NO_CREDENTIALS = (
    "No Claude API credentials on the server. Run `ant auth login` in a terminal, or start the API "
    "with ANTHROPIC_API_KEY set, then ask again."
)

HOUR_NOTE = (
    "\nThe dashboard's time control is set to production hour {hour:g}, so here \"now\" means hour "
    "{hour:g}, not the end of the data: the tools answer as of hour {hour:g} when you omit at_hour, "
    "and refuse later hours.\n"
)


class Busy(Exception):
    """The conversation, or the analyst, is already answering."""


class Mismatch(Exception):
    """A follow-up asked as of a different hour than its conversation."""


@dataclass
class Conversation:
    id: str
    at_hour: float | None  # None: the end of the data
    tools: list
    system: str
    history: list = field(default_factory=list)
    busy: bool = False


# ============================================================
# TOOLS AND RESULTS
# ============================================================

def as_of(tool, hour):
    """The same tool (name, description, schema), answering as of `hour`: an
    omitted at_hour means `hour`, and later hours are refused."""

    async def call(**arguments):
        at_hour = arguments.get("at_hour")

        if at_hour is not None and at_hour > hour:
            raise ToolError(
                f"at_hour {at_hour:g} is after hour {hour:g}, where the dashboard's time control is set. "
                f"Answers can only use what was known by hour {hour:g}; the user can move the time "
                "control to ask about later hours."
            )

        return await tool.call({**arguments, "at_hour": hour if at_hour is None else at_hour})

    return beta_async_tool(call, name=tool.name, description=tool.description, input_schema=tool.input_schema)


def result_text(content):
    if isinstance(content, str):
        return content

    parts = []
    for part in content or []:
        kind = part.get("type") if isinstance(part, dict) else getattr(part, "type", None)
        if kind == "text":
            parts.append(part["text"] if isinstance(part, dict) else part.text)
    return "\n".join(parts)


def describe_result(result):
    text = result_text(result.get("content"))
    return {
        "id": result["tool_use_id"],
        "is_error": bool(result.get("is_error")),
        "chars": len(text),
        "preview": text[:PREVIEW_CHARS],
    }


# ============================================================
# USAGE AND ERRORS
# ============================================================

USAGE_FIELDS = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")


def add_usage(total, usage):
    for name in USAGE_FIELDS:
        total[name] = total.get(name, 0) + (getattr(usage, name, None) or 0)


def cost_usd(usage_by_model):
    """Estimated from token counts and PRICES; None if a model has no price."""

    cost = 0.0
    for model, usage in usage_by_model.items():
        price = PRICES.get(model)
        if price is None:
            return None
        cost += (
            usage["input_tokens"] * price["in"]
            + usage["output_tokens"] * price["out"]
            + usage["cache_read_input_tokens"] * price["cache_read"]
            + usage["cache_creation_input_tokens"] * price["in"] * 1.25
        ) / 1e6
    return round(cost, 4)


def describe_error(error):
    if isinstance(error, anthropic.AuthenticationError):
        return "The Claude API rejected the server's credentials. Run `ant auth login` again (the login lasts about 8 hours), then ask again."
    if isinstance(error, anthropic.RateLimitError):
        return "Rate limited by the Claude API; wait a moment and ask again."
    if isinstance(error, anthropic.APIStatusError):
        return f"Claude API error {error.status_code}: {error.message}"
    if isinstance(error, anthropic.APIConnectionError):
        return "Couldn't reach the Claude API; check the network connection."

    log.exception("The analyst failed", exc_info=error)
    return "The analyst failed unexpectedly; the API's log has the details."


def sse(kind, data):
    return f"event: {kind}\ndata: {json.dumps(data, default=str)}\n\n"


# ============================================================
# THE ANALYST
# ============================================================

class Analyst:
    """Conversations with the assistant, for the API. `tools` and `system`
    come from assistant.connect(); `client_factory` makes a Claude client
    per question, so a fresh `ant auth login` takes effect without a restart."""

    def __init__(self, tools, system, last_hour, client_factory=anthropic.AsyncAnthropic):
        self.tools = tools
        self.system = system
        self.last_hour = last_hour
        self.client_factory = client_factory
        self.conversations = OrderedDict()
        self.running = 0

    async def status(self):
        async with self.client_factory() as client:
            available = assistant.has_credentials(client)

        return {
            "available": available,
            "reason": None if available else NO_CREDENTIALS,
            "model": assistant.MODEL,
            "effort": EFFORT,
            "max_tool_rounds": assistant.MAX_TOOL_ROUNDS,
            "tools": [tool.name for tool in self.tools],
            "price_per_mtok": PRICES[assistant.MODEL],
            "note": NOTE,
        }

    def open(self, conversation_id, at_hour):
        """The conversation to ask in (a new one without an id), marked busy."""

        if at_hour is not None and at_hour >= self.last_hour:
            at_hour = None

        if self.running >= MAX_RUNNING:
            raise Busy(f"The analyst is already answering {self.running} questions; ask again when one finishes.")

        if conversation_id is None:
            conversation = self._new(at_hour)
        else:
            conversation = self.conversations.get(conversation_id)
            if conversation is None:
                raise LookupError("This conversation has ended (the API restarted, or it was one of the oldest); start a new one.")
            if conversation.at_hour != at_hour:
                started = "the end of the data" if conversation.at_hour is None else f"hour {conversation.at_hour:g}"
                raise Mismatch(f"This conversation is as of {started}; start a new one to ask as of another hour.")
            if conversation.busy:
                raise Busy("This conversation is still answering its last question.")

        conversation.busy = True
        self.running += 1
        self.conversations.move_to_end(conversation.id)
        return conversation

    def _new(self, at_hour):
        tools, system = self.tools, self.system
        if at_hour is not None:
            tools = [as_of(tool, at_hour) for tool in tools]
            system += HOUR_NOTE.format(hour=at_hour)

        conversation = Conversation(uuid.uuid4().hex, at_hour, tools, system)
        self.conversations[conversation.id] = conversation

        # Forget the oldest idle conversations
        for old in [c for c in self.conversations.values() if not c.busy][: max(len(self.conversations) - MAX_CONVERSATIONS, 0)]:
            del self.conversations[old.id]

        return conversation

    def _release(self, conversation):
        conversation.busy = False
        self.running -= 1

    async def events(self, conversation, question):
        """Answer `question` in `conversation` as server-sent events. If the
        page goes away (or presses Stop), the run is cancelled, so it stops
        calling the API."""

        queue = asyncio.Queue()
        task = None

        def finished(_):
            self._release(conversation)
            queue.put_nowait(None)

        try:
            task = asyncio.create_task(self._answer(conversation, question, lambda kind, data: queue.put_nowait((kind, data))))
            task.add_done_callback(finished)

            while (item := await queue.get()) is not None:
                yield sse(*item)
        finally:
            if task is None:
                self._release(conversation)
            elif not task.done():
                task.cancel()

    async def _answer(self, conversation, question, emit):
        history = conversation.history
        start = len(history)
        started = time.monotonic()
        keep = False

        emit("start", {"conversation_id": conversation.id, "at_hour": conversation.at_hour, "model": assistant.MODEL})

        try:
            async with self.client_factory() as client:
                if not assistant.has_credentials(client):
                    emit("error", {"message": NO_CREDENTIALS})
                    return

                history.append({"role": "user", "content": question})
                params = assistant.request_params(conversation.tools, conversation.system, history, EFFORT)
                runner = client.beta.messages.tool_runner(**params, stream=True)

                final, turn, tool_calls, usage = None, 0, 0, {}

                async for stream in runner:
                    async for event in stream:
                        if event.type == "text":
                            emit("text", {"turn": turn, "text": event.text})
                        elif event.type == "content_block_stop" and event.content_block.type == "tool_use":
                            block = event.content_block
                            emit("tool_call", {"turn": turn, "id": block.id, "name": block.name, "input": block.input})

                    final = await stream.get_final_message()
                    add_usage(usage.setdefault(final.model, {}), final.usage)

                    # Mirror the conversation as sent, as assistant.answer() does
                    history.append({"role": "assistant", "content": final.content})

                    # Never run the tools of a declined or cut-off turn
                    if final.stop_reason in ("refusal", "max_tokens"):
                        break

                    results = await runner.generate_tool_call_response()
                    if results is not None:
                        history.append(results)
                        for result in results["content"]:
                            tool_calls += 1
                            emit("tool_result", describe_result(result))

                    turn += 1

            unfinished = final is not None and any(block.type == "tool_use" for block in final.content)
            answer = assistant.final_text(final)
            if unfinished and final.stop_reason == "tool_use":
                answer = f"Stopped after {assistant.MAX_TOOL_ROUNDS} rounds of tool calls without an answer. Try a narrower question."

            # Follow-ups only see exchanges that ended in a complete answer
            keep = final is not None and final.stop_reason != "refusal" and not unfinished

            total = {name: sum(u.get(name, 0) for u in usage.values()) for name in USAGE_FIELDS}
            emit("done", {
                "answer": answer,
                "stop_reason": final.stop_reason if final else None,
                "kept": keep,
                "models": sorted(usage),
                "usage": total,
                "cost_usd": cost_usd(usage),
                "seconds": round(time.monotonic() - started, 1),
                "tool_calls": tool_calls,
            })
        except Exception as error:
            emit("error", {"message": describe_error(error)})
        finally:
            if not keep:
                del history[start:]
