"""Database query tool — safe read-only queries into the internal DB."""
from __future__ import annotations

from agent.tools.base import Tool, ToolResult


class DatabaseTool(Tool):
    name = "query_database"
    display_name = "Database — Internal Query"
    description = (
        "Query the internal WhatsApp agent database. "
        "Use to retrieve user information, message history counts, or system stats."
    )
    parameters_schema = {
        "type": "object",
        "properties": {
            "query_type": {
                "type": "string",
                "enum": ["user_info", "message_count", "user_list", "recent_messages"],
                "description": "Type of information to retrieve",
            },
            "phone_number": {
                "type": "string",
                "description": "Phone number (digits only) to scope the query, e.g. '923124277939'",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of records to return (default 10)",
                "default": 10,
            },
        },
        "required": ["query_type"],
    }

    async def run(self, query_type: str, phone_number: str = "", limit: int = 10, **_) -> ToolResult:
        try:
            from sqlalchemy import select, func
            from database import AsyncSessionLocal, User, Message

            async with AsyncSessionLocal() as db:
                if query_type == "user_info":
                    if not phone_number:
                        return ToolResult(success=False, error="phone_number required for user_info")
                    r = await db.execute(select(User).where(User.number == phone_number))
                    u = r.scalar_one_or_none()
                    if not u:
                        return ToolResult(success=False, error=f"No user found for {phone_number}")
                    return ToolResult(success=True, output=(
                        f"User: {u.name or 'Unknown'}\n"
                        f"Number: {u.number}\nRole: {u.role}\n"
                        f"Status: {u.status}\nAllowed: {u.allowed}\n"
                        f"Joined: {u.created_at}"
                    ))

                if query_type == "message_count":
                    q = select(func.count()).select_from(Message)
                    if phone_number:
                        ru = await db.execute(select(User).where(User.number == phone_number))
                        u = ru.scalar_one_or_none()
                        if u:
                            q = q.where(Message.user_id == u.id)
                    count = (await db.execute(q)).scalar()
                    return ToolResult(success=True, output=f"Total messages: {count}")

                if query_type == "user_list":
                    result = await db.execute(select(User).limit(limit))
                    users = result.scalars().all()
                    lines = [f"{u.number} — {u.name or 'Unknown'} ({u.role}, allowed={u.allowed})" for u in users]
                    return ToolResult(success=True, output="\n".join(lines) or "No users found.")

                if query_type == "recent_messages":
                    q = select(Message).order_by(Message.created_at.desc()).limit(limit)
                    if phone_number:
                        ru = await db.execute(select(User).where(User.number == phone_number))
                        u = ru.scalar_one_or_none()
                        if u:
                            q = q.where(Message.user_id == u.id)
                    result = await db.execute(q)
                    msgs = result.scalars().all()
                    lines = [f"[{m.direction}] {m.message or '[media]'}" for m in msgs]
                    return ToolResult(success=True, output="\n".join(lines) or "No messages found.")

            return ToolResult(success=False, error=f"Unknown query_type: {query_type}")
        except Exception as exc:
            return ToolResult(success=False, error=str(exc))
