import os
import secrets
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AsyncSessionLocal, Message, User

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
EVOLUTION_API_URL = os.getenv("EVOLUTION_API_URL", "http://evolution-api:8080")
EVOLUTION_API_KEY = os.getenv("EVOLUTION_API_KEY", "")
INSTANCE_NAME = os.getenv("INSTANCE_NAME", "STC")

security = HTTPBasic()
templates = Jinja2Templates(directory="templates")
router = APIRouter()


def require_auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    valid = secrets.compare_digest(
        credentials.username.encode(), ADMIN_USERNAME.encode()
    ) and secrets.compare_digest(
        credentials.password.encode(), ADMIN_PASSWORD.encode()
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            headers={"WWW-Authenticate": "Basic"},
            detail="Unauthorized",
        )
    return credentials.username


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


# ---------------------------------------------------------------------------
# Root → Users
# ---------------------------------------------------------------------------

@router.get("/", response_class=RedirectResponse)
async def root(_: str = Depends(require_auth)):
    return RedirectResponse(url="/users")


# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------

@router.get("/users", response_class=HTMLResponse)
async def admin_users(
    request: Request,
    error: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    users = result.scalars().all()
    return templates.TemplateResponse(
        "users.html", {"request": request, "users": users, "error": error}
    )


@router.post("/users/add")
async def add_user(
    number: str = Form(...),
    role: str = Form("user"),
    allowed: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    number = number.strip().lstrip("+").replace(" ", "").replace("-", "")
    existing = await db.execute(select(User).where(User.number == number))
    if existing.scalar_one_or_none():
        return RedirectResponse(url="/users?error=exists", status_code=303)

    user = User(number=number, role=role, allowed=bool(allowed), status="active")
    db.add(user)
    await db.commit()
    return RedirectResponse(url="/users", status_code=303)


@router.post("/users/{user_id}/toggle")
async def toggle_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user:
        user.allowed = not user.allowed
        await db.commit()
    return RedirectResponse(url="/users", status_code=303)


@router.post("/users/{user_id}/delete")
async def delete_user(
    user_id: str,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if user:
        await db.delete(user)
        await db.commit()
    return RedirectResponse(url="/users", status_code=303)


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

@router.get("/messages", response_class=HTMLResponse)
async def admin_messages(
    request: Request,
    user_id: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    users_result = await db.execute(select(User).order_by(User.number))
    users = users_result.scalars().all()

    query = select(Message, User).join(User, Message.user_id == User.id)
    if user_id:
        query = query.where(Message.user_id == user_id)
    query = query.order_by(Message.created_at.desc()).limit(200)

    rows = (await db.execute(query)).all()
    messages = [{"message": m, "user": u} for m, u in rows]

    selected_user = None
    if user_id:
        r = await db.execute(select(User).where(User.id == user_id))
        selected_user = r.scalar_one_or_none()

    return templates.TemplateResponse(
        "messages.html",
        {
            "request": request,
            "messages": messages,
            "users": users,
            "selected_user": selected_user,
            "user_id": user_id,
        },
    )


@router.post("/reply")
async def send_reply(
    number: str = Form(...),
    text: str = Form(...),
    user_id: str = Form(...),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    jid = number if "@" in number else f"{number}@s.whatsapp.net"
    url = f"{EVOLUTION_API_URL}/message/sendText/{INSTANCE_NAME}"
    headers = {"apikey": EVOLUTION_API_KEY, "Content-Type": "application/json"}

    async with httpx.AsyncClient(timeout=30) as client:
        await client.post(url, json={"number": jid, "text": text}, headers=headers)

    msg = Message(user_id=user_id, message=text, direction="outbound", status="sent")
    db.add(msg)
    await db.commit()

    return RedirectResponse(url=f"/messages?user_id={user_id}", status_code=303)
