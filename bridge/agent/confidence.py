"""
Confidence scoring layer.
Asks the LLM to rate its own response. Returns (score, reason).
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger("bridge.confidence")

_SCORE_SYSTEM = (
    "You are a quality evaluator. Given a user query and an AI response, "
    "rate the confidence and accuracy of the response. "
    "Respond with a JSON object only: {\"score\": 0.0-1.0, \"reason\": \"brief reason\"}"
)

_SCORE_USER = "Query: {query}\n\nResponse: {response}"


async def score(client, model: str, query: str, response: str) -> tuple[float, str]:
    """
    Returns (confidence_score, reason).
    Score is 0.0-1.0. Returns (1.0, '') on any failure so the system doesn't block.
    """
    try:
        result = await client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SCORE_SYSTEM},
                {"role": "user", "content": _SCORE_USER.format(query=query, response=response)},
            ],
            temperature=0,
            max_tokens=150,
        )
        raw = result.choices[0].message.content or ""
        # Extract JSON even if the model adds surrounding text
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            data = json.loads(raw[start:end])
            sc = float(data.get("score", 1.0))
            reason = str(data.get("reason", ""))
            logger.info("Confidence score: %.2f — %s", sc, reason)
            return min(max(sc, 0.0), 1.0), reason
    except Exception as exc:
        logger.debug("Confidence scoring failed (non-fatal): %s", exc)
    return 1.0, ""
