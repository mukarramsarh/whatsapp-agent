"""Shared helper for tools whose upstream job is slow enough that blocking
the ReAct loop for it would leave WhatsApp silent for minutes.

A tool using this should still return its own immediate ToolResult right
away (e.g. "started, I'll message you shortly") through the normal reply
path -- that's what makes the ack fast. This helper only handles the LATER,
out-of-band follow-up: it runs poll_and_build() in the background and sends
whatever it returns as a separate WhatsApp message once the job actually
finishes, decoupled from the request that kicked it off.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable

from agent.tools.base import ToolSession
from whatsapp import send_media, send_text

logger = logging.getLogger("bridge.async_job")

# (text, file_bytes, file_name, file_mime) -- file fields may all be None.
PollResult = tuple[str, "bytes | None", "str | None", "str | None"]


def start_background_job(
    session: ToolSession | None,
    poll_and_build: Callable[[], Awaitable[PollResult]],
) -> None:
    """Fire-and-forget: schedules poll_and_build() to run in the background
    and sends its result as a follow-up WhatsApp message once it resolves.
    No-ops (logs a warning) if session is None -- there's no one to message,
    e.g. when a tool is exercised outside a real WhatsApp request."""
    if session is None:
        logger.warning("start_background_job called with no session — nothing will be sent")
        return

    async def _run() -> None:
        try:
            text, file_bytes, file_name, file_mime = await poll_and_build()
        except Exception as exc:
            logger.error("background job failed: %s", exc)
            await send_text(session.remote_jid, f"Sorry, something went wrong while processing that: {exc}")
            return
        if text:
            await send_text(session.remote_jid, text)
        if file_bytes:
            await send_media(
                session.remote_jid,
                file_bytes,
                file_mime or "application/octet-stream",
                file_name or "file",
            )

    asyncio.create_task(_run())
