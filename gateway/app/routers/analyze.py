import asyncio

from fastapi import APIRouter, Depends, Form, HTTPException, UploadFile

from app.cortex_client import CortexUpstreamError, HubSession
from app.deps import get_hub, get_jobs
from app.jobs import JobStore
from app.schemas import SubmitAccepted
from app.security import require_api_key

router = APIRouter(prefix="/api/v1", tags=["analyze"], dependencies=[Depends(require_api_key)])

# bd-analyzer's own JOBS dict uses exactly these strings (services/bd-analyzer/
# app/main.py) — "queued"/"running"/"cancelling" are all still in-flight,
# "done"/"error"/"cancelled" are terminal.
TERMINAL_DONE = {"done"}
TERMINAL_ERROR = {"error", "cancelled"}
POLL_INTERVAL_SECONDS = 5
MAX_POLL_SECONDS = 20 * 60  # BD analysis is real, LLM-driven document work — minutes, not seconds

# bd-analyzer's own real, server-validated enum (services/bd-analyzer/app/
# config.py's OFFER_TRACK_NAMES) — validated here too so a bad value fails
# fast with a clear message instead of 400ing deep inside a 20-minute job.
VALID_TRACKS = ("technical", "consulting", "secondment", "managed_services")


@router.post("/analyze", status_code=202, response_model=SubmitAccepted)
async def submit_analyze(
    file: UploadFile,
    track: str = Form(...),
    lang: str = Form("ar"),
    jobs: JobStore = Depends(get_jobs),
    hub: HubSession = Depends(get_hub),
):
    if track not in VALID_TRACKS:
        raise HTTPException(
            status_code=400,
            detail={"error": f"track must be one of {VALID_TRACKS}", "code": "INVALID_TRACK"},
        )
    content = await file.read()
    job = await jobs.create(kind="analyze", kind_prefix="bd")
    asyncio.create_task(
        _run_analyze(job.job_id, content, file.filename or "upload", file.content_type, track, lang, jobs, hub)
    )
    return SubmitAccepted(job_id=job.job_id, estimated_seconds=300, poll_url=f"/api/v1/jobs/{job.job_id}")


async def _run_analyze(
    job_id: str,
    content: bytes,
    filename: str,
    content_type: str | None,
    track: str,
    lang: str,
    jobs: JobStore,
    hub: HubSession,
) -> None:
    await jobs.mark_processing(job_id)
    try:
        upload = await hub.post_multipart(
            "/bd/api/upload",
            files={"file": (filename, content, content_type or "application/octet-stream")},
            data={"track": track, "lang": lang},
        )
        upstream_id = upload["job_id"]
        await hub.post_json(f"/bd/api/run/{upstream_id}", json={})

        elapsed = 0
        while elapsed < MAX_POLL_SECONDS:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            elapsed += POLL_INTERVAL_SECONDS
            state = await hub.get_json(f"/bd/api/result/{upstream_id}")
            status = state.get("status")
            if status in TERMINAL_DONE:
                await jobs.mark_done(
                    job_id,
                    result={"track": track, "filename": filename, "analysis": state.get("result")},
                    file_ref={
                        "upstream_path": f"/bd/api/result/{upstream_id}/pptx",
                        "filename": f"{upstream_id}.pptx",
                        "content_type": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                    },
                )
                return
            if status in TERMINAL_ERROR:
                result = state.get("result") or {}
                await jobs.mark_error(
                    job_id,
                    result.get("error", f"BD analysis ended with status={status}"),
                    "ANALYSIS_FAILED",
                )
                return
        await jobs.mark_error(job_id, "BD analysis did not finish within the poll window", "TIMEOUT")
    except CortexUpstreamError as e:
        await jobs.mark_error(job_id, e.message, e.code, e.retry_after)
    except Exception as e:
        await jobs.mark_error(job_id, str(e), "GATEWAY_ERROR")
