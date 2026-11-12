"""Layer 3: LLM-as-judge — uses the same model to evaluate response quality."""

import json
import logging
import time

from app.llm_client import chat_kwargs, get_role_client, parse_json_loosely
from app.prompts.judge_system import JUDGE_SYSTEM_PROMPT
from app.tracing import tracer

logger = logging.getLogger(__name__)


def judge_response(
    user_query: str,
    agent_response: str,
    retrieved_context: str,
    validator_flags: list | None = None,
) -> dict:
    """Evaluate the agent's response using the LLM as a judge.

    validator_flags: what the static output validator (gate 2) found, e.g.
    SLA numbers with no source. Returns dict with keys: verdict
    (PASS/FLAG/BLOCK), reason, details.
    """
    evaluation_input = json.dumps(
        {
            "user_query": user_query,
            "retrieved_context": retrieved_context or "(none — no knowledge base chunks and no tool results for this turn)",
            "static_validator_flags": validator_flags or [],
            "agent_response": agent_response,
        }
    )

    try:
        client, model = get_role_client("judge")
        with tracer.start_as_current_span(
            "gen_ai.chat",
            attributes={
                "gen_ai.system": "openai",
                "gen_ai.request.model": model,
                "gen_ai.request.temperature": 0.0,
                "gen_ai.request.max_tokens": 512,
                "gen_ai.operation.name": "chat",
                "mu.llm_call_type": "judge",
            },
        ) as span:
            t0 = time.monotonic()
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": JUDGE_SYSTEM_PROMPT},
                    {"role": "user", "content": evaluation_input},
                ],
                # Thinking models (qwen3.x, gpt-5.x) reason before the JSON
                # verdict and that reasoning counts against the budget.
                **chat_kwargs(model, max_tokens=2500, temperature=0.0),
            )
            duration = time.monotonic() - t0
            span.set_attribute("mu.llm_duration_s", round(duration, 3))
            span.set_attribute("gen_ai.response.model", model)
            if resp.usage:
                span.set_attribute(
                    "gen_ai.usage.prompt_tokens", resp.usage.prompt_tokens or 0
                )
                span.set_attribute(
                    "gen_ai.usage.completion_tokens", resp.usage.completion_tokens or 0
                )

            if resp.usage:
                from app.ops.metrics import record_llm_usage
                record_llm_usage(
                    "judge", model,
                    resp.usage.prompt_tokens or 0,
                    resp.usage.completion_tokens or 0,
                    duration,
                )

            content = (resp.choices[0].message.content or "").strip()
            if not content:
                raise ValueError(
                    "empty verdict (reasoning budget exhausted?) "
                    f"finish_reason={resp.choices[0].finish_reason}"
                )
            verdict = parse_json_loosely(content)
            # Ensure required fields
            if "verdict" not in verdict:
                verdict["verdict"] = "PASS"
            if "reason" not in verdict:
                verdict["reason"] = ""
            span.set_attribute("mu.judge_verdict", verdict["verdict"])
            return verdict
    except Exception as e:
        # Fail closed-ish: a judge that could not run must not look like a
        # judge that approved. FLAG keeps the answer visible but marks it.
        logger.warning("LLM judge failed: %s — marking response FLAG (judge unavailable)", e)
        return {
            "verdict": "FLAG",
            "reason": f"Judge unavailable: {str(e)[:160]}",
            "details": {"judge_unavailable": True},
        }
