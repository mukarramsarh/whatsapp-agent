import os
import asyncio
import logging
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("bridge")

EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL", "http://evolution-api:8080")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")
INSTANCE_NAME = os.getenv("INSTANCE_NAME", "whatsapp-agent")
BRIDGE_PORT = int(os.getenv("BRIDGE_PORT", "5021"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", f"http://bridge:{BRIDGE_PORT}/webhook")

# ---------------------------------------------------------------------------
# Startup: register webhook with Evolution API
# ---------------------------------------------------------------------------

async def _register_webhook(retries: int = 10, delay: float = 8.0) -> None:
    """Try to register the per-instance webhook, retrying until Evolution API is up."""
    url = f"{EVOLUTION_API_URL}/webhook/set/{INSTANCE_NAME}"
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}
    payload = {
        "webhook": {
            "enabled": True,
            "url": WEBHOOK_URL,
            "webhookByEvents": False,
            "webhookBase64": False,
            "events": [
                "MESSAGES_UPSERT",
                "CONNECTION_UPDATE",
                "QRCODE_UPDATED",
            ],
        }
    }

    for attempt in range(1, retries + 1):
        await asyncio.sleep(delay)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(url, json=payload, headers=headers)
            if r.status_code in (200, 201):
                logger.info("Webhook registered: %s → %s", INSTANCE_NAME, WEBHOOK_URL)
                return
            logger.warning(
                "Webhook registration attempt %d/%d: HTTP %d — %s",
                attempt, retries, r.status_code, r.text[:200],
            )
        except Exception as exc:
            logger.warning(
                "Webhook registration attempt %d/%d failed: %s", attempt, retries, exc
            )

    logger.error(
        "Could not register webhook after %d attempts. "
        "Create the instance first via the Evolution API, then restart the bridge.",
        retries,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):  # noqa: D401
    asyncio.create_task(_register_webhook())
    yield


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="WhatsApp Agent — Bridge (Phase 1)",
    version="0.1.0",
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Outbound helpers
# ---------------------------------------------------------------------------

async def send_text(remote_jid: str, text: str) -> None:
    url = f"{EVOLUTION_API_URL}/message/sendText/{INSTANCE_NAME}"
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}
    payload = {"number": remote_jid, "text": text}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(url, json=payload, headers=headers)
            r.raise_for_status()
        logger.info("Sent reply to %s", remote_jid)
    except Exception as exc:
        logger.error("Failed to send message to %s: %s", remote_jid, exc)


# ---------------------------------------------------------------------------
# Message processing
# ---------------------------------------------------------------------------

def _extract_text(message: dict) -> str:
    """Pull plain text out of various WhatsApp message shapes."""
    return (
        message.get("conversation")
        or (message.get("extendedTextMessage") or {}).get("text")
        or (message.get("imageMessage") or {}).get("caption")
        or (message.get("videoMessage") or {}).get("caption")
        or (message.get("documentMessage") or {}).get("caption")
        or ""
    )


async def process_webhook(payload: dict) -> None:
    event: str = payload.get("event", "")
    logger.debug("Event received: %s", event)

    if event == "connection.update":
        state = (payload.get("data") or {}).get("state", "")
        logger.info("Connection state: %s", state)
        return

    if event == "qrcode.updated":
        qr = (payload.get("data") or {}).get("qrcode", {})
        logger.info("QR code updated — scan to connect. base64 present: %s", bool(qr.get("base64")))
        return

    if event != "messages.upsert":
        return

    data: dict = payload.get("data") or {}
    key: dict = data.get("key") or {}

    # Ignore messages we sent
    if key.get("fromMe", False):
        return

    remote_jid: str = key.get("remoteJid", "")

    # Skip group messages in Phase 1
    if "@g.us" in remote_jid:
        logger.info("Skipping group message from %s", remote_jid)
        return

    message: dict = data.get("message") or {}
    text = _extract_text(message)
    sender = data.get("pushName", remote_jid)

    if not text:
        logger.info("Non-text message from %s — skipping in Phase 1", remote_jid)
        return

    logger.info("Message from %s (%s): %s", sender, remote_jid, text)

    reply = f'Hi! I got your message: "{text}". I will reply to you soon.'
    await send_text(remote_jid, reply)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.post("/webhook")
async def webhook(request: Request, background_tasks: BackgroundTasks):
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    background_tasks.add_task(process_webhook, payload)
    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "healthy", "instance": INSTANCE_NAME, "bridge": "phase-1"}
