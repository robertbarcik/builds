"""LocalDesk — main FastAPI application with chat orchestration."""

import asyncio
import json
import logging
import sqlite3
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from opentelemetry import trace
from starlette.responses import StreamingResponse

from app.approvals import approval_trigger, run_tool
from app.approvals import router as approvals_router
from app.audit_chat import router as audit_chat_router
from app.auth import LoginMiddleware
from app.config import DATABASE_PATH, MODE, ROLES, SERVER_HOST, SERVER_PORT
from app.conversations import _conversations, get_or_create
from app.db import ensure_schema
from app.guardrails.pipeline import post_process, pre_process
from app.llm_client import chat_kwargs, get_client, get_model, voice_available
from app.ops.metrics import record_llm_usage
from app.ops.metrics import router as metrics_router
from app.ops.sentinel import router as sentinel_router
from app.ops.sentinel import sentinel
from app.ops.simulation import router as simulation_router
from app.ops.simulation import simulation
from app.tools.assets import lookup_asset
from app.tools.audit_report import audit_report
from app.tools.definitions import TOOLS
from app.tools.incidents import create_incident, escalate_ticket, get_incident, list_incidents
from app.tools.knowledge import search_kb
from app.tools.servers import (
    list_servers,
    migrate_server,
    provision_server,
    reboot_server,
    terminate_server,
)
from app.tools.sla import check_sla
from app.reports.router import router as reports_router
from app.tracing import tracer
from app.voice import router as voice_router
from app.voice_live import router as voice_live_router
from app.ws_hub import hub

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Tool dispatch map
TOOL_HANDLERS = {
    "check_sla": lambda args: check_sla(**args),
    "create_incident": lambda args: create_incident(**args),
    "lookup_asset": lambda args: lookup_asset(**args),
    "escalate_ticket": lambda args: escalate_ticket(**args),
    "list_incidents": lambda args: list_incidents(**args),
    "get_incident": lambda args: get_incident(**args),
    "audit_report": lambda args: audit_report(**args),
    "search_kb": lambda args: search_kb(**args),
    # Server fleet. Handlers are only reached through app.approvals.run_tool,
    # which holds reboot / terminate / migrate (and medium/large provisioning)
    # until a human clicks Approve.
    "list_servers": lambda args: list_servers(**args),
    "provision_server": lambda args: provision_server(**args),
    "reboot_server": lambda args: reboot_server(**args),
    "terminate_server": lambda args: terminate_server(**args),
    "migrate_server": lambda args: migrate_server(**args),
}

async def _sim_state_changed(running: bool):
    """Sentinel lifecycle is tied to the simulation toggle."""
    if running:
        await sentinel.start()
    else:
        await sentinel.stop()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("LocalDesk starting up")
    ensure_schema()
    hub.set_loop(asyncio.get_running_loop())
    simulation.on_state_change = _sim_state_changed
    yield
    await simulation.stop()
    await sentinel.stop()
    logger.info("LocalDesk shutting down")


app = FastAPI(title="LocalDesk", lifespan=lifespan)
app.add_middleware(LoginMiddleware)  # no-op unless LOGIN_ENABLED=1 (deployed box)
app.include_router(metrics_router)
app.include_router(simulation_router)
app.include_router(sentinel_router)
app.include_router(reports_router)
app.include_router(audit_chat_router)
app.include_router(approvals_router)
app.include_router(voice_router)
app.include_router(voice_live_router)


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await hub.connect(ws)
    try:
        while True:
            await ws.receive_text()  # client pings; content ignored
    except WebSocketDisconnect:
        hub.disconnect(ws)

# Serve static files (frontend)
static_dir = Path(__file__).parent.parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    index_path = Path(__file__).parent.parent / "static" / "index.html"
    return HTMLResponse(index_path.read_text())


def _tool_data(raw: str, limit: int = 6000):
    """Tool result as JSON for the UI's data cards (parsed if possible, capped)."""
    if len(raw) > limit:
        raw = raw[:limit]
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"text": raw}


VOICE_CHANNEL_HINT = (
    "\n\n[spoken channel: the answer will be read aloud by a voice agent. "
    "Use your tools exactly as usual BEFORE stating any fact (check_sla for "
    "any response/resolution time, search_kb for procedures, lookup_asset, "
    "create_incident, list_servers and the server actions). Then reply in at "
    "most three short plain sentences, "
    "no markdown, no lists, no headings, in the same language as the request.]"
)


