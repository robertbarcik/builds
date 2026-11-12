import httpx

from app.config import EMBEDDING_BASE_URL, EMBEDDING_MODEL


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Get embeddings from Ollama for a list of texts."""
    url = f"{EMBEDDING_BASE_URL}/api/embed"
    resp = httpx.post(url, json={"model": EMBEDDING_MODEL, "input": texts}, timeout=120)
    resp.raise_for_status()
    return resp.json()["embeddings"]


def embed_query(text: str) -> list[float]:
    """Get embedding for a single query string."""
    return embed_texts([text])[0]
