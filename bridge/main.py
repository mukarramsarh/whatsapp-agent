import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request

from database import AsyncSessionLocal, Message, User, init_db
from admin import router as admin_router
from whatsapp import (
    MEDIA_MESSAGE_KEYS,
    download_and_save,
    send_text,
)

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("bridge")

from sqlalchemy import select

EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL", "http://evolution-api:8080")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")
INSTANCE_NAME = os.getenv("INSTANCE_NAME", "STC")
BRIDGE_PORT = int(os.getenv("BRIDGE_PORT", "5021"))
WEBHOOK_URL = os.getenv("WEBHOOK_URL", f"http://bridge:{BRIDGE_PORT}/webhook")


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

async def _register_webhook(retries: int = 10, delay: float = 8.0) -> None:
    url = f"{EVOLUTION_API_URL}/webhook/set/{INSTANCE_NAME}"
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}
    payload = {
        "webhook": {
            "enabled": True,
            "url": WEBHOOK_URL,
            "webhookByEvents": False,
            "webhookBase64": False,
            "events": ["MESSAGES_UPSERT", "CONNECTION_UPDATE", "QRCODE_UPDATED"],
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
                "Webhook registration %d/%d: HTTP %d — %s",
                attempt, retries, r.status_code, r.text[:200],
            )
        except Exception as exc:
            logger.warning("Webhook registration %d/%d failed: %s", attempt, retries, exc)
    logger.error("Could not register webhook after %d attempts.", retries)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    asyncio.create_task(_register_webhook())
    yield


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="WhatsApp Agent — Bridge", version="0.3.0", lifespan=lifespan)
app.include_router(admin_router)


# ---------------------------------------------------------------------------
# Message processing
# ---------------------------------------------------------------------------

def _extract_text(message: dict) -> str:
    return (
        message.get("conversation")
        or (message.get("extendedTextMessage") or {}).get("text")
        or (message.get("imageMessage") or {}).get("caption")
        or (message.get("videoMessage") or {}).get("caption")
        or (message.get("documentMessage") or {}).get("caption")
        or (message.get("documentWithCaptionMessage", {}).get("message", {}).get("documentMessage") or {}).get("caption")
        or ""
    )


async def _get_or_create_user(number: str, name: str | None) -> tuple[User, bool]:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.number == number))
        user = result.scalar_one_or_none()
        if user:
            if name and not user.name:
                user.name = name
                await db.commit()
            return user, False
        user = User(number=number, name=name, allowed=False, status="active", role="user")
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user, True


async def _store_message(
    user_id: str, text: str, attachment_json: str | None
) -> None:
    async with AsyncSessionLocal() as db:
        msg = Message(
            user_id=user_id,
            message=text or None,
            attachment=attachment_json,
            direction="inbound",
            status="received",
        )
        db.add(msg)
        await db.commit()


async def process_webhook(payload: dict) -> None:
    event: str = payload.get("event", "")
    logger.debug("Event: %s", event)

    if event == "connection.update":
        logger.info("Connection state: %s", (payload.get("data") or {}).get("state", ""))
        return
    if event == "qrcode.updated":
        logger.info("QR code updated — scan to connect.")
        return
    if event != "messages.upsert":
        return

    data: dict = payload.get("data") or {}
    key: dict = data.get("key") or {}

    if key.get("fromMe", False):
        return

    remote_jid: str = key.get("remoteJid", "")
    if "@g.us" in remote_jid:
        return

    number = remote_jid.replace("@s.whatsapp.net", "").replace("@c.us", "")
    push_name: str | None = data.get("pushName") or None
    message: dict = data.get("message") or {}
    message_type: str = data.get("messageType", "")

    text = _extract_text(message)
    has_media = bool(any(k in message for k in MEDIA_MESSAGE_KEYS))

    logger.info(
        "Message from %s (%s): %s%s",
        push_name or number,
        remote_jid,
        text or "",
        " [+media]" if has_media else "",
    )

    user, is_new = await _get_or_create_user(number, push_name)
    if is_new:
        logger.info("New number %s auto-registered (allowed=False).", number)

    if not user.allowed:
        logger.info("Number %s not allowed — ignoring.", number)
        return

    # Download media if present
    attachment_json: str | None = None
    if has_media:
        meta = await download_and_save(data)
        if meta:
            attachment_json = json.dumps(meta)
        else:
            # Fallback: note the type without file
            attachment_json = json.dumps({
                "path": None,
                "name": message_type or "media",
                "mime": "",
                "size": 0,
            })

    await _store_message(user.id, text, attachment_json)

    # Phase 1 auto-reply (text messages only; skip if media-only)
    if text:
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
