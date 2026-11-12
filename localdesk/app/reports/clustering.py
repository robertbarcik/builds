"""Incident clustering — embed open-ticket summaries, group, label with an LLM."""

import json
import logging
import time

from app.db import get_conn
from app.llm_client import chat_kwargs, get_role_client, parse_json_loosely
from app.ops.metrics import record_llm_usage
from app.rag.embeddings import embed_texts
from app.tracing import tracer

logger = logging.getLogger(__name__)

SIMILARITY_THRESHOLD = 0.72

LABEL_PROMPT = """You are labeling groups of related IT service-desk tickets.
For each group, produce a short label and a one-sentence probable root cause.
Respond ONLY with JSON: {"clusters": [{"index": <group index>, "label": "<max 6 words>",
"root_cause": "<one sentence>"}]}"""


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


def _union_find_groups(n: int, similar_pairs) -> list:
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i, j in similar_pairs:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[rj] = ri

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def cluster_incidents() -> dict:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT ticket_id, summary FROM incidents "
            "WHERE status IN ('open', 'escalated') ORDER BY created_at DESC LIMIT 40"
        ).fetchall()
    finally:
        conn.close()

    if len(rows) < 2:
        return {"clusters": [], "singletons": [r[0] for r in rows], "model": ""}

    summaries = [r[1] for r in rows]
    vectors = embed_texts(summaries)

    pairs = [
        (i, j)
        for i in range(len(rows))
        for j in range(i + 1, len(rows))
        if _cosine(vectors[i], vectors[j]) >= SIMILARITY_THRESHOLD
    ]
    groups = _union_find_groups(len(rows), pairs)
    multi = [g for g in groups if len(g) > 1]
    singletons = [rows[g[0]][0] for g in groups if len(g) == 1]

    if not multi:
        return {"clusters": [], "singletons": singletons, "model": ""}

    # LLM labels the multi-ticket groups
    payload = [
        {"index": gi, "tickets": [{"id": rows[i][0], "summary": rows[i][1]} for i in g]}
        for gi, g in enumerate(multi)
    ]
    client, model = get_role_client("writer")
    labels = {}
    try:
        with tracer.start_as_current_span(
            "gen_ai.chat",
            attributes={
                "gen_ai.system": "openai",
                "gen_ai.request.model": model,
                "gen_ai.operation.name": "chat",
                "mu.llm_call_type": "cluster_labeler",
            },
        ) as span:
            t0 = time.monotonic()
            resp = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": LABEL_PROMPT},
                    {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
                ],
                **chat_kwargs(model, max_tokens=700, temperature=0.2),
            )
            duration = time.monotonic() - t0
            span.set_attribute("mu.llm_duration_s", round(duration, 3))
            if resp.usage:
                record_llm_usage(
                    "writer", model,
                    resp.usage.prompt_tokens or 0,
                    resp.usage.completion_tokens or 0,
                    duration,
                )
            parsed = parse_json_loosely(resp.choices[0].message.content or "")
            for entry in parsed.get("clusters", []):
                labels[entry.get("index")] = entry
    except Exception as e:
        logger.warning("Cluster labeling failed: %s", e)

    clusters = []
    for gi, g in enumerate(multi):
        meta = labels.get(gi, {})
        clusters.append({
            "tickets": [rows[i][0] for i in g],
            "label": meta.get("label", f"{len(g)} related tickets"),
            "root_cause": meta.get("root_cause", "Similar symptoms — probably one root cause."),
        })
    return {"clusters": clusters, "singletons": singletons, "model": model}
