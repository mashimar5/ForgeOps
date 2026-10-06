"""
AI-ASSISTANT TOOLS -- a command-line operations assistant

Answers questions about the production line with Claude, grounded in the MCP
tools from mcp_server.py. The assistant starts that server, hands its tools
to Claude, and Claude calls them to gather evidence before answering: the ML
generates the evidence, and the LLM finds, organizes and explains it.

Needs Claude API credentials: ANTHROPIC_API_KEY, or a profile from
`ant auth login`. Each question uses a few cents of API usage.

    .venv/bin/python src/assistant.py "Which parts should we inspect now?"
    .venv/bin/python src/assistant.py            # interactive; Ctrl-D to quit
"""

import argparse
import asyncio
import json
import sys
from contextlib import asynccontextmanager
from pathlib import Path

import anthropic
from anthropic.lib.tools.mcp import async_mcp_tool
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


MODEL = "claude-opus-5-5"
MAX_TOOL_ROUNDS = 12

SERVER = StdioServerParameters(
    command=sys.executable,
    args=[str(Path(__file__).with_name("mcp_server.py"))],
)

GUIDELINES = """\
You are an operations analyst for a manufacturing line. Engineers and managers ask \
you about the line; the tools return evidence from its data and its quality-risk model.

- Gather evidence with the tools before answering. Every number you state must come \
from a tool result. If the tools can't answer a question, say so instead of guessing.
- Say which production hour your answer describes. If the user names no time, use \
the end of the data and say that is what "now" means here.
- Keep the evidence's caveats: risk scores rank parts and are not probabilities; \
station failure rates are associations, not causes; the line monitor is only an \
indicator.
- Lead with the answer, then the evidence. Keep it short, and use a small table when \
comparing several parts or stations.
"""


@asynccontextmanager
async def connect():
    """Start the MCP server; yield its tools (as Claude tools) and the system prompt."""

    async with stdio_client(SERVER) as (read, write):
        async with ClientSession(read, write) as session:
            server_info = await session.initialize()
            listed = await session.list_tools()

            tools = [async_mcp_tool(tool, session) for tool in listed.tools]
            yield tools, f"{GUIDELINES}\nAbout the data and the tools:\n{server_info.instructions}"


def describe_call(block):
    args = ", ".join(f"{key}={json.dumps(value)}" for key, value in block.input.items())
    return f"{block.name}({args})"


async def answer(client, tools, system, history, question, effort, model=MODEL, on_message=None):
    """
    Ask one question; `history` grows append-only with every turn.

    on_message, if given, is called with every API response (its model,
    usage and stop_reason) -- the eval runner records them.
    """

    history.append({"role": "user", "content": question})

    runner = client.beta.messages.tool_runner(
        model=model,
        max_tokens=16000,
        system=system,
        tools=tools,
        messages=list(history),
        output_config={"effort": effort},
        max_iterations=MAX_TOOL_ROUNDS,
        # If the model declines, retry on the model Anthropic recommends for that case
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    )

    final = None

    async for message in runner:
        final = message

        if on_message is not None:
            on_message(message)

        # Mirror the conversation exactly as sent: each assistant turn unchanged,
        # then its tool results. Editing earlier turns would invalidate them.
        history.append({"role": "assistant", "content": message.content})

        for block in message.content:
            if block.type == "tool_use":
                print(f"  -> {describe_call(block)}", file=sys.stderr)

        tool_results = await runner.generate_tool_call_response()
        if tool_results is not None:
            history.append(tool_results)

    if final is None:
        return "(no response)"

    if final.stop_reason == "refusal":
        return "The model declined to answer this question."

    text = "".join(block.text for block in final.content if block.type == "text")

    if final.stop_reason == "max_tokens":
        text += "\n\n(The answer was cut off at the token limit.)"

    return text


async def main():
    parser = argparse.ArgumentParser(description="Ask the operations assistant about the production line.")
    parser.add_argument("question", nargs="*", help="the question; omit it for an interactive session")
    parser.add_argument(
        "--effort",
        default="medium",
        choices=["low", "medium", "high", "xhigh", "max"],
        help="how much the model thinks before answering (default: medium)",
    )
    args = parser.parse_args()

    client = anthropic.AsyncAnthropic()

    if all(getattr(client, name, None) is None for name in ("api_key", "auth_token", "credentials")):
        sys.exit(
            "No Claude API credentials found. Run `ant auth login`, or set ANTHROPIC_API_KEY "
            "to a key from platform.claude.com, then try again."
        )

    async with connect() as (tools, system):
        history = []

        questions = [" ".join(args.question)] if args.question else None

        if questions is None:
            print("Ask about the production line (Ctrl-D to quit).")

        while True:
            if questions is not None:
                if not questions:
                    break
                question = questions.pop()
            else:
                try:
                    question = (await asyncio.to_thread(input, "\n> ")).strip()
                except EOFError:
                    break
                if not question:
                    continue

            try:
                print(await answer(client, tools, system, history, question, args.effort))
            except anthropic.AuthenticationError:
                sys.exit("Claude API credentials were rejected. Check ANTHROPIC_API_KEY or run `ant auth login`.")
            except anthropic.RateLimitError:
                print("Rate limited by the Claude API; wait a moment and ask again.", file=sys.stderr)
            except anthropic.APIStatusError as error:
                print(f"Claude API error {error.status_code}: {error.message}", file=sys.stderr)
            except anthropic.APIConnectionError:
                print("Couldn't reach the Claude API; check the network connection.", file=sys.stderr)


if __name__ == "__main__":
    asyncio.run(main())
