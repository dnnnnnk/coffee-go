# src/handlers.py
from __future__ import annotations

from typing import Any
from sqlalchemy import select, func
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.types import InlineKeyboardButton
from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from .keyboards import pickup_choice_kb
from datetime import datetime, timedelta
from sqlalchemy import select
from . import services
from . import db
from . import payments
from .config import get_settings
from .db import session_scope
# optional (будет добавлено в keyboards.py)
try:
    from .keyboards import balance_kb, topup_pay_kb
except Exception:
    balance_kb = None  # type: ignore
    topup_pay_kb = None  # type: ignore
from .keyboards import (
    back_to_main_kb,
    cafes_kb,
    categories_kb,
    checkout_kb,
    main_menu_kb,
    modifiers_kb,
    product_kb,
    products_kb,
    cart_kb,
)
from .models import (
    CafeCB,
    CategoryCB,
    ProductCB,
    ModGroupCB,
    ToggleModCB,
    ModsDoneCB,
    CartOpenCB,
    CartDelCB,
    CartClearCB,
    CheckoutCB,
    ProfileCB,
    UserFlow,
    FSM_KEY_CAFE_ID,
    FSM_KEY_PRODUCT_ID,
    FSM_KEY_SELECTED_MODS,
    FSM_KEY_GROUP_RULES,
    PickupASAPCB,
    PickupTimeCB,
    FSM_KEY_PICKUP_MODE,
    FSM_KEY_PICKUP_TIME,
)

router = Router()
settings = get_settings()


# ----------------------------
# Safe helpers
# ----------------------------

def _safe_int(x: Any) -> int | None:
    try:
        return int(x)
    except Exception:
        return None

async def try_pin(bot, chat_id: int, message_id: int) -> None:
    try:
        await bot.pin_chat_message(chat_id=chat_id, message_id=message_id, disable_notification=True)
    except Exception:
        # в личке нельзя, в группе может не быть прав — пофиг
        pass

async def try_delete(q: CallbackQuery) -> None:
    try:
        if q.message:
            await q.message.delete()
    except Exception:
        # если нельзя удалить — хотя бы уберём клавиатуру
        try:
            await q.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass

async def safe_edit(q: CallbackQuery, text: str, reply_markup=None) -> None:
    """
    Надёжное редактирование:
    - если сообщение нельзя изменить/оно не изменилось -> просто отвечаем новым сообщением
    """
    try:
        if q.message:
            await q.message.edit_text(text, reply_markup=reply_markup)
        else:
            await q.bot.send_message(q.from_user.id, text, reply_markup=reply_markup)
    except TelegramBadRequest:
        try:
            if q.message:
                await q.message.answer(text, reply_markup=reply_markup)
            else:
                await q.bot.send_message(q.from_user.id, text, reply_markup=reply_markup)
        except Exception:
            # главное — не падать
            pass

def cart_conflict_kb(*, back_cb: str) -> Any:
    kb = InlineKeyboardBuilder()
    kb.button(text="🧹 Очистить корзину и добавить", callback_data="cart:clear_and_add_pending")
    kb.button(text="⬅️ Назад", callback_data=back_cb)
    kb.adjust(1)
    return kb.as_markup()


# ----------------------------
# Cart/cafe conflict error helper
# ----------------------------

def _is_cart_cafe_conflict_error(e: Exception) -> bool:
    msg = getattr(e, "message", None) or str(e)
    return "Корзина привязана к другой кофейне" in msg


async def _get_cart_bound_cafe_id(session, user_id: int) -> int | None:
    """
    Возвращает cafe_id, к которому реально привязана текущая корзина юзера.
    """
    q = select(func.min(db.cart_items.c.cafe_id)).where(db.cart_items.c.user_id == user_id)
    cafe_id = (await session.execute(q)).scalar_one()
    return int(cafe_id) if cafe_id is not None else None

async def _get_selected_cafe_id(state: FSMContext) -> int | None:
    data = await state.get_data()
    return _safe_int(data.get(FSM_KEY_CAFE_ID))


async def _set_selected_cafe_id(state: FSMContext, cafe_id: int) -> None:
    await state.update_data({FSM_KEY_CAFE_ID: int(cafe_id)})


# ----------------------------
# Text formatters
# ----------------------------

def _fmt_cart_text(cart: services.CartView) -> str:
    if not cart.lines:
        return "🧺 Корзина пустая."

    lines = ["🧺 <b>Корзина</b>\n"]

    for i, ln in enumerate(cart.lines, start=1):
        mods = ""
        if ln.modifiers:
            mods = " — " + ", ".join(
                f"{m.name}{(' +' + str(m.price_delta)).replace('.00','') if m.price_delta != 0 else ''}"
                for m in ln.modifiers
            )

        status = ""
        if ln.blocked:
            status = f"\n⛔ <i>{ln.blocked_reason or 'Позиция недоступна'}</i>"

        lines.append(
            f"#{i} • <b>{ln.product_name}</b>{mods}\n"
            f"{ln.qty} × {ln.unit_price:.2f} ₽ = <b>{ln.line_total:.2f} ₽</b>{status}"
        )

    lines.append(f"\nИтого (без недоступных): <b>{cart.total:.2f} ₽</b>")
    return "\n\n".join(lines)


