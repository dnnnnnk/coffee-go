from __future__ import annotations

from typing import Any, Optional
from decimal import Decimal

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


# Helper functions for form/flash handling
def _to_int(x: Any | None, default: int | None = None) -> int | None:
    if x is None:
        return default
    try:
        return int(x)
    except Exception:
        return default


def _to_decimal_str(x: str | None) -> Decimal | None:
    if x is None:
        return None
    s = str(x).strip()
    if s == "":
        return None
    # allow comma decimal separator
    s = s.replace(",", ".")
    try:
        return Decimal(s)
    except Exception:
        return None


def _flash_redirect(url: str, *, msg: str, ok: bool = True) -> RedirectResponse:
    # simplest approach: flash via query params (no server-side flash storage)
    from urllib.parse import urlencode

    q = urlencode({"msg": msg, "ok": "1" if ok else "0"})
    sep = "&" if "?" in url else "?"
    return RedirectResponse(url=f"{url}{sep}{q}", status_code=303)



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
    msg: Optional[str] = None,
    ok: Optional[str] = None,
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
            "flash_message": msg,
            "flash_ok": (ok != "0"),
        },
    )


@router.post("/orders/{order_id}/status")
async def orders_status_post(
    request: Request,
    order_id: int,
    new_status: str = Form(...),
    cafe_id: int = Depends(admin_required()),
) -> RedirectResponse:
    settings = get_settings()

    # preserve current filter in redirect
    status_filter = request.query_params.get("status")
    base_url = "/orders" + (f"?status={status_filter}" if status_filter else "")

    # guard against obvious invalid inputs (UI should never send these)
    allowed_targets = {"accepted", "in_progress", "done"}
    if new_status not in allowed_targets:
        return _flash_redirect(base_url, msg="Недопустимый статус.", ok=False)

    try:
        async with db.session_scope(settings) as s:
            ok_ = await services.web_update_order_status(
                s,
                cafe_id=cafe_id,
                order_id=order_id,
                new_status=new_status,
            )
            if not ok_:
                return _flash_redirect(base_url, msg="Заказ не найден.", ok=False)
    except services.ServiceError as e:
        # service already produces user-friendly messages
        return _flash_redirect(base_url, msg=e.message, ok=False)
    except Exception:
        return _flash_redirect(base_url, msg="Не удалось обновить статус заказа.", ok=False)

    return _flash_redirect(base_url, msg="Статус заказа обновлён.", ok=True)


