import asyncio

from fastapi import APIRouter, Depends, HTTPException, Path
from fastapi.responses import StreamingResponse

from app.cortex_client import CortexUpstreamError, HubSession
from app.deps import get_hub, get_jobs
from app.jobs import Job, JobStore
from app.schemas import SubmitAccepted, TenderGenerateRequest
from app.security import require_api_key

router = APIRouter(prefix="/api/v1/tender", tags=["tender"], dependencies=[Depends(require_api_key)])


@router.post("/generate", status_code=202, response_model=SubmitAccepted)
async def submit_tender(
    body: TenderGenerateRequest,
    jobs: JobStore = Depends(get_jobs),
    hub: HubSession = Depends(get_hub),
):
    """Cortex's tender /generate is synchronous upstream (a real LLM call,
    tens of seconds) — wrapped in a job here so every capability the bridge
    talks to is uniformly submit-then-poll, matching e-lib-integration.md's
    own pattern rather than making the bridge special-case this one."""
    job = await jobs.create(kind="tender", kind_prefix="tnd")
    asyncio.create_task(_run_tender(job.job_id, body.answers, jobs, hub))
    return SubmitAccepted(job_id=job.job_id, estimated_seconds=45, poll_url=f"/api/v1/jobs/{job.job_id}")


async def _run_tender(job_id: str, answers: dict, jobs: JobStore, hub: HubSession) -> None:
    await jobs.mark_processing(job_id)
    try:
        upstream = await hub.post_json("/rfp/generate", json=answers)
        if not upstream.get("ok"):
            await jobs.mark_error(job_id, upstream.get("error", "tender generation failed"), "GENERATION_FAILED")
            return
        await jobs.mark_done(
            job_id,
            result={
                "doc": upstream.get("doc"),
                "meta": upstream.get("meta"),
                "used_llm": upstream.get("used_llm"),
                "fallback_reason": upstream.get("fallback_reason"),
            },
        )
    except CortexUpstreamError as e:
        await jobs.mark_error(job_id, e.message, e.code, e.retry_after)
    except Exception as e:
        await jobs.mark_error(job_id, str(e), "GATEWAY_ERROR")


@router.get("/{job_id}/export/{fmt}")
async def export_tender(
    job_id: str,
    fmt: str = Path(..., pattern="^(pdf|docx)$"),
    jobs: JobStore = Depends(get_jobs),
    hub: HubSession = Depends(get_hub),
):
    """Live-verified against the real hub (a first attempt at this 400'd):
    /rfp/export-pdf needs pre-rendered HTML, not {doc, meta} directly — it
    literally 400s "no html" otherwise (services/tender/app.py's real
    export_pdf route). /rfp/render is doc_type-agnostic (handles both simple
    and gov_ksa shapes) and produces exactly that HTML, so pdf export is a
    render-then-export pair, not a single call.

    /rfp/export-docx is different again: it builds a government-template
    (كراسة) Word document straight from {doc, meta}, but only works when
    `doc` has the gov_ksa "sections" shape — it 400s "no كراسة document to
    export" for a simple sow/rfp doc. docx export only works for
    doc_type=gov_ksa jobs; a simple-mode job can only export as PDF."""
    job: Job | None = await jobs.get(job_id)
    if job is None or job.status != "done" or job.kind != "tender":
        raise HTTPException(
            status_code=404,
            detail={"error": f"no completed tender job {job_id}", "code": "JOB_NOT_FOUND"},
        )
    doc, meta = (job.result or {}).get("doc"), (job.result or {}).get("meta")
    title = (meta or {}).get("title") or job_id
    try:
        if fmt == "pdf":
            rendered = await hub.post_json("/rfp/render", {"doc": doc, "meta": meta})
            upstream = await hub.post_raw(
                "/rfp/export-pdf", json={"html": rendered["html"], "meta": meta, "title": title}
            )
        else:
            upstream = await hub.post_raw(
                "/rfp/export-docx", json={"doc": doc, "meta": meta, "title": title}
            )
    except CortexUpstreamError as e:
        raise HTTPException(status_code=e.status_code, detail={"error": e.message, "code": e.code}) from e
    except Exception as e:
        raise HTTPException(status_code=502, detail={"error": str(e), "code": "GATEWAY_ERROR"}) from e
    if upstream.status_code >= 400:
        raise HTTPException(
            status_code=upstream.status_code,
            detail={"error": upstream.text[:300], "code": "EXPORT_FAILED"},
        )
    content_type = (
        "application/pdf"
        if fmt == "pdf"
        else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    return StreamingResponse(
        iter([upstream.content]),
        media_type=content_type,
        headers={"Content-Disposition": f'attachment; filename="{job_id}.{fmt}"'},
    )