def _fmt_product_text(p: dict, groups: list[dict]) -> str:
    name = str(p["name"])
    price = p["base_price"]
    desc = (p.get("description") or "").strip()
    has_mods = bool(groups)

    text = f"☕ <b>{name}</b>\nЦена: <b>{price}</b>"
    if desc:
        text += f"\n\n{desc}"
    text += "\n\n" + ("Есть модификаторы." if has_mods else "Без модификаторов.")
    return text


def _fmt_profile_text(profile: dict) -> str:
    cups = int(profile.get("cups_total") or 0)
    fav_cafe = profile.get("favorite_cafe")
    fav_prod = profile.get("favorite_product")

    out = [f"👤 <b>Профиль</b>\n\n☕ Выпито чашек: <b>{cups}</b>"]
    if fav_cafe:
        out.append(f"🏆 Любимая кофейня: <b>{fav_cafe['name']}</b> ({int(fav_cafe['cups'])} чаш.)")
    if fav_prod:
        out.append(f"🥤 Любимый напиток: <b>{fav_prod['name']}</b> ({int(fav_prod['cups'])} чаш.)")
    return "\n".join(out)


# ----------------------------
# Screens
# ----------------------------

async def _show_main(m_or_q: Message | CallbackQuery, *, edit: bool) -> None:
    text = "Меню:"
    if isinstance(m_or_q, Message):
        await m_or_q.answer(text, reply_markup=main_menu_kb())
    else:
        if edit:
            await safe_edit(m_or_q, text, reply_markup=main_menu_kb())
        else:
            await m_or_q.message.answer(text, reply_markup=main_menu_kb())


async def _show_cafes(q: CallbackQuery, state: FSMContext) -> None:
    async with session_scope(settings) as s:
        cafes = await services.list_cafes(s)
    await safe_edit(q, "Выбери кофейню:", reply_markup=cafes_kb(cafes))
    await state.set_state(UserFlow.choosing_cafe)


async def _show_categories(q: CallbackQuery, state: FSMContext, cafe_id: int) -> None:
    async with session_scope(settings) as s:
        cats = await services.list_menu_categories(s, cafe_id)
    await _set_selected_cafe_id(state, cafe_id)
    await state.set_state(None)
    await safe_edit(q, "Категории:", reply_markup=categories_kb(cafe_id, cats))


async def _show_products(q: CallbackQuery, state: FSMContext, cafe_id: int, category_id: int | None) -> None:
    async with session_scope(settings) as s:
        prods = await services.list_menu_products(
            s, cafe_id, None if not category_id or category_id == 0 else category_id
        )
    await _set_selected_cafe_id(state, cafe_id)
    await state.set_state(None)
    await safe_edit(q, "Ассортимент:", reply_markup=products_kb(cafe_id, prods))


async def _show_product(q: CallbackQuery, state: FSMContext, cafe_id: int, product_id: int) -> None:
    async with session_scope(settings) as s:
        full = await services.get_product_full(s, cafe_id, product_id)

    p = full["product"]
    groups = full["groups"]
    has_mods = bool(groups)

    price = f"{float(p['base_price']):.2f} ₽"
    p_for_text = dict(p)
    p_for_text["base_price"] = price

    await _set_selected_cafe_id(state, cafe_id)
    await state.set_state(None)
    await safe_edit(
        q,
        _fmt_product_text(p_for_text, groups),
        reply_markup=product_kb(cafe_id, product_id, has_modifiers=has_mods),
    )


async def _show_cart(q: CallbackQuery, state: FSMContext, cafe_id: int) -> None:
    async with session_scope(settings) as s:
        cart = await services.get_cart_view(s, settings, user_id=q.from_user.id, cafe_id=cafe_id)

    line_pairs = [(i, ln.cart_item_id) for i, ln in enumerate(cart.lines, start=1)]
    await safe_edit(
        q,
        _fmt_cart_text(cart),
        reply_markup=cart_kb(cafe_id=cafe_id, line_pairs=line_pairs, has_lines=bool(cart.lines)),
    )


# ----------------------------
# Balance helpers
# ----------------------------

async def _show_balance(q: CallbackQuery) -> None:
    async with session_scope(settings) as s:
        bal = await services.get_balance(s, user_id=q.from_user.id)
    kb = balance_kb() if callable(balance_kb) else back_to_main_kb()
    await safe_edit(q, f"💰 Баланс: <b>{bal:.2f} ₽</b>", reply_markup=kb)




