"""
Voice support:
  - STT: DeepInfra Whisper large-v3-turbo by default (STT_PROVIDER=deepinfra);
         local faster-whisper if STT_PROVIDER=local (CPU/GPU hosts).
  - TTS: edge-tts (Microsoft neural voices, Arabic + English) first; if it is
         unreachable, a DeepInfra TTS model (TTS_PROVIDER=auto|edge|deepinfra).
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from pathlib import Path

logger = logging.getLogger("bridge.voice")

WHISPER_MODEL_SIZE = os.getenv("WHISPER_MODEL", "base")
STT_PROVIDER = os.getenv("STT_PROVIDER", "deepinfra").lower()
TTS_PROVIDER = os.getenv("TTS_PROVIDER", "auto").lower()
TTS_MAX_CHARS = int(os.getenv("TTS_MAX_CHARS", "2500"))

# Lazy singletons (local STT only)
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
    """Transcribe an audio file to text. Returns text, or None if unavailable/failed."""
    if STT_PROVIDER != "local":
        import deepinfra

        try:
            audio = await asyncio.to_thread(audio_path.read_bytes)
        except Exception as exc:
            logger.error("Cannot read audio %s: %s", audio_path, exc)
            return None
        text = await deepinfra.transcribe(audio, audio_path.name, language)
        logger.info("Transcribed %s via DeepInfra: %d chars", audio_path.name, len(text or ""))
        return text

    model = await _get_whisper()
    if model is None:
        logger.warning("Whisper model not loaded — skipping transcription.")
        return None

    try:
        def _run():
            # Constrain to the two supported languages. Detect first; keep the
            # result only if it is English or Arabic, otherwise run both forced
            # passes and keep the one Whisper is more confident about.
            forced = language
            if forced is None:
                segs, info = model.transcribe(
                    str(audio_path), beam_size=5, language=None, vad_filter=True,
                )
                if info.language in ("en", "ar"):
                    return " ".join(s.text for s in segs).strip()
                return _best_of_forced(model, audio_path)

            segs, _ = model.transcribe(
                str(audio_path), beam_size=5, language=forced, vad_filter=True,
            )
            return " ".join(s.text for s in segs).strip()

        return await asyncio.to_thread(_run)
    except Exception as exc:
        logger.error("Transcription failed for %s: %s", audio_path, exc)
        return None


def _best_of_forced(model, audio_path: Path) -> str:
    """Transcribe forced as English and forced as Arabic, keep whichever Whisper
    is more confident about (mean per-segment avg_logprob, less negative = better)."""
    best_text, best_score = "", float("-inf")
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
        if score > best_score:
            best_text, best_score = text, score
    return best_text


# ---------------------------------------------------------------------------
# TTS
# ---------------------------------------------------------------------------

VOICES = {
    "en": os.getenv("TTS_VOICE_EN", "en-US-JennyNeural"),
    "ar": os.getenv("TTS_VOICE_AR", "ar-SA-HamedNeural"),
}

_URL_RE = re.compile(r"https?://\S+|www\.\S+")
_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0000FE0F\U0000200D\U00002B00-\U00002BFF]+"
)
_MD_RE = re.compile(r"[*_~`#>|]+")
_BULLET_RE = re.compile(r"^[\s•\-–—·●▪◦]+", re.M)


def prepare_for_speech(text: str) -> str:
    """Make a chat reply listenable: drop URLs, emoji, markdown marks and
    bullet glyphs, collapse whitespace, cap the length at a sentence end."""
    text = _URL_RE.sub(" ", text or "")
    text = _EMOJI_RE.sub(" ", text)
    text = _MD_RE.sub(" ", text)
    text = _BULLET_RE.sub("", text)
    text = re.sub(r"\s*\n+\s*", ". ", text)
    text = re.sub(r"\.\s*\.", ".", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    if len(text) > TTS_MAX_CHARS:
        cut = text[:TTS_MAX_CHARS]
        end = max(cut.rfind(". "), cut.rfind("؟"), cut.rfind("!"), cut.rfind("؛"))
        text = cut[: end + 1] if end > TTS_MAX_CHARS // 2 else cut
    return text


async def _edge_tts(text: str, language: str) -> bytes | None:
    try:
        import edge_tts

        voice = VOICES.get(language, VOICES["en"])
        communicate = edge_tts.Communicate(text, voice)

        async def _collect() -> bytes:
            chunks: list[bytes] = []
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    chunks.append(chunk["data"])
            return b"".join(chunks)

        audio = await asyncio.wait_for(_collect(), timeout=60)
        if not audio:
            logger.warning("edge-tts returned no audio (lang=%s)", language)
            return None
        logger.info("edge-tts synthesized %d bytes (lang=%s, voice=%s)", len(audio), language, voice)
        return audio
    except ImportError:
        logger.warning("edge-tts not installed.")
        return None
    except Exception as exc:
        logger.warning("edge-tts failed: %s", exc)
        return None


async def synthesize(text: str, language: str = "en") -> bytes | None:
    """Convert a reply to speech. Returns audio bytes (MP3 from edge-tts, WAV
    from the DeepInfra fallback; Evolution API transcodes either to the
    WhatsApp voice-note format) or None if every provider failed."""
    text = prepare_for_speech(text)
    if not text:
        return None

    if TTS_PROVIDER in ("auto", "edge"):
        audio = await _edge_tts(text, language)
        if audio:
            return audio
        if TTS_PROVIDER == "edge":
            return None

    import deepinfra

    audio = await deepinfra.synthesize(text, language)
    if audio:
        logger.info("DeepInfra TTS synthesized %d bytes (lang=%s)", len(audio), language)
    return audio
