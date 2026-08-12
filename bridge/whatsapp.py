"""
Evolution API client — all outbound and media operations live here
so both main.py and admin.py can import without circular deps.
"""
import asyncio
import base64
import logging
import mimetypes
import os
import uuid
from pathlib import Path

import httpx

logger = logging.getLogger("bridge.whatsapp")

EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL", "http://evolution-api:8080")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")
INSTANCE_NAME = os.getenv("INSTANCE_NAME", "STC")
MEDIA_DIR = Path(os.getenv("MEDIA_DIR", "/app/media"))

MEDIA_MESSAGE_KEYS = {
    "imageMessage",
    "videoMessage",
    "audioMessage",
    "documentMessage",
    "stickerMessage",
    "documentWithCaptionMessage",
}


def _headers() -> dict:
    return {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}


def media_type_from_mime(mimetype: str) -> str:
    if mimetype.startswith("image/"):
        return "image"
    if mimetype.startswith("video/"):
        return "video"
    if mimetype.startswith("audio/"):
        return "audio"
    return "document"


def ext_from_mime(mimetype: str, filename: str = "") -> str:
    if filename and "." in filename:
        return "." + filename.rsplit(".", 1)[-1].lower()
    return mimetypes.guess_extension(mimetype) or ".bin"


# ---------------------------------------------------------------------------
# Outbound
# ---------------------------------------------------------------------------

async def send_text(jid: str, text: str) -> None:
    url = f"{EVOLUTION_API_URL}/message/sendText/{INSTANCE_NAME}"
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, json={"number": jid, "text": text}, headers=_headers())
            r.raise_for_status()
        logger.info("Sent text to %s", jid)
    except Exception as exc:
        logger.error("send_text failed for %s: %s", jid, exc)


async def send_media(
    jid: str,
    file_bytes: bytes,
    mimetype: str,
    filename: str,
    caption: str = "",
) -> bool:
    url = f"{EVOLUTION_API_URL}/message/sendMedia/{INSTANCE_NAME}"
    payload = {
        "number": jid,
        "mediatype": media_type_from_mime(mimetype),
        "mimetype": mimetype,
        "caption": caption,
        "media": base64.b64encode(file_bytes).decode(),
        "fileName": filename,
    }
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(url, json=payload, headers=_headers())
            r.raise_for_status()
        logger.info("Sent media %s to %s", filename, jid)
        return True
    except Exception as exc:
        logger.error("send_media failed for %s: %s", jid, exc)
        return False


# ---------------------------------------------------------------------------
# Inbound media download
# ---------------------------------------------------------------------------

async def download_and_save(message_data: dict) -> dict | None:
    """
    Ask Evolution API for the base64 of a media message,
    write it to MEDIA_DIR, and return a metadata dict.
    Returns None if the message has no downloadable media or on error.
    """
    message: dict = message_data.get("message") or {}
    if not any(k in message for k in MEDIA_MESSAGE_KEYS):
        return None

    await asyncio.to_thread(MEDIA_DIR.mkdir, parents=True, exist_ok=True)

    url = f"{EVOLUTION_API_URL}/chat/getBase64FromMediaMessage/{INSTANCE_NAME}"
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(
                url,
                json={"message": message_data, "convertToMp4": False},
                headers=_headers(),
            )
            r.raise_for_status()
            data = r.json()
    except Exception as exc:
        logger.error("Media download from Evolution API failed: %s", exc)
        return None

    b64 = data.get("base64", "")
    mimetype = data.get("mimetype", "application/octet-stream")
    if not b64:
        logger.warning("Evolution API returned no base64 for media message")
        return None

    # Try to get the original filename from the message
    media_key = next((k for k in message if k in MEDIA_MESSAGE_KEYS), "")
    original_name = (message.get(media_key) or {}).get("fileName", "")
    caption = (message.get(media_key) or {}).get("caption", "")

    ext = ext_from_mime(mimetype, original_name)
    filename = f"{uuid.uuid4()}{ext}"
    file_path = MEDIA_DIR / filename

    try:
        file_bytes = base64.b64decode(b64)
        await asyncio.to_thread(file_path.write_bytes, file_bytes)
    except Exception as exc:
        logger.error("Failed to write media file: %s", exc)
        return None

    logger.info("Saved inbound media: %s (%s, %d bytes)", filename, mimetype, len(file_bytes))
    return {
        "path": f"media/{filename}",
        "name": original_name or filename,
        "mime": mimetype,
        "size": len(file_bytes),
        "caption": caption,
    }


def save_upload(file_bytes: bytes, mimetype: str, original_name: str) -> dict:
    """Save an admin-uploaded file to MEDIA_DIR and return metadata dict."""
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    ext = ext_from_mime(mimetype, original_name)
    filename = f"{uuid.uuid4()}{ext}"
    (MEDIA_DIR / filename).write_bytes(file_bytes)
    return {
        "path": f"media/{filename}",
        "name": original_name or filename,
        "mime": mimetype,
        "size": len(file_bytes),
        "caption": "",
    }
