"""
DeepInfra client for everything that isn't chat completions: speech-to-text,
text-to-speech fallback, image editing / generation and image understanding.

Chat + embeddings keep using the OpenAI-compatible client in agent/runner.py
and embeddings.py; this module is the media side of the same account.

All calls are plain httpx with retry/backoff on 429/5xx, and return None (or
raise DeepInfraError for the image tools, which need to explain the failure to
the user) instead of ever fabricating a result.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import os

import httpx

logger = logging.getLogger("bridge.deepinfra")

API_KEY = os.getenv("DEEPINFRA_API_KEY") or os.getenv("AI_API_KEY", "")
BASE = os.getenv("DEEPINFRA_BASE_URL", "https://api.deepinfra.com/v1").rstrip("/")

STT_MODEL = os.getenv("STT_MODEL", "openai/whisper-large-v3-turbo")
IMAGE_EDIT_MODEL = os.getenv("IMAGE_EDIT_MODEL", "black-forest-labs/FLUX.1-Kontext-dev")
IMAGE_GEN_MODEL = os.getenv("IMAGE_GEN_MODEL", "black-forest-labs/FLUX-1-schnell")
VISION_MODEL = os.getenv("VISION_MODEL", "Qwen/Qwen3-VL-30B-A3B-Instruct")
TTS_FALLBACK_AR = os.getenv("TTS_FALLBACK_AR", "ResembleAI/chatterbox-multilingual")
TTS_FALLBACK_EN = os.getenv("TTS_FALLBACK_EN", "hexgrad/Kokoro-82M")

MAX_IMAGE_SIDE = int(os.getenv("IMAGE_MAX_SIDE", "1280"))

_RETRY_STATUS = {429, 500, 502, 503, 504}


class DeepInfraError(Exception):
    """A DeepInfra media call failed; the message is safe to show a user."""


def _auth() -> dict:
    return {"Authorization": f"Bearer {API_KEY}"}


async def _request(method: str, url: str, *, retries: int = 3, timeout: float = 120, **kw) -> httpx.Response:
    delay = 1.5
    last: Exception | None = None
    for attempt in range(retries):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.request(method, url, headers=_auth(), **kw)
            if r.status_code in _RETRY_STATUS and attempt < retries - 1:
                logger.warning("DeepInfra %s -> %d, retrying in %.1fs", url, r.status_code, delay)
                await asyncio.sleep(delay)
                delay *= 2
                continue
            return r
        except httpx.TransportError as exc:
            last = exc
            if attempt < retries - 1:
                await asyncio.sleep(delay)
                delay *= 2
    raise DeepInfraError(f"network error: {last}")


# ---------------------------------------------------------------------------
# Image helpers
# ---------------------------------------------------------------------------

def normalize_image(data: bytes, max_side: int = MAX_IMAGE_SIDE) -> tuple[bytes, str]:
    """Decode any WhatsApp image (jpeg/png/webp sticker/…), flatten alpha,
    downscale so the longest side is <= max_side, return (jpeg_bytes, 'image/jpeg').
    Keeps model input small and predictable."""
    from PIL import Image, ImageOps

    img = Image.open(io.BytesIO(data))
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")
    w, h = img.size
    scale = max_side / max(w, h)
    if scale < 1:
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=90)
    return out.getvalue(), "image/jpeg"


def _b64_from_image_response(body: dict) -> bytes:
    items = body.get("data") or []
    if not items:
        raise DeepInfraError("the image service returned no image")
    item = items[0]
    if item.get("b64_json"):
        return base64.b64decode(item["b64_json"])
    if item.get("url"):
        raise DeepInfraError("the image service returned a URL instead of image data")
    raise DeepInfraError("the image service returned an empty result")


# ---------------------------------------------------------------------------
# Image edit / generate / describe
# ---------------------------------------------------------------------------

async def edit_image(image_bytes: bytes, instruction: str) -> bytes:
    """Image-to-image: apply `instruction` (English works best) to the photo."""
    jpeg, mime = normalize_image(image_bytes)
    r = await _request(
        "POST", f"{BASE}/openai/images/edits",
        data={"model": IMAGE_EDIT_MODEL, "prompt": instruction, "n": "1", "response_format": "b64_json"},
        files={"image": ("input.jpg", jpeg, mime)},
        timeout=180,
    )
    if r.status_code != 200:
        logger.error("image edit failed: %d %s", r.status_code, r.text[:300])
        raise DeepInfraError(f"image editing failed (HTTP {r.status_code})")
    return _b64_from_image_response(r.json())


async def generate_image(prompt: str, size: str = "1024x1024") -> bytes:
    """Text-to-image."""
    r = await _request(
        "POST", f"{BASE}/openai/images/generations",
        json={"model": IMAGE_GEN_MODEL, "prompt": prompt, "n": 1, "size": size, "response_format": "b64_json"},
        timeout=180,
    )
    if r.status_code != 200:
        logger.error("image generation failed: %d %s", r.status_code, r.text[:300])
        raise DeepInfraError(f"image generation failed (HTTP {r.status_code})")
    return _b64_from_image_response(r.json())


async def describe_image(image_bytes: bytes, question: str, language: str = "en") -> str:
    """Vision-language understanding of a photo (what is in it, read signs,
    answer a question about it)."""
    jpeg, mime = normalize_image(image_bytes)
    data_uri = f"data:{mime};base64,{base64.b64encode(jpeg).decode()}"
    lang_rule = "Answer in Arabic." if language == "ar" else "Answer in English."
    payload = {
        "model": VISION_MODEL,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_uri}},
                {"type": "text", "text": f"{question}\n\n{lang_rule} Be accurate; say so if something is unclear."},
            ],
        }],
        "max_tokens": 900,
        "temperature": 0.2,
    }
    r = await _request("POST", f"{BASE}/openai/chat/completions", json=payload, timeout=120)
    if r.status_code != 200:
        logger.error("vision call failed: %d %s", r.status_code, r.text[:300])
        raise DeepInfraError(f"image understanding failed (HTTP {r.status_code})")
    try:
        return (r.json()["choices"][0]["message"]["content"] or "").strip()
    except Exception as exc:
        raise DeepInfraError("the vision model returned an unexpected response") from exc


# ---------------------------------------------------------------------------
# Speech
# ---------------------------------------------------------------------------

async def transcribe(audio_bytes: bytes, filename: str = "voice.ogg", language: str | None = None) -> str | None:
    """Speech-to-text via Whisper large-v3-turbo. Returns text or None."""
    data = {"model": STT_MODEL, "response_format": "json"}
    if language:
        data["language"] = language
    try:
        r = await _request(
            "POST", f"{BASE}/openai/audio/transcriptions",
            data=data, files={"file": (filename, audio_bytes, "application/octet-stream")}, timeout=120,
        )
    except DeepInfraError as exc:
        logger.error("STT network failure: %s", exc)
        return None
    if r.status_code != 200:
        logger.error("STT failed: %d %s", r.status_code, r.text[:300])
        return None
    return (r.json().get("text") or "").strip() or None


async def synthesize(text: str, language: str) -> bytes | None:
    """Fallback text-to-speech (used when edge-tts is unreachable). Returns WAV."""
    try:
        if language == "ar":
            r = await _request(
                "POST", f"{BASE}/inference/{TTS_FALLBACK_AR}",
                json={"text": text, "language_id": "ar"}, timeout=120,
            )
            if r.status_code != 200:
                logger.error("fallback TTS (ar) failed: %d %s", r.status_code, r.text[:200])
                return None
            audio = r.json().get("audio", "")
            return base64.b64decode(audio.split(",", 1)[-1]) if audio else None
        r = await _request(
            "POST", f"{BASE}/openai/audio/speech",
            json={"model": TTS_FALLBACK_EN, "input": text, "voice": "af_bella"}, timeout=120,
        )
        if r.status_code != 200:
            logger.error("fallback TTS (en) failed: %d %s", r.status_code, r.text[:200])
            return None
        return r.content
    except DeepInfraError as exc:
        logger.error("fallback TTS network failure: %s", exc)
        return None