def _run_chat_pipeline(
    user_message: str,
    session_id: str,
    channel: str = "text",
    progress=None,
) -> dict:
    """Core pipeline: pre-process -> LLM+tools -> post-process. Returns result dict.

    channel: "text" (threads, CLI) or "voice" (GPT-Live client delegation — the
    result is spoken, so the agent is asked for a short plain answer and the
    audit record is marked so the guardrail chart can show that voice traffic
    went through the full pipeline).
    progress: optional callback(stage: str, detail: dict) invoked from the
    worker thread as the pipeline advances (used to narrate tool calls to the
    voice agent and the UI while the caller keeps talking).
    """
    t_start = time.monotonic()
    trace = {"llm_calls": 0, "tools": [], "stages": {}}

    def _report(stage: str, **detail):
        if progress:
            try:
                progress(stage, detail)
            except Exception:  # never let UI narration break the pipeline
                logger.debug("progress callback failed", exc_info=True)

    with tracer.start_as_current_span(
        "chat_request",
        attributes={
            "mu.session_id": session_id,
            "mu.mode": MODE,
            "mu.channel": channel,
            "mu.user_message_length": len(user_message),
        },
    ) as root_span:

        # Layer 1: Pre-process (input filters)
        t0 = time.monotonic()
        with tracer.start_as_current_span(
            "guardrail.pre_process",
            attributes={"guardrail.layer": "input_filter"},
        ) as pre_span:
            pre_result = pre_process(user_message)
            pre_span.set_attribute("guardrail.allowed", pre_result.allowed)
            if pre_result.guardrail_triggers:
                pre_span.set_attribute(
                    "guardrail.triggers", json.dumps(pre_result.guardrail_triggers)
                )
        trace["stages"]["pre_filter_ms"] = int((time.monotonic() - t0) * 1000)
        _report("pre_filter", allowed=pre_result.allowed,
                triggers=list(pre_result.guardrail_triggers))

        if channel == "voice":
            # Marks the audit record: this voice turn DID pass the guardrails
            # (contrast with the realtime channel, which bypasses them).
            pre_result.guardrail_triggers.append("channel_voice_live: guardrails applied")

        if not pre_result.allowed:
            root_span.set_attribute("mu.blocked_by", "input_filter")
            root_span.set_attribute("mu.guardrail_triggers",
                                    json.dumps(pre_result.guardrail_triggers))
            trace["stages"]["total_ms"] = int((time.monotonic() - t_start) * 1000)
            return {
                "response": pre_result.response,
                "tool_calls": [],
                "guardrail_triggers": pre_result.guardrail_triggers,
                "judge_verdict": "PASS",
                "blocked_by": "input_filter",
                "trace": trace,
            }

        # Build conversation history
        messages = get_or_create(session_id)
        user_content = pre_result.sanitized_input
        if channel == "voice":
            user_content += VOICE_CHANNEL_HINT
        messages.append({"role": "user", "content": user_content})

        # Grounding from EARLIER turns of this session: tool results already in
        # the history are context the model legitimately answers from ("what's
        # the resolution time?" two turns after check_sla ran). Gate 2 and the
        # judge must see them too, or a correctly remembered number gets
        # blocked as a fabrication.
        prior_tool_results = [
            {"name": "earlier_turn", "arguments": {}, "result": m.get("content", "")}
            for m in messages[:-1] if m.get("role") == "tool" and m.get("content")
        ][-8:]

        # LLM call with tool use loop
        client = get_client()
        model = get_model()
        all_tool_calls = []
        context_chunks = []
        max_iterations = 6   # get_incident → list_servers → action → answer needs four
        llm_call_count = 0
        total_prompt_tokens = 0
        total_completion_tokens = 0
        llm_seconds = 0.0

        for iteration in range(max_iterations):
            _report("llm_call", iteration=iteration, model=model)
            with tracer.start_as_current_span(
                "gen_ai.chat",
                attributes={
                    "gen_ai.system": "openai",
                    "gen_ai.request.model": model,
                    "gen_ai.request.temperature": 0.3,
                    "gen_ai.request.max_tokens": 1024,
                    "gen_ai.operation.name": "chat",
                    "mu.llm_call_iteration": iteration,
                    "mu.tools_available": len(TOOLS),
                },
            ) as llm_span:
                t0 = time.monotonic()
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    tools=TOOLS,
                    **chat_kwargs(model, max_tokens=1024, temperature=0.3, tools=True),
                )
                duration = time.monotonic() - t0
                llm_seconds += duration
                llm_call_count += 1
                choice = resp.choices[0]

                # Token usage
                if resp.usage:
                    total_prompt_tokens += resp.usage.prompt_tokens or 0
                    total_completion_tokens += resp.usage.completion_tokens or 0
                    llm_span.set_attribute(
                        "gen_ai.usage.prompt_tokens", resp.usage.prompt_tokens or 0
                    )
                    llm_span.set_attribute(
                        "gen_ai.usage.completion_tokens", resp.usage.completion_tokens or 0
                    )

                llm_span.set_attribute("gen_ai.response.model", model)
                llm_span.set_attribute(
                    "gen_ai.response.finish_reasons",
                    json.dumps([choice.finish_reason or "stop"]),
                )
                llm_span.set_attribute("mu.llm_duration_s", round(duration, 3))

            if choice.finish_reason == "tool_calls" or (
                choice.message.tool_calls and len(choice.message.tool_calls) > 0
            ):
                # Process tool calls
                messages.append(choice.message.model_dump())
                for tc in choice.message.tool_calls:
                    fn_name = tc.function.name
                    try:
                        fn_args = json.loads(tc.function.arguments)
                    except json.JSONDecodeError:
                        fn_args = {}

                    _report("tool_start", name=fn_name, arguments=fn_args)
                    t_tool = time.monotonic()
                    with tracer.start_as_current_span(
                        f"tool.{fn_name}",
                        attributes={
                            "mu.tool.name": fn_name,
                            "mu.tool.arguments": json.dumps(fn_args),
                        },
                    ) as tool_span:
                        handler = TOOL_HANDLERS.get(fn_name)
                        if handler:
                            try:
                                # High-impact calls block here until a human
                                # answers the confirmation window.
                                tool_result, approval = run_tool(
                                    fn_name, fn_args, handler,
                                    session_id=session_id, channel=channel, report=_report,
                                )
                                if approval:
                                    trace.setdefault("approvals", []).append(approval)
                                    pre_result.guardrail_triggers.append(approval_trigger(approval))
                            except Exception as e:  # bad args from the model, DB hiccup
                                tool_result = json.dumps({"error": f"{fn_name} failed: {e}"})
                                tool_span.set_attribute("mu.tool.error", True)
                            logger.info("Tool call: %s(%s)", fn_name, fn_args)
                        else:
                            tool_result = json.dumps({"error": f"Unknown tool: {fn_name}"})
                            tool_span.set_attribute("mu.tool.error", True)

                        tool_span.set_attribute(
                            "mu.tool.result_length", len(tool_result)
                        )
                    tool_ms = int((time.monotonic() - t_tool) * 1000)
                    trace["tools"].append({"name": fn_name, "ms": tool_ms})
                    _report("tool_done", name=fn_name, arguments=fn_args, ms=tool_ms,
                            result=tool_result[:300], data=_tool_data(tool_result))

                    all_tool_calls.append(
                        {"name": fn_name, "arguments": fn_args, "result": tool_result}
                    )

                    # If it was a KB search, track the chunks for guardrails
                    if fn_name == "search_kb":
                        try:
                            kb_result = json.loads(tool_result)
                            for r in kb_result.get("results", []):
                                context_chunks.append(
                                    {"text": r.get("content", ""), "source": r.get("source", "")}
                                )
                        except json.JSONDecodeError:
                            pass

                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": tool_result,
                        }
                    )
            else:
                # Final response
                break

        assistant_content = choice.message.content or ""

        # If the model ended with tool calls and no text response, force a final response
        if not assistant_content and all_tool_calls:
            with tracer.start_as_current_span(
                "gen_ai.chat",
                attributes={
                    "gen_ai.system": "openai",
                    "gen_ai.request.model": model,
                    "gen_ai.request.temperature": 0.3,
                    "gen_ai.request.max_tokens": 1024,
                    "gen_ai.operation.name": "chat",
                    "mu.llm_call_type": "forced_final",
                },
            ) as final_span:
                _report("llm_call", iteration=llm_call_count, model=model, forced_final=True)
                t_final = time.monotonic()
                resp = client.chat.completions.create(
                    model=model,
                    messages=messages,
                    **chat_kwargs(model, max_tokens=1024, temperature=0.3),
                )
                assistant_content = resp.choices[0].message.content or ""
                llm_call_count += 1
                llm_seconds += time.monotonic() - t_final
                if resp.usage:
                    total_prompt_tokens += resp.usage.prompt_tokens or 0
                    total_completion_tokens += resp.usage.completion_tokens or 0
                    final_span.set_attribute(
                        "gen_ai.usage.prompt_tokens", resp.usage.prompt_tokens or 0
                    )
                    final_span.set_attribute(
                        "gen_ai.usage.completion_tokens", resp.usage.completion_tokens or 0
                    )
                final_span.set_attribute("gen_ai.response.model", model)

        messages.append({"role": "assistant", "content": assistant_content})

        # Keep conversation manageable (last ~20 messages + system).
        # The window must start on a user turn — cutting mid tool exchange
        # leaves orphaned tool messages the API rejects.
        if len(messages) > 40:
            cut = len(messages) - 20
            while cut > 1 and messages[cut].get("role") != "user":
                cut -= 1
            _conversations[session_id] = [messages[0]] + messages[cut:]

        trace["llm_calls"] = llm_call_count
        trace["stages"]["llm_ms"] = int(llm_seconds * 1000)
        trace["tokens"] = {"prompt": total_prompt_tokens, "completion": total_completion_tokens}
        _report("judge_start")

        # Layers 2 & 3: Post-process (output validation + LLM judge)
        t_post = time.monotonic()
        with tracer.start_as_current_span(
            "guardrail.post_process",
            attributes={"guardrail.layer": "output_validation_and_judge"},
        ) as post_span:
            post_result = post_process(
                user_input=user_message,
                sanitized_input=pre_result.sanitized_input,
                model_response=assistant_content,
                context_chunks=context_chunks,
                tool_calls=all_tool_calls,
                pre_triggers=pre_result.guardrail_triggers,
                prior_tool_results=prior_tool_results,
            )
            post_span.set_attribute(
                "guardrail.judge_verdict",
                post_result.judge_verdict.get("verdict", "PASS"),
            )
            if post_result.guardrail_triggers:
                post_span.set_attribute(
                    "guardrail.triggers", json.dumps(post_result.guardrail_triggers)
                )
        trace["stages"]["post_ms"] = int((time.monotonic() - t_post) * 1000)
        trace["stages"]["total_ms"] = int((time.monotonic() - t_start) * 1000)
        trace["judge"] = {
            "verdict": post_result.judge_verdict.get("verdict", "PASS"),
            "reason": (post_result.judge_verdict.get("reason") or "")[:240],
        }

        # Set summary attributes on root span
        root_span.set_attribute("gen_ai.request.model", model)
        root_span.set_attribute("mu.llm_call_count", llm_call_count)
        root_span.set_attribute("mu.tool_call_count", len(all_tool_calls))
        root_span.set_attribute("mu.total_prompt_tokens", total_prompt_tokens)
        root_span.set_attribute("mu.total_completion_tokens", total_completion_tokens)
        root_span.set_attribute("mu.total_tokens", total_prompt_tokens + total_completion_tokens)
        root_span.set_attribute(
            "mu.judge_verdict", post_result.judge_verdict.get("verdict", "PASS")
        )
        root_span.set_attribute("mu.response_length", len(post_result.response))
        if all_tool_calls:
            root_span.set_attribute(
                "mu.tools_used", json.dumps([tc["name"] for tc in all_tool_calls])
            )

        record_llm_usage(
            "agent", model, total_prompt_tokens, total_completion_tokens,
            llm_seconds, session_id=session_id,
        )

        # Synthetic incidents from tools land on the live dashboard immediately
        for tc in all_tool_calls:
            if tc["name"] == "create_incident":
                try:
                    hub.broadcast_threadsafe("incident_created", json.loads(tc["result"]))
                except (json.JSONDecodeError, TypeError):
                    pass

        return {
            "response": post_result.response,
            "tool_calls": [
                {"name": tc["name"], "arguments": tc["arguments"]}
                for tc in all_tool_calls
            ],
            # Full results for the UI's data cards (the audit log has them too)
            "tool_results": [
                {"name": tc["name"], "arguments": tc["arguments"], "data": _tool_data(tc["result"])}
                for tc in all_tool_calls
            ],
            "guardrail_triggers": post_result.guardrail_triggers,
            "judge_verdict": post_result.judge_verdict.get("verdict", "PASS"),
            "blocked_by": "judge" if not post_result.allowed else None,
            "trace": trace,
        }


