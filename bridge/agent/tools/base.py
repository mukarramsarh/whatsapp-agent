"""
Tool base class. All tools inherit from this.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ToolResult:
    success: bool
    output: str
    error: str = ""

    def __str__(self) -> str:
        if self.success:
            return self.output
        return f"Tool error: {self.error}"


class Tool(ABC):
    name: str = ""
    display_name: str = ""
    description: str = ""
    parameters_schema: dict = {}

    def __init__(self, config: dict | None = None):
        self.config: dict = config or {}

    @abstractmethod
    async def run(self, **kwargs) -> ToolResult:
        ...

    def to_openai_function(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }
