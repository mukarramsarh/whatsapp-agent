"""
Async client for the Cortex WhatsApp gateway (separate project,
cortex-whatsapp-gateway) — submit a search, poll until done, return the
result. Same submit/poll shape as bridge/ocr.py's PH OCR client, since both
front a job-based upstream.
"""
import asyncio
import logging
import os

import httpx

CORTEX_GATEWAY_URL = os.getenv("CORTEX_GATEWAY_URL", "http://host.docker.internal:8090")
CORTEX_GATEWAY_API_KEY = os.getenv("CORTEX_GATEWAY_API_KEY", "")

logger = logging.getLogger("bridge.cortex_gateway")

VALID_PATHWAYS = ("technical", "consulting", "contracts_tenders")


def _headers() -> dict:
    if CORTEX_GATEWAY_API_KEY:
        return {"Authorization": f"Bearer {CORTEX_GATEWAY_API_KEY}"}
    return {}


async def submit_search(query: str, pathway: str = "technical", limit: int = 5) -> str | None:
    """Submit a search to the gateway. Returns job_id or None on error."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(
                f"{CORTEX_GATEWAY_URL}/api/v1/search",
                json={"query": query, "pathway": pathway, "limit": limit},
                headers=_headers(),
            )
            r.raise_for_status()
            job = r.json()
            job_id = job["job_id"]
            logger.info("cortex search: submitted job %s (pathway=%s)", job_id, pathway)
            return job_id
    except Exception as exc:
        logger.error("cortex search: submit failed for query %r: %s", query, exc)
        return None


async def poll(job_id: str, interval: int = 3, max_wait: int = 60) -> dict | None:
    """Poll until status is done/error. Returns the job body or None on timeout."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max_wait

    while loop.time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(
                    f"{CORTEX_GATEWAY_URL}/api/v1/jobs/{job_id}",
                    headers=_headers(),
                )
                r.raise_for_status()
                data = r.json()

            status = data.get("status", "unknown")
            logger.info("cortex search: job %s — %s", job_id, status)
            if status in ("done", "error"):
                return data
        except Exception as exc:
            logger.error("cortex search: poll error for job %s: %s", job_id, exc)

        await asyncio.sleep(interval)

    logger.error("cortex search: job %s timed out after %ds", job_id, max_wait)
    return None


async def search_and_wait(query: str, pathway: str = "technical", limit: int = 5) -> dict | None:
    """Submit + poll. Returns the done/error job body, or None on timeout."""
    job_id = await submit_search(query, pathway=pathway, limit=limit)
    if not job_id:
        return None
    return await poll(job_id)
