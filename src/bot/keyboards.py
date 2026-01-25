# src/keyboards.py
from __future__ import annotations
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable
from src.models import PickupASAPCB, PickupTimeCB

from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.models import (
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
)

# ----------------------------
# Formatting helpers
# ----------------------------

TWOPLACES = Decimal("0.01")


def _fmt_money(x) -> str:
    d = x if isinstance(x, Decimal) else Decimal(str(x))
    d = d.quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    # "220.00 ₽"
    return f"{d:.2f} ₽"


def _cut(text: str, n: int = 40) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


# ----------------------------
# Main menu
# ----------------------------

def main_menu_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="☕ Кофейни", callback_data="open:cafes")
    b.button(text="🧺 Корзина", callback_data=CartOpenCB(cafe_id=0).pack())  # открыть корзину любой кофейни
    b.button(text="👤 Профиль", callback_data=ProfileCB().pack())
    b.adjust(1)
    return b.as_markup()

def pickup_choice_kb(cafe_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⚡ Как можно скорее", callback_data=PickupASAPCB(cafe_id=cafe_id).pack())
    b.button(text="🕒 Ко времени", callback_data=PickupTimeCB(cafe_id=cafe_id).pack())
    b.button(text="⬅️ Назад в корзину", callback_data=CartOpenCB(cafe_id=cafe_id).pack())
    b.adjust(1)
    return b.as_markup()

def payment_pending_kb(*, cafe_id: int, order_id: int, pay_url: str | None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()

    if pay_url:
        b.button(text="🔗 Перейти к оплате", url=pay_url)

    b.button(text="✅ Я оплатил", callback_data=f"order:check:{order_id}")
    b.button(text="🧺 Корзина", callback_data=CartOpenCB(cafe_id=cafe_id).pack())
    b.button(text="⬅️ Каталог", callback_data=f"open:categories:{cafe_id}")
    b.button(text="⬅️ В меню", callback_data="open:main")
    b.adjust(1)
    return b.as_markup()

def back_to_main_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⬅️ В меню", callback_data="open:main")
    return b.as_markup()


# ----------------------------
# Cafes
# cafes: list[dict{id,name,is_overloaded}]
# ----------------------------

def cafes_kb(cafes: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for c in cafes:
        name = str(c["name"])
        overloaded = bool(c.get("is_overloaded", False))
        text = f"{'⛔ ' if overloaded else ''}{_cut(name, 44)}"
        b.button(text=text, callback_data=CafeCB(cafe_id=int(c["id"])).pack())
    b.button(text="⬅️ В меню", callback_data="open:main")
    b.adjust(1)
    return b.as_markup()


# ----------------------------
# Catalog: categories + quick actions
# categories: list[dict{id,name}]
# ----------------------------

def categories_kb(cafe_id: int, categories: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    # "Все товары"
    b.button(text="📋 Все товары", callback_data=CategoryCB(cafe_id=cafe_id, category_id=0).pack())

    for cat in categories:
        b.button(
            text=_cut(str(cat["name"]), 44),
            callback_data=CategoryCB(cafe_id=cafe_id, category_id=int(cat["id"])).pack(),
        )

    b.button(text="🧺 Корзина", callback_data=CartOpenCB(cafe_id=cafe_id).pack())
    b.button(text="⬅️ Кофейни", callback_data="open:cafes")
    b.adjust(1)
    return b.as_markup()


# ----------------------------
# Products list
# products: list[dict{id,name,base_price,description}]
# ----------------------------

def products_kb(cafe_id: int, products: list[dict]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for p in products:
        name = _cut(str(p["name"]), 34)
        price = _fmt_money(p["base_price"])
        b.button(
            text=f"{name} — {price}",
            callback_data=ProductCB(cafe_id=cafe_id, product_id=int(p["id"])).pack(),
        )

    b.button(text="🧺 Корзина", callback_data=CartOpenCB(cafe_id=cafe_id).pack())
    b.button(text="⬅️ Категории", callback_data=f"open:categories:{cafe_id}")
    b.adjust(1)
    return b.as_markup()


# ----------------------------
# Product screen
# has_modifiers: bool
# ----------------------------

def product_kb(cafe_id: int, product_id: int, *, has_modifiers: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()

    if has_modifiers:
        # переход в режим выбора модификаторов (FSM заведём в handlers)
        b.button(text="Продолжить✅", callback_data=f"open:mods:{cafe_id}:{product_id}")
    else:
        # сразу добавить (без модификаторов)
        b.button(text="➕ В корзину", callback_data=f"add:{cafe_id}:{product_id}")

    b.button(text="🧺 Корзина", callback_data=CartOpenCB(cafe_id=cafe_id).pack())
    b.button(text="⬅️ Назад", callback_data=f"open:products:{cafe_id}")
    b.adjust(1)
    return b.as_markup()


# ----------------------------
# Modifiers flow
# groups: list[dict{id,name,required,min_select,max_select}]
# modifiers: list[dict{id,name,price_delta}]
# selected_ids: set[int]
# active_group_id: int
# ----------------------------

def modifiers_kb(
    *,
    cafe_id: int,
    product_id: int,
    groups: list[dict],
    modifiers: list[dict],
    selected_ids: set[int],
    active_group_id: int,
) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()

    # группы (как вкладки)
    for g in groups:
        gid = int(g["id"])
        req = bool(g.get("required", False))
        min_sel = int(g.get("min_select", 0))
        max_sel = int(g.get("max_select", 1))
        # посчитаем выбранные в группе по наличию в текущем списке модов группы — корректнее считать в handlers,
        # но для UX хватит “точки активности”.
        prefix = "✅ " if gid == active_group_id else ""
        suffix = " *" if req or min_sel > 0 else ""
        label = _cut(str(g["name"]), 22) + suffix
        b.button(text=f"{prefix}{label}", callback_data=ModGroupCB(cafe_id=cafe_id, product_id=product_id, group_id=gid).pack())

    # 2-3 группы в ряд, чтобы не было простыни
    if len(groups) <= 3:
        b.adjust(len(groups))
    else:
        b.adjust(3)

    # модификаторы активной группы
    for m in modifiers:
        mid = int(m["id"])
        checked = "☑️" if mid in selected_ids else "⬜️"
        name = _cut(str(m["name"]), 28)
        delta = Decimal(str(m["price_delta"]))
        delta_txt = ""
        if delta != 0:
            sign = "+" if delta > 0 else "−"
            delta_txt = f" ({sign}{_fmt_money(abs(delta)).replace(' ₽','')})"
        b.button(
            text=f"{checked} {name}{delta_txt}",
            callback_data=ToggleModCB(
                cafe_id=cafe_id, product_id=product_id, group_id=active_group_id, modifier_id=mid
            ).pack(),
        )

    # по одному в ряд (чтобы точно влезало и не ломало интерфейс)
    if modifiers:
        b.adjust(1)

    # действия
    b.button(text="Продолжить✅", callback_data=ModsDoneCB(cafe_id=cafe_id, product_id=product_id).pack())
    b.button(text="🧺 Корзина", callback_data=CartOpenCB(cafe_id=cafe_id).pack())
    b.button(text="⬅️ К товару", callback_data=ProductCB(cafe_id=cafe_id, product_id=product_id).pack())
    b.adjust(1)

    return b.as_markup()


# ----------------------------
# Cart
# lines: iterable of dicts from services CartView lines (handlers converts if нужно)
# Expect each line has: cart_item_id, title, qty, line_total, blocked
# ----------------------------

def cart_kb(*, cafe_id: int, line_pairs: Iterable[tuple[int, int]], has_lines: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()

    # кнопки удаления по позициям (показываем только id; текст строки — в сообщении)
    # если линий много — это будет простыня. но у нас MAX_CART_ITEMS ограничен.
    for idx, cid in line_pairs:
        b.button(
            text=f"❌ Удалить #{idx}",
            callback_data=CartDelCB(cafe_id=cafe_id, cart_item_id=int(cid)).pack(),
        )
    if line_pairs:
        b.adjust(1)

    if has_lines:
        b.button(text="💳 Оплатить", callback_data=CheckoutCB(cafe_id=cafe_id).pack())
        b.button(text="🧹 Очистить корзину", callback_data=CartClearCB(cafe_id=cafe_id).pack())

    b.button(text="⬅️ Каталог", callback_data=f"open:categories:{cafe_id}")
    b.button(text="⬅️ В меню", callback_data="open:main")
    b.adjust(1)

    return b.as_markup()


def checkout_kb(*, cafe_id: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🧺 Корзина", callback_data=CartOpenCB(cafe_id=cafe_id).pack())
    b.button(text="⬅️ Каталог", callback_data=f"open:categories:{cafe_id}")
    b.button(text="⬅️ В меню", callback_data="open:main")
    b.adjust(1)
    return b.as_markup()
