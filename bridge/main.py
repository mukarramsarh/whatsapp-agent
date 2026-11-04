import asyncio
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from sqlalchemy import select

from database import AsyncSessionLocal, Message, Setting, ToolConfig, UserRole, User, init_db
from admin import router as admin_router
from whatsapp import MEDIA_DIR, MEDIA_MESSAGE_KEYS, download_and_save, send_media, send_text, send_voice
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
# Shared secret Evolution API sends as the X-Webhook-Secret header; /webhook
# rejects anything without it (the bridge sits behind a public domain).
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
# Numbers (comma separated) that are auto-allowed as admins on first sight.
INITIAL_ALLOWED_NUMBERS = {n.strip().lstrip("+") for n in os.getenv("INITIAL_ALLOWED_NUMBERS", "").split(",") if n.strip()}


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

async def _ensure_instance() -> bool:
    """Create the WhatsApp instance in Evolution API if it doesn't exist yet,
    so a fresh deployment needs no manual create_instance.sh step."""
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.get(f"{EVOLUTION_API_URL}/instance/fetchInstances", headers=headers)
        r.raise_for_status()
        existing = set()
        for item in r.json() or []:
            name = item.get("name") or (item.get("instance") or {}).get("instanceName")
            if name:
                existing.add(name)
        if INSTANCE_NAME in existing:
            return True
        r = await client.post(
            f"{EVOLUTION_API_URL}/instance/create",
            json={"instanceName": INSTANCE_NAME, "qrcode": True, "integration": "WHATSAPP-BAILEYS"},
            headers=headers,
        )
        if r.status_code in (200, 201):
            logger.info("Created WhatsApp instance '%s' — open /connect to scan the QR code.", INSTANCE_NAME)
            return True
        logger.warning("Instance create: HTTP %d — %s", r.status_code, r.text[:200])
        return False


async def _register_webhook(retries: int = 15, delay: float = 8.0) -> None:
    url = f"{EVOLUTION_API_URL}/webhook/set/{INSTANCE_NAME}"
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}
    webhook = {
        "enabled": True,
        "url": WEBHOOK_URL,
        "webhookByEvents": False,
        "webhookBase64": False,
        "events": ["MESSAGES_UPSERT", "CONNECTION_UPDATE", "QRCODE_UPDATED"],
    }
    if WEBHOOK_SECRET:
        webhook["headers"] = {"X-Webhook-Secret": WEBHOOK_SECRET}
    payload = {"webhook": webhook}
    for attempt in range(1, retries + 1):
        await asyncio.sleep(delay)
        try:
            if not await _ensure_instance():
                continue
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
        bootstrap = number in INITIAL_ALLOWED_NUMBERS
        user = User(
            number=number, name=name, allowed=bootstrap, status="active",
            role="admin" if bootstrap else "user",
        )
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


