import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from sqlalchemy import select

from database import AsyncSessionLocal, Message, Setting, ToolConfig, UserRole, User, init_db
from admin import router as admin_router
from whatsapp import MEDIA_DIR, MEDIA_MESSAGE_KEYS, download_and_save, send_media, send_text
from wa_format import to_plain, to_whatsapp
from agent.tools import ALL_TOOL_CLASSES
import agent.context as ctx_builder
import agent.language as lang_module
import voice as voice_module

load_dotenv()

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("bridge")

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
            logger.warning("Webhook %d/%d: HTTP %d — %s", attempt, retries, r.status_code, r.text[:200])
        except Exception as exc:
            logger.warning("Webhook %d/%d failed: %s", attempt, retries, exc)
    logger.error("Could not register webhook after %d attempts.", retries)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    asyncio.create_task(_register_webhook())
    yield


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="WhatsApp AI Agent — Bridge", version="2.0.0", lifespan=lifespan)
app.include_router(admin_router)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_text(message: dict) -> str:
    return (
        message.get("conversation")
        or (message.get("extendedTextMessage") or {}).get("text")
        or (message.get("imageMessage") or {}).get("caption")
        or (message.get("videoMessage") or {}).get("caption")
        or (message.get("documentMessage") or {}).get("caption")
        or (
            (message.get("documentWithCaptionMessage", {}).get("message", {}).get("documentMessage") or {})
            .get("caption")
        )
        or ""
    )


async def _load_settings() -> dict:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(Setting))
        return {s.key: s.value for s in result.scalars().all()}


async def _load_tools(settings: dict) -> list:
    """Instantiate enabled tools with their configs."""
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(ToolConfig).where(ToolConfig.enabled == True))
        configs = {tc.name: json.loads(tc.config or "{}") for tc in result.scalars().all()}

    tools = []
    for cls in ALL_TOOL_CLASSES:
        if cls.name in configs:
            tools.append(cls(config=configs[cls.name]))
    return tools


async def _get_role_prompt(role_name: str) -> str:
    async with AsyncSessionLocal() as db:
        r = await db.execute(select(UserRole).where(UserRole.name == role_name))
        role = r.scalar_one_or_none()
        return role.system_prompt or "" if role else ""


async def _get_or_create_user(number: str, name: str | None) -> tuple[User, bool]:
    async with AsyncSessionLocal() as db:
        r = await db.execute(select(User).where(User.number == number))
        user = r.scalar_one_or_none()
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
    user_id: str,
    text: str | None,
    attachment_json: str | None,
    direction: str = "inbound",
    status: str = "received",
) -> str:
    async with AsyncSessionLocal() as db:
        msg = Message(
            user_id=user_id,
            message=text or None,
            attachment=attachment_json,
            direction=direction,
            status=status,
        )
        db.add(msg)
        await db.commit()
        await db.refresh(msg)
        return msg.id


async def _update_attachment(msg_id: str, attachment_json: str) -> None:
    async with AsyncSessionLocal() as db:
        r = await db.execute(select(Message).where(Message.id == msg_id))
        msg = r.scalar_one_or_none()
        if msg:
            msg.attachment = attachment_json
            await db.commit()


async def _embed_message(msg_id: str, content: str, number: str, direction: str = "inbound") -> None:
    try:
        import embeddings
        await embeddings.store_message_embedding(msg_id, content, number, direction)
    except Exception as exc:
        logger.debug("Embedding skipped: %s", exc)


# ---------------------------------------------------------------------------
# Core agent pipeline
# ---------------------------------------------------------------------------

