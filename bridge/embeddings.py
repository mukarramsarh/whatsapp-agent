"""
Vector embeddings using OpenAI-compatible API.
Stored in the wa_message_embeddings table (JSON float list for portability).
Cosine similarity search done in-process — fine for thousands of messages.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time

import httpx

import metrics

logger = logging.getLogger("bridge.embeddings")

EMBEDDING_URL = os.getenv("AI_EMBEDDING_URL", "")
EMBEDDING_MODEL = os.getenv("AI_EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_API_KEY = os.getenv("AI_API_KEY", "local-key")
EMBEDDING_DIM = int(os.getenv("EMBEDDING_DIM", "1024"))
# Transport: "ollama" (native /api/embed) or "openai" (OpenAI-compatible /embeddings)
EMBEDDING_API = os.getenv("AI_EMBEDDING_API", "openai").lower()
MAX_RETRIES = int(os.getenv("AI_MAX_RETRIES", "4"))

# Status codes worth retrying: rate-limit + transient server errors.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


# ---------------------------------------------------------------------------
# Embedding generation
# ---------------------------------------------------------------------------

async def _post_with_retry(
    client: httpx.AsyncClient, url: str, payload: dict, headers: dict
) -> httpx.Response:
    """POST with exponential backoff on 429 / 5xx / transport errors.

    The DGX inference servers return 429 when their GPU memory budget is
    saturated, so we back off (1s → 2s → 4s …) rather than fail immediately.
    Non-retryable HTTP errors and the final attempt re-raise.
    """
    delay = 1.0
    for attempt in range(MAX_RETRIES):
        try:
            r = await client.post(url, json=payload, headers=headers)
            if r.status_code in _RETRYABLE_STATUS:
                r.raise_for_status()  # -> HTTPStatusError, handled below
            r.raise_for_status()
            return r
        except (httpx.HTTPStatusError, httpx.TransportError) as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            retryable = status is None or status in _RETRYABLE_STATUS
            if attempt == MAX_RETRIES - 1 or not retryable:
                raise
            metrics.embedding_retries_total.inc()
            logger.debug("Embedding POST retry %d (status=%s) in %.0fs", attempt + 1, status, delay)
            await asyncio.sleep(delay)
            delay *= 2
    # Unreachable — the loop either returns or raises.
    raise RuntimeError("embedding retry loop exhausted")


async def embed(text: str) -> list[float] | None:
    """Call the embeddings API and return a float vector."""
    if not EMBEDDING_URL or not text.strip():
        metrics.embedding_requests_total.labels(status="no_url").inc()
        return None
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            if EMBEDDING_API == "ollama":
                url = EMBEDDING_URL.rstrip("/") + "/api/embed"
                payload = {"model": EMBEDDING_MODEL, "input": text[:8000]}
                headers = {"Content-Type": "application/json"}
                r = await _post_with_retry(client, url, payload, headers)
                vec = r.json()["embeddings"][0]
            else:
                # OpenAI-compatible transport
                url = EMBEDDING_URL.rstrip("/") + "/embeddings"
                headers = {
                    "Authorization": f"Bearer {EMBEDDING_API_KEY}",
                    "Content-Type": "application/json",
                }
                payload = {"model": EMBEDDING_MODEL, "input": text[:8000]}
                r = await _post_with_retry(client, url, payload, headers)
                vec = r.json()["data"][0]["embedding"]
        metrics.embedding_duration_seconds.observe(time.monotonic() - start)
        metrics.embedding_requests_total.labels(status="success").inc()
        return vec
    except Exception as exc:
        metrics.embedding_duration_seconds.observe(time.monotonic() - start)
        metrics.embedding_requests_total.labels(status="failure").inc()
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