# ----------------------------
# Commands
# ----------------------------

@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await m.answer("Ок. Что делаем?", reply_markup=main_menu_kb())


@router.message(Command("menu"))
async def cmd_menu(m: Message, state: FSMContext) -> None:
    await state.set_state(None)
    await m.answer("Меню:", reply_markup=main_menu_kb())


# ----------------------------
# Generic open:* routing (string callbacks)
# ----------------------------

@router.callback_query(F.data == "open:main")
async def open_main(q: CallbackQuery, state: FSMContext) -> None:
    await q.answer()
    await state.set_state(None)
    await _show_main(q, edit=True)


@router.callback_query(F.data == "open:cafes")
async def open_cafes(q: CallbackQuery, state: FSMContext) -> None:
    await q.answer()
    await _show_cafes(q, state)


@router.callback_query(F.data == "open:cart")
async def open_cart_from_menu(q: CallbackQuery, state: FSMContext) -> None:
    await q.answer()

    # 1) если пользователь уже выбрал кофейню — пробуем показать корзину этой кофейни
    cafe_id = await _get_selected_cafe_id(state)
    if cafe_id:
        try:
            await _show_cart(q, state, cafe_id)
            return
        except services.Validation:
            # корзина реально привязана к другой кофейне — попробуем определить к какой
            pass
        except services.ServiceError as e:
            await safe_edit(q, e.message, reply_markup=back_to_main_kb())
            return

    # 2) если кофейня не выбрана или корзина привязана к другой кофейне —
    # пытаемся найти cafe_id по реально лежащим в корзине позициям
    async with session_scope(settings) as s:
        cart_cafe_id = (
            await s.execute(
                select(db.cart_items.c.cafe_id)
                .where(db.cart_items.c.user_id == q.from_user.id)
                .limit(1)
            )
        ).scalar_one_or_none()

    if cart_cafe_id is None:
        # корзина пустая и кофейня не выбрана — просто отправим в выбор кофеен
        await safe_edit(q, "Сначала выбери кофейню.", reply_markup=back_to_main_kb())
        return

    cafe_id = int(cart_cafe_id)
    await _set_selected_cafe_id(state, cafe_id)

    try:
        await _show_cart(q, state, cafe_id)
    except services.ServiceError as e:
        await safe_edit(q, e.message, reply_markup=back_to_main_kb())


# ----------------------------
# open:balance
# ----------------------------

@router.callback_query(F.data == "open:balance")
async def open_balance(q: CallbackQuery, state: FSMContext) -> None:
    await q.answer()
    await state.set_state(None)
    await _show_balance(q)


@router.callback_query(F.data.startswith("open:categories:"))
async def open_categories(q: CallbackQuery, state: FSMContext) -> None:
    await q.answer()
    cafe_id = _safe_int(q.data.split(":")[-1])
    if not cafe_id:
        await safe_edit(q, "Не понял кофейню. Попробуй ещё раз.", reply_markup=back_to_main_kb())
        return
    try:
        await _show_categories(q, state, cafe_id)
    except services.ServiceError as e:
        await safe_edit(q, e.message, reply_markup=back_to_main_kb())


@router.callback_query(F.data.startswith("open:products:"))
async def open_products(q: CallbackQuery, state: FSMContext) -> None:
    await q.answer()
    cafe_id = _safe_int(q.data.split(":")[-1])
    if not cafe_id:
        await safe_edit(q, "Не понял кофейню. Попробуй ещё раз.", reply_markup=back_to_main_kb())
        return
    try:
        await _show_products(q, state, cafe_id, category_id=None)
    except services.ServiceError as e:
        await safe_edit(q, e.message, reply_markup=back_to_main_kb())


