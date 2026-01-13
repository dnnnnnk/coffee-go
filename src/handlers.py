# src/handlers.py
from __future__ import annotations

from typing import Any

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import services
from config import get_settings
from db import session_scope
from keyboards import (
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
from models import (
    AddToCartCB,
    CafeCB,
    CartClearCB,
    CartDelCB,
    CartOpenCB,
    CategoryCB,
    CheckoutCB,
    ModGroupCB,
    ModsDoneCB,
    ProductCB,
    ProfileCB,
    ToggleModCB,
    UserFlow,
    FSM_KEY_CAFE_ID,
    FSM_KEY_PRODUCT_ID,
    FSM_KEY_SELECTED_MODS,
    FSM_KEY_GROUP_RULES,
)

import payments

router = Router()
settings = get_settings()


# ----------------------------
# Helpers
# ----------------------------

def _safe_int(x: Any) -> int | None:
    try:
        return int(x)
    except Exception:
        return None


async def _get_selected_cafe_id(state: FSMContext) -> int | None:
    data = await state.get_data()
    return _safe_int(data.get(FSM_KEY_CAFE_ID))


async def _set_selected_cafe_id(state: FSMContext, cafe_id: int) -> None:
    await state.update_data({FSM_KEY_CAFE_ID: int(cafe_id)})


def _fmt_cart_text(cart: services.CartView) -> str:
    if not cart.lines:
        return "🧺 Корзина пустая."

    lines = []
    lines.append("🧺 <b>Корзина</b>\n")

    for ln in cart.lines:
        mods = ""
        if ln.modifiers:
            mods = " — " + ", ".join(
                f"{m.name}{' +' + str(m.price_delta) if m.price_delta != 0 else ''}".replace(".00", "")
                for m in ln.modifiers
            )

        status = ""
        if ln.blocked:
            status = f"\n⛔ <i>{ln.blocked_reason or 'Позиция недоступна'}</i>"

        lines.append(
            f"#{ln.cart_item_id} • <b>{ln.product_name}</b>{mods}\n"
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


async def _show_main(message_or_query: Message | CallbackQuery, *, edit: bool) -> None:
    text = "Меню:"
    if isinstance(message_or_query, Message):
        await message_or_query.answer(text, reply_markup=main_menu_kb())
    else:
        if edit:
            await message_or_query.message.edit_text(text, reply_markup=main_menu_kb())
        else:
            await message_or_query.message.answer(text, reply_markup=main_menu_kb())


async def _show_cafes(q: CallbackQuery, state: FSMContext) -> None:
    async with session_scope(settings) as s:
        cafes = await services.list_cafes(s)
    await q.message.edit_text("Выбери кофейню:", reply_markup=cafes_kb(cafes))
    await state.set_state(UserFlow.choosing_cafe)


async def _show_categories(q: CallbackQuery, state: FSMContext, cafe_id: int) -> None:
    async with session_scope(settings) as s:
        cats = await services.list_menu_categories(s, cafe_id)
    await _set_selected_cafe_id(state, cafe_id)
    await state.set_state(None)
    await q.message.edit_text("Категории:", reply_markup=categories_kb(cafe_id, cats))


async def _show_products(q: CallbackQuery, state: FSMContext, cafe_id: int, category_id: int | None) -> None:
    async with session_scope(settings) as s:
        prods = await services.list_menu_products(s, cafe_id, None if not category_id or category_id == 0 else category_id)
    await _set_selected_cafe_id(state, cafe_id)
    await state.set_state(None)
    await q.message.edit_text("Ассортимент:", reply_markup=products_kb(cafe_id, prods))


async def _show_product(q: CallbackQuery, state: FSMContext, cafe_id: int, product_id: int) -> None:
    async with session_scope(settings) as s:
        full = await services.get_product_full(s, cafe_id, product_id)

    p = full["product"]
    groups = full["groups"]
    has_mods = bool(groups)

    # price formatting for message (db gives Decimal-like; services works with Decimal, but here fine)
    price = f"{p['base_price']:.2f} ₽" if hasattr(p["base_price"], "quantize") else f"{float(p['base_price']):.2f} ₽"
    p_for_text = dict(p)
    p_for_text["base_price"] = price

    await _set_selected_cafe_id(state, cafe_id)
    await state.set_state(None)
    await q.message.edit_text(
        _fmt_product_text(p_for_text, groups),
        reply_markup=product_kb(cafe_id, product_id, has_modifiers=has_mods),
    )


async def _show_cart(q: CallbackQuery, state: FSMContext, cafe_id: int) -> None:
    async with session_scope(settings) as s:
        cart = await services.get_cart_view(s, settings, user_id=q.from_user.id, cafe_id=cafe_id)

    line_ids = [ln.cart_item_id for ln in cart.lines]
    await q.message.edit_text(
        _fmt_cart_text(cart),
        reply_markup=cart_kb(cafe_id=cafe_id, line_ids=line_ids, has_lines=bool(cart.lines)),
    )


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
    cafe_id = await _get_selected_cafe_id(state)
    if not cafe_id:
        await q.message.edit_text("Сначала выбери кофейню.", reply_markup=back_to_main_kb())
        return
    await _show_cart(q, state, cafe_id)


@router.callback_query(F.data.startswith("open:categories:"))
async def open_categories(q: CallbackQuery, state: FSMContext) -> None:
    await q.answer()
    parts = q.data.split(":")
    cafe_id = _safe_int(parts[-1])
    if not cafe_id:
        await q.message.edit_text("Не понял кофейню. Попробуй ещё раз.", reply_markup=back_to_main_kb())
        return
    await _show_categories(q, state, cafe_id)


@router.callback_query(F.data.startswith("open:products:"))
async def open_products(q: CallbackQuery, state: FSMContext) -> None:
    await q.answer()
    parts = q.data.split(":")
    cafe_id = _safe_int(parts[-1])
    if not cafe_id:
        await q.message.edit_text("Не понял кофейню. Попробуй ещё раз.", reply_markup=back_to_main_kb())
        return
    await _show_products(q, state, cafe_id, category_id=None)


@router.callback_query(F.data.startswith("open:mods:"))
async def open_mods(q: CallbackQuery, state: FSMContext) -> None:
    """
    Enter modifiers selection for product:
      open:mods:{cafe_id}:{product_id}
    """
    await q.answer()
    parts = q.data.split(":")
    if len(parts) != 4:
        await q.message.edit_text("Некорректная команда.", reply_markup=back_to_main_kb())
        return

    cafe_id = _safe_int(parts[2])
    product_id = _safe_int(parts[3])
    if not cafe_id or not product_id:
        await q.message.edit_text("Некорректные данные.", reply_markup=back_to_main_kb())
        return

    async with session_scope(settings) as s:
        # checks cafe exists + product exists + modifiers groups exist
        full = await services.get_product_full(s, cafe_id, product_id)

    groups = full["groups"]
    if not groups:
        # fallback to simple add
        await q.message.edit_text("У товара нет модификаторов.", reply_markup=product_kb(cafe_id, product_id, has_modifiers=False))
        return

    # pre-store rules and all modifiers by group in FSM to avoid DB on every toggle
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
    await q.message.edit_text(
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
        await q.message.edit_text(e.message, reply_markup=back_to_main_kb())


@router.callback_query(CategoryCB.filter())
async def on_category_selected(q: CallbackQuery, callback_data: CategoryCB, state: FSMContext) -> None:
    await q.answer()
    try:
        await _show_products(q, state, callback_data.cafe_id, callback_data.category_id)
    except services.ServiceError as e:
        await q.message.edit_text(e.message, reply_markup=back_to_main_kb())


@router.callback_query(ProductCB.filter())
async def on_product_open(q: CallbackQuery, callback_data: ProductCB, state: FSMContext) -> None:
    await q.answer()
    try:
        await _show_product(q, state, callback_data.cafe_id, callback_data.product_id)
    except services.ServiceError as e:
        await q.message.edit_text(e.message, reply_markup=back_to_main_kb())


@router.callback_query(CartOpenCB.filter())
async def on_cart_open(q: CallbackQuery, callback_data: CartOpenCB, state: FSMContext) -> None:
    await q.answer()
    try:
        await _show_cart(q, state, callback_data.cafe_id)
    except services.ServiceError as e:
        await q.message.edit_text(e.message, reply_markup=back_to_main_kb())


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
# Add to cart (no modifiers) — string callback from keyboards.product_kb
# ----------------------------

@router.callback_query(F.data.startswith("add:"))
async def on_add_simple(q: CallbackQuery, state: FSMContext) -> None:
    await q.answer()
    parts = q.data.split(":")
    if len(parts) != 3:
        await q.message.edit_text("Некорректная команда.", reply_markup=back_to_main_kb())
        return
    cafe_id = _safe_int(parts[1])
    product_id = _safe_int(parts[2])
    if not cafe_id or not product_id:
        await q.message.edit_text("Некорректные данные.", reply_markup=back_to_main_kb())
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
        await q.message.edit_text("Добавлено в корзину.", reply_markup=cart_kb(cafe_id=cafe_id, line_ids=[], has_lines=True))
        # сразу открыть корзину — лучше UX
        await _show_cart(q, state, cafe_id)
    except services.ServiceError as e:
        await q.message.edit_text(e.message, reply_markup=product_kb(cafe_id, product_id, has_modifiers=False))


# ----------------------------
# Modifiers flow
# ----------------------------

@router.callback_query(ModGroupCB.filter())
async def on_mod_group(q: CallbackQuery, callback_data: ModGroupCB, state: FSMContext) -> None:
    await q.answer()
    data = await state.get_data()

    if data.get(FSM_KEY_PRODUCT_ID) != callback_data.product_id or data.get(FSM_KEY_CAFE_ID) != callback_data.cafe_id:
        await q.message.edit_text("Сессия модификаторов устарела. Открой товар заново.", reply_markup=back_to_main_kb())
        await state.set_state(None)
        return

    mods_by_group = data.get("mods_by_group") or {}
    active_gid = callback_data.group_id
    selected = set(int(x) for x in (data.get(FSM_KEY_SELECTED_MODS) or []))

    # Need groups list for tabs -> load once from DB (редко: переключение группы)
    async with session_scope(settings) as s:
        full = await services.get_product_full(s, callback_data.cafe_id, callback_data.product_id)
    groups = full["groups"]

    mods = mods_by_group.get(str(active_gid))
    if mods is None:
        # fallback: from fresh full
        for g in groups:
            if int(g["id"]) == int(active_gid):
                mods = list(g.get("modifiers", []))
                break
        mods = mods or []

    await state.update_data({"active_group_id": int(active_gid)})

    await q.message.edit_text(
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
        await q.answer("Сессия устарела. Открой товар заново.", show_alert=True)
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

    # ids in this group
    group_mods = mods_by_group.get(str(gid), [])
    group_mod_ids = {int(m["id"]) for m in group_mods}

    mid = int(callback_data.modifier_id)

    if mid not in group_mod_ids:
        # safety: ignore foreign modifier
        await q.answer("Некорректный модификатор.", show_alert=True)
        return

    # toggle logic
    if mid in selected:
        selected.remove(mid)
    else:
        if max_sel <= 1:
            # replace selection in this group
            selected -= group_mod_ids
            selected.add(mid)
        else:
            # enforce max
            cur = len(selected & group_mod_ids)
            if cur + 1 > max_sel:
                await q.answer("Слишком много модификаторов в этой группе.", show_alert=True)
                return
            selected.add(mid)

    await state.update_data({FSM_KEY_SELECTED_MODS: sorted(selected)})

    # re-render current screen
    active_gid = int(data.get("active_group_id") or gid)
    # groups for tabs (fetch once; OK)
    async with session_scope(settings) as s:
        full = await services.get_product_full(s, callback_data.cafe_id, callback_data.product_id)
    groups = full["groups"]

    mods = mods_by_group.get(str(active_gid), [])
    await q.message.edit_text(
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
        await q.answer("Сессия устарела. Открой товар заново.", show_alert=True)
        await state.set_state(None)
        return

    selected = [int(x) for x in (data.get(FSM_KEY_SELECTED_MODS) or [])]
    group_rules = data.get(FSM_KEY_GROUP_RULES) or {}
    mods_by_group = data.get("mods_by_group") or {}

    # validate required/min/max purely from cached rules first (fast UX)
    for gid_str, rule in group_rules.items():
        gid = int(gid_str)
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

    # hard validate + stoplist + add to cart
    try:
        async with session_scope(settings) as s:
            # validate modifiers against DB (belonging/active)
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

        await state.set_state(None)
        await _set_selected_cafe_id(state, cafe_id)

        await q.message.edit_text("Добавлено в корзину.", reply_markup=CartOpenCB(cafe_id=cafe_id).as_markup() if False else None)
        # нормально: сразу открыть корзину
        await _show_cart(q, state, cafe_id)

    except services.ServiceError as e:
        await q.message.edit_text(e.message, reply_markup=back_to_main_kb())


# ----------------------------
# Checkout
# ----------------------------

@router.callback_query(CheckoutCB.filter())
async def on_checkout(q: CallbackQuery, callback_data: CheckoutCB, state: FSMContext) -> None:
    await q.answer()

    cafe_id = int(callback_data.cafe_id)

    try:
        async with session_scope(settings) as s:
            if not settings.PAYMENTS_ENABLED:
                res = await services.checkout_test_paid(s, settings, user_id=q.from_user.id, cafe_id=cafe_id)
                await q.message.edit_text(
                    f"✅ Заказ создан.\nКод получения: <b>{res.code}</b>\nСумма: <b>{res.total:.2f} ₽</b>",
                    reply_markup=checkout_kb(cafe_id=cafe_id, pay_url=None, payments_enabled=False),
                )
                return

            # боевой режим: создаём заказ, потом платёж
            res = await services.checkout_pending_payment(s, settings, user_id=q.from_user.id, cafe_id=cafe_id)
            pay = await payments.create_payment_for_order(
                s,
                settings,
                order_id=res.order_id,
                amount=res.total,
                user_id=q.from_user.id,
                cafe_id=cafe_id,
            )

            await q.message.edit_text(
                f"💳 Оплата заказа <b>#{res.order_id}</b>\nСумма: <b>{res.total:.2f} ₽</b>\n\n"
                f"После оплаты код появится в заказе.",
                reply_markup=checkout_kb(
                    cafe_id=cafe_id,
                    pay_url=pay.confirmation_url,
                    payments_enabled=True,
                ),
            )

    except services.ServiceError as e:
        await q.message.edit_text(e.message, reply_markup=back_to_main_kb())
    except payments.PaymentError as e:
        await q.message.edit_text(f"Ошибка оплаты: {e.message}", reply_markup=back_to_main_kb())


# ----------------------------
# Profile
# ----------------------------

@router.callback_query(ProfileCB.filter())
async def on_profile(q: CallbackQuery, state: FSMContext) -> None:
    await q.answer()
    async with session_scope(settings) as s:
        profile = await services.get_profile(s, q.from_user.id)
    await q.message.edit_text(_fmt_profile_text(profile), reply_markup=back_to_main_kb())
