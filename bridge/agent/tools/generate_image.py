"""Text-to-image tool: create a new picture from a description."""
from __future__ import annotations

import logging

import deepinfra
from agent.tools.base import Tool, ToolResult

logger = logging.getLogger("bridge.tool.generate_image")


class GenerateImageTool(Tool):
    name = "generate_image"
    display_name = "Generate Image (text-to-image)"
    description = (
        "Create a brand-new image from a text description (illustration, logo "
        "concept, poster, product mock-up, scene). The picture is sent to the "
        "user as an image. Use edit_image instead when the user wants to change "
        "a photo they already sent."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "A detailed visual description in ENGLISH (translate from Arabic if needed).",
            },
            "orientation": {
                "type": "string",
                "enum": ["square", "portrait", "landscape"],
                "default": "square",
            },
        },
        "required": ["prompt"],
    }

    _SIZES = {"square": "1024x1024", "portrait": "768x1024", "landscape": "1024x768"}

    async def run(self, prompt: str, orientation: str = "square", session=None, **_) -> ToolResult:
        size = self._SIZES.get(orientation, "1024x1024")
        try:
            img = await deepinfra.generate_image(prompt, size)
        except deepinfra.DeepInfraError as exc:
            return ToolResult(success=False, output="", error=str(exc))
        except Exception as exc:
            logger.error("generate_image error: %s", exc)
            return ToolResult(success=False, output="", error="The image could not be generated.")
        return ToolResult(
            success=True,
            output="The image was created and will be sent to the user as a picture.",
            file_bytes=img,
            file_name="generated.jpg",
            file_mime="image/jpeg",
        )
