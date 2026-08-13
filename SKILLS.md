# Engineering Skills & Best Practices

Reference for this project. Apply these patterns consistently.

---

## Python & Async

### Always use `asyncio.create_task()` for fire-and-forget work
```python
# GOOD — non-blocking, webhook returns immediately
asyncio.create_task(_embed_message(msg_id, text, number))

# BAD — blocks the request handler
await _embed_message(msg_id, text, number)
```

### Use `asyncio.to_thread()` for CPU-bound or blocking I/O in async code
```python
# Blocking file read in an async function
file_bytes = await asyncio.to_thread(file_path.read_bytes)

# Blocking model inference
result = await asyncio.to_thread(whisper_model.transcribe, str(path))
```

### Lazy-load heavy models once with an async lock
```python
_model = None
_lock = asyncio.Lock()

async def _get_model():
    global _model
    async with _lock:
        if _model is None:
            _model = await asyncio.to_thread(load_model)
        return _model
```
Prevents race conditions on startup when multiple requests arrive before the model is ready.

### Prefer context managers for DB sessions
```python
# GOOD — session always closed, even on exception
async with AsyncSessionLocal() as db:
    result = await db.execute(select(User))

# BAD — leaks if exception is raised before close
db = AsyncSessionLocal()
result = await db.execute(select(User))
await db.close()
```

### Use `expire_on_commit=False` in session factory
```python
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)
```
Without this, accessing model attributes after `commit()` triggers a lazy-load that fails in async context.

### SQLAlchemy URL for passwords with special characters
```python
# GOOD — URL.create() percent-encodes special chars automatically
URL.create(drivername="postgresql+asyncpg", password="ph@3263#!")

# BAD — @ in password breaks f-string URL parsing
f"postgresql+asyncpg://user:ph@3263#!@host/db"
```

---

## FastAPI

### Return early from background tasks — never raise HTTPException inside them
```python
# Background tasks run after the response is sent — exceptions are swallowed
async def process_webhook(payload: dict) -> None:
    if not valid:
        logger.warning("Invalid payload")
        return  # early return, not raise HTTPException
```

### Use `BackgroundTasks` for webhook processing
```python
@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    payload = await request.json()
    background_tasks.add_task(process_webhook, payload)
    return {"status": "ok"}   # returns immediately, processing continues
```
This keeps webhook response time under 100ms regardless of agent processing time.

### Depend on auth in every protected route
```python
@router.get("/settings")
async def settings(request: Request, _: str = Depends(require_auth)):
    ...
# The _ convention signals "auth side-effect only, result unused"
```

### Use `secrets.compare_digest` for credential comparison
```python
# GOOD — constant-time comparison prevents timing attacks
secrets.compare_digest(provided.encode(), expected.encode())

# BAD — short-circuits on first mismatch, leaks info via timing
provided == expected
```

---

## Database & Migrations

### Always `await db.refresh(obj)` after commit if you need the generated ID
```python
db.add(msg)
await db.commit()
await db.refresh(msg)   # re-fetches server-generated id, created_at, etc.
return msg.id
```

### `create_all` is safe for startups — it won't drop existing tables
```python
async with engine.begin() as conn:
    await conn.run_sync(Base.metadata.create_all)
```
New columns/tables are added; existing ones are left alone. For column renames or drops, you need explicit migrations (Alembic).

### Seed defaults idempotently
```python
# Check before inserting — safe to call on every startup
r = await db.execute(select(Setting).where(Setting.key == key))
if not r.scalar_one_or_none():
    db.add(Setting(key=key, value=default_value))
```

### Store complex data as JSON text, not multiple columns
```python
# attachment column stores all file metadata in one field
attachment = json.dumps({"path": "media/x.pdf", "mime": "application/pdf", "ocr_text": "..."})
```
Avoids schema migrations when adding new metadata fields (like `ocr_text`, `transcript`).

---

## AI Agent Design (ReAct)