@router.callback_query(F.data.startswith("open:mods:"))
async def open_mods(q: CallbackQuery, state: FSMContext) -> None:
    """
    open:mods:{cafe_id}:{product_id}
    """
    await q.answer()
    parts = q.data.split(":")
    if len(parts) != 4:
        await safe_edit(q, "Некорректная команда.", reply_markup=back_to_main_kb())
        return

    cafe_id = _safe_int(parts[2])
    product_id = _safe_int(parts[3])
    if not cafe_id or not product_id:
        await safe_edit(q, "Некорректные данные.", reply_markup=back_to_main_kb())
        return

    try:
        async with session_scope(settings) as s:
            full = await services.get_product_full(s, cafe_id, product_id)
    except services.ServiceError as e:
        await safe_edit(q, e.message, reply_markup=back_to_main_kb())
        return

    groups = full["groups"]
    if not groups:
        await safe_edit(
            q,
            "У товара нет модификаторов.",
            reply_markup=product_kb(cafe_id, product_id, has_modifiers=False),
        )
        return

    group_rules: dict[str, dict[str, int | bool]] = {}
    mods_by_group: dict[str, list[dict]] = {}

    for g in groups:
        gid = int(g["id"])
        group_rules[str(gid)] = {
            "required": bool(g["required"]),
            "min": int(g["min_select"]),
            "max": int(g["max_select"]),
        }
        mods_by_group[str(gid)] = list(g.get("modifiers", []))

    active_gid = int(groups[0]["id"])
    await state.set_state(UserFlow.choosing_modifiers)
    await state.update_data(
        {
            FSM_KEY_CAFE_ID: cafe_id,
            FSM_KEY_PRODUCT_ID: product_id,
            FSM_KEY_SELECTED_MODS: [],
            FSM_KEY_GROUP_RULES: group_rules,
            "mods_by_group": mods_by_group,
            "active_group_id": active_gid,
        }
    )

    active_mods = mods_by_group[str(active_gid)]
    await safe_edit(
        q,
        "Выбери модификаторы:",
        reply_markup=modifiers_kb(
            cafe_id=cafe_id,
            product_id=product_id,
            groups=groups,
            modifiers=active_mods,
            selected_ids=set(),
            active_group_id=active_gid,
        ),
    )


# ----------------------------
# CallbackData routes
# ----------------------------

@router.callback_query(CafeCB.filter())
async def on_cafe_selected(q: CallbackQuery, callback_data: CafeCB, state: FSMContext) -> None:
    await q.answer()
    try:
        await _show_categories(q, state, callback_data.cafe_id)
    except services.ServiceError as e:
        await safe_edit(q, e.message, reply_markup=back_to_main_kb())


@router.callback_query(CategoryCB.filter())
async def on_category_selected(q: CallbackQuery, callback_data: CategoryCB, state: FSMContext) -> None:
    await q.answer()
    try:
        await _show_products(q, state, callback_data.cafe_id, callback_data.category_id)
    except services.ServiceError as e:
        await safe_edit(q, e.message, reply_markup=back_to_main_kb())


@router.callback_query(ProductCB.filter())
async def on_product_open(q: CallbackQuery, callback_data: ProductCB, state: FSMContext) -> None:
    await q.answer()
    try:
        await _show_product(q, state, callback_data.cafe_id, callback_data.product_id)
    except services.ServiceError as e:
        await safe_edit(q, e.message, reply_markup=back_to_main_kb())


@router.callback_query(CartOpenCB.filter())
async def on_cart_open(q: CallbackQuery, callback_data: CartOpenCB, state: FSMContext) -> None:
    await q.answer()
    try:
        await _set_selected_cafe_id(state, int(callback_data.cafe_id))
        await _show_cart(q, state, callback_data.cafe_id)
    except services.Validation:
        # если корзина привязана к другой кофейне — перекинем на реальную кофейню корзины
        async with session_scope(settings) as s:
            cart_cafe_id = (
                await s.execute(
                    select(db.cart_items.c.cafe_id)
                    .where(db.cart_items.c.user_id == q.from_user.id)
                    .limit(1)
                )
            ).scalar_one_or_none()

        if cart_cafe_id is None:
            await safe_edit(q, "🧺 Корзина пустая.", reply_markup=back_to_main_kb())
            return

        real_cafe_id = int(cart_cafe_id)
        await _set_selected_cafe_id(state, real_cafe_id)
        await _show_cart(q, state, real_cafe_id)
    except services.ServiceError as e:
        await safe_edit(q, e.message, reply_markup=back_to_main_kb())


@router.callback_query(CartDelCB.filter())
async def on_cart_del(q: CallbackQuery, callback_data: CartDelCB, state: FSMContext) -> None:
    await q.answer()
    async with session_scope(settings) as s:
        await services.remove_cart_line(s, user_id=q.from_user.id, cart_item_id=callback_data.cart_item_id)
    await _show_cart(q, state, callback_data.cafe_id)



@router.callback_query(CartClearCB.filter())
async def on_cart_clear(q: CallbackQuery, callback_data: CartClearCB, state: FSMContext) -> None:
    await q.answer()
    async with session_scope(settings) as s:
        await services.clear_cart(s, user_id=q.from_user.id, cafe_id=callback_data.cafe_id)
    await _show_cart(q, state, callback_data.cafe_id)


# ----------------------------
# Clear and add pending (cart:clear_and_add_pending)
# ----------------------------