@app.post("/api/chat")
async def chat(request: Request) -> JSONResponse:
    body = await request.json()
    user_message = body.get("message", "").strip()
    session_id = body.get("session_id", "default")

    if not user_message:
        return JSONResponse({"error": "Empty message"}, status_code=400)

    # The pipeline is synchronous (sequential LLM calls); run it off the event
    # loop so telemetry, sentinel and websocket pushes keep flowing meanwhile.
    try:
        result = await asyncio.to_thread(_run_chat_pipeline, user_message, session_id)
    except Exception as e:
        logger.exception("chat pipeline failed")
        return JSONResponse({"error": f"pipeline failed: {e}"}, status_code=502)
    return JSONResponse(result)


@app.post("/api/chat-stream")
async def chat_stream(request: Request):
    """SSE streaming version — runs full pipeline then streams response word-by-word."""
    body = await request.json()
    user_message = body.get("message", "").strip()
    session_id = body.get("session_id", "default")

    if not user_message:
        async def error_stream():
            yield f"data: {json.dumps({'type': 'error', 'content': 'Empty message'})}\n\n"
        return StreamingResponse(error_stream(), media_type="text/event-stream")

    async def event_stream():
        try:
            result = await asyncio.to_thread(_run_chat_pipeline, user_message, session_id)
        except Exception as e:
            logger.exception("chat pipeline failed")
            yield f"data: {json.dumps({'type': 'error', 'content': f'pipeline failed: {e}'})}\n\n"
            return

        # Send metadata first
        meta = {
            "type": "meta",
            "tool_calls": result["tool_calls"],
            "tool_results": result.get("tool_results", []),
            "guardrail_triggers": result["guardrail_triggers"],
            "judge_verdict": result["judge_verdict"],
            "trace": result.get("trace"),
        }
        yield f"data: {json.dumps(meta)}\n\n"

        # Stream response text word-by-word
        words = result["response"].split(" ")
        for i, word in enumerate(words):
            chunk = word if i == 0 else " " + word
            yield f"data: {json.dumps({'type': 'chunk', 'content': chunk})}\n\n"
            await asyncio.sleep(0.03)

        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/api/reset")
