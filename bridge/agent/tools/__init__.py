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
from agent.tools.cortex_search import CortexSearchTool
from agent.tools.classify import ClassifyDocumentTool
from agent.tools.cortex_ocr import CortexOcrTool
from agent.tools.analyze_rfp import AnalyzeRfpTool
from agent.tools.generate_tender import GenerateTenderTool
from agent.tools.edit_image import EditImageTool
from agent.tools.generate_image import GenerateImageTool
from agent.tools.describe_image import DescribeImageTool

# Ordered list of all tool classes
ALL_TOOL_CLASSES: list[type[Tool]] = [
    OCRTool,
    DatabaseTool,
    SSHTool,
    HTTPAPITool,
    LibraryTool,
    CortexSearchTool,
    ClassifyDocumentTool,
    CortexOcrTool,
    AnalyzeRfpTool,
    GenerateTenderTool,
    EditImageTool,
    GenerateImageTool,
    DescribeImageTool,
]

__all__ = ["Tool", "ToolResult", "ALL_TOOL_CLASSES"]