@router.callback_query(F.data == "cart:clear_and_add_pending")
async def on_clear_and_add_pending(q: CallbackQuery, state: FSMContext) -> None:
    await q.answer()

    data = await state.get_data()
    pending = data.get("pending_add")

    if not pending:
        await safe_edit(q, "Кнопка устарела. Открой меню заново.", reply_markup=back_to_main_kb())
        return

    target_cafe_id = int(pending["cafe_id"])
    product_id = int(pending["product_id"])
    modifier_ids = [int(x) for x in (pending.get("modifier_ids") or [])]
    qty = int(pending.get("qty") or 1)

    async with session_scope(settings) as s:
        bound = await _get_cart_bound_cafe_id(s, user_id=q.from_user.id)
        if bound is not None:
            await services.clear_cart(s, user_id=q.from_user.id, cafe_id=int(bound))

        await services.add_to_cart(
            s,
            settings,
            user_id=q.from_user.id,
            cafe_id=target_cafe_id,
            product_id=product_id,
            modifier_ids=modifier_ids,
            qty=qty,
        )

    await state.update_data({"pending_add": None})
    await _set_selected_cafe_id(state, target_cafe_id)
    await _show_cart(q, state, target_cafe_id)


# ----------------------------
# Add to cart (no modifiers) — string callback: add:{cafe_id}:{product_id}
# ----------------------------

@router.callback_query(F.data.startswith("add:"))
async def on_add_simple(q: CallbackQuery, state: FSMContext) -> None:
    await q.answer()
    parts = q.data.split(":")
    if len(parts) != 3:
        await safe_edit(q, "Некорректная команда.", reply_markup=back_to_main_kb())
        return

    cafe_id = _safe_int(parts[1])
    product_id = _safe_int(parts[2])
    if not cafe_id or not product_id:
        await safe_edit(q, "Некорректные данные.", reply_markup=back_to_main_kb())
        return

    try:
        async with session_scope(settings) as s:
            await services.add_to_cart(
                s,
                settings,
                user_id=q.from_user.id,
                cafe_id=cafe_id,
                product_id=product_id,
                modifier_ids=[],
                qty=1,
            )
        await _set_selected_cafe_id(state, cafe_id)
        await _show_cart(q, state, cafe_id)

    except services.Validation:
        await state.update_data(
            {
                "pending_add": {
                    "cafe_id": cafe_id,
                    "product_id": product_id,
                    "modifier_ids": [],
                    "qty": 1,
                }
            }
        )
        await safe_edit(
            q,
            "🧺 Корзина привязана к другой кофейне.\nОчистить её и добавить этот товар?",
            reply_markup=cart_conflict_kb(back_cb=f"open:products:{cafe_id}"),
        )

    except services.ServiceError as e:
        # иногда конфликт кофейни летит как ServiceError (Validation наследуется от ServiceError)
        if _is_cart_cafe_conflict_error(e):
            await state.update_data(
                {
                    "pending_add": {
                        "cafe_id": cafe_id,
                        "product_id": product_id,
                        "modifier_ids": [],
                        "qty": 1,
                    }
                }
            )
            await safe_edit(
                q,
                "🧺 Корзина привязана к другой кофейне.\nОчистить её и добавить этот товар?",
                reply_markup=cart_conflict_kb(back_cb=f"open:products:{cafe_id}"),
            )
            return

        await safe_edit(q, e.message, reply_markup=product_kb(cafe_id, product_id, has_modifiers=False))


# ----------------------------
# Modifiers flow
# ----------------------------

@router.callback_query(ModGroupCB.filter())
async def on_mod_group(q: CallbackQuery, callback_data: ModGroupCB, state: FSMContext) -> None:
    await q.answer()
    data = await state.get_data()

    if data.get(FSM_KEY_PRODUCT_ID) != callback_data.product_id or data.get(FSM_KEY_CAFE_ID) != callback_data.cafe_id:
        await safe_edit(q, "Кнопка устарела. Открой товар заново.", reply_markup=back_to_main_kb())
        await state.set_state(None)
        return

    mods_by_group = data.get("mods_by_group") or {}
    active_gid = int(callback_data.group_id)
    selected = set(int(x) for x in (data.get(FSM_KEY_SELECTED_MODS) or []))

    try:
        async with session_scope(settings) as s:
            full = await services.get_product_full(s, callback_data.cafe_id, callback_data.product_id)
    except services.ServiceError as e:
        await q.answer(e.message, show_alert=True)
        return

    groups = full["groups"]

    mods = mods_by_group.get(str(active_gid))
    if mods is None:
        mods = []
        for g in groups:
            if int(g["id"]) == int(active_gid):
                mods = list(g.get("modifiers", []))
                break

    await state.update_data({"active_group_id": int(active_gid)})

    await safe_edit(
        q,
        "Выбери модификаторы:",
        reply_markup=modifiers_kb(
            cafe_id=callback_data.cafe_id,
            product_id=callback_data.product_id,
            groups=groups,
            modifiers=mods,
            selected_ids=selected,
            active_group_id=int(active_gid),
        ),
    )


