import json
import os
import secrets
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database import AsyncSessionLocal, Message, Setting, ToolConfig, User, UserRole
from whatsapp import MEDIA_DIR, save_upload, send_media, send_text

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")

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


def _parse_attachment(raw: str | None) -> dict | None:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {"name": str(raw), "mime": "", "path": None, "size": 0}


# ---------------------------------------------------------------------------
# Media serving
# ---------------------------------------------------------------------------

@router.get("/media/{filename}")
async def serve_media(filename: str, _: str = Depends(require_auth)):
    file_path = MEDIA_DIR / filename
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)


# ---------------------------------------------------------------------------
# Root
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
    users_result = await db.execute(select(User).order_by(User.created_at.desc()))
    users = users_result.scalars().all()
    roles_result = await db.execute(select(UserRole).order_by(UserRole.name))
    roles = roles_result.scalars().all()
    return templates.TemplateResponse(
        "users.html", {"request": request, "users": users, "roles": roles, "error": error}
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
    db.add(User(number=number, role=role, allowed=bool(allowed), status="active"))
    await db.commit()
    return RedirectResponse(url="/users", status_code=303)


@router.post("/users/{user_id}/toggle")
async def toggle_user(user_id: str, db: AsyncSession = Depends(get_db), _: str = Depends(require_auth)):
    r = await db.execute(select(User).where(User.id == user_id))
    user = r.scalar_one_or_none()
    if user:
        user.allowed = not user.allowed
        await db.commit()
    return RedirectResponse(url="/users", status_code=303)


@router.post("/users/{user_id}/role")
async def change_user_role(
    user_id: str,
    role: str = Form(...),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    r = await db.execute(select(User).where(User.id == user_id))
    user = r.scalar_one_or_none()
    if user:
        user.role = role
        await db.commit()
    return RedirectResponse(url="/users", status_code=303)


@router.post("/users/{user_id}/delete")
async def delete_user(user_id: str, db: AsyncSession = Depends(get_db), _: str = Depends(require_auth)):
    r = await db.execute(select(User).where(User.id == user_id))
    user = r.scalar_one_or_none()
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
    messages = [
        {"message": m, "user": u, "attachment": _parse_attachment(m.attachment)}
        for m, u in rows
    ]

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
    text: str = Form(""),
    user_id: str = Form(...),
    file: Optional[UploadFile] = File(None),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    clean = number.strip().lstrip("+")
    jid = clean if "@" in clean else f"{clean}@s.whatsapp.net"
    attachment_json: str | None = None
    has_file = file and file.filename

    if has_file:
        file_bytes = await file.read()
        mimetype = file.content_type or "application/octet-stream"
        meta = save_upload(file_bytes, mimetype, file.filename)
        await send_media(jid, file_bytes, mimetype, file.filename, caption=text)
        attachment_json = json.dumps(meta)
    elif text:
        await send_text(jid, text)

    if text or has_file:
        db.add(Message(
            user_id=user_id,
            message=text or None,
            attachment=attachment_json,
            direction="outbound",
            status="sent",
        ))
        await db.commit()

    return RedirectResponse(url=f"/messages?user_id={user_id}", status_code=303)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@router.get("/settings", response_class=HTMLResponse)
async def admin_settings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    result = await db.execute(select(Setting).order_by(Setting.category, Setting.key))
    settings_list = result.scalars().all()
    settings = {s.key: s for s in settings_list}

    roles_result = await db.execute(select(UserRole).order_by(UserRole.name))
    roles = roles_result.scalars().all()

    # Group settings by category
    categories: dict[str, list] = {}
    for s in settings_list:
        categories.setdefault(s.category, []).append(s)

    return templates.TemplateResponse(
        "settings.html",
        {"request": request, "settings": settings, "categories": categories, "roles": roles},
    )


@router.post("/settings/save")
async def save_settings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    form = await request.form()
    for key, value in form.items():
        if key.startswith("_"):
            continue
        r = await db.execute(select(Setting).where(Setting.key == key))
        setting = r.scalar_one_or_none()
        if setting:
            setting.value = str(value)
        else:
            db.add(Setting(key=key, value=str(value)))
    await db.commit()
    return RedirectResponse(url="/settings?saved=1", status_code=303)


@router.post("/settings/roles/{role_name}")
async def save_role(
    role_name: str,
    system_prompt: str = Form(""),
    display_name: str = Form(""),
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    r = await db.execute(select(UserRole).where(UserRole.name == role_name))
    role = r.scalar_one_or_none()
    if role:
        role.system_prompt = system_prompt
        if display_name:
            role.display_name = display_name
        await db.commit()
    return RedirectResponse(url="/settings?saved=1#roles", status_code=303)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@router.get("/tools", response_class=HTMLResponse)
async def admin_tools(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    result = await db.execute(select(ToolConfig).order_by(ToolConfig.name))
    tools = result.scalars().all()
    return templates.TemplateResponse("tools.html", {"request": request, "tools": tools})


@router.post("/tools/{name}/toggle")
async def toggle_tool(name: str, db: AsyncSession = Depends(get_db), _: str = Depends(require_auth)):
    r = await db.execute(select(ToolConfig).where(ToolConfig.name == name))
    tool = r.scalar_one_or_none()
    if tool:
        tool.enabled = not tool.enabled
        await db.commit()
    return RedirectResponse(url="/tools", status_code=303)


@router.post("/tools/{name}/config")
async def update_tool_config(
    name: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _: str = Depends(require_auth),
):
    body = await request.json()
    config_str = json.dumps(body, ensure_ascii=False, indent=2)
    r = await db.execute(select(ToolConfig).where(ToolConfig.name == name))
    tool = r.scalar_one_or_none()
    if not tool:
        raise HTTPException(status_code=404)
    tool.config = config_str
    await db.commit()
    return JSONResponse({"status": "saved"})


@router.get("/tools/{name}/config")
async def get_tool_config(name: str, db: AsyncSession = Depends(get_db), _: str = Depends(require_auth)):
    r = await db.execute(select(ToolConfig).where(ToolConfig.name == name))
    tool = r.scalar_one_or_none()
    if not tool:
        raise HTTPException(status_code=404)
    try:
        return JSONResponse(json.loads(tool.config or "{}"))
    except Exception:
        return JSONResponse({})
