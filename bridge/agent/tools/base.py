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
    # Optional file to deliver back to the user alongside the text reply
    # (e.g. a generated RFP PDF, a BD analysis .pptx). Set all three or
    # none — main.py's reply step sends this via send_media after the
    # normal text reply, once per request (first tool result that sets it).
    file_bytes: bytes | None = None
    file_name: str | None = None
    file_mime: str | None = None

    def __str__(self) -> str:
        if self.success:
            return self.output
        return f"Tool error: {self.error}"


@dataclass
class ToolSession:
    """Per-request context a tool can use to act outside its own return
    value — e.g. sending a proactive follow-up WhatsApp message once a
    slow background job finishes (see agent/tools/_async_job.py). Passed
    to every tool's run() as the `session` kwarg; existing tools that
    don't need it simply ignore it via their existing **_ catch-all."""
    remote_jid: str


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