@router.callback_query(ToggleModCB.filter())
async def on_toggle_mod(q: CallbackQuery, callback_data: ToggleModCB, state: FSMContext) -> None:
    await q.answer()
    data = await state.get_data()

    if data.get(FSM_KEY_PRODUCT_ID) != callback_data.product_id or data.get(FSM_KEY_CAFE_ID) != callback_data.cafe_id:
        await q.answer("Кнопка устарела. Открой товар заново.", show_alert=True)
        await state.set_state(None)
        return

    selected = set(int(x) for x in (data.get(FSM_KEY_SELECTED_MODS) or []))
    group_rules = data.get(FSM_KEY_GROUP_RULES) or {}
    mods_by_group = data.get("mods_by_group") or {}

    gid = int(callback_data.group_id)
    rule = group_rules.get(str(gid))
    if not rule:
        await q.answer("Не понял группу модификаторов.", show_alert=True)
        return

    max_sel = int(rule.get("max", 1))

    group_mods = mods_by_group.get(str(gid), [])
    group_mod_ids = {int(m["id"]) for m in group_mods}

    mid = int(callback_data.modifier_id)
    if mid not in group_mod_ids:
        await q.answer("Некорректный модификатор.", show_alert=True)
        return

    if mid in selected:
        selected.remove(mid)
    else:
        if max_sel <= 1:
            selected -= group_mod_ids
            selected.add(mid)
        else:
            cur = len(selected & group_mod_ids)
            if cur + 1 > max_sel:
                await q.answer("Слишком много модификаторов в этой группе.", show_alert=True)
                return
            selected.add(mid)

    await state.update_data({FSM_KEY_SELECTED_MODS: sorted(selected)})

    active_gid = int(data.get("active_group_id") or gid)
    try:
        async with session_scope(settings) as s:
            full = await services.get_product_full(s, callback_data.cafe_id, callback_data.product_id)
    except services.ServiceError as e:
        await q.answer(e.message, show_alert=True)
        return

    groups = full["groups"]
    mods = mods_by_group.get(str(active_gid), [])
    await safe_edit(
        q,
        "Выбери модификаторы:",
        reply_markup=modifiers_kb(
            cafe_id=callback_data.cafe_id,
            product_id=callback_data.product_id,
            groups=groups,
            modifiers=mods,
            selected_ids=selected,
            active_group_id=active_gid,
        ),
    )


@router.callback_query(ModsDoneCB.filter())
async def on_mods_done(q: CallbackQuery, callback_data: ModsDoneCB, state: FSMContext) -> None:
    await q.answer()
    data = await state.get_data()

    cafe_id = _safe_int(data.get(FSM_KEY_CAFE_ID))
    product_id = _safe_int(data.get(FSM_KEY_PRODUCT_ID))
    if cafe_id != callback_data.cafe_id or product_id != callback_data.product_id:
        await q.answer("Кнопка устарела. Открой товар заново.", show_alert=True)
        await state.set_state(None)
        return

    selected = [int(x) for x in (data.get(FSM_KEY_SELECTED_MODS) or [])]
    group_rules = data.get(FSM_KEY_GROUP_RULES) or {}
    mods_by_group = data.get("mods_by_group") or {}

    # быстрая проверка required/min/max из кеша
    for gid_str, rule in group_rules.items():
        group_mod_ids = {int(m["id"]) for m in mods_by_group.get(gid_str, [])}
        cnt = len(set(selected) & group_mod_ids)

        required = bool(rule.get("required", False))
        min_sel = int(rule.get("min", 0))
        max_sel = int(rule.get("max", 1))

        effective_min = max(min_sel, 1) if required else min_sel
        if cnt < effective_min:
            await q.answer("Не выбраны обязательные модификаторы.", show_alert=True)
            return
        if cnt > max_sel:
            await q.answer("Слишком много модификаторов в одной группе.", show_alert=True)
            return

    try:
        async with session_scope(settings) as s:
            await services.validate_modifier_selection(s, product_id=product_id, modifier_ids=selected)
            await services.add_to_cart(
                s,
                settings,
                user_id=q.from_user.id,
                cafe_id=cafe_id,
                product_id=product_id,
                modifier_ids=selected,
                qty=1,
            )

        await _set_selected_cafe_id(state, cafe_id)
        await state.set_state(None)
        await _show_cart(q, state, cafe_id)

    except services.Validation:
        await state.update_data(
            {
                "pending_add": {
                    "cafe_id": cafe_id,
                    "product_id": product_id,
                    "modifier_ids": selected,
                    "qty": 1,
                }
            }
        )
        await safe_edit(
            q,
            "🧺 Корзина привязана к другой кофейне.\nОчистить её и добавить этот товар?",
            reply_markup=cart_conflict_kb(back_cb=f"open:products:{cafe_id}"),
        )

    except services.ServiceError as e:
        # иногда конфликт кофейни летит как ServiceError
        if _is_cart_cafe_conflict_error(e):
            await state.update_data(
                {
                    "pending_add": {
                        "cafe_id": cafe_id,
                        "product_id": product_id,
                        "modifier_ids": selected,
                        "qty": 1,
                    }
                }
            )
            await safe_edit(
                q,
                "🧺 Корзина привязана к другой кофейне.\nОчистить её и добавить этот товар?",
                reply_markup=cart_conflict_kb(back_cb=f"open:mods:{cafe_id}:{product_id}"),
            )
            return

        await safe_edit(q, e.message, reply_markup=back_to_main_kb())


