from fastapi import Request

from app.cortex_client import HubSession
from app.jobs import JobStore


def get_hub(request: Request) -> HubSession:
    return request.app.state.hub


def get_jobs(request: Request) -> JobStore:
    return request.app.state.jobs
