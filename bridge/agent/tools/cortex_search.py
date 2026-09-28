"""Cortex e-library search tool — wraps cortex_gateway.py as a ReAct-compatible
tool, calling the real Cortex knowledge base via the separate
cortex-whatsapp-gateway service. Distinct from search_knowledge_base
(library.py), which only searches this bridge's own local Postgres table."""
from __future__ import annotations

import logging

from agent.tools.base import Tool, ToolResult

logger = logging.getLogger("bridge.tool.cortex_search")


class CortexSearchTool(Tool):
    name = "search_cortex_elibrary"
    display_name = "Cortex E-Library Search"
    description = (
        "Search Cortex's real e-Library knowledge base (technical / consulting / "
        "contracts & tenders documents) and get a synthesized answer with cited "
        "sources. Use this for substantive questions about methodology, past "
        "proposals, contracts, or tender documents — this is the authoritative "
        "knowledge base, not the local one."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "The user's question, in its original language",
            },
            "pathway": {
                "type": "string",
                "enum": ["technical", "consulting", "contracts_tenders"],
                "description": "Which e-Library pathway to search. Default technical.",
                "default": "technical",
            },
        },
        "required": ["query"],
    }

    async def run(self, query: str, pathway: str = "technical", **_) -> ToolResult:
        try:
            import cortex_gateway

            result = await cortex_gateway.search_and_wait(query, pathway=pathway)
            if result is None:
                # Covers both a submit failure (bad gateway auth, gateway down,
                # network) and a poll timeout — see the bridge.cortex_gateway
                # log line right above this tool's error for the real reason.
                return ToolResult(
                    success=False, output="", error="Cortex e-library search failed or timed out."
                )
            if result.get("status") == "error":
                return ToolResult(success=False, output="", error=result.get("error", "search failed"))

            answer = result.get("answer", "").strip()
            sources = result.get("sources", [])
            if not answer:
                return ToolResult(success=True, output="No answer found in the Cortex e-library for this query.")

            output = answer
            if sources:
                output += "\n\nSources: " + ", ".join(sources)
            return ToolResult(success=True, output=output)
        except Exception as exc:
            logger.error("Cortex e-library search error: %s", exc)
            return ToolResult(success=False, output="", error=str(exc))
