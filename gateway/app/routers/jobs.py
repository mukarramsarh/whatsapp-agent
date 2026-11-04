from fastapi import APIRouter, Depends, HTTPException

from app.deps import get_jobs
from app.jobs import JobStore
from app.security import require_api_key

router = APIRouter(prefix="/api/v1", tags=["jobs"], dependencies=[Depends(require_api_key)])


@router.get("/jobs/{job_id}")
async def get_job_status(job_id: str, jobs: JobStore = Depends(get_jobs)):
    job = await jobs.get(job_id)
    if job is None:
        raise HTTPException(
            status_code=404,
            detail={"error": f"no such job: {job_id}", "code": "JOB_NOT_FOUND"},
        )

    base = {"job_id": job.job_id, "status": job.status}
    if job.status == "done":
        base.update(job.result or {})
    elif job.status == "error":
        base["error"] = job.error
        base["code"] = job.error_code
        if job.retry_after:
            base["retry_after"] = job.retry_after
    return base
