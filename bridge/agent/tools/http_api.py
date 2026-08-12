"""HTTP API tool — call configured external REST APIs."""
from __future__ import annotations

import json
import logging

import httpx

from agent.tools.base import Tool, ToolResult

logger = logging.getLogger("bridge.tool.http_api")


class HTTPAPITool(Tool):
    name = "call_external_api"
    display_name = "HTTP API — External REST Call"
    description = (
        "Make HTTP requests to configured external APIs (ERP, inventory, CRM, etc.). "
        "Use when the user needs live data from external business systems."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "api_name": {
                "type": "string",
                "description": "Name of the configured API (see tool settings for available APIs)",
            },
            "endpoint": {
                "type": "string",
                "description": "API endpoint path (e.g. '/api/v1/products')",
            },
            "method": {
                "type": "string",
                "enum": ["GET", "POST", "PUT", "PATCH", "DELETE"],
                "default": "GET",
                "description": "HTTP method",
            },
            "params": {
                "type": "object",
                "description": "Query parameters for GET, or request body for POST/PUT",
            },
        },
        "required": ["api_name", "endpoint"],
    }

    async def run(
        self,
        api_name: str,
        endpoint: str,
        method: str = "GET",
        params: dict | None = None,
        **_,
    ) -> ToolResult:
        apis: dict = self.config.get("apis", {})
        if not apis:
            return ToolResult(success=False, error="No external APIs configured.")
        if api_name not in apis:
            return ToolResult(success=False, error=f"API '{api_name}' not configured. Available: {', '.join(apis)}")

        api_cfg = apis[api_name]
        base_url = api_cfg.get("base_url", "").rstrip("/")
        headers = api_cfg.get("headers", {})

        url = base_url + "/" + endpoint.lstrip("/")
        params = params or {}

        try:
            async with httpx.AsyncClient(timeout=30) as client:
                if method == "GET":
                    r = await client.get(url, headers=headers, params=params)
                else:
                    r = await client.request(method, url, headers=headers, json=params)

                try:
                    body = r.json()
                    output = json.dumps(body, ensure_ascii=False, indent=2)
                except Exception:
                    output = r.text[:2000]

                return ToolResult(
                    success=r.is_success,
                    output=output,
                    error="" if r.is_success else f"HTTP {r.status_code}",
                )
        except Exception as exc:
            logger.error("HTTP API error (%s %s): %s", method, url, exc)
            return ToolResult(success=False, error=str(exc))
