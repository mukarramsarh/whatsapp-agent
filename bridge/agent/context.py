"""
Context builder: recent messages + vector-similar messages from DB.
"""
from __future__ import annotations

import json
import logging

from sqlalchemy import select

from database import AsyncSessionLocal, Message, User

logger = logging.getLogger("bridge.context")


async def _recent_messages(number: str, limit: int) -> list[dict]:
    """Fetch the most recent N messages for a phone number."""
    async with AsyncSessionLocal() as db:
        r = await db.execute(select(User).where(User.number == number))
        user = r.scalar_one_or_none()
        if not user:
            return []
        result = await db.execute(
            select(Message)
            .where(Message.user_id == user.id)
            .order_by(Message.created_at.desc())
            .limit(limit)
        )
        msgs = result.scalars().all()
    return [
        {
            "role": "user" if m.direction == "inbound" else "assistant",
            "content": m.message or "[media attachment]",
        }
        for m in reversed(msgs)
    ]


async def _vector_messages(query: str, number: str, limit: int) -> list[dict]:
    """Fetch semantically similar past messages via vector search."""
    if limit <= 0:
        return []
    try:
        import embeddings

        results = await embeddings.search_messages(query, number=number, limit=limit)
        return [{"role": r["direction"], "content": r["content"]} for r in results]
    except Exception as exc:
        logger.debug("Vector search unavailable: %s", exc)
        return []


async def build(
    number: str,
    query: str,
    recent_count: int = 20,
    old_count: int = 5,
    vector_count: int = 5,
) -> list[dict]:
    """
    Return a list of OpenAI-format messages representing context.
    Combines recent conversation + semantically relevant older messages.
    """
    recent = await _recent_messages(number, recent_count)
    similar = await _vector_messages(query, number, vector_count)

    # De-duplicate: remove from similar anything already in recent
    recent_contents = {m["content"] for m in recent}
    unique_similar = [m for m in similar if m["content"] not in recent_contents]

    context_msgs: list[dict] = []

    if unique_similar:
        context_msgs.append({
            "role": "system",
            "content": (
                "=== Relevant historical context (similar past conversations) ===\n"
                + "\n".join(f"[{m['role'].upper()}]: {m['content']}" for m in unique_similar[:old_count])
                + "\n=== End of historical context ==="
            ),
        })

    context_msgs.extend(recent)
    return context_msgs