@router.get("/stoplist", response_class=HTMLResponse)
async def stoplist_get(
    request: Request,
    cafe_id: int = Depends(login_required()),
) -> HTMLResponse:
    settings = get_settings()

    async with db.session_scope(settings) as s:
        view = await services.web_get_stoplist(s, cafe_id=cafe_id)
        cafe = await db.get_cafe(s, cafe_id)

    return _templates(request).TemplateResponse(
        "stoplist.html",
        {
            "request": request,
            "cafe_id": cafe_id,
            "admin_unlocked": is_admin(request),
            "cafe_name": (cafe or {}).get("name"),
            "products": view["products"],
            "modifiers": view["modifiers"],
            "q": None,
            "flash_message": None,
            "flash_ok": True,
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
    msg: Optional[str] = None,
    ok: Optional[str] = None,
    cafe_id: int = Depends(admin_required()),
) -> HTMLResponse:
    settings = get_settings()

    async with db.session_scope(settings) as s:
        menu = await services.web_menu_read(s, cafe_id=cafe_id, include_inactive=True)

    flash_message = msg
    flash_ok = (ok != "0")
    return _templates(request).TemplateResponse(
        "menu.html",
        {
            "request": request,
            "cafe_id": cafe_id,
            "admin_unlocked": True,
            "is_admin": is_admin(request),
            "cafe_name": (menu.get("cafe") or {}).get("name"),
            "categories": menu.get("categories") or [],
            "flash_message": flash_message,
            "flash_ok": flash_ok,
        },
    )




# --- Menu CRUD Endpoints ---

@router.post("/menu/category/create")
async def menu_category_create(
    request: Request,
    name: str = Form(...),
    sort: int = Form(0),
    is_active: int = Form(1),
    cafe_id: int = Depends(admin_required()),
) -> RedirectResponse:
    settings = get_settings()
    nm = (name or "").strip()
    if not nm:
        return _flash_redirect("/menu", msg="Название категории не может быть пустым", ok=False)
    async with db.session_scope(settings) as s:
        try:
            await services.web_menu_crud_create_category(
                s,
                cafe_id=cafe_id,
                name=nm,
                sort=int(sort),
                is_active=bool(int(is_active)),
            )
        except Exception as e:
            return _flash_redirect("/menu", msg=f"Не удалось создать категорию: {e}", ok=False)
    return _redirect("/menu")


@router.post("/menu/category/{category_id}/update")
async def menu_category_update(
    request: Request,
    category_id: int,
    name: Optional[str] = Form(None),
    sort: Optional[str] = Form(None),
    is_active: Optional[str] = Form(None),
    cafe_id: int = Depends(admin_required()),
) -> RedirectResponse:
    settings = get_settings()

    nm = name.strip() if name is not None else None
    srt = _to_int(sort, None)
    act = None
    if is_active is not None:
        try:
            act = bool(int(is_active))
        except Exception:
            act = None

    async with db.session_scope(settings) as s:
        try:
            ok_ = await services.web_menu_crud_update_category(
                s,
                cafe_id=cafe_id,
                category_id=int(category_id),
                name=(nm if nm != "" else None),
                sort=srt,
                is_active=act,
            )
            if not ok_:
                return _flash_redirect("/menu", msg="Категория не найдена", ok=False)
        except Exception as e:
            return _flash_redirect("/menu", msg=f"Не удалось обновить категорию: {e}", ok=False)

    return _redirect("/menu")


@router.post("/menu/category/{category_id}/delete")
async def menu_category_delete(
    request: Request,
    category_id: int,
    cafe_id: int = Depends(admin_required()),
) -> RedirectResponse:
    settings = get_settings()
    async with db.session_scope(settings) as s:
        try:
            ok_ = await services.web_menu_crud_delete_category(s, cafe_id=cafe_id, category_id=int(category_id))
            if not ok_:
                return _flash_redirect("/menu", msg="Категория не найдена", ok=False)
        except Exception as e:
            return _flash_redirect("/menu", msg=f"Не удалось удалить категорию: {e}", ok=False)
    return _redirect("/menu")


@router.post("/menu/product/create")
async def menu_product_create(
    request: Request,
    category_id: int = Form(...),
    name: str = Form(...),
    base_price: str = Form(...),
    description: str = Form(""),
    sort: int = Form(0),
    is_active: int = Form(1),
    cafe_id: int = Depends(admin_required()),
) -> RedirectResponse:
    settings = get_settings()
    nm = (name or "").strip()
    if not nm:
        return _flash_redirect("/menu", msg="Название товара не может быть пустым", ok=False)
    price = _to_decimal_str(base_price)
    if price is None:
        return _flash_redirect("/menu", msg="Некорректная цена товара", ok=False)

    async with db.session_scope(settings) as s:
        try:
            await services.web_menu_crud_create_product(
                s,
                cafe_id=cafe_id,
                category_id=int(category_id),
                name=nm,
                base_price=price,
                description=((description or "").strip() or None),
                sort=int(sort),
                is_active=bool(int(is_active)),
            )
        except Exception as e:
            return _flash_redirect("/menu", msg=f"Не удалось создать товар: {e}", ok=False)

    return _redirect("/menu")


@router.post("/menu/product/{product_id}/update")
async def menu_product_update(
    request: Request,
    product_id: int,
    name: Optional[str] = Form(None),
    base_price: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    category_id: Optional[str] = Form(None),
    sort: Optional[str] = Form(None),
    is_active: Optional[str] = Form(None),
    cafe_id: int = Depends(admin_required()),
) -> RedirectResponse:
    settings = get_settings()

    nm = name.strip() if name is not None else None
    price: Decimal | None = None
    if base_price is not None:
        if str(base_price).strip() == "":
            price = None
        else:
            price = _to_decimal_str(base_price)
            if price is None:
                return _flash_redirect("/menu", msg="Некорректная цена товара", ok=False)
    cat_id = _to_int(category_id, None)
    srt = _to_int(sort, None)
    act = None
    if is_active is not None:
        try:
            act = bool(int(is_active))
        except Exception:
            act = None

    desc = None
    if description is not None:
        d = description.strip()
        desc = d or None

    async with db.session_scope(settings) as s:
        try:
            ok_ = await services.web_menu_crud_update_product(
                s,
                cafe_id=cafe_id,
                product_id=int(product_id),
                name=(nm if nm != "" else None),
                description=desc,
                base_price=price,
                category_id=cat_id,
                sort=srt,
                is_active=act,
            )
            if not ok_:
                return _flash_redirect("/menu", msg="Товар не найден", ok=False)
        except Exception as e:
            return _flash_redirect("/menu", msg=f"Не удалось обновить товар: {e}", ok=False)

    return _redirect("/menu")


@router.post("/menu/product/{product_id}/delete")
async def menu_product_delete(
    request: Request,
    product_id: int,
    cafe_id: int = Depends(admin_required()),
) -> RedirectResponse:
    settings = get_settings()
    async with db.session_scope(settings) as s:
        try:
            ok_ = await services.web_menu_crud_delete_product(s, cafe_id=cafe_id, product_id=int(product_id))
            if not ok_:
                return _flash_redirect("/menu", msg="Товар не найден", ok=False)
        except Exception as e:
            return _flash_redirect("/menu", msg=f"Не удалось удалить товар: {e}", ok=False)
    return _redirect("/menu")


@router.post("/menu/group/create")
async def menu_group_create(
    request: Request,
    product_id: int = Form(...),
    name: str = Form(...),
    required: int = Form(0),
    min_select: int = Form(0),
    max_select: int = Form(1),
    sort: int = Form(0),
    is_active: int = Form(1),
    cafe_id: int = Depends(admin_required()),
) -> RedirectResponse:
    settings = get_settings()
    nm = (name or "").strip()
    if not nm:
        return _flash_redirect("/menu", msg="Название группы не может быть пустым", ok=False)
    if int(max_select) < int(min_select):
        return _flash_redirect("/menu", msg="max_select не может быть меньше min_select", ok=False)

    async with db.session_scope(settings) as s:
        try:
            await services.web_menu_assert_product_belongs_to_cafe(s, cafe_id=cafe_id, product_id=int(product_id))
            await services.web_menu_crud_create_modifier_group(
                s,
                product_id=int(product_id),
                name=nm,
                required=bool(int(required)),
                min_select=int(min_select),
                max_select=int(max_select),
                sort=int(sort),
                is_active=bool(int(is_active)),
            )
        except Exception as e:
            return _flash_redirect("/menu", msg=f"Не удалось создать группу: {e}", ok=False)
    return _redirect("/menu")


@router.post("/menu/group/{group_id}/update")
async def menu_group_update(
    request: Request,
    group_id: int,
    name: Optional[str] = Form(None),
    required: Optional[str] = Form(None),
    min_select: Optional[str] = Form(None),
    max_select: Optional[str] = Form(None),
    sort: Optional[str] = Form(None),
    is_active: Optional[str] = Form(None),
    cafe_id: int = Depends(admin_required()),
) -> RedirectResponse:
    settings = get_settings()

    nm = name.strip() if name is not None else None
    req = None
    if required is not None:
        try:
            req = bool(int(required))
        except Exception:
            req = None
    min_s = _to_int(min_select, None)
    max_s = _to_int(max_select, None)
    if (min_s is not None) and (max_s is not None) and (max_s < min_s):
        return _flash_redirect("/menu", msg="max_select не может быть меньше min_select", ok=False)
    srt = _to_int(sort, None)
    act = None
    if is_active is not None:
        try:
            act = bool(int(is_active))
        except Exception:
            act = None

    async with db.session_scope(settings) as s:
        try:
            await services.web_menu_assert_group_belongs_to_cafe(s, cafe_id=cafe_id, group_id=int(group_id))
            ok_ = await services.web_menu_crud_update_modifier_group(
                s,
                group_id=int(group_id),
                name=(nm if nm != "" else None),
                required=req,
                min_select=min_s,
                max_select=max_s,
                sort=srt,
                is_active=act,
            )
            if not ok_:
                return _flash_redirect("/menu", msg="Группа не найдена", ok=False)
        except Exception as e:
            return _flash_redirect("/menu", msg=f"Не удалось обновить группу: {e}", ok=False)
    return _redirect("/menu")


@router.post("/menu/group/{group_id}/delete")
async def menu_group_delete(
    request: Request,
    group_id: int,
    cafe_id: int = Depends(admin_required()),
) -> RedirectResponse:
    settings = get_settings()
    async with db.session_scope(settings) as s:
        try:
            ok_ = await services.web_menu_crud_delete_modifier_group(s, cafe_id=cafe_id, group_id=int(group_id))
            if not ok_:
                return _flash_redirect("/menu", msg="Группа не найдена", ok=False)
        except Exception as e:
            return _flash_redirect("/menu", msg=f"Не удалось удалить группу: {e}", ok=False)
    return _redirect("/menu")


@router.post("/menu/modifier/create")
async def menu_modifier_create(
    request: Request,
    group_id: int = Form(...),
    name: str = Form(...),
    price_delta: str = Form("0"),
    sort: int = Form(0),
    is_active: int = Form(1),
    cafe_id: int = Depends(admin_required()),
) -> RedirectResponse:
    settings = get_settings()
    nm = (name or "").strip()
    if not nm:
        return _flash_redirect("/menu", msg="Название модификатора не может быть пустым", ok=False)
    delta = _to_decimal_str(price_delta)
    if delta is None:
        return _flash_redirect("/menu", msg="Некорректный price_delta", ok=False)

    async with db.session_scope(settings) as s:
        try:
            await services.web_menu_assert_group_belongs_to_cafe(s, cafe_id=cafe_id, group_id=int(group_id))
            await services.web_menu_crud_create_modifier(
                s,
                group_id=int(group_id),
                name=nm,
                price_delta=delta,
                sort=int(sort),
                is_active=bool(int(is_active)),
            )
        except Exception as e:
            return _flash_redirect("/menu", msg=f"Не удалось создать модификатор: {e}", ok=False)
    return _redirect("/menu")


@router.post("/menu/modifier/{modifier_id}/update")
async def menu_modifier_update(
    request: Request,
    modifier_id: int,
    name: Optional[str] = Form(None),
    price_delta: Optional[str] = Form(None),
    sort: Optional[str] = Form(None),
    is_active: Optional[str] = Form(None),
    cafe_id: int = Depends(admin_required()),
) -> RedirectResponse:
    settings = get_settings()

    nm = name.strip() if name is not None else None
    delta: Decimal | None = None
    if price_delta is not None:
        if str(price_delta).strip() == "":
            delta = None
        else:
            delta = _to_decimal_str(price_delta)
            if delta is None:
                return _flash_redirect("/menu", msg="Некорректный price_delta", ok=False)
    srt = _to_int(sort, None)
    act = None
    if is_active is not None:
        try:
            act = bool(int(is_active))
        except Exception:
            act = None

    async with db.session_scope(settings) as s:
        try:
            await services.web_menu_assert_modifier_belongs_to_cafe(s, cafe_id=cafe_id, modifier_id=int(modifier_id))
            ok_ = await services.web_menu_crud_update_modifier(
                s,
                modifier_id=int(modifier_id),
                name=(nm if nm != "" else None),
                price_delta=delta,
                sort=srt,
                is_active=act,
            )
            if not ok_:
                return _flash_redirect("/menu", msg="Модификатор не найден", ok=False)
        except Exception as e:
            return _flash_redirect("/menu", msg=f"Не удалось обновить модификатор: {e}", ok=False)

    return _redirect("/menu")


@router.post("/menu/modifier/{modifier_id}/delete")
async def menu_modifier_delete(
    request: Request,
    modifier_id: int,
    cafe_id: int = Depends(admin_required()),
) -> RedirectResponse:
    settings = get_settings()
    async with db.session_scope(settings) as s:
        try:
            ok_ = await services.web_menu_crud_delete_modifier(s, cafe_id=cafe_id, modifier_id=int(modifier_id))
            if not ok_:
                return _flash_redirect("/menu", msg="Модификатор не найден", ok=False)
        except Exception as e:
            return _flash_redirect("/menu", msg=f"Не удалось удалить модификатор: {e}", ok=False)
    return _redirect("/menu")