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
from pathlib import Path

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
        return None

    try:
        def _run():
            # Constrain to the two supported languages. Detect first; keep the
            # result only if it is English or Arabic. Otherwise, DON'T just
            # assume Arabic — that used to be the fallback (Whisper mislabels
            # Arabic as e.g. Hebrew, both RTL Semitic), but the same "not
            # en/ar" bucket also catches English misdetected as something
            # else entirely (confirmed live: a clear English voice note came
            # back detected=ur, and forcing Arabic on it produced a garbled,
            # wrong-language transcript). Run BOTH forced passes and keep
            # whichever one Whisper is actually more confident about.
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
                logger.info(
                    "Transcribed %s: detected=%s (unsupported) — comparing forced en/ar",
                    audio_path.name, detected,
                )
                return _best_of_forced(model, audio_path)

            segs, info = model.transcribe(
                str(audio_path), beam_size=5, language=forced, vad_filter=True,
            )
            text = " ".join(s.text for s in segs).strip()
            logger.info(
                "Transcribed %s: lang=%s (forced), %d chars",
                audio_path.name, forced, len(text),
            )
            return text

        return await asyncio.to_thread(_run)
    except Exception as exc:
        logger.error("Transcription failed for %s: %s", audio_path, exc)
        return None


def _best_of_forced(model, audio_path: Path) -> str:
    """Transcribe forced as English and forced as Arabic, and keep whichever
    Whisper is actually more confident about (mean per-segment avg_logprob,
    less negative = better) instead of assuming ambiguous audio is always
    Arabic. Costs a second transcription pass, but only for audio Whisper's
    own language auto-detect couldn't confidently place in en/ar to begin
    with — the common, cheap case (a single confident pass) is unaffected."""
    best_text, best_lang, best_score = "", None, float("-inf")
    for lang in ("en", "ar"):
        try:
            segs, _ = model.transcribe(str(audio_path), beam_size=5, language=lang, vad_filter=True)
            segs = list(segs)
        except Exception as exc:
            logger.warning("Forced transcription (%s) failed: %s", lang, exc)
            continue
        if not segs:
            continue
        text = " ".join(s.text for s in segs).strip()
        score = sum(getattr(s, "avg_logprob", 0.0) for s in segs) / len(segs)
        logger.info("  forced=%s: avg_logprob=%.3f, %d chars", lang, score, len(text))
        if score > best_score:
            best_text, best_lang, best_score = text, lang, score
    logger.info("Picked forced=%s (avg_logprob=%.3f)", best_lang, best_score)
    return best_text


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
            return None

        audio_bytes = b"".join(audio_chunks)
        logger.info("TTS synthesized %d bytes (lang=%s, voice=%s)", len(audio_bytes), language, voice)
        return audio_bytes

    except ImportError:
        logger.warning("edge-tts not installed — voice replies disabled.")
        return None
    except Exception as exc:
        logger.error("TTS synthesis failed: %s", exc)
        return None
