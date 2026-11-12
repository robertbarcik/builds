"""CLI client for LocalDesk — connects to MCP server with full guardrail pipeline."""

import asyncio
import json
import sys
from contextlib import AsyncExitStack
from pathlib import Path

# Ensure app package is importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.guardrails.pipeline import post_process, pre_process
from app.llm_client import get_client, get_model
from app.prompts.agent_system import AGENT_SYSTEM_PROMPT

MCP_SERVER = str(Path(__file__).resolve().parent / "mcp_server.py")
MAX_TOOL_ROUNDS = 5

# Terminal colors
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"


def mcp_to_openai_tools(mcp_tools) -> list[dict]:
    """Convert MCP tool schemas to OpenAI function-calling format."""
    openai_tools = []
    for tool in mcp_tools:
        schema = tool.inputSchema or {"type": "object", "properties": {}}
        openai_tools.append({
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": {
                    "type": schema.get("type", "object"),
                    "properties": schema.get("properties", {}),
                    "required": schema.get("required", []),
                },
            },
        })
    return openai_tools


async def run():
    stack = AsyncExitStack()
    try:
        # Connect to MCP server via stdio
        server_params = StdioServerParameters(
            command=sys.executable,
            args=[MCP_SERVER],
        )
        read_stream, write_stream = await stack.enter_async_context(
            stdio_client(server_params)
        )
        session = await stack.enter_async_context(
            ClientSession(read_stream, write_stream)
        )
        await session.initialize()

        # Convert MCP tools to OpenAI format
        tools_result = await session.list_tools()
        openai_tools = mcp_to_openai_tools(tools_result.tools)
        tool_names = [t.name for t in tools_result.tools]

        print(f"{BOLD}mu{RESET} {DIM}— LocalDesk CLI{RESET}")
        print(f"{DIM}Tools: {', '.join(tool_names)}{RESET}")
        print(f"{DIM}Type 'quit' to exit{RESET}\n")

        messages = [{"role": "system", "content": AGENT_SYSTEM_PROMPT}]
        client = get_client()
        model = get_model()

        while True:
            try:
                user_input = input(f"{BOLD}You:{RESET} ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not user_input or user_input.lower() in ("quit", "exit", "q"):
                break

            # Guardrail: pre-process
            pre_result = pre_process(user_input)
            if not pre_result.allowed:
                print(f"\n  {RED}[BLOCKED]{RESET} {pre_result.response}\n")
                continue

            messages.append({"role": "user", "content": pre_result.sanitized_input})
            all_tool_calls = []
            context_chunks = []

            # Tool-calling loop
            for _ in range(MAX_TOOL_ROUNDS):
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=openai_tools,
                    temperature=0.3,
                    max_tokens=1024,
                )
                choice = resp.choices[0]

                if choice.message.tool_calls:
                    messages.append(choice.message.model_dump())
                    for tc in choice.message.tool_calls:
                        fn_name = tc.function.name
                        try:
                            fn_args = json.loads(tc.function.arguments)
                        except json.JSONDecodeError:
                            fn_args = {}

                        print(f"  {DIM}[tool: {fn_name}({json.dumps(fn_args, ensure_ascii=False)})]{RESET}")

                        try:
                            result = await session.call_tool(fn_name, fn_args)
                            tool_result_text = (
                                result.content[0].text if result.content else "No result."
                            )
                        except Exception as e:
                            tool_result_text = f"Error: {e}"

                        all_tool_calls.append({
                            "name": fn_name,
                            "arguments": fn_args,
                            "result": tool_result_text,
                        })

                        # Track KB chunks for guardrails
                        if fn_name == "search_kb":
                            try:
                                kb_result = json.loads(tool_result_text)
                                for r in kb_result.get("results", []):
                                    context_chunks.append({
                                        "text": r.get("content", ""),
                                        "source": r.get("source", ""),
                                    })
                            except json.JSONDecodeError:
                                pass

                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": tool_result_text,
                        })
                else:
                    break

            assistant_content = choice.message.content or ""

            # Force a final text response if model ended on tool calls
            if not assistant_content and all_tool_calls:
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    temperature=0.3,
                    max_tokens=1024,
                )
                assistant_content = resp.choices[0].message.content or ""

            messages.append({"role": "assistant", "content": assistant_content})

            # Keep conversation manageable
            if len(messages) > 40:
                messages = [messages[0]] + messages[-20:]

            # Guardrail: post-process
            post_result = post_process(
                user_input=user_input,
                sanitized_input=pre_result.sanitized_input,
                model_response=assistant_content,
                context_chunks=context_chunks,
                tool_calls=all_tool_calls,
                pre_triggers=pre_result.guardrail_triggers,
            )

            verdict = post_result.judge_verdict.get("verdict", "PASS")
            if post_result.guardrail_triggers:
                triggers = ", ".join(post_result.guardrail_triggers)
                print(f"  {YELLOW}[guardrails: {triggers}]{RESET}")

            verdict_color = GREEN if verdict == "PASS" else YELLOW if verdict == "FLAG" else RED
            print(f"  {verdict_color}[judge: {verdict}]{RESET}")
            print(f"\n{BOLD}Assistant:{RESET} {post_result.response}\n")

    finally:
        await stack.aclose()


def main():
    asyncio.run(run())


if __name__ == "__main__":
    main()