async def _run_agent_pipeline(
    user_message: str,
    number: str,
    user: User,
    remote_jid: str,
    attachment_meta: dict | None,
    is_voice: bool,
) -> None:
    """Full ReAct pipeline: context → agent → confidence → reply."""
    from agent.runner import AgentRunner

    settings = await _load_settings()
    tools = await _load_tools(settings)
    role_prompt = await _get_role_prompt(user.role)
    language = lang_module.detect(user_message)

    context_msgs = await ctx_builder.build(
        number=number,
        query=user_message,
        recent_count=int(settings.get("context_recent_count", "20")),
        old_count=int(settings.get("context_old_count", "5")),
        vector_count=int(settings.get("context_vector_count", "5")),
    )

    # For a voice note the transcript IS the message — don't hand the audio file
    # to the agent as an attachment, or it treats the request as "process a file".
    agent_attachment = None if is_voice else attachment_meta

    runner = AgentRunner(settings=settings, tools=tools)
    result = await runner.run(
        user_message=user_message,
        context_messages=context_msgs,
        role_prompt=role_prompt,
        language=language,
        attachment_meta=agent_attachment,
    )

    # Convert the model's Markdown into WhatsApp-safe formatting before sending.
    reply_text = to_whatsapp(result.content)
    logger.info(
        "Agent replied (%s, conf=%.2f, tools=%s, iters=%d): %d chars",
        language, result.confidence,
        result.tool_calls_made or "none",
        result.iterations,
        len(reply_text),
    )

    # Deliver reply. Always send text; for a voice note, also send a spoken version.
    await send_text(remote_jid, reply_text)
    voice_enabled = settings.get("voice_enabled", "false").lower() == "true"
    if is_voice and voice_enabled:
        audio = await voice_module.synthesize(to_plain(reply_text), language)
        if audio:
            await send_media(remote_jid, audio, "audio/mpeg", "reply.mp3")
        else:
            logger.info("Voice reply skipped — TTS unavailable; text already sent.")

    # Persist outbound message (store what the user actually saw)
    out_id = await _store_message(user.id, reply_text, None, "outbound", "sent")
    asyncio.create_task(_embed_message(out_id, reply_text, number, "outbound"))


# ---------------------------------------------------------------------------
# Webhook processor
# ---------------------------------------------------------------------------

async def process_webhook(payload: dict) -> None:
    event: str = payload.get("event", "")

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
        return  # ignore groups

    number = remote_jid.replace("@s.whatsapp.net", "").replace("@c.us", "")
    push_name: str | None = data.get("pushName") or None
    message: dict = data.get("message") or {}
    message_type: str = data.get("messageType", "")

    text = _extract_text(message)
    has_media = any(k in message for k in MEDIA_MESSAGE_KEYS)
    is_audio = "audioMessage" in message

    logger.info(
        "Inbound from %s (%s)%s%s",
        push_name or number, remote_jid,
        f": {text}" if text else "",
        " [+media]" if has_media else "",
    )

    user, is_new = await _get_or_create_user(number, push_name)
    if is_new:
        logger.info("New number %s auto-registered (allowed=False).", number)

    if not user.allowed:
        logger.info("Number %s not allowed — ignoring.", number)
        return

    # --- Download media ---
    attachment_meta: dict | None = None
    attachment_json: str | None = None
    if has_media:
        meta = await download_and_save(data)
        if meta:
            attachment_meta = meta
            attachment_json = json.dumps(meta)
        else:
            attachment_json = json.dumps({"path": None, "name": message_type or "media", "mime": "", "size": 0})

    # --- Voice transcription ---
    is_voice = False
    if is_audio and attachment_meta and attachment_meta.get("path"):
        settings_check = await _load_settings()
        if settings_check.get("voice_enabled", "false").lower() == "true":
            audio_file = MEDIA_DIR / attachment_meta["path"].split("/", 1)[-1]
            transcript = await voice_module.transcribe(audio_file)
            if transcript:
                text = transcript
                is_voice = True
                logger.info("Voice transcribed: %s", text[:100])
            attachment_meta["transcript"] = transcript or ""
            attachment_json = json.dumps(attachment_meta)

    # --- Store inbound message ---
    msg_id = await _store_message(user.id, text, attachment_json)

    # --- Embed inbound message (background) ---
    if text:
        asyncio.create_task(_embed_message(msg_id, text, number, "inbound"))

    # --- Skip agent if no usable content ---
    if not text and not attachment_meta:
        logger.info("No text or media — nothing to process.")
        return

    # --- Run agent pipeline ---
    await _run_agent_pipeline(
        user_message=text,
        number=number,
        user=user,
        remote_jid=remote_jid,
        attachment_meta=attachment_meta,
        is_voice=is_voice,
    )


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
    return {"status": "healthy", "instance": INSTANCE_NAME, "bridge": "phase-2"}
