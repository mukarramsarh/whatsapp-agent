from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


class ErrorEnvelope(BaseModel):
    error: str
    code: str


class SubmitAccepted(BaseModel):
    job_id: str
    status: Literal["queued"] = "queued"
    estimated_seconds: int
    poll_url: str


# ── Search (e-Library) ───────────────────────────────────────────────────────

VALID_PATHWAYS = ("technical", "consulting", "contracts_tenders")


class SearchRequest(BaseModel):
    query: str = Field(..., min_length=1, description="The user's question or search term")
    pathway: str = Field(
        "technical",
        description=f"Which e-Library pathway to search: one of {VALID_PATHWAYS}",
    )
    language: Optional[str] = Field(None, description='"ar" or "en" — currently informational only')
    limit: int = Field(5, ge=1, le=20, description="Max sources to retrieve (maps to Cortex's k)")
    filters: Optional[dict[str, Any]] = Field(
        None,
        description=(
            "Accepted for forward-compatibility with e-lib-integration.md's original spec. "
            "Cortex's /api/query has no category/year filtering today, so this is currently ignored."
        ),
    )


class SearchResult(BaseModel):
    """Matches what Cortex's hub /api/query actually returns
    (services/hub/src/hub/elibrary/rag.py:1520-1526, 2022-2034) — plain
    filename lists, not per-source objects. `sources` is the CITED subset
    (files the generated answer actually names); `retrieved` is every file
    the search fetched, unfiltered, in rank order. There is deliberately no
    score/preview_text/file_id here: the hub's JSON response doesn't expose
    per-chunk scores or excerpts to API callers (only to its own UI's richer
    `chunks` field, which /api/query does not return), so this gateway does
    not fabricate fields Cortex itself doesn't provide. Per-source download
    is a documented follow-up (see README), not implemented in v1."""

    answer: str
    sources: list[str]
    retrieved: list[str]
    refused: bool = False


# ── Tender ────────────────────────────────────────────────────────────────────


class TenderGenerateRequest(BaseModel):
    """Passed through verbatim to Cortex's tender /generate. That endpoint reads
    a raw JSON body with no fixed schema beyond an optional `doc_type` field
    (services/tender/app.py's `generate()`) — this gateway does not invent a
    stricter contract than the one Cortex itself actually enforces."""

    answers: dict[str, Any]
