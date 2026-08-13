# WhatsApp AI Agent — Project Context

## What this is

A 3-layer self-hosted WhatsApp AI assistant built for ProcurementHub. Every inbound WhatsApp message flows through a protocol gateway → a Python bridge that adds AI intelligence → a reply sent back to the user. The system supports Arabic and English, voice messages, document OCR, and a pluggable tool framework.

---

## Architecture (3 layers)

```
WhatsApp ──► Evolution API (port 5020)  ←── Baileys / WhatsApp Web protocol
                     │ webhook POST /webhook
                     ▼
              Python Bridge (port 5021)  ←── FastAPI, SQLAlchemy, ReAct agent
                     │ OpenAI-compatible API
                     ▼
              Self-hosted LLM (port 8000) ←── vLLM / Ollama serving OSS model (~20B)
```

Supporting services:
- **PostgreSQL** (pgvector/pgvector:pg15) — messages, users, settings, embeddings
- **Redis** — Evolution API session cache
- **PH OCR API** (port 5010) — async document/image text extraction (internal service)
- **pgAdmin** (port 5022) — DB management UI

---

## Git & Deployment

| Thing | Value |
|---|---|
| Repo | `git@github.com-mukarramsarh:mukarramsarh/whatsapp-agent.git` |
| SSH key | `~/.ssh/id_ed25519_procurementhub` via host alias `github.com-mukarramsarh` |
| Deploy | Dokploy at `192.168.100.62` |
| Live branch | `main` — **do not break this branch** |
| Dev branch | `phase_01` — current active development |

Always push with: `git push git@github.com-mukarramsarh:mukarramsarh/whatsapp-agent.git <branch>`

---

## Branch status

### `main` (live on Dokploy)
- Evolution API + Python bridge
- User allow-list with admin panel at `:5021`
- HTTP Basic Auth on admin panel
- Inbound media download (images, video, audio, documents)
- OCR integration: image/PDF → PH OCR API → extracted text → reply to user
- Admin panel: users, messages, reply with file attachment

### `phase_01` (in development — NOT deployed yet)
Everything in `main` plus:
- **ReAct agent loop** via OpenAI-compatible LLM API
- **5 pluggable tools**: OCR, DB query, SSH, HTTP API, knowledge base search
- **Hybrid RAG context**: recent N messages + vector similarity search
- **Confidence layer**: self-scoring + retry when below threshold
- **Bilingual**: Arabic/English auto-detected, language-matched TTS
- **Voice**: faster-whisper STT + edge-tts TTS
- **Vector embeddings**: cosine similarity search (JSON storage, no pgvector extension required)
- **New admin pages**: `/settings` (AI, context, confidence, voice, role prompts), `/tools`
- **4 user roles**: user, admin, vip, staff — each with editable system prompt
- **New DB tables**: `wa_settings`, `wa_user_roles`, `wa_tool_configs`, `wa_message_embeddings`, `wa_knowledge_docs`

---

## File structure

```
bridge/
├── main.py              # FastAPI app, webhook handler, agent pipeline orchestration
├── admin.py             # Admin panel routes (users, messages, settings, tools, reply)
├── database.py          # SQLAlchemy models + init_db() + seed() for defaults
├── whatsapp.py          # Evolution API client (send_text, send_media, download_and_save)
├── ocr.py               # PH OCR async client (submit, poll, submit_and_wait)
├── voice.py             # STT (faster-whisper) + TTS (edge-tts)
├── embeddings.py        # Vector embedding store + cosine search
├── agent/
│   ├── __init__.py      # Re-exports AgentRunner, AgentResult
│   ├── runner.py        # ReAct loop: LLM → tool_calls → execute → loop → answer
│   ├── context.py       # Hybrid context builder (recent msgs + vector search)
│   ├── confidence.py    # Self-scoring: LLM rates its own response 0.0–1.0
│   ├── language.py      # Arabic/English detection + per-lang system instructions
│   └── tools/
│       ├── __init__.py  # ALL_TOOL_CLASSES registry
│       ├── base.py      # Tool ABC + ToolResult dataclass
│       ├── ocr.py       # OCR tool (wraps ocr.py)
│       ├── database.py  # Internal DB read-only queries
│       ├── ssh.py       # Remote SSH execution (asyncssh)
│       ├── http_api.py  # HTTP REST calls to configured external APIs
│       └── library.py   # Knowledge base semantic search
├── templates/
│   ├── base.html        # Sidebar layout, Tailwind CDN, WA brand colors
│   ├── users.html       # User management + role assignment
│   ├── messages.html    # Message log with media preview + OCR text box
│   ├── settings.html    # Tabbed settings: AI, context, confidence, voice, roles
│   └── tools.html       # Tool cards with enable/disable + JSON config editor
├── requirements.txt     # Python dependencies
└── Dockerfile           # uvicorn --no-access-log (NOT --log-config /dev/null)
```

---

## Database tables

| Table | Purpose |
|---|---|
| `wa_users` | Phone numbers, names, roles, allow-list flag |
| `wa_messages` | All inbound/outbound messages + attachment JSON |
| `wa_settings` | Key-value store for all admin-configurable settings |
| `wa_user_roles` | Role definitions with per-role system prompt |
| `wa_tool_configs` | Tool enable/disable + JSON config per tool |
| `wa_message_embeddings` | Vector embeddings for conversation history |
| `wa_knowledge_docs` | Knowledge base documents for library tool |