### Structure: Reason → Act → Observe → loop until done
```
LLM decides what tool to call (Reason)
  → Tool executes (Act)
  → Result added to messages (Observe)
  → LLM sees result and either calls another tool or gives final answer
```
Cap iterations (default 10) to prevent infinite loops.

### Always pass tool results as `"role": "tool"` messages
```python
messages.append({
    "role": "tool",
    "tool_call_id": tc.id,        # must match the tool_call id
    "content": str(tool_result),
})
```
Missing `tool_call_id` causes API errors on most models.

### Give tools precise, unambiguous descriptions
```python
# GOOD — tells the model exactly when and how to use it
description = (
    "Extract text from an image (JPEG, PNG) or PDF. "
    "Use when the user sends a document or asks what an attached image says."
)

# BAD — too vague, model won't know when to call it
description = "OCR tool"
```

### Tool parameters schema must be strict JSON Schema
```python
parameters_schema = {
    "type": "object",
    "properties": {
        "file_path": {
            "type": "string",
            "description": "Relative path like 'media/uuid.pdf'",
        }
    },
    "required": ["file_path"],    # list required params explicitly
}
```

### Tool errors should be informative, not exceptions
```python
# GOOD — agent can recover and inform the user
return ToolResult(success=False, error=f"Server '{name}' not configured. Available: {list(servers)}")

# BAD — unhandled exception crashes the agent loop
raise KeyError(f"Server not found: {name}")
```

### Confidence scoring: fail open, never block
```python
try:
    score, reason = await _score_response(...)
except Exception:
    return 1.0, ""   # assume confidence is fine, don't block the reply
```

---

## RAG & Context

### Hybrid retrieval: recency + semantic similarity
- **Recent messages** (exact, ordered): captures immediate conversation flow
- **Vector similarity** (semantic): surfaces relevant older context the model wouldn't otherwise see
- De-duplicate before injecting: remove from similar anything already in recent

### Chunk text before embedding — don't embed full documents at once
```
PDF → split by paragraph/page → embed each chunk → store separately
```
Smaller chunks give more precise similarity scores.

### Set a relevance threshold on vector results
```python
return [r for r in scored if r["score"] > 0.30]   # discard low-similarity noise
```
Irrelevant context injected into the prompt degrades quality more than no context.

### Language instruction belongs in system prompt, not user message
```python
# System prompt
f"Reply in the same language as the user. {lang_instruction(language)}"

# NOT injected into the user's message — that's confusing
```

### Keep context messages in chronological order
```
[system]                    ← master prompt + role + language
[system]                    ← historical similar context (if any)
[user] [assistant] [user]   ← recent conversation (oldest first)
[user]                      ← current message
```
LLMs attend more to recent tokens — put the current question last.

---

## Security

### Never interpolate user input into shell commands
```python
# GOOD — asyncssh takes command as a string, no shell injection via args
await conn.run(command, timeout=30)

# DANGEROUS if command came from user input without validation
subprocess.run(f"ls {user_input}", shell=True)
```

### Scope external tool access — allowlist, not blocklist
```python
# SSH tool: only configured servers can be reached
servers = self.config.get("servers", {})
if server not in servers:
    return ToolResult(success=False, error=f"Server not configured")
```

### HTTP auth credentials: constant-time compare, never log
```python
secrets.compare_digest(provided.encode(), expected.encode())   # timing-safe
logger.info("Auth for user: %s", username)   # log username, never password
```

### API keys: environment variables, never hardcoded
```python
API_KEY = os.getenv("OCR_API_KEY", "")    # empty string = disabled, not a default key
```

### Validate all file paths before serving
```python
file_path = MEDIA_DIR / filename     # filename comes from URL param
if not file_path.exists():
    raise HTTPException(status_code=404)
# SQLAlchemy ORM prevents SQL injection; Path object prevents directory traversal
```

