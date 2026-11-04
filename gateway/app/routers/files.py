from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse

from app.cortex_client import CortexUpstreamError, HubSession
from app.deps import get_hub, get_jobs
from app.jobs import JobStore
from app.security import require_api_key

router = APIRouter(prefix="/api/v1", tags=["files"], dependencies=[Depends(require_api_key)])


@router.get("/files/{file_id}")
async def download_file(
    file_id: str,
    jobs: JobStore = Depends(get_jobs),
    hub: HubSession = Depends(get_hub),
):
    """Streams a downloadable artifact a completed job produced.

    file_id is the same job_id GET /api/v1/jobs/{job_id} was polled with —
    the job's file_ref (set by whichever router completed it) describes the
    real upstream path in Cortex to relay the bytes from. Kept as a live
    relay rather than caching the file locally, since Cortex's own hub
    already enforces the session/pathway auth on that download.
    """
    job = await jobs.get(file_id)
    if job is None or job.status != "done" or not job.file_ref:
        raise HTTPException(
            status_code=404,
            detail={"error": f"no downloadable file for {file_id}", "code": "FILE_NOT_FOUND"},
        )

    ref = job.file_ref
    try:
        upstream = await hub.get_raw(ref["upstream_path"])
    except CortexUpstreamError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail={"error": e.message, "code": e.code},
        ) from e

    content_type = ref.get("content_type") or upstream.headers.get("content-type", "application/octet-stream")
    filename = ref.get("filename", file_id)
    return StreamingResponse(
        iter([upstream.content]),
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
