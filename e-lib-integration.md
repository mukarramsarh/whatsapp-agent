# E-Library API Integration Requirements

**From:** Mukarram
**To:** Mohanad
**Purpose:** Expose e-library search/retrieval as an API endpoint so the AI agent can query it as a tool.

---

## Context

We are building a WhatsApp AI assistant. One of its tools will be **"Search the E-Library"**. When a user asks a question, the agent will call your API, get the result, and reply to the user on WhatsApp.

Your API may live on the cloud. Our agent runs locally/on-premise. **We cannot receive callbacks** (no public URL), so we will use a **submit → poll** async pattern.

---

## What We Need: 2 Endpoints

---

### Endpoint 1 — Authentication *(optional but recommended)*

If your system requires a token, provide this once. We store the token and reuse it.

```
POST /api/v1/auth/token
```

**Request**
```json
{
  "client_id": "whatsapp-agent",
  "client_secret": "shared-secret-key"
}
```

**Response `200`**
```json
{
  "token": "eyJhbGciOi...",
  "expires_in": 86400
}
```

**Notes**
- If you prefer simpler auth (fixed API key in header), skip this endpoint entirely and we'll just send `Authorization: Bearer <api_key>` on every request.
- Token or API key — your choice. Tell us which.

---

### Endpoint 2 — Submit a Search Request

```
POST /api/v1/search
Authorization: Bearer <token_or_api_key>
Content-Type: application/json
```

**Request body**
```json
{
  "query": "user's question or search term",
  "language": "ar",
  "filters": {
    "category": "engineering",
    "year_from": 2015,
    "year_to": 2024
  },
  "limit": 5
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `query` | string | ✅ | The search term or question |
| `language` | string | ❌ | `"ar"` or `"en"` — for result language preference |
| `filters` | object | ❌ | Any filter your system supports (category, date range, type, etc.) |
| `limit` | integer | ❌ | Max number of results (default 5) |

---

**Response `202 Accepted`** — job submitted, returns a job ID to poll

```json
{
  "job_id": "a3f8c1d2b0e44f1a",
  "status": "queued",
  "estimated_seconds": 5,
  "poll_url": "/api/v1/jobs/a3f8c1d2b0e44f1a"
}
```

> ⚠️ If the search always completes in under 2 seconds, you may return the result directly with `200` instead of `202`. Tell us which pattern you use.

---

### Endpoint 3 — Poll Job Status

```
GET /api/v1/jobs/{job_id}
Authorization: Bearer <token_or_api_key>
```

We will call this every **5 seconds** until `status` is `done` or `error`. Timeout after **5 minutes**.

---

**While processing — Response `200`**
```json
{
  "job_id": "a3f8c1d2b0e44f1a",
  "status": "processing"
}
```

**When done — Response `200`**
```json
{
  "job_id": "a3f8c1d2b0e44f1a",
  "status": "done",
  "results": [
    {
      "title": "Introduction to Structural Engineering",
      "author": "Ahmed Al-Rashid",
      "year": 2020,
      "category": "Engineering",
      "summary": "A 2-3 sentence summary of the book or chapter...",
      "relevance_score": 0.92,
      "file_id": "doc-789",
      "file_type": "pdf",
      "preview_text": "Optional excerpt matching the query..."
    }
  ],
  "total_found": 42
}
```

**On error — Response `200`**
```json
{
  "job_id": "a3f8c1d2b0e44f1a",
  "status": "error",
  "error": "No results found for the given query"
}
```

---

### Endpoint 4 — Download a File *(optional)*

If results include downloadable files (PDFs, documents):

```
GET /api/v1/files/{file_id}
Authorization: Bearer <token_or_api_key>
```

**Response** — stream the file directly with correct `Content-Type` header.
```
Content-Type: application/pdf
Content-Disposition: attachment; filename="book-title.pdf"
[binary file content]
```

We will forward this file to the user on WhatsApp.

---

## Status Values

| `status` | Meaning |
|---|---|
| `queued` | Request received, waiting to process |
| `processing` | Search in progress |
| `done` | Results ready |
| `error` | Failed — check `error` field |

---

## Error Responses

All errors follow this format:

```json
{
  "error": "Human-readable description",
  "code": "INVALID_QUERY"
}
```

| HTTP code | When |
|---|---|
| `400` | Bad request (missing required field, invalid query) |
| `401` | Invalid or expired token |
| `404` | Job ID not found |
| `429` | Rate limit exceeded |
| `500` | Server error |

---

## Auth Summary — Pick One

Tell us which you prefer and we'll configure accordingly:

| Option | How |
|---|---|
| **A — API Key (simplest)** | We send `Authorization: Bearer <fixed_key>` on every request |
| **B — Token exchange** | We call `/auth/token` first, cache the token, refresh when expired |

---

## What I Will Build On My Side

For reference, this is the integration code we will write. If you share this file with an AI assistant, it can generate a working integration automatically.

```python
# Tool: E-Library Search
# Pattern: submit → poll → return results to AI agent

BASE_URL = "https://your-elibrary-api.com"
API_KEY  = "your-api-key"          # from environment variable E_LIB_API_KEY

async def search_library(query: str, language: str = "en", filters: dict = {}) -> str:
    headers = {"Authorization": f"Bearer {API_KEY}"}

    # 1. Submit search
    r = await http.post(f"{BASE_URL}/api/v1/search",
                        json={"query": query, "language": language, "filters": filters, "limit": 5},
                        headers=headers)
    job_id = r.json()["job_id"]

    # 2. Poll until done
    for _ in range(60):                        # max 5 min (60 × 5s)
        await asyncio.sleep(5)
        r = await http.get(f"{BASE_URL}/api/v1/jobs/{job_id}", headers=headers)
        data = r.json()
        if data["status"] == "done":
            return format_results(data["results"])
        if data["status"] == "error":
            return f"Search failed: {data['error']}"

    return "Search timed out."
```

---

## Questions for You

Please answer these so we can finalize the integration:

1. **Auth method** — API key in header, or token exchange?
2. **Sync or async?** — Does search always return instantly, or does it take time?
3. **File downloads** — Do you return downloadable files, or text/summaries only? or we can leave it for future.
4. **Base URL** — What is the production base URL?
5. **Rate limits** — Any limits we should respect (requests/minute)?
6. **Filters** — What filter fields does your system support?

---

*Document version: 1.0 — WhatsApp AI Agent team*
