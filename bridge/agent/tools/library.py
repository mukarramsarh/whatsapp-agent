"""Knowledge base / library search tool — semantic vector search over stored documents."""
from __future__ import annotations

import logging

from agent.tools.base import Tool, ToolResult

logger = logging.getLogger("bridge.tool.library")


class LibraryTool(Tool):
    name = "search_knowledge_base"
    display_name = "Knowledge Base — Semantic Search"
    description = (
        "Search the internal knowledge base for relevant documents, policies, manuals, "
        "or historical data. Use when the user asks about company policies, procedures, "
        "product specs, or any information that may be stored in the knowledge base."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query — be specific and descriptive",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of results to return (default 5)",
                "default": 5,
            },
        },
        "required": ["query"],
    }

    async def run(self, query: str, limit: int = 5, **_) -> ToolResult:
        try:
            import embeddings

            results = await embeddings.search_knowledge_base(query, limit=limit)
            if not results:
                return ToolResult(success=True, output="No relevant documents found in the knowledge base.")

            lines = []
            for i, r in enumerate(results, 1):
                lines.append(f"[Result {i}] (score: {r['score']:.2f})\n{r['content']}")
            return ToolResult(success=True, output="\n\n---\n\n".join(lines))
        except Exception as exc:
            logger.error("Knowledge base search error: %s", exc)
            return ToolResult(success=False, error=str(exc))
