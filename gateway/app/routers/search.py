import asyncio
import logging

from fastapi import APIRouter, Depends

from app.cortex_client import CortexUpstreamError, HubSession
from app.deps import get_hub, get_jobs
from app.jobs import JobStore
from app.schemas import VALID_PATHWAYS, SearchRequest, SubmitAccepted
from app.security import require_api_key

logger = logging.getLogger("search")

router = APIRouter(prefix="/api/v1", tags=["search"], dependencies=[Depends(require_api_key)])

_PATHWAY_ALIASES = {
    "tech": "technical",
    "consult": "consulting",
    "contracts": "contracts_tenders",
    "tenders": "contracts_tenders",
    "contracts-tenders": "contracts_tenders",
    "contracts and tenders": "contracts_tenders",
}


def _normalize_pathway(raw: str) -> str:
    """Real callers here are LLM tool-callers (a WhatsApp bridge agent), not
    strict API clients — their tool-call arguments aren't guaranteed to match
    the enum exactly (wrong case, a close synonym, empty). Rejecting outright
    with a 400 just breaks the whole search for a caller that can't always
    control its own generated argument values. Best-effort normalize, and
    fall back to the default pathway rather than hard-fail."""
    key = (raw or "").strip().lower().replace(" ", "_")
    if key in VALID_PATHWAYS:
        return key
    if key in _PATHWAY_ALIASES:
        return _PATHWAY_ALIASES[key]
    logger.warning("unrecognized pathway %r — defaulting to 'technical'", raw)
    return "technical"


@router.post("/search", status_code=202, response_model=SubmitAccepted)
async def submit_search(
    body: SearchRequest,
    jobs: JobStore = Depends(get_jobs),
    hub: HubSession = Depends(get_hub),
):
    body.pathway = _normalize_pathway(body.pathway)

    job = await jobs.create(kind="search", kind_prefix="lib")
    asyncio.create_task(_run_search(job.job_id, body, jobs, hub))

    return SubmitAccepted(
        job_id=job.job_id,
        estimated_seconds=15,
        poll_url=f"/api/v1/jobs/{job.job_id}",
    )


async def _run_search(job_id: str, body: SearchRequest, jobs: JobStore, hub: HubSession) -> None:
    await jobs.mark_processing(job_id)
    try:
        upstream = await hub.post_json(
            "/api/query",
            json={"pathway": body.pathway, "question": body.query, "k": body.limit},
        )
    except CortexUpstreamError as e:
        await jobs.mark_error(job_id, e.message, e.code, e.retry_after)
        return
    except Exception as e:  # upstream unreachable, timeout, etc.
        await jobs.mark_error(job_id, str(e), "GATEWAY_ERROR")
        return

    await jobs.mark_done(
        job_id,
        result={
            "answer": upstream.get("answer", ""),
            "sources": upstream.get("sources", []),
            "retrieved": upstream.get("retrieved", []),
            "refused": upstream.get("refused", False),
        },
    )
