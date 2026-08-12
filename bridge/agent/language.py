"""Language detection — Arabic vs English."""
from __future__ import annotations

import re

_ARABIC_RE = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")


def detect(text: str) -> str:
    """Return 'ar' if text is predominantly Arabic, else 'en'."""
    if not text or not text.strip():
        return "en"
    arabic = len(_ARABIC_RE.findall(text))
    ratio = arabic / max(len(text.replace(" ", "")), 1)
    return "ar" if ratio > 0.20 else "en"


LANG_INSTRUCTION = {
    "ar": (
        "تحدث باللغة العربية الفصحى في ردودك. "
        "إذا كانت الإجابة تحتوي على مصطلحات تقنية أو أرقام، يمكنك ذكرها بالإنجليزية."
    ),
    "en": (
        "Reply in English. Use clear, professional language."
    ),
}


def instruction(lang: str) -> str:
    return LANG_INSTRUCTION.get(lang, LANG_INSTRUCTION["en"])
