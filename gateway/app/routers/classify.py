import asyncio
import io
import re
import zipfile

from fastapi import APIRouter, Depends, UploadFile

from app.cortex_client import CortexUpstreamError, HubSession
from app.deps import get_hub, get_jobs
from app.jobs import JobStore
from app.schemas import SubmitAccepted
from app.security import require_api_key

router = APIRouter(prefix="/api/v1", tags=["classify"], dependencies=[Depends(require_api_key)])

POLL_INTERVAL_SECONDS = 5
MAX_POLL_SECONDS = 10 * 60
JOB_ID_FROM_LOCATION = re.compile(r"/jobs/([^/?#]+)")


@router.post("/classify", status_code=202, response_model=SubmitAccepted)
async def submit_classify(
    file: UploadFile,
    jobs: JobStore = Depends(get_jobs),
    hub: HubSession = Depends(get_hub),
):
    """Cortex's classifier only has a bulk /classify-zip endpoint for actual
    documents (its plain /classify is for classifying rows of an uploaded
    Excel taxonomy sheet, a different feature entirely) — so a single
    WhatsApp-forwarded document is wrapped in a one-file zip here to match
    the upstream contract Cortex actually exposes. /classify-zip takes no
    category selection at all — it classifies against Cortex's own fixed
    internal taxonomy (services/classifier/taxonomy.py) — confirmed by
    reading that route's real signature, not assumed. An earlier version of
    this endpoint required a category_ids field that the upstream call
    silently ignored; removed."""
    content = await file.read()
    filename = file.filename or "upload"
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(filename, content)
    zip_bytes = zip_buf.getvalue()

    job = await jobs.create(kind="classify", kind_prefix="cls")
    asyncio.create_task(_run_classify(job.job_id, zip_bytes, jobs, hub))
    return SubmitAccepted(job_id=job.job_id, estimated_seconds=60, poll_url=f"/api/v1/jobs/{job.job_id}")


async def _run_classify(job_id: str, zip_bytes: bytes, jobs: JobStore, hub: HubSession) -> None:
    await jobs.mark_processing(job_id)
    try:
        resp = await hub.post_raw(
            "/classify/classify-zip",
            files={"file": ("upload.zip", zip_bytes, "application/zip")},
        )
        if resp.status_code not in (302, 303):
            raise CortexUpstreamError(
                resp.status_code,
                f"expected a redirect to the classify job, got {resp.status_code}: {resp.text[:300]}",
                "UNEXPECTED_UPSTREAM_RESPONSE",
            )
        location = resp.headers.get("location", "")
        m = JOB_ID_FROM_LOCATION.search(location)
        if not m:
            raise CortexUpstreamError(
                502, f"could not find a job id in redirect target: {location!r}", "UNEXPECTED_UPSTREAM_RESPONSE"
            )
        upstream_id = m.group(1)

        elapsed = 0
        while elapsed < MAX_POLL_SECONDS:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            elapsed += POLL_INTERVAL_SECONDS
            state = await hub.get_json(f"/classify/jobs/{upstream_id}/status")
            status = state.get("status")
            if status == "done" or state.get("has_output"):
                manifest = await hub.get_json(f"/classify/jobs/{upstream_id}/manifest.json")
                await jobs.mark_done(
                    job_id,
                    result={"manifest": manifest, "processed": state.get("processed"), "total": state.get("total")},
                    file_ref={
                        "upstream_path": f"/classify/jobs/{upstream_id}/download",
                        "filename": "classified.zip",
                        "content_type": "application/zip",
                    },
                )
                return
            if status == "error":
                await jobs.mark_error(job_id, state.get("message", "classification failed"), "CLASSIFY_FAILED")
                return
        await jobs.mark_error(job_id, "classification did not finish within the poll window", "TIMEOUT")
    except CortexUpstreamError as e:
        await jobs.mark_error(job_id, e.message, e.code, e.retry_after)
    except Exception as e:
        await jobs.mark_error(job_id, str(e), "GATEWAY_ERROR")
