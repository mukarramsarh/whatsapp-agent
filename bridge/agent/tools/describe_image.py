"""Image understanding tool: answer questions about / describe a photo."""
from __future__ import annotations

import logging
from pathlib import Path

import deepinfra
from agent.language import detect
from agent.tools.base import Tool, ToolResult

logger = logging.getLogger("bridge.tool.describe_image")


class DescribeImageTool(Tool):
    name = "describe_image"
    display_name = "Describe / Analyse Image"
    description = (
        "Look at a photo or screenshot the user sent and describe it or answer "
        "a question about it (what is in it, read signs or labels, identify "
        "objects, check a chart or diagram). For extracting the full text of a "
        "scanned document use cortex_ocr_extract_text instead."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "The path from the [Attached file: ... | path: ...] marker of the image.",
            },
            "question": {
                "type": "string",
                "description": "What to look for or answer; 'Describe this image in detail.' if the user gave none.",
                "default": "Describe this image in detail.",
            },
        },
        "required": ["file_path"],
    }

    async def run(self, file_path: str, question: str = "Describe this image in detail.", session=None, **_) -> ToolResult:
        from whatsapp import MEDIA_DIR

        full = MEDIA_DIR / Path(file_path).name
        if not full.exists():
            return ToolResult(success=False, output="", error=f"Image not found: {file_path}")
        try:
            lang = getattr(session, "language", None) or detect(question)
            text = await deepinfra.describe_image(full.read_bytes(), question, lang)
        except deepinfra.DeepInfraError as exc:
            return ToolResult(success=False, output="", error=str(exc))
        except Exception as exc:
            logger.error("describe_image error: %s", exc)
            return ToolResult(success=False, output="", error="The image could not be analysed.")
        return ToolResult(success=True, output=text or "The image could not be described.")
