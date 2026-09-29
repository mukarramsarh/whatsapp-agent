"""
Async client for the Cortex WhatsApp gateway (separate project,
cortex-whatsapp-gateway) — generic submit/poll/download helpers shared by
every Cortex-backed tool (search, classify, Cortex OCR, BD analysis, tender
generation). Same submit/poll shape as bridge/ocr.py's PH OCR client, since
both front a job-based upstream.
"""
import asyncio
import logging
import os

import httpx

CORTEX_GATEWAY_URL = os.getenv("CORTEX_GATEWAY_URL", "http://host.docker.internal:8090")
CORTEX_GATEWAY_API_KEY = os.getenv("CORTEX_GATEWAY_API_KEY", "")

logger = logging.getLogger("bridge.cortex_gateway")

VALID_PATHWAYS = ("technical", "consulting", "contracts_tenders")
# services/bd-analyzer/app/config.py's real OFFER_TRACK_NAMES, with the
# Arabic labels the analyzer's own UI uses — for presenting real choices to
# the user instead of asking for free text.
VALID_BD_TRACKS = {
    "technical": "المسار التقني",
    "consulting": "المسار الاستشاري",
    "secondment": "مسار الإعارة",
    "managed_services": "مسار الخدمات المدارة",
}


def _headers() -> dict:
    if CORTEX_GATEWAY_API_KEY:
        return {"Authorization": f"Bearer {CORTEX_GATEWAY_API_KEY}"}
    return {}


async def submit_json(path: str, payload: dict) -> str | None:
    """POST a JSON body to a submit endpoint. Returns job_id or None on error."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(f"{CORTEX_GATEWAY_URL}{path}", json=payload, headers=_headers())
            r.raise_for_status()
            job_id = r.json()["job_id"]
            logger.info("cortex gateway: submitted %s -> job %s", path, job_id)
            return job_id
    except Exception as exc:
        logger.error("cortex gateway: submit failed for %s: %s", path, exc)
        return None


async def submit_multipart(
    path: str, file_bytes: bytes, filename: str, content_type: str = "application/octet-stream"
) -> str | None:
    """POST a file to a submit endpoint. Returns job_id or None on error."""
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                f"{CORTEX_GATEWAY_URL}{path}",
                files={"file": (filename, file_bytes, content_type)},
                headers=_headers(),
            )
            r.raise_for_status()
            job_id = r.json()["job_id"]
            logger.info("cortex gateway: submitted %s -> job %s", path, job_id)
            return job_id
    except Exception as exc:
        logger.error("cortex gateway: submit failed for %s: %s", path, exc)
        return None


async def poll(job_id: str, interval: int = 3, max_wait: int = 60) -> dict | None:
    """Poll until status is done/error. Returns the job body or None on timeout."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max_wait

    while loop.time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.get(f"{CORTEX_GATEWAY_URL}/api/v1/jobs/{job_id}", headers=_headers())
                r.raise_for_status()
                data = r.json()

            status = data.get("status", "unknown")
            logger.info("cortex gateway: job %s — %s", job_id, status)
            if status in ("done", "error"):
                return data
        except Exception as exc:
            logger.error("cortex gateway: poll error for job %s: %s", job_id, exc)

        await asyncio.sleep(interval)

    logger.error("cortex gateway: job %s timed out after %ds", job_id, max_wait)
    return None


async def get_file(path: str) -> tuple[bytes, str] | None:
    """GET a file from the gateway (e.g. /api/v1/files/{job_id} or a tender
    export path). Returns (bytes, content_type) or None on error."""
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.get(f"{CORTEX_GATEWAY_URL}{path}", headers=_headers())
            r.raise_for_status()
            return r.content, r.headers.get("content-type", "application/octet-stream")
    except Exception as exc:
        logger.error("cortex gateway: file fetch failed for %s: %s", path, exc)
        return None


# ── e-Library search ─────────────────────────────────────────────────────────

async def submit_search(query: str, pathway: str = "technical", limit: int = 5) -> str | None:
    return await submit_json("/api/v1/search", {"query": query, "pathway": pathway, "limit": limit})


async def search_and_wait(query: str, pathway: str = "technical", limit: int = 5) -> dict | None:
    """Submit + poll. Returns the done/error job body, or None on timeout."""
    job_id = await submit_search(query, pathway=pathway, limit=limit)
    if not job_id:
        return None
    return await poll(job_id)
