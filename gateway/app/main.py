import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse

from app.config import get_settings
from app.cortex_client import HubSession
from app.jobs import JobStore
from app.routers import analyze, classify, files, jobs, ocr, search, tender

settings = get_settings()
logging.basicConfig(level=settings.log_level)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.hub = HubSession(
        base_url=settings.cortex_hub_base_url,
        username=settings.cortex_service_username,
        password=settings.cortex_service_password,
        timeout=settings.http_timeout_seconds,
    )
    app.state.jobs = JobStore(ttl_seconds=settings.job_ttl_seconds)
    app.state.jobs.start()
    try:
        yield
    finally:
        app.state.jobs.stop()
        await app.state.hub.aclose()


app = FastAPI(
    title="Cortex WhatsApp Gateway",
    description=(
        "A standalone API in front of the real Cortex hub, built for the "
        "WhatsApp bridge (see e-lib-integration.md's original request). "
        "Every capability is submit-then-poll: POST a request, then GET "
        "/api/v1/jobs/{job_id} until status is done or error."
    ),
    version="0.1.0",
    lifespan=lifespan,
)


@app.exception_handler(HTTPException)
async def http_exception_handler(_request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, dict) and "error" in detail:
        return JSONResponse(status_code=exc.status_code, content=detail)
    return JSONResponse(status_code=exc.status_code, content={"error": str(detail), "code": "ERROR"})


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


app.include_router(search.router)
app.include_router(analyze.router)
app.include_router(classify.router)
app.include_router(tender.router)
app.include_router(ocr.router)
app.include_router(jobs.router)
app.include_router(files.router)