@router.callback_query(PickupASAPCB.filter())
async def on_pickup_asap(q: CallbackQuery, callback_data: PickupASAPCB, state: FSMContext) -> None:
    await q.answer()
    cafe_id = callback_data.cafe_id

    await state.set_state(None)
    await state.update_data({FSM_KEY_PICKUP_MODE: "asap", FSM_KEY_PICKUP_TIME: None})

    try:
        async with session_scope(settings) as s:
            res = await services.checkout_with_balance(
                s,
                settings,
                user_id=q.from_user.id,
                cafe_id=cafe_id,
                pickup_type="asap",
                pickup_at=None,
            )
            receipt = await services.build_order_receipt(s, res.order_id)
            text = services.format_receipt(receipt)

        # удалить старое сообщение с кнопками, чтобы чек был выше
        await try_delete(q)

        # 1) Чек (выше)
        msg = await q.message.answer(text)
        await try_pin(q.bot, q.message.chat.id, msg.message_id)

        # 2) Сообщение ниже
        await q.message.answer(
            f"✅ Заказ создан.\nКод: <b>{res.code}</b>\nСумма: <b>{res.total:.2f} ₽</b>",
            reply_markup=checkout_kb(cafe_id=cafe_id, pay_url=None, payments_enabled=False),
        )

    except getattr(services, "InsufficientBalance", services.ServiceError) as e:  # type: ignore
        # если реально недостаточно денег — покажем баланс
        if e.__class__.__name__ == "InsufficientBalance":
            await safe_edit(q, f"❌ Недостаточно средств на балансе.\n\nПополнить?", reply_markup=(balance_kb() if callable(balance_kb) else back_to_main_kb()))
        else:
            await safe_edit(q, e.message, reply_markup=back_to_main_kb())

@router.callback_query(PickupTimeCB.filter())
async def on_pickup_time(q: CallbackQuery, callback_data: PickupTimeCB, state: FSMContext) -> None:
    await q.answer()
    cafe_id = callback_data.cafe_id

    await state.set_state(UserFlow.waiting_pickup_time)
    await state.update_data({FSM_KEY_CAFE_ID: cafe_id, FSM_KEY_PICKUP_MODE: "time"})

    await safe_edit(q, "Ок, напиши время в формате <b>hh:mm</b> (не раньше чем через 10 минут).", reply_markup=back_to_main_kb())

@router.message(UserFlow.waiting_pickup_time)
async def on_pickup_time_input(m: Message, state: FSMContext) -> None:
    text = (m.text or "").strip()
    data = await state.get_data()
    cafe_id = _safe_int(data.get(FSM_KEY_CAFE_ID))

    if not cafe_id:
        await state.set_state(None)
        await m.answer("Сессия устарела. Открой корзину заново.")
        return

    # parse HH:MM
    try:
        hh, mm = text.split(":")
        hh = int(hh)
        mm = int(mm)
        if not (0 <= hh <= 23 and 0 <= mm <= 59):
            raise ValueError
    except Exception:
        await m.answer("Нужен формат <b>hh:mm</b>. Например: <b>14:30</b>")
        return

    now = datetime.now()
    pickup_at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)

    # если время уже прошло сегодня — считаем что имелось в виду завтра
    if pickup_at <= now:
        pickup_at = pickup_at + timedelta(days=1)

    if pickup_at < now + timedelta(minutes=10):
        await m.answer("Слишком рано. Время должно быть минимум через <b>10 минут</b> от текущего.")
        return

    await state.set_state(None)
    await state.update_data({FSM_KEY_PICKUP_TIME: f"{hh:02d}:{mm:02d}"})

    try:
        async with session_scope(settings) as s:
            res = await services.checkout_with_balance(
                s,
                settings,
                user_id=m.from_user.id,
                cafe_id=cafe_id,
                pickup_type="time",
                pickup_at=pickup_at,
            )
            receipt = await services.build_order_receipt(s, res.order_id)
            check = services.format_receipt(receipt)

        # 1) чек
        msg = await m.answer(check)
        await try_pin(m.bot, m.chat.id, msg.message_id)

        # 2) кнопки ниже
        await m.answer(
            f"✅ Заказ создан.\nКод: <b>{res.code}</b>\nСумма: <b>{res.total:.2f} ₽</b>\nКо времени: <b>{pickup_at.strftime('%H:%M')}</b>",
            reply_markup=checkout_kb(cafe_id=cafe_id, pay_url=None, payments_enabled=False),
        )

    except getattr(services, "InsufficientBalance", services.ServiceError) as e:  # type: ignore
        if e.__class__.__name__ == "InsufficientBalance":
            await m.answer("❌ Недостаточно средств на балансе. Пополни и попробуй снова.", reply_markup=(balance_kb() if callable(balance_kb) else back_to_main_kb()))
        else:
            await m.answer(e.message, reply_markup=back_to_main_kb())

