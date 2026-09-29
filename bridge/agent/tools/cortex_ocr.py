"""Cortex OCR tool — wraps Cortex's own OCR service via cortex_gateway.py.
Distinct from ocr_extract_text (agent/tools/ocr.py), which fronts a
different, separate OCR service (the PH OCR API)."""
from __future__ import annotations

import logging
from pathlib import Path

from agent.tools.base import Tool, ToolResult

logger = logging.getLogger("bridge.tool.cortex_ocr")

POLL_MAX_WAIT = 60


class CortexOcrTool(Tool):
    name = "cortex_ocr_extract_text"
    display_name = "Cortex OCR — Extract Text"
    description = (
        "Extract text from an attached image or PDF using Cortex's own OCR "
        "service. There is also a separate ocr_extract_text tool for a "
        "different OCR service — use whichever one the user's context "
        "suggests, or this one by default for Cortex-related documents. "
        "Requires a file the user has attached; if none is attached yet, "
        "ask the user to send it first instead of calling this tool."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": (
                    "The relative file path from the [Attached file: name | "
                    "type: mime | path: path] marker in the conversation."
                ),
            }
        },
        "required": ["file_path"],
    }

    async def run(self, file_path: str, session=None, **_) -> ToolResult:
        try:
            from whatsapp import MEDIA_DIR
            import cortex_gateway

            full = MEDIA_DIR / Path(file_path).name
            if not full.exists():
                return ToolResult(success=False, output="", error=f"File not found: {file_path}")

            file_bytes = full.read_bytes()
            job_id = await cortex_gateway.submit_multipart(
                "/api/v1/ocr", file_bytes, full.name, "application/octet-stream"
            )
            if not job_id:
                return ToolResult(success=False, output="", error="Could not submit the file for OCR.")

            result = await cortex_gateway.poll(job_id, interval=3, max_wait=POLL_MAX_WAIT)
            if result is None:
                return ToolResult(success=False, output="", error="OCR timed out.")
            if result.get("status") == "error":
                return ToolResult(success=False, output="", error=result.get("error", "OCR failed"))

            text = (result.get("text") or "").strip()
            if not text:
                return ToolResult(success=True, output="No text could be extracted from the file.")
            return ToolResult(success=True, output=text)
        except Exception as exc:
            logger.error("Cortex OCR error: %s", exc)
            return ToolResult(success=False, output="", error=str(exc))
