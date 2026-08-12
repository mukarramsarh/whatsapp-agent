"""OCR tool — wraps bridge/ocr.py as a ReAct-compatible tool."""
from __future__ import annotations

from pathlib import Path

from agent.tools.base import Tool, ToolResult


class OCRTool(Tool):
    name = "ocr_extract_text"
    display_name = "OCR — Extract Text from Image / PDF"
    description = (
        "Extract text from an image (JPEG, PNG, TIFF, BMP, WebP) or PDF file. "
        "Use when the user sends a document or image and asks what it says, "
        "or when you need to read content from an attached file."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "Relative file path as stored in the system, e.g. 'media/uuid.pdf'",
            }
        },
        "required": ["file_path"],
    }

    async def run(self, file_path: str, **_) -> ToolResult:
        try:
            import ocr as ocr_client
            from whatsapp import MEDIA_DIR

            full = MEDIA_DIR / Path(file_path).name
            if not full.exists():
                return ToolResult(success=False, error=f"File not found: {file_path}")

            file_bytes = full.read_bytes()
            text = await ocr_client.submit_and_wait(file_bytes, full.name)
            if text:
                return ToolResult(success=True, output=text)
            return ToolResult(success=False, error="No text could be extracted from the file.")
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))
