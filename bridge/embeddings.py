"""
Vector embeddings using OpenAI-compatible API.
Stored in the wa_message_embeddings table (JSON float list for portability).
Cosine similarity search done in-process — fine for thousands of messages.
"""
from __future__ import annotations

import json
import logging
import math
import os

import httpx

logger = logging.getLogger("bridge.embeddings")

EMBEDDING_URL = os.getenv("AI_EMBEDDING_URL", "")
EMBEDDING_MODEL = os.getenv("AI_EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_API_KEY = os.getenv("AI_API_KEY", "local-key")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1536"))


# ---------------------------------------------------------------------------
# Embedding generation
# ---------------------------------------------------------------------------

async def embed(text: str) -> list[float] | None:
    """Call the embeddings API and return a float vector."""
    if not EMBEDDING_URL or not text.strip():
        return None
    try:
        url = EMBEDDING_URL.rstrip("/") + "/embeddings"
        headers = {
            "Authorization": f"Bearer {EMBEDDING_API_KEY}",
            "Content-Type": "application/json",
        }
        payload = {"model": EMBEDDING_MODEL, "input": text[:8000]}
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, json=payload, headers=headers)
            r.raise_for_status()
            return r.json()["data"][0]["embedding"]
    except Exception as exc:
        logger.debug("Embedding failed (non-fatal): %s", exc)
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    mag_a = math.sqrt(sum(x * x for x in a))
    mag_b = math.sqrt(sum(x * x for x in b))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


# ---------------------------------------------------------------------------
# Store message embedding
# ---------------------------------------------------------------------------

async def store_message_embedding(message_id: str, content: str, number: str, direction: str = "inbound") -> None:
    """Generate and persist an embedding for a message."""
    vec = await embed(content)
    if vec is None:
        return
    try:
        from sqlalchemy import select
        from database import AsyncSessionLocal, MessageEmbedding

        async with AsyncSessionLocal() as db:
            existing = await db.execute(
                select(MessageEmbedding).where(MessageEmbedding.message_id == message_id)
            )
            if existing.scalar_one_or_none():
                return
            db.add(MessageEmbedding(
                message_id=message_id,
                number=number,
                direction=direction,
                content=content,
                embedding_json=json.dumps(vec),
            ))
            await db.commit()
    except Exception as exc:
        logger.debug("Failed to store embedding: %s", exc)


# ---------------------------------------------------------------------------
# Search: conversation history
# ---------------------------------------------------------------------------

async def search_messages(query: str, number: str, limit: int = 5) -> list[dict]:
    """Return the most semantically similar past messages for a user."""
    query_vec = await embed(query)
    if query_vec is None:
        return []
    try:
        from sqlalchemy import select
        from database import AsyncSessionLocal, MessageEmbedding

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(MessageEmbedding)
                .where(MessageEmbedding.number == number)
                .order_by(MessageEmbedding.created_at.desc())
                .limit(500)
            )
            rows = result.scalars().all()

        scored = []
        for row in rows:
            if not row.embedding_json:
                continue
            vec = json.loads(row.embedding_json)
            sc = _cosine(query_vec, vec)
            scored.append({"score": sc, "content": row.content, "direction": row.direction})

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]
    except Exception as exc:
        logger.debug("Message vector search failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Search: knowledge base (documents, not conversations)
# ---------------------------------------------------------------------------

async def search_knowledge_base(query: str, limit: int = 5) -> list[dict]:
    """Search the knowledge base (KnowledgeDoc table)."""
    query_vec = await embed(query)
    if query_vec is None:
        return []
    try:
        from sqlalchemy import select
        from database import AsyncSessionLocal, KnowledgeDoc

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(KnowledgeDoc).where(KnowledgeDoc.active == True).limit(1000)
            )
            rows = result.scalars().all()

        scored = []
        for row in rows:
            if not row.embedding_json:
                continue
            vec = json.loads(row.embedding_json)
            sc = _cosine(query_vec, vec)
            scored.append({"score": sc, "content": row.content, "source": row.source})

        scored.sort(key=lambda x: x["score"], reverse=True)
        return [r for r in scored[:limit] if r["score"] > 0.3]
    except Exception as exc:
        logger.debug("Knowledge base search failed: %s", exc)
        return []
