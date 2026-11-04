"""In-memory job store.

Deliberately matches the pattern Cortex's own bd-analyzer/classifier/OCR
services already use for their internal JOBS[job_id] dicts, rather than adding
Redis/SQLite this project doesn't otherwise need. Same accepted limitation:
a process restart loses in-flight jobs. Jobs are short-lived by design (the
bridge's own poll loop gives up after 5 minutes), so a periodic TTL sweep is
enough — nothing here needs to survive a restart.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Literal, Optional

Status = Literal["queued", "processing", "done", "error"]


@dataclass
class Job:
    job_id: str
    kind: str
    status: Status = "queued"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    result: Optional[dict[str, Any]] = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    retry_after: Optional[str] = None
    # An opaque description of a downloadable artifact this job produced, if
    # any (e.g. {"kind": "bd_pptx", "upstream_path": "/bd/api/result/...pptx"}),
    # looked up by GET /api/v1/files/{file_id} using job_id as the file_id.
    file_ref: Optional[dict[str, Any]] = None


class JobStore:
    def __init__(self, ttl_seconds: int):
        self._ttl = ttl_seconds
        self._jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()
        self._sweep_task: Optional[asyncio.Task] = None

    def new_id(self, kind_prefix: str) -> str:
        return f"{kind_prefix}_{secrets.token_hex(8)}"

    async def create(self, kind: str, kind_prefix: str) -> Job:
        job = Job(job_id=self.new_id(kind_prefix), kind=kind)
        async with self._lock:
            self._jobs[job.job_id] = job
        return job

    async def get(self, job_id: str) -> Optional[Job]:
        async with self._lock:
            return self._jobs.get(job_id)

    async def mark_processing(self, job_id: str) -> None:
        await self._update(job_id, status="processing")

    async def mark_done(self, job_id: str, result: dict[str, Any], file_ref: Optional[dict] = None) -> None:
        await self._update(job_id, status="done", result=result, file_ref=file_ref)

    async def mark_error(
        self,
        job_id: str,
        error: str,
        error_code: str = "UPSTREAM_ERROR",
        retry_after: Optional[str] = None,
    ) -> None:
        await self._update(job_id, status="error", error=error, error_code=error_code, retry_after=retry_after)

    async def _update(self, job_id: str, **fields: Any) -> None:
        async with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            for k, v in fields.items():
                setattr(job, k, v)
            job.updated_at = time.time()

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            cutoff = time.time() - self._ttl
            async with self._lock:
                expired = [jid for jid, j in self._jobs.items() if j.updated_at < cutoff]
                for jid in expired:
                    del self._jobs[jid]

    def start(self) -> None:
        if self._sweep_task is None:
            self._sweep_task = asyncio.create_task(self._sweep_loop())

    def stop(self) -> None:
        if self._sweep_task is not None:
            self._sweep_task.cancel()
            self._sweep_task = None
