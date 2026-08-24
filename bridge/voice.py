"""
Voice support:
  - STT: faster-whisper (local, CPU/GPU)
  - TTS: edge-tts (Microsoft neural voices, supports Arabic + English)
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import tempfile
import time
from pathlib import Path

import metrics

logger = logging.getLogger("bridge.voice")

WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL", "base")

# Lazy singletons
_whisper_model = None
_whisper_lock = asyncio.Lock()


async def _get_whisper():
    global _whisper_model
    async with _whisper_lock:
        if _whisper_model is None:
            try:
                from faster_whisper import WhisperModel

                logger.info("Loading Whisper model '%s' …", WHISPER_MODEL_SIZE)
                _whisper_model = await asyncio.to_thread(
                    WhisperModel, WHISPER_MODEL_SIZE, device="cpu", compute_type="int8"
                )
                logger.info("Whisper model loaded.")
            except Exception as exc:
                logger.warning("Whisper not available: %s", exc)
                _whisper_model = False  # sentinel: don't retry
        return _whisper_model if _whisper_model is not False else None


# ---------------------------------------------------------------------------
# STT
# ---------------------------------------------------------------------------

async def transcribe(audio_path: Path, language: str | None = None) -> str | None:
    """
    Transcribe an audio file to text using faster-whisper.
    Returns transcribed text or None if unavailable/failed.
    """
    model = await _get_whisper()
    if model is None:
        logger.warning("Whisper model not loaded — skipping transcription.")
        metrics.stt_requests_total.labels(status="unavailable").inc()
        return None

    start = time.monotonic()
    try:
        def _run():
            # Constrain to the two supported languages. Detect first; keep the
            # result only if it is English or Arabic, otherwise force Arabic —
            # Whisper otherwise mislabels Arabic as e.g. Hebrew (both RTL Semitic)
            # and the reply comes back in the wrong language.
            forced = language  # honour an explicit caller override if given
            if forced is None:
                segs, info = model.transcribe(
                    str(audio_path), beam_size=5, language=None, vad_filter=True,
                )
                detected = info.language
                if detected in ("en", "ar"):
                    text = " ".join(s.text for s in segs).strip()
                    logger.info(
                        "Transcribed %s: detected=%s (kept), %d chars",
                        audio_path.name, detected, len(text),
                    )
                    return text
                # Not one of the two supported languages → redo as Arabic.
                logger.info("Transcribed %s: detected=%s → forcing Arabic", audio_path.name, detected)
                forced = "ar"

            segs, info = model.transcribe(
                str(audio_path), beam_size=5, language=forced, vad_filter=True,
            )
            text = " ".join(s.text for s in segs).strip()
            logger.info(
                "Transcribed %s: lang=%s (forced), %d chars",
                audio_path.name, forced, len(text),
            )
            return text

        result = await asyncio.to_thread(_run)
        metrics.stt_duration_seconds.observe(time.monotonic() - start)
        metrics.stt_requests_total.labels(status="success").inc()
        return result
    except Exception as exc:
        metrics.stt_duration_seconds.observe(time.monotonic() - start)
        metrics.stt_requests_total.labels(status="failure").inc()
        logger.error("Transcription failed for %s: %s", audio_path, exc)
        return None


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------

# edge-tts voice names
VOICES = {
    "en": os.getenv("TTS_VOICE_EN", "en-US-JennyNeural"),
    "ar": os.getenv("TTS_VOICE_AR", "ar-SA-HamedNeural"),
}


async def synthesize(text: str, language: str = "en") -> bytes | None:
    """
    Convert text to speech using edge-tts.
    Returns MP3 bytes or None if unavailable/failed.
    """
    if not text:
        return None

    start = time.monotonic()
    try:
        import edge_tts

        voice = VOICES.get(language, VOICES["en"])
        communicate = edge_tts.Communicate(text, voice)

        audio_chunks: list[bytes] = []
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio_chunks.append(chunk["data"])

        if not audio_chunks:
            logger.warning("TTS returned no audio for language=%s", language)
            metrics.tts_duration_seconds.observe(time.monotonic() - start)
            metrics.tts_requests_total.labels(status="empty", language=language).inc()
            return None

        audio_bytes = b"".join(audio_chunks)
        logger.info("TTS synthesized %d bytes (lang=%s, voice=%s)", len(audio_bytes), language, voice)
        metrics.tts_duration_seconds.observe(time.monotonic() - start)
        metrics.tts_requests_total.labels(status="success", language=language).inc()
        return audio_bytes

    except ImportError:
        logger.warning("edge-tts not installed — voice replies disabled.")
        metrics.tts_requests_total.labels(status="unavailable", language=language).inc()
        return None
    except Exception as exc:
        metrics.tts_duration_seconds.observe(time.monotonic() - start)
        metrics.tts_requests_total.labels(status="failure", language=language).inc()
        logger.error("TTS synthesis failed: %s", exc)
        return None
