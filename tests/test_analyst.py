"""
Tests for the AI analyst endpoints (analyst.py), run against the real
serving data. Build it first:

    .venv/bin/python src/build_serving_data.py
    .venv/bin/python -m pytest tests

No Claude API calls are made: a fake client plays scripted turns, and the
real MCP tools run in-process on the API's data.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from factory_service import SERVING_DIR  # noqa: E402

pytestmark = pytest.mark.skipif(
    not (SERVING_DIR / "parts.parquet").exists(),
    reason="Serving data not built; run src/build_serving_data.py first",
)

from anthropic.lib.tools import ToolError  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import assistant  # noqa: E402
from analyst import as_of  # noqa: E402
from api import app  # noqa: E402


TOOLS = {
    "get_factory_summary",
    "get_line_status",
    "get_part",
    "explain_part_risk",
    "get_inspection_queue",
    "get_batch_mate_alerts",
    "get_station",
    "list_stations",
}


# ============================================================
# A FAKE CLAUDE CLIENT
#
# Each question is a list of turns; a turn is a list of blocks,
# ("text", "...") or ("tool", name, input), or an exception to raise.
# ============================================================

USAGE = SimpleNamespace(input_tokens=1000, output_tokens=100, cache_read_input_tokens=0, cache_creation_input_tokens=None)


def block(spec, index):
    if spec[0] == "text":
        return SimpleNamespace(type="text", text=spec[1])
    return SimpleNamespace(type="tool_use", id=f"toolu_{index}", name=spec[1], input=spec[2])


class FakeStream:
    def __init__(self, message):
        self.message = message

    async def __aiter__(self):
        for content in self.message.content:
            if content.type == "text":
                yield SimpleNamespace(type="text", text=content.text)
            else:
                yield SimpleNamespace(type="content_block_stop", content_block=content)

    async def get_final_message(self):
        return self.message


class FakeRunner:
    """Plays the turns like the SDK's streaming tool runner: tools run when asked, a ToolError becomes an error result."""

    def __init__(self, turns, tools):
        self.turns = turns
        self.tools = {tool.name: tool for tool in tools}
        self.last = None

    async def __aiter__(self):
        for number, turn in enumerate(self.turns):
            if isinstance(turn, Exception):
                raise turn
            content = [block(spec, f"{number}_{i}") for i, spec in enumerate(turn)]
            calls = any(c.type == "tool_use" for c in content)
            self.last = SimpleNamespace(
                content=content, stop_reason="tool_use" if calls else "end_turn", usage=USAGE, model=assistant.MODEL
            )
            yield FakeStream(self.last)
            if not calls:
                return

    async def generate_tool_call_response(self):
        results = []
        for content in self.last.content:
            if content.type != "tool_use":
                continue
            try:
                output = await self.tools[content.name].call(content.input)
                results.append({"type": "tool_result", "tool_use_id": content.id, "content": output})
            except ToolError as error:
                results.append({"type": "tool_result", "tool_use_id": content.id, "content": error.content, "is_error": True})
        return {"role": "user", "content": results} if results else None


class FakeClient:
    def __init__(self, questions, signed_in=True):
        self.questions = list(questions)
        self.requests = []
        self.api_key = "fake" if signed_in else None
        self.auth_token = None
        self.credentials = None
        self.beta = SimpleNamespace(messages=SimpleNamespace(tool_runner=self.tool_runner))

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def tool_runner(self, **params):
        self.requests.append(params)
        return FakeRunner(self.questions.pop(0), params["tools"])


# ============================================================
# HELPERS
# ============================================================

@pytest.fixture(scope="module")
def client():
    with TestClient(app) as test_client:
        yield test_client


def use(client, fake):
    client.app.state.analyst.client_factory = lambda: fake
    return fake


def ask(client, question, **body):
    response = client.post("/analyst/ask", json={"question": question, **body})
    assert response.status_code == 200, response.text
    events = []
    for chunk in response.text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in chunk.splitlines())
        events.append((fields["event"], json.loads(fields["data"])))
    return events


def of_kind(events, kind):
    return [data for event, data in events if event == kind]


# ============================================================
# TESTS
# ============================================================