### Auth-protect media files — don't expose them publicly
```python
@router.get("/media/{filename}")
async def serve_media(filename: str, _: str = Depends(require_auth)):
    ...
```
Without this, anyone who knows the UUID filename can access private WhatsApp media.

### DB queries: always use ORM or parameterized queries
```python
# GOOD — parameterized, SQLAlchemy handles escaping
await db.execute(select(User).where(User.number == number))

# DANGEROUS — string interpolation = SQL injection
await db.execute(f"SELECT * FROM wa_users WHERE number = '{number}'")
```

### Secrets in Docker: use env vars, not docker-compose.yml values
```yaml
# GOOD — value comes from .env file (not committed)
ADMIN_PASSWORD: ${ADMIN_PASSWORD}

# BAD — secret committed to git
ADMIN_PASSWORD: "mysecret123"
```

---

## Logging

### Use structured log messages with %s placeholders, not f-strings
```python
# GOOD — message string is constant, args are lazy-formatted only if needed
logger.info("Sent text to %s (%d chars)", jid, len(text))

# BAD — f-string always evaluated, even at DEBUG level when not printed
logger.info(f"Sent text to {jid} ({len(text)} chars)")
```

### Log at the right level
| Level | When |
|---|---|
| `DEBUG` | Verbose internals, skipped operations (embedding skipped, vector search disabled) |
| `INFO` | Normal flow events (message received, agent replied, tool executed) |
| `WARNING` | Recoverable issues (webhook registration retry, Whisper not loaded) |
| `ERROR` | Failures that affect the user (LLM call failed, media download error) |

### Always include enough context to debug without reading code
```python
# GOOD — who, what, why
logger.error("OCR submit failed for %s (%s): %s", filename, mime, exc)

# BAD — tells you nothing
logger.error("Error: %s", exc)
```

---

## Docker & Deployment

### Health checks on all stateful services
```yaml
healthcheck:
  test: ["CMD-SHELL", "pg_isready -U ${POSTGRES_USER}"]
  interval: 10s
  timeout: 5s
  retries: 5
```
Prevents dependent services from starting before the DB is ready.

### Use `restart: unless-stopped` not `always`
`always` restarts even after intentional `docker stop`. `unless-stopped` respects manual stops.

### Named volumes, not bind mounts, for persistent data
```yaml
volumes:
  - media_data:/app/media    # survives container recreation
  - postgres_data:/var/lib/postgresql/data
```

### Build context = service directory only
```yaml
build:
  context: ./bridge          # only bridge/ is sent to Docker daemon, not whole repo
  dockerfile: Dockerfile
```

### Pin image tags in production, use `latest` only in dev
```yaml
image: postgres:15-alpine    # pinned — predictable
image: evoapicloud/evolution-api:latest   # latest = acceptable here (always want updates)
```

### Separate env vars per service — avoid shared env bleed
Each service in docker-compose.yml should only see vars it actually needs.

---

## Code Quality

### Functions do one thing — no "and" in the function name
```python
# BAD — two responsibilities
async def download_and_save_and_notify(...)

# GOOD — one responsibility each
meta = await download_and_save(data)   # downloads + saves, returns meta
asyncio.create_task(_embed_message(...))  # embeds, separately
```

### Return early — avoid deep nesting
```python
# GOOD
if not user.allowed:
    return
if not text and not attachment_meta:
    return
await _run_agent_pipeline(...)

# BAD — deeply nested
if user.allowed:
    if text or attachment_meta:
        await _run_agent_pipeline(...)
```

### Dataclasses for structured results — no naked tuples
```python
# GOOD — self-documenting
@dataclass
class AgentResult:
    content: str
    language: str
    tool_calls_made: list[str]
    confidence: float

# BAD — what is index 2?
return (text, "en", ["ocr"], 0.85)
```

### Never swallow exceptions silently
```python
# GOOD — log the error, return a safe default
except Exception as exc:
    logger.error("Embedding failed: %s", exc)
    return None

# BAD — hides bugs
except Exception:
    pass
```