async def _recent_attachment(user_id: str, minutes: int) -> dict | None:
    """The user's most recent non-audio attachment from the last `minutes`,
    so 'make it black and white' (typed or spoken) still refers to the photo
    they sent a moment ago."""
    since = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    async with AsyncSessionLocal() as db:
        r = await db.execute(
            select(Message)
            .where(Message.user_id == user_id, Message.direction == "inbound",
                   Message.attachment.is_not(None), Message.created_at >= since)
            .order_by(Message.created_at.desc())
            .limit(10)
        )
        rows = r.scalars().all()
    for m in rows:
        try:
            meta = json.loads(m.attachment)
        except Exception:
            continue
        if meta.get("path") and not str(meta.get("mime", "")).startswith("audio/"):
            meta = dict(meta, recent=True)
            return meta
    return None


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
    """Full pipeline: context → (orchestrator → specialist agents | single agent)
    → reply as text and/or voice → deliver any files the agents produced."""
    from agent.agents import build_team
    from agent.runner import AgentRunner
    from agent.tools.base import ToolSession

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
    # to the agent as an attachment. In that case (and for plain text) fall back
    # to the photo/document the user sent a moment ago, if any.
    agent_attachment = None if is_voice else attachment_meta
    if agent_attachment is None:
        minutes = int(settings.get("recent_attachment_minutes", "30"))
        if minutes > 0:
            agent_attachment = await _recent_attachment(user.id, minutes)

    voice_enabled = settings.get("voice_enabled", "false").lower() == "true"
    voice_reply = is_voice and voice_enabled
    session = ToolSession(remote_jid=remote_jid, language=language, voice_reply=voice_reply)

    runner = None
    if settings.get("multi_agent_enabled", "true").lower() == "true":
        runner = build_team(
            settings, tools, user.role, context_msgs, agent_attachment, language, voice_reply,
        )
    if runner is None:
        runner = AgentRunner(settings=settings, tools=tools)

    result = await runner.run(
        user_message=user_message,
        context_messages=context_msgs,
        role_prompt=role_prompt,
        language=language,
        attachment_meta=agent_attachment,
        session=session,
        is_voice=voice_reply,
    )

    # Convert the model's Markdown into WhatsApp-safe formatting before sending.
    reply_text = to_whatsapp(result.content)
    logger.info(
        "Agent replied (%s, voice=%s, conf=%.2f, tools=%s, iters=%d, files=%d): %d chars",
        language, voice_reply, result.confidence,
        result.tool_calls_made or "none", result.iterations,
        len(result.attachments), len(reply_text),
    )

    # --- Deliver the reply -------------------------------------------------
    # Voice in -> voice out. Text is added only when the admin chose
    # "voice_and_text", or when the answer is long (a spoken wall of text is
    # unusable), or when text-to-speech is unavailable.
    sent_text = False
    if voice_reply:
        audio = await voice_module.synthesize(to_plain(reply_text), language)
        sent_voice = bool(audio) and await send_voice(remote_jid, audio)
        mode = settings.get("voice_reply_mode", "voice_only")
        if not sent_voice or mode == "voice_and_text" or len(reply_text) > 700:
            await send_text(remote_jid, reply_text)
            sent_text = True
        if not sent_voice:
            logger.info("Voice reply unavailable — fell back to text.")
    else:
        await send_text(remote_jid, reply_text)
        sent_text = True

    # Files produced by tools/agents (edited photo, generated image, PDF, .pptx)
    for data, name, mime in result.attachments:
        ok = await send_media(remote_jid, data, mime or "application/octet-stream", name or "file")
        logger.info("Sent tool attachment: %s (%d bytes) ok=%s", name, len(data), ok)

    # Persist outbound message (store what the user actually received)
    stored = reply_text if sent_text else f"[voice] {to_plain(reply_text)}"
    out_id = await _store_message(user.id, stored, None, "outbound", "sent")
    asyncio.create_task(_embed_message(out_id, to_plain(reply_text), number, "outbound"))


# ---------------------------------------------------------------------------
# Webhook processor
# ---------------------------------------------------------------------------

_SEEN_IDS: dict = {}


async def process_webhook(payload: dict) -> None:
    try:
        await _process_webhook(payload)
    except Exception:
        logger.exception("Unhandled error while processing webhook")


async def _process_webhook(payload: dict) -> None:
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

    # Evolution API can redeliver a webhook; process each WhatsApp message once.
    msg_key = key.get("id")
    if msg_key:
        if msg_key in _SEEN_IDS:
            return
        _SEEN_IDS[msg_key] = None
        while len(_SEEN_IDS) > 2000:
            _SEEN_IDS.pop(next(iter(_SEEN_IDS)))

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
            if not transcript:
                await send_text(
                    remote_jid,
                    "عذرًا، لم أتمكن من فهم الرسالة الصوتية. يرجى إعادة إرسالها أو كتابتها.\n"
                    "Sorry, I couldn't understand that voice note. Please send it again or type it.",
                )
                await _store_message(user.id, None, attachment_json)
                return

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
    if WEBHOOK_SECRET:
        supplied = request.headers.get("x-webhook-secret", "")
        if not hmac.compare_digest(supplied.encode(), WEBHOOK_SECRET.encode()):
            raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
    background_tasks.add_task(process_webhook, payload)
    return {"status": "ok"}


@app.get("/health")
async def health():
    return {"status": "healthy", "instance": INSTANCE_NAME, "bridge": "phase-3-multi-agent"}
