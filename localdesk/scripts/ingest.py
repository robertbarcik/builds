#!/usr/bin/env python3
"""Ingest knowledge base documents into ChromaDB."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import chromadb

from app.config import VECTORSTORE_COLLECTION, VECTORSTORE_PATH
from app.rag.embeddings import embed_texts

KB_DIR = Path(__file__).resolve().parent.parent / "data" / "knowledge_base"
CHUNK_SIZE = 500  # characters
CHUNK_OVERLAP = 100


def chunk_text(text: str, source: str) -> list[dict]:
    """Split text into overlapping chunks."""
    chunks = []
    # Split by double newlines first (paragraphs/sections)
    sections = text.split("\n\n")
    current_chunk = ""
    chunk_idx = 0

    for section in sections:
        if len(current_chunk) + len(section) < CHUNK_SIZE:
            current_chunk += section + "\n\n"
        else:
            if current_chunk.strip():
                chunks.append(
                    {
                        "id": f"{source}::chunk-{chunk_idx}",
                        "text": current_chunk.strip(),
                        "source": source,
                    }
                )
                chunk_idx += 1
                # Keep overlap from end of previous chunk
                overlap = current_chunk[-CHUNK_OVERLAP:] if len(current_chunk) > CHUNK_OVERLAP else ""
                current_chunk = overlap + section + "\n\n"
            else:
                current_chunk = section + "\n\n"

    # Last chunk
    if current_chunk.strip():
        chunks.append(
            {
                "id": f"{source}::chunk-{chunk_idx}",
                "text": current_chunk.strip(),
                "source": source,
            }
        )

    return chunks


def load_documents() -> list[dict]:
    """Load and chunk all markdown files from the knowledge base."""
    all_chunks = []

    for md_file in sorted(KB_DIR.rglob("*.md")):
        relative = md_file.relative_to(KB_DIR)
        source = str(relative)
        text = md_file.read_text(encoding="utf-8")
        chunks = chunk_text(text, source)
        all_chunks.extend(chunks)
        print(f"  {source}: {len(chunks)} chunks")

    return all_chunks


def ingest():
    print("Loading documents...")
    chunks = load_documents()
    print(f"Total chunks: {len(chunks)}")

    print("Generating embeddings...")
    texts = [c["text"] for c in chunks]
    # Batch embeddings to avoid timeout
    batch_size = 32
    all_embeddings = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        all_embeddings.extend(embed_texts(batch))
        print(f"  Embedded {min(i + batch_size, len(texts))}/{len(texts)}")

    print("Storing in ChromaDB...")
    Path(VECTORSTORE_PATH).mkdir(parents=True, exist_ok=True)
    from chromadb.config import Settings
    client = chromadb.PersistentClient(path=VECTORSTORE_PATH, settings=Settings(anonymized_telemetry=False))

    # Delete existing collection if present
    try:
        client.delete_collection(VECTORSTORE_COLLECTION)
    except Exception:
        pass

    collection = client.create_collection(
        name=VECTORSTORE_COLLECTION,
        metadata={"hnsw:space": "cosine"},
    )

    collection.add(
        ids=[c["id"] for c in chunks],
        documents=texts,
        embeddings=all_embeddings,
        metadatas=[{"source": c["source"]} for c in chunks],
    )

    print(f"Done! {len(chunks)} chunks stored in collection '{VECTORSTORE_COLLECTION}'")


if __name__ == "__main__":
    ingest()
