"""Image-to-image tool: apply a requested change to a photo the user sent
and deliver the result back as a WhatsApp image."""
from __future__ import annotations

import logging
from pathlib import Path

import deepinfra
from agent.tools.base import Tool, ToolResult

logger = logging.getLogger("bridge.tool.edit_image")


class EditImageTool(Tool):
    name = "edit_image"
    display_name = "Edit Image (image-to-image)"
    description = (
        "Edit or transform a photo the user attached or sent earlier "
        "(change background, colours, style, add/remove objects, make it look "
        "like a painting, restyle, retouch, change time of day, and similar). "
        "The edited picture is sent back to the user as an image. "
        "Requires an image file marker from the conversation; if there is none, "
        "ask the user to send the photo first."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": "The path from the [Attached file: ... | path: ...] marker of the image to edit.",
            },
            "instruction": {
                "type": "string",
                "description": (
                    "What to change, written as a clear instruction in ENGLISH (translate "
                    "from Arabic if needed), e.g. 'Replace the background with a clean white "
                    "studio backdrop, keep the person unchanged'."
                ),
            },
        },
        "required": ["file_path", "instruction"],
    }

    async def run(self, file_path: str, instruction: str, session=None, **_) -> ToolResult:
        from whatsapp import MEDIA_DIR

        full = MEDIA_DIR / Path(file_path).name
        if not full.exists():
            return ToolResult(success=False, output="", error=f"Image not found: {file_path}")
        try:
            edited = await deepinfra.edit_image(full.read_bytes(), instruction)
        except deepinfra.DeepInfraError as exc:
            return ToolResult(success=False, output="", error=str(exc))
        except Exception as exc:
            logger.error("edit_image error: %s", exc)
            return ToolResult(success=False, output="", error="The image could not be processed.")
        return ToolResult(
            success=True,
            output="The edited image was created and will be sent to the user as a picture.",
            file_bytes=edited,
            file_name="edited.jpg",
            file_mime="image/jpeg",
        )
