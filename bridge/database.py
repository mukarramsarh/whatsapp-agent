import json
import os
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, String, Text, func, select, text
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def _db_url() -> URL:
    return URL.create(
        drivername="postgresql+asyncpg",
        username=os.getenv("POSTGRES_USER", "postgres"),
        password=os.getenv("POSTGRES_PASSWORD", ""),
        host=os.getenv("POSTGRES_HOST", "postgres"),
        port=int(os.getenv("POSTGRES_PORT", "5432")),
        database=os.getenv("POSTGRES_DB", "evolution"),
    )


engine = create_async_engine(_db_url(), echo=False, pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Existing tables
# ---------------------------------------------------------------------------

class User(Base):
    __tablename__ = "wa_users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    number: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    role: Mapped[str] = mapped_column(String(20), default="user")
    allowed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Message(Base):
    __tablename__ = "wa_messages"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    user_id: Mapped[str] = mapped_column(String(36), ForeignKey("wa_users.id"), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=True)
    attachment: Mapped[str] = mapped_column(Text, nullable=True)
    direction: Mapped[str] = mapped_column(String(10), default="inbound")
    status: Mapped[str] = mapped_column(String(20), default="received")
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# Phase 2: settings, roles, tools, embeddings, knowledge base
# ---------------------------------------------------------------------------

class Setting(Base):
    """Key-value store for all admin-configurable settings."""
    __tablename__ = "wa_settings"

    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    category: Mapped[str] = mapped_column(String(30), default="general")
    description: Mapped[str] = mapped_column(String(200), nullable=True)
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class UserRole(Base):
    """Role definitions with per-role system prompt and tool access."""
    __tablename__ = "wa_user_roles"

    name: Mapped[str] = mapped_column(String(30), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(60), nullable=False)
    system_prompt: Mapped[str] = mapped_column(Text, nullable=True)
    allowed_tools: Mapped[str] = mapped_column(Text, nullable=True)  # JSON list or null = all
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ToolConfig(Base):
    """Per-tool enable/disable switch and JSON configuration."""
    __tablename__ = "wa_tool_configs"

    name: Mapped[str] = mapped_column(String(60), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(80), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[str] = mapped_column(Text, default="{}")  # JSON
    updated_at: Mapped[DateTime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class MessageEmbedding(Base):
    """Vector embeddings for message history (stored as JSON for portability)."""
    __tablename__ = "wa_message_embeddings"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    message_id: Mapped[str] = mapped_column(String(36), ForeignKey("wa_messages.id"), nullable=False)
    number: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    direction: Mapped[str] = mapped_column(String(10), default="inbound")
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_json: Mapped[str] = mapped_column(Text, nullable=True)  # JSON float list
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KnowledgeDoc(Base):
    """Knowledge base documents for the library search tool."""
    __tablename__ = "wa_knowledge_docs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    source: Mapped[str] = mapped_column(String(200), nullable=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_json: Mapped[str] = mapped_column(Text, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

def _env_default(env_var: str, fallback: str) -> str:
    """Seed value for a setting: use the env var if set, else the hardcoded fallback.

    Only consulted the first time a setting row is created — after that the DB
    value (editable in /settings) is the source of truth, matching the rest of
    the settings table.
    """
    return os.getenv(env_var) or fallback


DEFAULT_SETTINGS: list[tuple[str, str, str, str]] = [
    # (key, value, category, description)
    ("master_prompt",
     "You are a highly intelligent AI assistant for ProcurementHub. "
     "Help users accurately and professionally with procurement, documents, data, and more. "
     "Use your available tools when appropriate. If you need more information, ask the user.",
     "ai", "Main system prompt injected into every conversation"),
    ("ai_base_url",        _env_default("AI_BASE_URL", "http://192.168.100.62:8002/v1"), "ai", "OpenAI-compatible LLM base URL (seeded from AI_BASE_URL on first boot)"),
    ("ai_model",           _env_default("AI_MODEL", "gpt-oss:20b"),                 "ai", "Model name to use for inference (seeded from AI_MODEL on first boot)"),
    ("ai_api_key",         _env_default("AI_API_KEY", "none"),                      "ai", "API key for the LLM endpoint (seeded from AI_API_KEY on first boot)"),
    ("ai_max_retries",     _env_default("AI_MAX_RETRIES", "4"),                     "ai", "Max retries with backoff on 429/5xx from the LLM"),
    ("agent_max_iterations", "10",                          "ai",         "Max ReAct tool-calling iterations per request"),
    ("context_recent_count",  "20",   "context",  "Number of most-recent messages to include as context"),
    ("context_old_count",     "5",    "context",  "Number of historical (vector-retrieved) messages to include"),
    ("context_vector_count",  "5",    "context",  "Number of semantically similar messages to fetch via vector search"),
    ("confidence_enabled",    "false","confidence","Enable confidence scoring layer"),
    ("confidence_threshold",  "0.7",  "confidence","Minimum confidence score (0.0–1.0) to accept a response"),
    ("confidence_max_retries","2",    "confidence","Max retries when confidence is below threshold"),
    ("voice_enabled",         "false","voice",     "Enable voice message transcription and TTS replies"),
]

DEFAULT_ROLES: list[tuple[str, str, str]] = [
    ("user",  "Regular User",  "You are helping a regular user. Be friendly, clear, and helpful."),
    ("admin", "Administrator", "You are assisting an admin user who may ask about system status, users, and configurations."),
    ("vip",   "VIP User",      "You are assisting a VIP user. Prioritize their requests and provide extra detail."),
    ("staff", "Staff Member",  "You are assisting a staff member. They may need help with internal processes."),
]


async def _seed(db: AsyncSession) -> None:
    from agent.tools import ALL_TOOL_CLASSES

    for key, value, category, description in DEFAULT_SETTINGS:
        r = await db.execute(select(Setting).where(Setting.key == key))
        if not r.scalar_one_or_none():
            db.add(Setting(key=key, value=value, category=category, description=description))

    for name, display_name, prompt in DEFAULT_ROLES:
        r = await db.execute(select(UserRole).where(UserRole.name == name))
        if not r.scalar_one_or_none():
            db.add(UserRole(name=name, display_name=display_name, system_prompt=prompt))

    for cls in ALL_TOOL_CLASSES:
        r = await db.execute(select(ToolConfig).where(ToolConfig.name == cls.name))
        if not r.scalar_one_or_none():
            db.add(ToolConfig(
                name=cls.name,
                display_name=cls.display_name,
                description=cls.description,
                enabled=True,
                config="{}",
            ))

    await db.commit()


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as db:
        await _seed(db)