The `attachment` column in `wa_messages` is a JSON string:
```json
{
  "path": "media/uuid.jpg",
  "name": "photo.jpg",
  "mime": "image/jpeg",
  "size": 126211,
  "caption": "",
  "ocr_text": "extracted text if applicable",
  "transcript": "voice transcript if applicable"
}
```

---

## Environment variables (full list)

```
# Evolution API
EVOLUTION_API_KEY=...
EVOLUTION_SERVER_URL=http://localhost:5020
INSTANCE_NAME=STC

# Webhook
WEBHOOK_URL=http://bridge:5021/webhook

# PostgreSQL (individual vars — no DATABASE_URL — handles special chars in password)
POSTGRES_HOST=postgres
POSTGRES_PORT=5432
POSTGRES_DB=evolution
POSTGRES_USER=...
POSTGRES_PASSWORD=...        # @ or # in password is safe because URL.create() handles it

# Redis
REDIS_PASSWORD=...

# Admin panel (HTTP Basic Auth)
ADMIN_USERNAME=admin
ADMIN_PASSWORD=...

# pgAdmin
PGADMIN_EMAIL=...
PGADMIN_PASSWORD=...

# PH OCR API
OCR_API_URL=http://192.168.100.62:5010
OCR_API_KEY=...

# AI agent (phase_01)
AI_BASE_URL=http://192.168.100.62:8000/v1   # OpenAI-compatible endpoint
AI_MODEL=your-model-name
AI_API_KEY=...
AI_EMBEDDING_URL=http://192.168.100.62:8000/v1   # leave empty to disable vector search
AI_EMBEDDING_MODEL=text-embedding-3-small

# Voice (phase_01)
WHISPER_MODEL=base          # tiny|base|small|medium|large|large-v3-turbo
TTS_VOICE_EN=en-US-JennyNeural
TTS_VOICE_AR=ar-SA-HamedNeural
```

---

## Key technical decisions & gotchas

### Evolution API
- Version: v2.3.7 (evoapicloud/evolution-api:latest)
- Webhook payload MUST be nested under `"webhook"` key — older format fails with 400
- Instance name: `STC` — mismatch causes 404 on all API calls
- JID format: `923124277939@s.whatsapp.net` — **no leading `+`** (breaks Evolution API with 400)
- `_clean_jid()` in `whatsapp.py` strips the `+` on every send call

### Database
- Use `URL.create()` not f-string for DB URL — passwords with `@` break f-string URLs
- Pass POSTGRES_* vars individually to bridge, not as a DATABASE_URL string
- Postgres image: `pgvector/pgvector:pg15` (phase_01) — switched from `postgres:15-alpine`
- `create_all` is safe to call on startup — idempotent, won't drop existing tables

### Webhook registration
- Bridge retries 10 times with 8s delay on startup before giving up
- `WEBHOOK_URL` must be reachable **from inside the evolution-api container** — use `http://bridge:5021/webhook` not `http://localhost:5021/webhook`

### Admin panel
- No `/admin` prefix — admin routes are at root: `/users`, `/messages`, `/settings`, `/tools`
- HTTP Basic Auth via `require_auth` dependency on every route
- Media files served via `/media/{filename}` (auth-protected, not public)

### Agent pipeline (phase_01)
- Agent is called for all allowed users regardless of media type
- Attachment metadata (path, mime, name) is injected into the user message so the LLM knows a file exists and can call the OCR tool
- Settings are loaded from DB on every message (no in-memory cache) — allows live admin changes
- Confidence layer defaults to disabled — enable in `/settings` once LLM is stable
- Vector search is skipped silently if `AI_EMBEDDING_URL` is empty

### Voice (phase_01)
- `voice_enabled` defaults to `false` in settings — enable explicitly
- faster-whisper loads the model lazily on first transcription request
- edge-tts requires internet access — for air-gapped: replace with XTTS v2 (Coqui)
- Audio reply format: MP3 sent via `send_media` as `audio/mpeg`

### Dockerfile
- CMD must use `--no-access-log` — using `--log-config /dev/null` crashes uvicorn (empty file error)

---

## Admin panel quick reference

| URL | What |
|---|---|
| `http://192.168.100.62:5021/users` | User allow-list + role assignment |
| `http://192.168.100.62:5021/messages` | Message log with media/OCR/voice |
| `http://192.168.100.62:5021/settings` | AI config, context, confidence, voice, roles |
| `http://192.168.100.62:5021/tools` | Tool enable/disable + JSON config |
| `http://192.168.100.62:5022` | pgAdmin (DB browser) |

---

## Adding a new tool

1. Create `bridge/agent/tools/my_tool.py` inheriting from `Tool`
2. Set `name`, `display_name`, `description`, `parameters_schema`
3. Implement `async def run(self, **kwargs) -> ToolResult`
4. Add the class to `ALL_TOOL_CLASSES` in `bridge/agent/tools/__init__.py`
5. Restart the bridge — the tool auto-registers in `wa_tool_configs` via `init_db()` seed

---

## Planned next phases

- **Voice on DGX Spark**: `faster-whisper-server` container with `large-v3-turbo` + CUDA; XTTS v2 for self-hosted Arabic TTS
- **ERP tool**: configure HTTP API tool with ERP base URL + auth headers via `/tools` admin
- **Knowledge base upload**: admin UI to upload PDFs → chunk → embed → store in `wa_knowledge_docs`
- **SSH tool**: configure server list in tool config JSON from `/tools` admin
- **Multi-agent**: spawn sub-agents for long-running tasks, report back via WhatsApp
