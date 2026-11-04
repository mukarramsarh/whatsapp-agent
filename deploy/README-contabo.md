# WhatsApp Agent on Contabo (Dokploy)

Stack: `docker-compose.contabo.yml` — Postgres (pgvector), Redis, Evolution API (WhatsApp),
**bridge** (agents + admin panel), **gateway** (link to the AI Tools hub). No GPU, no local models:
everything AI runs on DeepInfra.

## Agents

```
user ── WhatsApp ── Evolution API ── bridge
                                       │
                          orchestrator agent (gpt-oss-120b, native tool calling)
                                       │ delegate_to_agent
      ┌───────────┬────────────┬───────┴────┬──────────┐
  knowledge    documents     tender        media       ops (admin only, off by default)
  e-Library    OCR,          BD/RFP        image-to-image,
               classify,     analysis,     text-to-image,
               vision        tender gen    describe
```

Each specialist only receives its own tools. A specialist exists only while one of its tools is
enabled in the admin panel (`/tools`). Settings → AI → *Agent Mode* switches back to one flat agent.

All AI Tools capabilities (e-Library search, OCR, document classification, BD/RFP analysis, tender
generation) are tools that call the gateway, which logs in to the hub as a dedicated service
account and exposes a uniform submit/poll API.

## Voice and images

| Input | Output |
|---|---|
| Voice note | Whisper large-v3-turbo transcript → agents → **voice note** (edge-tts Arabic/English, DeepInfra TTS fallback). Long answers also get text. |
| Photo + instruction (typed or spoken) | FLUX.1 Kontext edit → edited **image** back in the chat |
| "Draw …" | FLUX text-to-image → image |
| Photo, "what is this?" | Qwen3-VL description |
| Scanned document photo | OCR through the hub |

A photo or file sent in the last 30 minutes stays attached to follow-ups, so "make it black and
white" (typed or as a voice note) refers to the photo just sent.

## First-time setup

1. Dokploy → new Compose app, Git provider, branch `deploy/contabo-dokploy`,
   compose path `./docker-compose.contabo.yml`, environment from `deploy/contabo.env.example`.
2. Domain → service `bridge`, port `5021`, HTTPS (Let's Encrypt). Only the admin panel and the
   secret-protected `/webhook` are public; Evolution API and the gateway are internal.
3. Create the gateway's service account on the hub (`POST /api/auth/users`, role `staff`,
   all tools at level `user`) and put its credentials in `CORTEX_SERVICE_USERNAME/PASSWORD`.
4. Open `https://<domain>/connect` (admin login) and scan the QR code with the agent's WhatsApp number.
5. In `/users`, allow the phone numbers that may talk to the agent (or set `INITIAL_ALLOWED_NUMBERS`
   before first boot to auto-allow admins).
