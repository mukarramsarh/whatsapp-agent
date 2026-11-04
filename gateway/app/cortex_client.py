"""A well-mannered HTTP client for Cortex's hub.

There is no service-to-service API key anywhere in Cortex (internal services
trust an X-PH-User header the hub's own proxy injects after resolving a real
session — see services/hub/src/hub/proxy.py / mounts.py in the Cortex repo).
So this client authenticates exactly like a browser would: POST
/api/auth/login, then carry the session token hub hands back on every later
request. hub deliberately never puts that token in the JSON body (an
intentional XSS defense for browser clients) — it's only in the Set-Cookie
header, which is exactly what this client reads. hub also accepts the same
token via an X-Session-Token header instead of the cookie
(services/hub/src/hub/auth.py), which is what's used here since this is a
server-to-server client, not a browser.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

logger = logging.getLogger("cortex_client")


class CortexUpstreamError(Exception):
    def __init__(self, status_code: int, message: str, code: str = "UPSTREAM_ERROR", retry_after: Optional[str] = None):
        super().__init__(message)
        self.status_code = status_code
        self.message = message
        self.code = code
        self.retry_after = retry_after


class CortexAuthError(Exception):
    pass


class HubSession:
    def __init__(self, base_url: str, username: str, password: str, timeout: float):
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)
        self._username = username
        self._password = password
        self._token: Optional[str] = None
        self._login_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _login(self) -> None:
        async with self._login_lock:
            resp = await self._client.post(
                "/api/auth/login",
                json={"username": self._username, "password": self._password},
            )
            if resp.status_code != 200:
                raise CortexAuthError(
                    f"Cortex hub login failed for service account "
                    f"'{self._username}' ({resp.status_code}): {resp.text[:300]}"
                )
            token = resp.cookies.get("ph_session")
            if not token:
                raise CortexAuthError(
                    "Cortex hub login succeeded but returned no ph_session cookie — "
                    "has the hub's auth response shape changed?"
                )
            self._token = token
            logger.info("re-authenticated to Cortex hub as %s", self._username)

    async def _ensure_token(self) -> None:
        if self._token is None:
            await self._login()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        await self._ensure_token()
        headers = kwargs.pop("headers", {}) or {}
        headers["X-Session-Token"] = self._token
        resp = await self._client.request(method, path, headers=headers, **kwargs)
        if resp.status_code == 401:
            # Token expired/invalid — re-login once and retry once.
            self._token = None
            await self._login()
            headers["X-Session-Token"] = self._token
            resp = await self._client.request(method, path, headers=headers, **kwargs)
        return resp

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        if resp.status_code < 400:
            return
        code = "RATE_LIMITED" if resp.status_code == 429 else "UPSTREAM_ERROR"
        message = resp.text[:500]
        try:
            body = resp.json()
            message = body.get("error") or body.get("message") or message
            code = body.get("code") or code
        except ValueError:
            pass
        raise CortexUpstreamError(
            status_code=resp.status_code,
            message=message,
            code=code,
            retry_after=resp.headers.get("Retry-After"),
        )

    async def get_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        resp = await self._request("GET", path, **kwargs)
        self._raise_for_status(resp)
        return resp.json()

    async def post_json(self, path: str, json: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        resp = await self._request("POST", path, json=json, **kwargs)
        self._raise_for_status(resp)
        return resp.json()

    async def post_multipart(
        self, path: str, files: dict[str, Any], data: Optional[dict[str, Any]] = None, **kwargs: Any
    ) -> dict[str, Any]:
        resp = await self._request("POST", path, files=files, data=data or {}, **kwargs)
        self._raise_for_status(resp)
        return resp.json()

    async def get_raw(self, path: str, **kwargs: Any) -> httpx.Response:
        resp = await self._request("GET", path, **kwargs)
        self._raise_for_status(resp)
        return resp

    async def post_raw(self, path: str, **kwargs: Any) -> httpx.Response:
        """For upstream routes that don't return JSON (e.g. classifier's
        /classify-zip, which 302-redirects to /jobs/<id> the way a browser
        form post would) — the caller inspects status/headers itself instead
        of this client assuming a JSON body."""
        return await self._request("POST", path, **kwargs)