async def reset_session(request: Request) -> JSONResponse:
    body = await request.json()
    session_id = body.get("session_id", "default")
    _conversations.pop(session_id, None)
    return JSONResponse({"status": "ok"})


@app.get("/api/status")
async def status() -> JSONResponse:
    from app.config import LLM_BASE_URL, LLM_MODEL, MODE
    from app.llm_client import get_role_client

    from app.voice_live import voice_mode_state

    roles = {}
    for role in ROLES:
        _, resolved_model = get_role_client(role) if role != "voice" else (None, ROLES[role].get("model", ""))
        roles[role] = resolved_model
    vm = voice_mode_state()
    roles["voice"] = vm["model"]
    return JSONResponse({
        "mode": MODE,
        "model": LLM_MODEL,
        "base_url": LLM_BASE_URL,
        "voice_available": voice_available(),
        "voice_mode": vm["mode"],
        "voice_model": vm["model"],
        "voice_guardrails": vm["guardrails"],
        "simulation_running": simulation.running,
        "roles": roles,
    })


@app.get("/api/dashboard")
async def dashboard() -> JSONResponse:
    conn = sqlite3.connect(DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    try:
        by_status = {}
        for row in conn.execute("SELECT status, COUNT(*) AS cnt FROM incidents GROUP BY status"):
            by_status[row["status"]] = row["cnt"]

        by_priority = {}
        for row in conn.execute("SELECT priority, COUNT(*) AS cnt FROM incidents GROUP BY priority"):
            by_priority[row["priority"]] = row["cnt"]

        total = conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]

        recent = [
            dict(row)
            for row in conn.execute(
                "SELECT ticket_id, summary, priority, status, created_at "
                "FROM incidents ORDER BY created_at DESC LIMIT 5"
            )
        ]
    finally:
        conn.close()

    return JSONResponse({
        "by_status": by_status,
        "by_priority": by_priority,
        "total": total,
        "recent": recent,
    })


def main():
    import uvicorn

    uvicorn.run(app, host=SERVER_HOST, port=SERVER_PORT, log_level="info")


if __name__ == "__main__":
    main()
