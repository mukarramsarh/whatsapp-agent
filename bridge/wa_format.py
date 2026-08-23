"""
Convert LLM Markdown output into WhatsApp-safe text.

WhatsApp supports a small formatting subset — *bold*, _italic_, ~strike~,
```mono``` — and cannot render Markdown tables, headings, HTML, or links.
LLMs trained on Markdown emit **bold**, tables, `#` headings and <br>, which
look broken in a chat. `to_whatsapp()` rewrites all of that; `to_plain()`
strips every marker for text-to-speech.
"""
from __future__ import annotations

import re

# Sentinels used mid-pipeline so later passes don't touch already-handled spans.
_BR = "\x02"       # placeholder for <br> until the very end
_BOLD = "\x01"     # placeholder for converted bold spans


def _is_separator(line: str) -> bool:
    """True for a Markdown table separator row like |---|:--:|."""
    if "|" not in line or "-" not in line:
        return False
    stripped = re.sub(r"[|:\-\s]", "", line)
    return stripped == ""


def _split_row(line: str) -> list[str]:
    line = line.strip()
    if line.startswith("|"):
        line = line[1:]
    if line.endswith("|"):
        line = line[:-1]
    return [c.strip() for c in line.split("|")]


def _render_table(header: list[str], rows: list[list[str]]) -> str:
    """Render a Markdown table as WhatsApp bullet blocks.

    First column becomes a bold bullet header; remaining columns become
    "Label: value" lines underneath it.
    """
    blocks: list[str] = []
    for row in rows:
        if not any(cell.strip() for cell in row):
            continue
        first = row[0].strip() if row else ""
        lines = [f"• *{first}*"] if first else ["•"]
        for label, cell in zip(header[1:], row[1:]):
            cell = cell.strip()
            if cell:
                lines.append(f"  {label.strip()}: {cell}" if label.strip() else f"  {cell}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)


def _convert_tables(text: str) -> str:
    lines = text.split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        if (
            "|" in lines[i]
            and i + 1 < n
            and _is_separator(lines[i + 1])
        ):
            header = _split_row(lines[i])
            i += 2  # skip header + separator
            rows: list[list[str]] = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(_split_row(lines[i]))
                i += 1
            out.append(_render_table(header, rows))
            continue
        out.append(lines[i])
        i += 1
    return "\n".join(out)


def to_whatsapp(text: str) -> str:
    """Rewrite Markdown into WhatsApp-safe formatting."""
    if not text:
        return text

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Protect <br> from the table pass (it would split rows), restore at the end.
    text = re.sub(r"<br\s*/?>", _BR, text, flags=re.I)

    # Tables -> bullet blocks (before inline passes so cells stay intact).
    text = _convert_tables(text)

    # Bold: **x** / __x__ -> sentinel, so the bullet pass can't confuse it.
    text = re.sub(r"\*\*(.+?)\*\*", _BOLD + r"\1" + _BOLD, text, flags=re.S)
    text = re.sub(r"__(.+?)__", _BOLD + r"\1" + _BOLD, text, flags=re.S)

    # Headings (#, ##, …) -> bold line.
    text = re.sub(r"^\s{0,3}#{1,6}\s*(.+?)\s*#*$", _BOLD + r"\1" + _BOLD, text, flags=re.M)

    # Bullets: -, +, * at line start (must be followed by a space) -> •.
    text = re.sub(r"^(\s*)[-*+]\s+", r"\1• ", text, flags=re.M)

    # Inline `code` -> plain (WhatsApp has no inline mono); leave ``` blocks.
    text = re.sub(r"(?<!`)`([^`\n]+)`(?!`)", r"\1", text)

    # Links [text](url) -> text (url).
    text = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r"\1 (\2)", text)

    # Restore sentinels.
    text = text.replace(_BOLD, "*").replace(_BR, "\n")

    # Collapse 3+ blank lines.
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def to_plain(text: str) -> str:
    """Strip all formatting markers — used for text-to-speech."""
    if not text:
        return text
    text = to_whatsapp(text)
    text = re.sub(r"```.*?```", "", text, flags=re.S)  # drop code blocks
    text = re.sub(r"[*_~`]", "", text)
    text = text.replace("•", "-")
    return text.strip()
