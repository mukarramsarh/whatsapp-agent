import asyncio

from fastapi import APIRouter, Depends, UploadFile

from app.cortex_client import CortexUpstreamError, HubSession
from app.deps import get_hub, get_jobs
from app.jobs import JobStore
from app.schemas import SubmitAccepted
from app.security import require_api_key

router = APIRouter(prefix="/api/v1", tags=["ocr"], dependencies=[Depends(require_api_key)])

# services/ocr/app/digitize.py's on-disk status.json: queued -> processing ->
# ready (success) | error. Note the success state is "ready", not "done".
POLL_INTERVAL_SECONDS = 3
MAX_POLL_SECONDS = 5 * 60


@router.post("/ocr", status_code=202, response_model=SubmitAccepted)
async def submit_ocr(
    file: UploadFile,
    jobs: JobStore = Depends(get_jobs),
    hub: HubSession = Depends(get_hub),
):
    content = await file.read()
    job = await jobs.create(kind="ocr", kind_prefix="ocr")
    asyncio.create_task(_run_ocr(job.job_id, content, file.filename or "upload", file.content_type, jobs, hub))
    return SubmitAccepted(job_id=job.job_id, estimated_seconds=30, poll_url=f"/api/v1/jobs/{job.job_id}")


async def _run_ocr(
    job_id: str, content: bytes, filename: str, content_type: str | None, jobs: JobStore, hub: HubSession
) -> None:
    await jobs.mark_processing(job_id)
    try:
        submitted = await hub.post_multipart(
            "/ocrtool/digitize/jobs",
            files={"file": (filename, content, content_type or "application/octet-stream")},
        )
        upstream_id = submitted["job_id"]

        elapsed = 0
        while elapsed < MAX_POLL_SECONDS:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            elapsed += POLL_INTERVAL_SECONDS
            state = await hub.get_json(f"/ocrtool/digitize/jobs/{upstream_id}/status")
            st = state.get("state")
            if st == "ready":
                text = await hub.get_json(f"/ocrtool/digitize/jobs/{upstream_id}/text")
                await jobs.mark_done(
                    job_id,
                    result={"text": text.get("text", ""), "filename": filename},
                    file_ref={
                        "upstream_path": f"/ocrtool/digitize/jobs/{upstream_id}/download",
                        "filename": f"{upstream_id}.pdf",
                        "content_type": "application/pdf",
                    },
                )
                return
            if st == "error":
                await jobs.mark_error(job_id, state.get("error", "OCR failed"), "OCR_FAILED")
                return
        await jobs.mark_error(job_id, "OCR did not finish within the poll window", "TIMEOUT")
    except CortexUpstreamError as e:
        await jobs.mark_error(job_id, e.message, e.code, e.retry_after)
    except Exception as e:
        await jobs.mark_error(job_id, str(e), "GATEWAY_ERROR")
