from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import HTTPException
from starlette.requests import Request


# --- session keys ---
SESSION_KEY_CAFE_ID = "cafe_id"
SESSION_KEY_ADMIN_UNTIL = "admin_until"  # ISO datetime


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _get_admin_until(request: Request) -> Optional[datetime]:
    raw = request.session.get(SESSION_KEY_ADMIN_UNTIL)
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def get_cafe_id(request: Request) -> Optional[int]:
    v = request.session.get(SESSION_KEY_CAFE_ID)
    if v is None:
        return None
    try:
        return int(v)
    except Exception:
        return None


def is_admin(request: Request) -> bool:
    until = _get_admin_until(request)
    return bool(until and until > _utcnow())


def set_session(request: Request, cafe_id: int) -> None:
    request.session[SESSION_KEY_CAFE_ID] = int(cafe_id)
    request.session.pop(SESSION_KEY_ADMIN_UNTIL, None)


def clear_session(request: Request) -> None:
    request.session.clear()


def set_admin(request: Request, value: bool, *, ttl_min: int = 30) -> None:
    if not value:
        request.session.pop(SESSION_KEY_ADMIN_UNTIL, None)
        return

    ttl_min = int(ttl_min) if ttl_min and int(ttl_min) > 0 else 30
    until = _utcnow() + timedelta(minutes=ttl_min)
    request.session[SESSION_KEY_ADMIN_UNTIL] = until.isoformat()


# =========================
# Dependencies for FastAPI
# =========================

def login_required(redirect_to: str = "/login"):
    """
    FastAPI dependency: returns cafe_id or redirects via HTTP 303.
    Usage:
        cafe_id: int = Depends(login_required())
    """
    async def dep(request: Request) -> int:
        cafe_id = get_cafe_id(request)
        if cafe_id is None:
            raise HTTPException(status_code=303, headers={"Location": redirect_to})
        return cafe_id

    return dep


def admin_required(
    redirect_to: str = "/orders",
    *,
    login_redirect_to: str = "/login",
):
    """
    FastAPI dependency: requires login + admin unlock.
    Usage:
        cafe_id: int = Depends(admin_required())
    """
    async def dep(request: Request) -> int:
        cafe_id = get_cafe_id(request)
        if cafe_id is None:
            raise HTTPException(status_code=303, headers={"Location": login_redirect_to})

        if not is_admin(request):
            raise HTTPException(status_code=303, headers={"Location": redirect_to})

        return cafe_id

    return dep