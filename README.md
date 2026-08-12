# WhatsApp Agent

A 3-layer WhatsApp AI agent built with Evolution API, a Python bridge, and an AI layer.

## Architecture

```
WhatsApp User
     │
     ▼
[Layer 1] Evolution API  (port 5020)   — handles WhatsApp protocol (Baileys)
     │ webhook POST
     ▼
[Layer 2] Python Bridge  (port 5021)   — validates, formats, routes messages
     │ (Phase 2+)
     ▼
[Layer 3] AI / MCP Agents              — LLMs, tools, embeddings, knowledge base
```

**Infrastructure:** PostgreSQL + Redis (internal), all containerised via Docker Compose.

---

## Phase 1 — Current

- Evolution API connected to WhatsApp
- Bridge echoes every incoming message back:
  `Hi! I got your message: "<text>". I will reply to you soon.`

---

## Quick Start

### 1. Configure environment

```bash
cp .env.example .env
# Edit .env — set EVOLUTION_API_KEY, POSTGRES_PASSWORD, REDIS_PASSWORD
```

### 2. Start the stack

```bash
docker compose up -d
```

### 3. Create the WhatsApp instance & scan QR code

```bash
chmod +x scripts/create_instance.sh
./scripts/create_instance.sh
```

Then open `http://localhost:5020/manager` (or your server URL) to scan the QR code.

### 4. Verify

```bash
# Bridge health
curl http://localhost:5021/health

# Evolution API health
curl http://localhost:5020/
```

Send a WhatsApp message to your number — the bot will echo it back.

---

## Ports

| Service       | Port | Notes                   |
|---------------|------|-------------------------|
| Evolution API | 5020 | WhatsApp gateway + UI  |
| Bridge        | 5021 | Python webhook handler  |
| PostgreSQL    | —    | Internal only           |
| Redis         | —    | Internal only           |

---

## Evolution API Manager

Visit `http://<your-server>:5020/manager` to:
- View connected instances
- Scan QR codes
- Monitor webhooks

---

## Environment Variables

See [`.env.example`](.env.example) for all variables and descriptions.

---

## Deployment on Dokploy

1. Push this repo to GitHub.
2. In Dokploy, create a new **Docker Compose** app pointing to this repo.
3. Set all env variables from `.env.example` in Dokploy's environment panel.
4. Set `EVOLUTION_SERVER_URL` to your public URL (e.g. `https://evolution.yourdomain.com`).
5. Set `WEBHOOK_URL` to `http://bridge:5021/webhook` (internal Docker network).
6. Deploy, then run `scripts/create_instance.sh` from your local machine (pointing `EVOLUTION_SERVER_URL` to the public URL).
