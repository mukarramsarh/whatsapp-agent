"""
Tool registry — all available tools are listed here.
Each class is instantiated with its DB config when the agent runs.
"""
from agent.tools.base import Tool, ToolResult
from agent.tools.ocr import OCRTool
from agent.tools.database import DatabaseTool
from agent.tools.ssh import SSHTool
from agent.tools.http_api import HTTPAPITool
from agent.tools.library import LibraryTool

# Ordered list of all tool classes
ALL_TOOL_CLASSES: list[type[Tool]] = [
    OCRTool,
    DatabaseTool,
    SSHTool,
    HTTPAPITool,
    LibraryTool,
]

__all__ = ["Tool", "ToolResult", "ALL_TOOL_CLASSES"]