def test_status_lists_the_assistants_tools(client):
    use(client, FakeClient([]))
    status = client.get("/analyst/status").json()

    assert status["available"] is True
    assert set(status["tools"]) == TOOLS
    assert status["model"] == assistant.MODEL
    assert status["effort"] == "medium"

    use(client, FakeClient([], signed_in=False))
    status = client.get("/analyst/status").json()
    assert status["available"] is False
    assert "ant auth login" in status["reason"]


def test_answer_streams_text_tool_calls_and_cost(client):
    fake = use(client, FakeClient([[[("text", "Checking."), ("tool", "get_line_status", {})], [("text", "The line is fine.")]]]))
    events = ask(client, "How is the line doing?", at_hour=7300)

    assert [event for event, _ in events] == ["start", "text", "tool_call", "tool_result", "text", "done"]
    assert of_kind(events, "tool_call")[0]["name"] == "get_line_status"

    # The tool answered as of the dashboard's hour, though Claude gave none
    result = of_kind(events, "tool_result")[0]
    assert not result["is_error"]
    assert json.loads(result["preview"])["at_hour"] == 7300

    done = of_kind(events, "done")[0]
    assert done["answer"] == "The line is fine."
    assert done["tool_calls"] == 1
    assert done["usage"]["input_tokens"] == 2000
    assert done["cost_usd"] == pytest.approx((2000 * 4.0 + 200 * 20.0) / 1e6)

    # Asked like the assistant, plus the hour, streamed
    params = fake.requests[0]
    assert params["model"] == assistant.MODEL
    assert params["output_config"] == {"effort": "medium"}
    assert params["max_iterations"] == assistant.MAX_TOOL_ROUNDS
    assert params["stream"] is True
    assert "hour 7300" in params["system"]


def test_later_hours_are_refused(client):
    use(client, FakeClient([[[("tool", "get_factory_summary", {"at_hour": 9000}), ("tool", "get_factory_summary", {"at_hour": 5000})], [("text", "Done.")]]]))
    events = ask(client, "Compare hours 5000 and 9000", at_hour=7300)

    later, earlier = of_kind(events, "tool_result")
    assert later["is_error"] and "7300" in later["preview"]
    assert not earlier["is_error"] and json.loads(earlier["preview"])["at_hour"] == 5000


def test_end_of_data_asks_like_the_eval(client):
    analyst = client.app.state.analyst
    fake = use(client, FakeClient([[[("tool", "get_factory_summary", {})], [("text", "Done.")]], [[("text", "Done.")]]]))

    events = ask(client, "How is the line doing?")
    params = fake.requests[0]
    assert params["system"] == analyst.system
    assert params["tools"] == analyst.tools
    assert json.loads(of_kind(events, "tool_result")[0]["preview"])["at_hour"] == analyst.last_hour

    # An hour at or past the end of the data is the end of the data
    ask(client, "How is the line doing?", at_hour=analyst.last_hour + 10)
    assert fake.requests[1]["system"] == analyst.system


def test_follow_ups_see_the_conversation(client):
    fake = use(client, FakeClient([[[("text", "First answer.")]], [[("text", "Second answer.")]]]))

    conversation = of_kind(ask(client, "First?", at_hour=7300), "start")[0]["conversation_id"]
    ask(client, "Second?", at_hour=7300, conversation_id=conversation)

    messages = fake.requests[1]["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    assert messages[0]["content"] == "First?" and messages[2]["content"] == "Second?"

    # A follow-up stays at its conversation's hour; an unknown conversation is gone
    assert client.post("/analyst/ask", json={"question": "Third?", "at_hour": 8000, "conversation_id": conversation}).status_code == 409
    assert client.post("/analyst/ask", json={"question": "Third?", "conversation_id": "nope"}).status_code == 404


def test_a_failed_answer_leaves_no_trace(client):
    fake = use(client, FakeClient([[[("tool", "get_line_status", {})], RuntimeError("boom")], [[("text", "Fine.")]]]))

    events = ask(client, "How is the line doing?", at_hour=7300)
    assert events[-1][0] == "error"
    assert "unexpectedly" in events[-1][1]["message"]

    # The conversation is free again, and its history no longer holds the failed question
    conversation = events[0][1]["conversation_id"]
    ask(client, "And now?", at_hour=7300, conversation_id=conversation)
    assert [m["content"] for m in fake.requests[1]["messages"]] == ["And now?"]


def test_hour_cap_keeps_the_tool_definitions(client):
    for tool in client.app.state.analyst.tools:
        assert as_of(tool, 7300).to_dict() == tool.to_dict()
