"""
Async OCR client for PH OCR API (port 5010).
Submit a document/image, poll until done, return extracted text.
"""
import asyncio
import logging
import os

import httpx

OCR_API_URL = os.getenv("OCR_API_URL", "http://192.168.100.62:5010")
OCR_API_KEY = os.getenv("OCR_API_KEY", "")

logger = logging.getLogger("bridge.ocr")

OCR_MIME_TYPES = {
    "image/jpeg",
    "image/jpg",
    "image/png",
    "image/tiff",
    "image/bmp",
    "image/webp",
    "application/pdf",
}


def is_ocreable(mimetype: str) -> bool:
    return mimetype.lower() in OCR_MIME_TYPES


def _headers() -> dict:
    if OCR_API_KEY:
        return {"Authorization": f"Bearer {OCR_API_KEY}"}
    return {}


async def submit(file_bytes: bytes, filename: str, output: str = "text") -> str | None:
    """Submit file to OCR API. Returns job_id or None on error."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"{OCR_API_URL}/api/v1/send_document",
                files={"file": (filename, file_bytes)},
                data={"output": output},
                headers=_headers(),
            )
            r.raise_for_status()
            job = r.json()
            job_id = job["job_id"]
            if job.get("duplicate"):
                logger.info("OCR: duplicate submission — reusing job %s", job_id)
            else:
                logger.info("OCR: submitted job %s for %s", job_id, filename)
            return job_id
    except Exception as exc:
        logger.error("OCR: submit failed for %s: %s", filename, exc)
        return None


async def poll(job_id: str, interval: int = 5, max_wait: int = 300) -> dict | None:
    """Poll until status is done/error. Returns result dict or None on timeout."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + max_wait

    while loop.time() < deadline:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.get(
                    f"{OCR_API_URL}/api/v1/jobs/{job_id}",
                    headers=_headers(),
                )
                r.raise_for_status()
                data = r.json()

            status = data.get("status", "unknown")
            logger.info(
                "OCR: job %s — %s (%s/%s pages)",
                job_id, status,
                data.get("pages_done", 0),
                data.get("page_count", "?"),
            )
            if status in ("done", "error"):
                return data
        except Exception as exc:
            logger.error("OCR: poll error for job %s: %s", job_id, exc)

        await asyncio.sleep(interval)

    logger.error("OCR: job %s timed out after %ds", job_id, max_wait)
    return None


async def submit_and_wait(file_bytes: bytes, filename: str) -> str | None:
    """Submit + poll. Returns extracted text string or None."""
    job_id = await submit(file_bytes, filename)
    if not job_id:
        return None

    result = await poll(job_id)
    if not result or result.get("status") != "done":
        return None

    text = result.get("text", "").strip()
    return text if text else None
