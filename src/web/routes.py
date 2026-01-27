from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from ..config import get_settings
from .. import services, db
from .auth import (
    admin_required,
    clear_session,
    get_cafe_id,
    is_admin,
    login_required,
    set_admin,
    set_session,
)


router = APIRouter()

from fastapi.responses import RedirectResponse

@router.get("/", include_in_schema=False)
async def root(request: Request) -> RedirectResponse:
    if get_cafe_id(request) is not None:
        return _redirect("/orders")
    return RedirectResponse(url="/login", status_code=302)

def _templates(request: Request):
    return request.app.state.templates


def _redirect(url: str) -> RedirectResponse:
    # 303 is correct for redirect after POST
    return RedirectResponse(url=url, status_code=303)



@router.get("/login", response_class=HTMLResponse)
async def login_get(request: Request) -> HTMLResponse:
    if get_cafe_id(request) is not None:
        return _redirect("/orders")

    return _templates(request).TemplateResponse(
        "login.html",
        {
            "request": request,
            "error": None,
        },
    )



@router.post("/login", response_model=None)
async def login_post(
    request: Request,
    login: str = Form(...),
    password: str = Form(...),
) -> Response:
    settings = get_settings()

    async with db.session_scope(settings) as s:
        cafe_id = await services.web_auth_login(s, login=login, password=password)

    if cafe_id is None:
        return _templates(request).TemplateResponse(
            "login.html",
            {
                "request": request,
                "error": "Неверный логин или пароль.",
            },
            status_code=401,
        )

    set_session(request, cafe_id)
    return _redirect("/orders")


@router.post("/logout")
async def logout_post(request: Request) -> RedirectResponse:
    clear_session(request)
    return _redirect("/login")


@router.get("/orders", response_class=HTMLResponse)
async def orders_get(
    request: Request,
    status: Optional[str] = None,
    cafe_id: int = Depends(login_required()),
) -> HTMLResponse:
    settings = get_settings()

    async with db.session_scope(settings) as s:
        orders = await services.web_list_orders(s, cafe_id=cafe_id, status_filter=status)

    return _templates(request).TemplateResponse(
        "orders.html",
        {
            "request": request,
            "cafe_id": cafe_id,
            "admin_unlocked": is_admin(request),
            "orders": orders,
            "status_filter": status,
        },
    )


@router.post("/orders/{order_id}/status")
async def orders_status_post(
    request: Request,
    order_id: int,
    new_status: str = Form(...),
    cafe_id: int = Depends(login_required()),
) -> RedirectResponse:
    settings = get_settings()

    async with db.session_scope(settings) as s:
        await services.web_update_order_status(
            s,
            cafe_id=cafe_id,
            order_id=order_id,
            new_status=new_status,
        )

    return _redirect("/orders")


@router.get("/stoplist", response_class=HTMLResponse)
async def stoplist_get(
    request: Request,
    cafe_id: int = Depends(login_required()),
) -> HTMLResponse:
    settings = get_settings()

    async with db.session_scope(settings) as s:
        data = await services.web_get_stoplist(s, cafe_id=cafe_id)

    return _templates(request).TemplateResponse(
        "stoplist.html",
        {
            "request": request,
            "cafe_id": cafe_id,
            "admin_unlocked": is_admin(request),
            "data": data,
        },
    )


@router.post("/stoplist")
async def stoplist_post(
    request: Request,
    kind: str = Form(...),
    enabled: bool = Form(False),
    product_id: Optional[int] = Form(None),
    modifier_id: Optional[int] = Form(None),
    cafe_id: int = Depends(login_required()),
) -> RedirectResponse:
    """
    kind: 'product' | 'modifier'
    enabled: checkbox -> True/False
    """
    settings = get_settings()

    async with db.session_scope(settings) as s:
        if kind == "product":
            if product_id is None:
                return _redirect("/stoplist")
            await services.web_set_stoplist_product(s, cafe_id=cafe_id, product_id=int(product_id), enabled=bool(enabled))
        elif kind == "modifier":
            if modifier_id is None:
                return _redirect("/stoplist")
            await services.web_set_stoplist_modifier(s, cafe_id=cafe_id, modifier_id=int(modifier_id), enabled=bool(enabled))

    return _redirect("/stoplist")


@router.get("/admin/unlock", response_class=HTMLResponse)
async def admin_unlock_get(
    request: Request,
    cafe_id: int = Depends(login_required()),
) -> HTMLResponse:
    return _templates(request).TemplateResponse(
        "admin_unlock.html",
        {
            "request": request,
            "cafe_id": cafe_id,
            "admin_unlocked": is_admin(request),
            "flash_message": None,
            "flash_ok": True,
        },
    )


@router.post("/admin/unlock", response_model=None)
async def admin_unlock_post(
    request: Request,
    admin_password: str = Form(...),
    cafe_id: int = Depends(login_required()),
) -> Response:
    settings = get_settings()

    async with db.session_scope(settings) as s:
        ok = await services.web_admin_unlock(s, cafe_id=cafe_id, admin_password=admin_password)

    if ok:
        set_admin(request, True, ttl_min=int(settings.WEB_ADMIN_UNLOCK_TTL_MIN))
        return _redirect("/orders")

    return _templates(request).TemplateResponse(
        "admin_unlock.html",
        {
            "request": request,
            "cafe_id": cafe_id,
            "admin_unlocked": False,
            "flash_message": "Неверный админ-пароль.",
            "flash_ok": False,
        },
        status_code=403,
    )


@router.get("/menu", response_class=HTMLResponse)
async def menu_get(
    request: Request,
    cafe_id: int = Depends(admin_required()),
) -> HTMLResponse:
    settings = get_settings()

    async with db.session_scope(settings) as s:
        menu = await services.web_menu_read(s, cafe_id=cafe_id)

    return _templates(request).TemplateResponse(
        "menu.html",
        {
            "request": request,
            "cafe_id": cafe_id,
            "admin_unlocked": True,
            "menu": menu,
        },
    )