# ----------------------------
# Balance / Topup
# ----------------------------

@router.callback_query(F.data == "balance:topup")
async def on_balance_topup(q: CallbackQuery, state: FSMContext) -> None:
    await q.answer()
    # просим сумму
    await state.set_state(getattr(UserFlow, "waiting_topup_amount", UserFlow.waiting_pickup_time))
    await safe_edit(q, "Введи сумму пополнения в ₽ (например: <b>300</b>)", reply_markup=back_to_main_kb())


@router.message(getattr(UserFlow, "waiting_topup_amount", UserFlow.waiting_pickup_time))
async def on_topup_amount_input(m: Message, state: FSMContext) -> None:
    text = (m.text or "").strip().replace(",", ".")
    try:
        amount = float(text)
    except Exception:
        await m.answer("Нужна сумма числом. Например: <b>300</b>")
        return

    if amount <= 0:
        await m.answer("Сумма должна быть больше нуля.")
        return

    # округляем до 2 знаков
    amount = round(amount, 2)

    await state.set_state(None)

    try:
        async with session_scope(settings) as s:
            topup_id = await services.create_topup_request(s, settings, user_id=m.from_user.id, amount=amount)
            pay = await payments.create_payment_for_topup(
                s,
                settings,
                topup_id=topup_id,
                amount=amount,
                user_id=m.from_user.id,
            )

        kb = topup_pay_kb(topup_id=topup_id, pay_url=pay.confirmation_url) if callable(topup_pay_kb) else back_to_main_kb()
        await m.answer(
            f"💳 Пополнение на <b>{amount:.2f} ₽</b>\n\nОплати по кнопке ниже.",
            reply_markup=kb,
        )

    except payments.PaymentError as e:
        await m.answer(f"Ошибка оплаты: {e.message}", reply_markup=back_to_main_kb())
    except services.ServiceError as e:
        await m.answer(e.message, reply_markup=back_to_main_kb())


@router.callback_query(F.data.startswith("topup:check:"))
async def on_topup_check(q: CallbackQuery) -> None:
    await q.answer()
    try:
        topup_id = int(q.data.split(":")[-1])
    except Exception:
        await q.answer("Не понял пополнение.", show_alert=True)
        return

    try:
        async with session_scope(settings) as s:
            info = await payments.refresh_topup_status_from_provider(s, settings, topup_id=topup_id)

            if str(info.get("status")) == "succeeded":
                # показать новый баланс
                await _show_balance(q)
                return

        await q.answer("Платёж ещё не подтверждён. Попробуй позже.", show_alert=True)

    except payments.PaymentError as e:
        await q.answer(f"Не могу проверить оплату: {e.message}", show_alert=True)
    except services.ServiceError as e:
        await q.answer(e.message, show_alert=True)

# ----------------------------
# Checkout
# ----------------------------

@router.callback_query(CheckoutCB.filter())
async def on_checkout(q: CallbackQuery, callback_data: CheckoutCB, state: FSMContext) -> None:
    await q.answer()
    cafe_id = int(callback_data.cafe_id)

    await state.set_state(UserFlow.choosing_pickup)
    await state.update_data({FSM_KEY_CAFE_ID: cafe_id})
    await safe_edit(q, "Когда сделать кофе?", reply_markup=pickup_choice_kb(cafe_id))

# ----------------------------
# Profile
# ----------------------------

@router.callback_query(ProfileCB.filter())
async def on_profile(q: CallbackQuery, state: FSMContext) -> None:
    await q.answer()
    async with session_scope(settings) as s:
        profile = await services.get_profile(s, q.from_user.id)
    await safe_edit(q, _fmt_profile_text(profile), reply_markup=back_to_main_kb())


# ----------------------------
# Catch-all: unknown/old buttons
# ----------------------------

@router.callback_query()
async def unknown_callback(q: CallbackQuery) -> None:
    await q.answer("Кнопка устарела. Открой меню заново: /start", show_alert=True)

