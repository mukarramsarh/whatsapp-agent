import secrets

from fastapi import Header, HTTPException

from app.config import get_settings


async def require_api_key(authorization: str = Header(default="")) -> None:
    settings = get_settings()
    prefix = "Bearer "
    if not authorization.startswith(prefix):
        raise HTTPException(
            status_code=401,
            detail={"error": "missing or malformed Authorization header", "code": "MISSING_TOKEN"},
        )
    token = authorization[len(prefix):].strip()
    if not secrets.compare_digest(token, settings.gateway_api_key):
        raise HTTPException(
            status_code=401,
            detail={"error": "invalid API key", "code": "INVALID_TOKEN"},
        )
