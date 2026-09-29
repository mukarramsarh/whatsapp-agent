"""Document classification tool — wraps Cortex's real classifier (against
its own fixed internal taxonomy) via cortex_gateway.py."""
from __future__ import annotations

import logging
from pathlib import Path

from agent.tools.base import Tool, ToolResult

logger = logging.getLogger("bridge.tool.classify")

POLL_MAX_WAIT = 90


class ClassifyDocumentTool(Tool):
    name = "classify_document"
    display_name = "Cortex Document Classification"
    description = (
        "Classify an attached procurement/tender document (RFI, RFQ, RFP, "
        "ITB, contract agreement, SOW, purchase order, and similar types) "
        "using Cortex's real classifier against its own fixed taxonomy — no "
        "category choice needed from the user, it's fully automatic. "
        "Requires a file the user has attached in this conversation; if none "
        "is attached yet, ask the user to send the document first instead of "
        "calling this tool."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": (
                    "The relative file path from the [Attached file: name | "
                    "type: mime | path: path] marker in the conversation — "
                    "not a path you invent."
                ),
            }
        },
        "required": ["file_path"],
    }

    async def run(self, file_path: str, session=None, **_) -> ToolResult:
        try:
            from whatsapp import MEDIA_DIR
            import cortex_gateway

            full = MEDIA_DIR / Path(file_path).name
            if not full.exists():
                return ToolResult(success=False, output="", error=f"File not found: {file_path}")

            file_bytes = full.read_bytes()
            job_id = await cortex_gateway.submit_multipart(
                "/api/v1/classify", file_bytes, full.name, "application/octet-stream"
            )
            if not job_id:
                return ToolResult(
                    success=False, output="", error="Could not submit the document for classification."
                )

            result = await cortex_gateway.poll(job_id, interval=3, max_wait=POLL_MAX_WAIT)
            if result is None:
                return ToolResult(success=False, output="", error="Classification timed out.")
            if result.get("status") == "error":
                return ToolResult(success=False, output="", error=result.get("error", "classification failed"))

            return ToolResult(success=True, output=_summarize(result.get("manifest") or {}))
        except Exception as exc:
            logger.error("Classification error: %s", exc)
            return ToolResult(success=False, output="", error=str(exc))


def _summarize(manifest: dict) -> str:
    """manifest's real shape (services/classifier/app.py's /manifest.json
    route, confirmed by reading it, not assumed): {"ok": true, "files": [
    {orig_name, new_name, entity, file_type, file_type_short, project,
    status, error}, ...]}."""
    files = manifest.get("files") or []
    if not files:
        return "Classification completed, but no files were reported in the result."
    lines = []
    for f in files:
        name = f.get("orig_name") or f.get("new_name") or "file"
        if f.get("status") == "error":
            lines.append(f"- {name}: failed ({f.get('error') or 'unknown error'})")
            continue
        label = f.get("file_type") or f.get("file_type_short") or "unclassified"
        extra = ", ".join(x for x in (f.get("entity"), f.get("project")) if x)
        lines.append(f"- {name}: {label}" + (f" ({extra})" if extra else ""))
    return "\n".join(lines)
