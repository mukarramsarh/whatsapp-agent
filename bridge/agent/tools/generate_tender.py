"""Tender/RFP generation tool — submits to Cortex's real tender generator
via cortex_gateway.py, supporting both of its two unrelated shapes:

- Simple mode (doc_type sow/rfp): one free-text `brief` field drives
  everything; the rest is cosmetic metadata.
- Government mode (doc_type gov_ksa): a nested {slots, answers, ai_slots}
  payload where `project_goal` plus a handful of `slots.*` fields matter;
  the LLM tool-call interface here stays FLAT (entity_name, dept_name,
  city, ...) and this tool reshapes them into Cortex's real nested
  payload itself -- flat arguments are far more reliable for prompt-mode
  tool-calling than asking the model to construct nested JSON.

Async, same as analyze_rfp: acknowledges immediately, generates + exports
to PDF in the background, and sends the file as a follow-up message.
"""
from __future__ import annotations

import logging
from pathlib import Path

from agent.tools._async_job import start_background_job
from agent.tools.base import Tool, ToolResult

logger = logging.getLogger("bridge.tool.generate_tender")

POLL_MAX_WAIT = 5 * 60


class GenerateTenderTool(Tool):
    name = "generate_tender"
    display_name = "Cortex Tender / RFP Generation"
    description = (
        "Generate a real tender/RFP/SOW document using Cortex's own "
        "generator. Two unrelated modes -- establish which one the user "
        "wants BEFORE collecting fields for either, don't mix questions "
        "from both:\n"
        "1) Simple mode (doc_type sow or rfp): needs only 'brief' -- a "
        "free-text description of the project/scope; this is the one field "
        "that actually matters. title/company_name are optional extras.\n"
        "2) Government template mode (doc_type gov_ksa, the official Saudi "
        "government format): needs 'project_goal' (free text) plus "
        "entity_name, dept_name, competition_name, and city. Everything "
        "else in this mode is optional with sensible defaults.\n"
        "If a required field for the chosen mode is missing, ask the user "
        "for it specifically rather than guessing or calling with an empty "
        "value. Once generated, the document is sent back as a real PDF "
        "file, not as chat text -- acknowledge that generation has started "
        "and that the file will follow."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "doc_type": {
                "type": "string",
                "enum": ["sow", "rfp", "gov_ksa"],
                "description": "Which mode — establish this first.",
            },
            "brief": {
                "type": "string",
                "description": "Required for sow/rfp — free-text project/scope description.",
            },
            "title": {"type": "string", "description": "Optional, sow/rfp — document title."},
            "company_name": {"type": "string", "description": "Optional, sow/rfp — requesting entity name."},
            "submission_deadline": {"type": "string", "description": "Optional, rfp only."},
            "project_goal": {
                "type": "string",
                "description": "Required for gov_ksa — free-text project goal/scope.",
            },
            "entity_name": {"type": "string", "description": "Required for gov_ksa — government entity name."},
            "dept_name": {"type": "string", "description": "Required for gov_ksa — requesting department."},
            "competition_name": {"type": "string", "description": "Required for gov_ksa — competition/tender name."},
            "city": {"type": "string", "description": "Required for gov_ksa."},
            "duration_months": {"type": "integer", "description": "Optional, gov_ksa."},
            "doc_value_num": {"type": "string", "description": "Optional, gov_ksa — contract value."},
            "template_id": {"type": "string", "description": "Optional, gov_ksa — defaults to Cortex's own default."},
        },
        "required": ["doc_type"],
    }

    async def run(
        self,
        doc_type: str,
        brief: str | None = None,
        title: str | None = None,
        company_name: str | None = None,
        submission_deadline: str | None = None,
        project_goal: str | None = None,
        entity_name: str | None = None,
        dept_name: str | None = None,
        competition_name: str | None = None,
        city: str | None = None,
        duration_months: int | None = None,
        doc_value_num: str | None = None,
        template_id: str | None = None,
        session=None,
        **_,
    ) -> ToolResult:
        if doc_type not in ("sow", "rfp", "gov_ksa"):
            return ToolResult(success=False, output="", error="doc_type must be one of sow, rfp, gov_ksa")

        if doc_type in ("sow", "rfp"):
            if not brief:
                return ToolResult(
                    success=False,
                    output="",
                    error="brief is required — ask the user for a short description of the project/scope.",
                )
            payload: dict = {"doc_type": doc_type, "brief": brief}
            if title:
                payload["title"] = title
            if company_name:
                payload["company_name"] = company_name
            if doc_type == "rfp" and submission_deadline:
                payload["submission_deadline"] = submission_deadline
        else:
            missing = [
                name
                for name, val in (
                    ("project_goal", project_goal),
                    ("entity_name", entity_name),
                    ("dept_name", dept_name),
                    ("competition_name", competition_name),
                    ("city", city),
                )
                if not val
            ]
            if missing:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"Missing required government-template fields: {', '.join(missing)}",
                )
            slots: dict = {
                "entity_name": entity_name,
                "dept_name": dept_name,
                "competition_name": competition_name,
                "city": city,
            }
            if duration_months:
                slots["duration_months"] = duration_months
            if doc_value_num:
                slots["doc_value_num"] = doc_value_num
            payload = {"doc_type": "gov_ksa", "project_goal": project_goal, "slots": slots}
            if template_id:
                payload["template_id"] = template_id

        try:
            async def _poll_and_build():
                import cortex_gateway

                job_id = await cortex_gateway.submit_json("/api/v1/tender/generate", {"answers": payload})
                if not job_id:
                    return "Sorry, I couldn't start generating the document.", None, None, None

                result = await cortex_gateway.poll(job_id, interval=5, max_wait=POLL_MAX_WAIT)
                if result is None:
                    return "Document generation timed out.", None, None, None
                if result.get("status") == "error":
                    return f"Document generation failed: {result.get('error', 'unknown error')}", None, None, None

                fallback_reason = result.get("fallback_reason")
                note = f" (note: {fallback_reason})" if fallback_reason and not result.get("used_llm") else ""

                file_data = await cortex_gateway.get_file(f"/api/v1/tender/{job_id}/export/pdf")
                if file_data:
                    file_bytes, file_mime = file_data
                    return f"Your {doc_type} document is ready{note}.", file_bytes, f"{doc_type}-{job_id}.pdf", file_mime
                return f"Document generated{note}, but I couldn't export it as a PDF.", None, None, None

            start_background_job(session, _poll_and_build)
            return ToolResult(
                success=True,
                output=(
                    f"Generating your {doc_type} document now — this takes about a minute. "
                    "I'll send you the finished PDF here shortly."
                ),
            )
        except Exception as exc:
            logger.error("Tender generation error: %s", exc)
            return ToolResult(success=False, output="", error=str(exc))
