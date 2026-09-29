"""BD/RFP analysis tool — submits to Cortex's real BD analyzer via
cortex_gateway.py. Genuinely slow (the gateway budgets up to 20 minutes),
so this acknowledges immediately and does the actual poll + result delivery
in the background via _async_job.start_background_job, sending a separate
follow-up WhatsApp message when the analysis actually finishes."""
from __future__ import annotations

import logging
from pathlib import Path

from agent.tools._async_job import start_background_job
from agent.tools.base import Tool, ToolResult

logger = logging.getLogger("bridge.tool.analyze_rfp")

POLL_INTERVAL_SECONDS = 10
POLL_MAX_WAIT = 20 * 60  # matches the gateway's own budget

# services/bd-analyzer/app/config.py's real OFFER_TRACK_NAMES, with the
# Arabic labels its own UI uses.
TRACK_LABELS = {
    "technical": "المسار التقني",
    "consulting": "المسار الاستشاري",
    "secondment": "مسار الإعارة",
    "managed_services": "مسار الخدمات المدارة",
}


class AnalyzeRfpTool(Tool):
    name = "analyze_rfp"
    display_name = "Cortex BD / RFP Analysis"
    description = (
        "Run a full BD/RFP analysis on an attached document using Cortex's "
        "real analyzer. Genuinely slow (several minutes) — this tool "
        "acknowledges immediately and sends the actual results (a summary "
        "plus a .pptx) as a separate follow-up message once the analysis "
        "finishes; it does not block the conversation. Requires a file the "
        "user has attached (ask them to send it if missing) and track — "
        "one of technical/consulting/secondment/managed_services. If the "
        "user hasn't said which track, ask them to pick one by presenting "
        "the four real choices (with their Arabic labels if the "
        "conversation is in Arabic), don't guess or ask for free text."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "file_path": {
                "type": "string",
                "description": (
                    "The relative file path from the [Attached file: name | "
                    "type: mime | path: path] marker in the conversation."
                ),
            },
            "track": {
                "type": "string",
                "enum": list(TRACK_LABELS.keys()),
                "description": "Which analysis track — a real, required choice.",
            },
            "lang": {
                "type": "string",
                "enum": ["ar", "en", "both"],
                "description": "Output language for the analysis.",
                "default": "ar",
            },
        },
        "required": ["file_path", "track"],
    }

    async def run(
        self, file_path: str, track: str, lang: str = "ar", session=None, **_
    ) -> ToolResult:
        try:
            from whatsapp import MEDIA_DIR

            full = MEDIA_DIR / Path(file_path).name
            if not full.exists():
                return ToolResult(success=False, output="", error=f"File not found: {file_path}")
            if track not in TRACK_LABELS:
                return ToolResult(
                    success=False,
                    output="",
                    error=f"track must be one of {list(TRACK_LABELS.keys())}",
                )

            file_bytes = full.read_bytes()
            filename = full.name

            async def _poll_and_build():
                import cortex_gateway

                job_id = await cortex_gateway.submit_multipart(
                    "/api/v1/analyze",
                    file_bytes,
                    filename,
                    "application/octet-stream",
                    data={"track": track, "lang": lang},
                )
                if not job_id:
                    return "Sorry, I couldn't start the BD analysis — the analysis service didn't accept the request.", None, None, None

                result = await cortex_gateway.poll(job_id, interval=POLL_INTERVAL_SECONDS, max_wait=POLL_MAX_WAIT)
                if result is None:
                    return "The BD analysis didn't finish within 20 minutes — it may still be running; ask me to check back later.", None, None, None
                if result.get("status") == "error":
                    return f"BD analysis failed: {result.get('error', 'unknown error')}", None, None, None

                summary = _summarize(result)
                file_data = await cortex_gateway.get_file(f"/api/v1/files/{job_id}")
                if file_data:
                    file_bytes_out, file_mime = file_data
                    return summary, file_bytes_out, f"{Path(filename).stem}-analysis.pptx", file_mime
                return summary, None, None, None

            start_background_job(session, _poll_and_build)
            return ToolResult(
                success=True,
                output=(
                    f"Started the BD analysis on {filename} ({TRACK_LABELS[track]} / {track}). "
                    "This takes a few minutes — I'll message you here with a summary and "
                    "the full .pptx as soon as it's ready."
                ),
            )
        except Exception as exc:
            logger.error("BD analysis error: %s", exc)
            return ToolResult(success=False, output="", error=str(exc))


def _summarize(job_body: dict) -> str:
    """job_body's real shape (cortex-whatsapp-gateway's analyze.py):
    {"track", "filename", "analysis": <bd-analyzer's own raw result dict,
    no fixed schema — the gateway deliberately doesn't invent one>}. Report
    what's plausibly present without assuming exact keys; the .pptx is the
    authoritative detailed output either way."""
    analysis = job_body.get("analysis")
    track = job_body.get("track", "")
    header = f"BD analysis complete ({track})." if track else "BD analysis complete."
    if not isinstance(analysis, dict) or not analysis:
        return f"{header} See the attached .pptx for the full result."
    for key in ("summary", "overview", "executive_summary"):
        val = analysis.get(key)
        if isinstance(val, str) and val.strip():
            return f"{header}\n\n{val.strip()}\n\nSee the attached .pptx for the full analysis."
    return f"{header} See the attached .pptx for the full analysis."
