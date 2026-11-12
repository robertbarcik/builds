"""Knowledge base search tool (RAG)."""

import json

from app.rag.retriever import retrieve


def search_kb(query: str) -> str:
    chunks = retrieve(query, n_results=3)
    results = []
    for chunk in chunks:
        results.append(
            {
                "source": chunk["source"],
                "content": chunk["text"],
            }
        )
    return json.dumps({"query": query, "results": results})
