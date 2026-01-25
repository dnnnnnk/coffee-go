

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import wraps
from typing import Any, Awaitable, Callable, Optional, TypeVar

from starlette.requests import Request
from starlette.responses import RedirectResponse, Response


# --- session keys ---
SESSION_KEY_CAFE_ID = "cafe_id"
SESSION_KEY_ADMIN_UNTIL = "admin_until"  # ISO datetime


T = TypeVar("T")
Endpoint = Callable[..., Awaitable[Response]]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_admin_until(request: Request) -> Optional[datetime]:
    raw = request.session.get(SESSION_KEY_ADMIN_UNTIL)
    if not raw:
        return None
    try:
        # stored as ISO, e.g. 2026-01-26T12:34:56.000000+00:00
        dt = datetime.fromisoformat(str(raw))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def get_cafe_id(request: Request) -> Optional[int]:
    """Return current cafe_id from session or None."""
    v = request.session.get(SESSION_KEY_CAFE_ID)
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return None


def is_admin(request: Request) -> bool:
    """Admin unlock is time-based: valid while admin_until > now."""
    until = _get_admin_until(request)
    if not until:
        return False
    return until > _utcnow()


def set_session(request: Request, cafe_id: int) -> None:
    """Login as cafe. Drops previous admin unlock."""
    request.session[SESSION_KEY_CAFE_ID] = int(cafe_id)
    request.session.pop(SESSION_KEY_ADMIN_UNTIL, None)


def clear_session(request: Request) -> None:
    """Full logout."""
    request.session.clear()


def set_admin(request: Request, value: bool, *, ttl_min: int = 30) -> None:
    """Enable/disable admin mode for current session."""
    if not value:
        request.session.pop(SESSION_KEY_ADMIN_UNTIL, None)
        return

    ttl_min = int(ttl_min) if ttl_min and int(ttl_min) > 0 else 30
    until = _utcnow() + timedelta(minutes=ttl_min)
    request.session[SESSION_KEY_ADMIN_UNTIL] = until.isoformat()


def login_required(redirect_to: str = "/login") -> Callable[[Endpoint], Endpoint]:
    """Decorator for Starlette/FastAPI endpoints.

    Your endpoint MUST accept `request: Request` argument.
    """

    def deco(fn: Endpoint) -> Endpoint:
        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Response:
            request = kwargs.get("request")
            if request is None:
                # try to find Request among positional args
                for a in args:
                    if isinstance(a, Request):
                        request = a
                        break
            if request is None or not isinstance(request, Request):
                # programmer error: endpoint without request
                return RedirectResponse(url=redirect_to, status_code=303)

            if get_cafe_id(request) is None:
                return RedirectResponse(url=redirect_to, status_code=303)

            return await fn(*args, **kwargs)

        return wrapper

    return deco


def admin_required(
    redirect_to: str = "/dashboard",
    *,
    login_redirect_to: str = "/login",
) -> Callable[[Endpoint], Endpoint]:
    """Decorator: requires login + admin unlock."""

    def deco(fn: Endpoint) -> Endpoint:
        @wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Response:
            request = kwargs.get("request")
            if request is None:
                for a in args:
                    if isinstance(a, Request):
                        request = a
                        break
            if request is None or not isinstance(request, Request):
                return RedirectResponse(url=login_redirect_to, status_code=303)

            if get_cafe_id(request) is None:
                return RedirectResponse(url=login_redirect_to, status_code=303)

            if not is_admin(request):
                return RedirectResponse(url=redirect_to, status_code=303)

            return await fn(*args, **kwargs)

        return wrapper

    return deco