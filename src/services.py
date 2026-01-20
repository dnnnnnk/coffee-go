# src/services.py
from __future__ import annotations
from sqlalchemy import select
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Iterable
from decimal import Decimal
from sqlalchemy.exc import IntegrityError

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import db


# ----------------------------
# Errors (user-friendly)
# ----------------------------

class ServiceError(Exception):
    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


class NotFound(ServiceError):
    pass


class Validation(ServiceError):
    pass


class Overloaded(ServiceError):
    pass


class Stoplisted(ServiceError):
    pass



class CartEmpty(ServiceError):
    pass

class InsufficientBalance(ServiceError):
    pass


# ----------------------------
# Money helpers
# ----------------------------

TWOPLACES = Decimal("0.01")


def d2(x: Any) -> Decimal:
    if isinstance(x, Decimal):
        return x.quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    return Decimal(str(x)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


def money_mul(price: Decimal, qty: int) -> Decimal:
    return (price * Decimal(qty)).quantize(TWOPLACES, rounding=ROUND_HALF_UP)


# ----------------------------
# Data returned to handlers
# ----------------------------

@dataclass(frozen=True)
class ModifierView:
    id: int
    name: str
    price_delta: Decimal


@dataclass(frozen=True)
class CartLineView:
    cart_item_id: int
    product_id: int
    product_name: str
    qty: int
    base_price: Decimal
    modifiers: list[ModifierView]
    unit_price: Decimal
    line_total: Decimal
    modifiers_key: str
    blocked: bool
    blocked_reason: str | None


@dataclass(frozen=True)
class CartView:
    cafe_id: int
    lines: list[CartLineView]
    total: Decimal


@dataclass(frozen=True)
class CheckoutResult:
    order_id: int
    code: str
    total: Decimal
    status: str  # paid | pending_payment
    payment_url: str | None = None


# ----------------------------
# Cafe / menu read
# ----------------------------

async def list_cafes(session: AsyncSession) -> list[dict]:
    return await db.list_active_cafes(session)


async def list_menu_categories(session: AsyncSession, cafe_id: int) -> list[dict]:
    cafe = await db.get_cafe(session, cafe_id)
    if not cafe or not cafe["is_active"]:
        raise NotFound("Кофейня не найдена.")
    return await db.list_categories(session, cafe_id)


async def list_menu_products(session: AsyncSession, cafe_id: int, category_id: int | None = None) -> list[dict]:
    cafe = await db.get_cafe(session, cafe_id)
    if not cafe or not cafe["is_active"]:
        raise NotFound("Кофейня не найдена.")
    return await db.list_products(session, cafe_id, category_id)


async def get_product_full(session: AsyncSession, cafe_id: int, product_id: int) -> dict:
    """
    Returns:
      {
        product: {...},
        groups: [
          {id,name,required,min_select,max_select,sort, modifiers:[{id,name,price_delta,sort},...]}
        ]
      }
    """
    p = await db.get_product(session, cafe_id, product_id)
    if not p:
        raise NotFound("Товар не найден.")
    if not p.get("is_active", True):
        raise Validation("Товар сейчас недоступен.")

    groups = await db.list_modifier_groups(session, product_id)
    out_groups: list[dict] = []
    for g in groups:
        mods = await db.list_modifiers(session, g["id"])
        out_groups.append({**g, "modifiers": mods})

    return {"product": p, "groups": out_groups}


# ----------------------------
# Stoplist / overload checks
# ----------------------------

async def ensure_can_order_from_cafe(session: AsyncSession, settings, cafe_id: int) -> None:
    cafe = await db.get_cafe(session, cafe_id)
    if not cafe or not cafe["is_active"]:
        raise NotFound("Кофейня не найдена.")
    if cafe["is_overloaded"]:
        raise Overloaded("Кофейня перегружена — заказы временно приостановлены.")

    if getattr(settings, "OVERLOAD_AUTO_ENABLED", False):
        active = await db.count_active_orders(session, cafe_id, set(settings.OVERLOAD_ACTIVE_STATUSES))
        if active >= int(settings.OVERLOAD_ACTIVE_ORDERS_LIMIT):
            raise Overloaded("Кофейня перегружена — заказы временно приостановлены.")


async def ensure_available(
    session: AsyncSession,
    cafe_id: int,
    product_id: int,
    modifier_ids: list[int],
) -> None:
    # Product stoplist
    if await db.is_product_blocked(session, cafe_id, product_id):
        raise Stoplisted("Этот товар сейчас недоступен в выбранной кофейне.")

    # Modifiers stoplist
    if modifier_ids:
        blocked = await db.find_blocked_modifiers(session, cafe_id, modifier_ids)
        if blocked:
            raise Stoplisted("Один из выбранных модификаторов сейчас недоступен в выбранной кофейне.")


# ----------------------------
# Modifier validation / pricing
# ----------------------------

async def _fetch_product_groups_and_mods(session: AsyncSession, product_id: int) -> tuple[list[dict], dict[int, dict]]:
    """
    Returns:
      groups: [{id, required, min_select, max_select, is_active}]
      mods_by_id: {modifier_id: {id,name,price_delta,group_id,is_active}}
    Includes inactive too (so we can error clearly).
    """
    # groups
    qg = select(
        db.modifier_groups.c.id,
        db.modifier_groups.c.required,
        db.modifier_groups.c.min_select,
        db.modifier_groups.c.max_select,
        db.modifier_groups.c.is_active,
    ).where(db.modifier_groups.c.product_id == product_id)
    groups = (await session.execute(qg)).mappings().all()
    groups_list = [dict(r) for r in groups]

    # modifiers
    qm = (
        select(
            db.modifiers.c.id,
            db.modifiers.c.name,
            db.modifiers.c.price_delta,
            db.modifiers.c.group_id,
            db.modifiers.c.is_active,
        )
        .select_from(db.modifiers.join(db.modifier_groups, db.modifier_groups.c.id == db.modifiers.c.group_id))
        .where(db.modifier_groups.c.product_id == product_id)
    )
    mods = (await session.execute(qm)).mappings().all()
    mods_by_id = {int(r["id"]): dict(r) for r in mods}
    return groups_list, mods_by_id


def _count_by_group(modifier_ids: list[int], mods_by_id: dict[int, dict]) -> dict[int, int]:
    counts: dict[int, int] = {}
    for mid in modifier_ids:
        m = mods_by_id.get(mid)
        if not m:
            continue
        gid = int(m["group_id"])
        counts[gid] = counts.get(gid, 0) + 1
    return counts


async def validate_modifier_selection(
    session: AsyncSession,
    *,
    product_id: int,
    modifier_ids: list[int],
) -> list[dict]:
    """
    Validates:
      - all modifier_ids belong to this product
      - modifiers/groups active
      - per-group min/max/required rules satisfied

    Returns normalized modifier rows (unique sorted), each:
      {id,name,price_delta,group_id}
    """
    modifier_ids = sorted({int(x) for x in modifier_ids}) if modifier_ids else []

    groups, mods_by_id = await _fetch_product_groups_and_mods(session, product_id)

    if not groups:
        if modifier_ids:
            raise Validation("У этого товара нет модификаторов.")
        return []

    # unknown or foreign modifier ids
    unknown = [mid for mid in modifier_ids if mid not in mods_by_id]
    if unknown:
        raise Validation("Выбраны некорректные модификаторы.")

    # inactive modifiers/groups
    for mid in modifier_ids:
        m = mods_by_id[mid]
        if not m["is_active"]:
            raise Validation("Выбранный модификатор сейчас недоступен.")
        # group active
        # (we already have groups list, but easiest: check group in groups map)
    groups_by_id = {int(g["id"]): g for g in groups}
    for mid in modifier_ids:
        gid = int(mods_by_id[mid]["group_id"])
        g = groups_by_id.get(gid)
        if not g or not g["is_active"]:
            raise Validation("Выбранный модификатор сейчас недоступен.")

    counts = _count_by_group(modifier_ids, mods_by_id)

    # enforce rules for every active group
    for g in groups:
        gid = int(g["id"])
        if not g["is_active"]:
            # if group inactive -> no modifiers allowed from it (already blocked above)
            continue
        c = counts.get(gid, 0)
        min_sel = int(g["min_select"])
        max_sel = int(g["max_select"])
        required = bool(g["required"])

        # required implies at least 1 unless min_select is higher
        effective_min = max(min_sel, 1) if required else min_sel

        if c < effective_min:
            raise Validation("Не выбраны обязательные модификаторы.")
        if c > max_sel:
            raise Validation("Слишком много модификаторов в одной группе.")

    # normalized rows
    normalized = []
    for mid in modifier_ids:
        m = mods_by_id[mid]
        normalized.append(
            {
                "id": int(m["id"]),
                "name": str(m["name"]),
                "price_delta": d2(m["price_delta"]),
                "group_id": int(m["group_id"]),
            }
        )
    return normalized


async def calc_unit_price(
    session: AsyncSession,
    *,
    cafe_id: int,
    product_id: int,
    modifier_ids: list[int],
) -> tuple[Decimal, Decimal, list[ModifierView], str]:
    """
    Returns: (base_price, unit_price, modifiers_view, modifiers_key)
    """
    p = await db.get_product(session, cafe_id, product_id)
    if not p:
        raise NotFound("Товар не найден.")
    if not p.get("is_active", True):
        raise Validation("Товар сейчас недоступен.")

    mods_rows = await validate_modifier_selection(session, product_id=product_id, modifier_ids=modifier_ids)
    modifiers_key = db.normalize_modifiers_key([m["id"] for m in mods_rows])

    base_price = d2(p["base_price"])
    delta_sum = sum((m["price_delta"] for m in mods_rows), Decimal("0.00"))
    unit_price = d2(base_price + delta_sum)

    mods_view = [ModifierView(id=m["id"], name=m["name"], price_delta=m["price_delta"]) for m in mods_rows]
    return base_price, unit_price, mods_view, modifiers_key


# ----------------------------
# Cart invariants
# ----------------------------

async def _get_user_cart_cafes(session: AsyncSession, user_id: int) -> list[int]:
    q = select(db.cart_items.c.cafe_id).where(db.cart_items.c.user_id == user_id).group_by(db.cart_items.c.cafe_id)
    rows = (await session.execute(q)).scalars().all()
    return [int(x) for x in rows]


async def ensure_cart_same_cafe(session: AsyncSession, user_id: int, cafe_id: int) -> None:
    cafes_in_cart = await _get_user_cart_cafes(session, user_id)
    if not cafes_in_cart:
        return
    # allow only one cafe cart at a time to avoid “смешал две кофейни”
    if len(cafes_in_cart) == 1 and cafes_in_cart[0] == cafe_id:
        return
    raise Validation("Корзина привязана к другой кофейне. Очисти корзину или вернись к той кофейне.")


# ----------------------------
# Cart operations
# ----------------------------

async def add_to_cart(
    session: AsyncSession,
    settings,
    *,
    user_id: int,
    cafe_id: int,
    product_id: int,
    modifier_ids: list[int] | None = None,
    qty: int = 1,
) -> int:
    """
    Adds item to cart. Returns cart_item_id.
    Checks:
      - cafe exists and not overloaded
      - cart is for this cafe
      - product/modifiers validation + stoplist
      - cart size/qty limits
    """
    qty = int(qty)
    if qty <= 0:
        raise Validation("Некорректное количество.")

    await db.ensure_user(session, user_id)
    await ensure_can_order_from_cafe(session, settings, cafe_id)
    await ensure_cart_same_cafe(session, user_id, cafe_id)

    # compute key + validate modifiers + ensure stoplist
    modifier_ids = [int(x) for x in (modifier_ids or [])]
    await ensure_available(session, cafe_id, product_id, modifier_ids)
    _, _, _, modifiers_key = await calc_unit_price(session, cafe_id=cafe_id, product_id=product_id, modifier_ids=modifier_ids)

    # enforce cart line count
    lines = await db.count_cart_lines(session, user_id, cafe_id)
    # if line exists, we can add without increasing lines. check by attempting update first.
    # We'll do a cheap existence check:
    q = select(db.cart_items.c.id, db.cart_items.c.qty).where(
        and_(
            db.cart_items.c.user_id == user_id,
            db.cart_items.c.cafe_id == cafe_id,
            db.cart_items.c.product_id == product_id,
            db.cart_items.c.modifiers_key == modifiers_key,
        )
    )
    existing = (await session.execute(q)).first()

    if existing is None and lines >= int(settings.MAX_CART_ITEMS):
        raise Validation("Слишком много позиций в корзине.")

    cart_item_id = await db.upsert_cart_line_qty(
        session, user_id=user_id, cafe_id=cafe_id, product_id=product_id, modifiers_key=modifiers_key, qty_delta=qty
    )

    # enforce per-line qty limit
    q2 = select(db.cart_items.c.qty).where(db.cart_items.c.id == cart_item_id)
    new_qty = int((await session.execute(q2)).scalar_one())
    if new_qty > int(settings.MAX_QTY_PER_ITEM):
        # clamp back to max
        await db.set_cart_line_qty(session, cart_item_id, user_id, int(settings.MAX_QTY_PER_ITEM))
        raise Validation("Слишком большое количество для одной позиции.")

    return cart_item_id


async def remove_cart_line(session: AsyncSession, user_id: int, cart_item_id: int) -> bool:
    await db.ensure_user(session, user_id)
    return await db.delete_cart_line(session, cart_item_id, user_id)


async def clear_cart(session: AsyncSession, user_id: int, cafe_id: int) -> int:
    await db.ensure_user(session, user_id)
    return await db.clear_cart(session, user_id, cafe_id)


async def get_cart_view(session: AsyncSession, settings, *, user_id: int, cafe_id: int) -> CartView:
    await db.ensure_user(session, user_id)
    await ensure_cart_same_cafe(session, user_id, cafe_id)

    raw_lines = await db.get_cart(session, user_id, cafe_id)
    lines: list[CartLineView] = []
    total = Decimal("0.00")

    # preload stoplist to mark lines (no hard fail in view)
    for r in raw_lines:
        cart_item_id = int(r["id"])
        product_id = int(r["product_id"])
        product_name = str(r["product_name"])
        base_price = d2(r["base_price"])
        qty = int(r["qty"])
        modifiers_key = str(r["modifiers_key"] or "")
        modifier_ids = [int(x) for x in modifiers_key.split(",") if x.strip()] if modifiers_key else []

        blocked = False
        reason = None

        try:
            # validate modifiers still valid + active
            mods_rows = await validate_modifier_selection(session, product_id=product_id, modifier_ids=modifier_ids)
            await ensure_available(session, cafe_id, product_id, [m["id"] for m in mods_rows])
            # compute price
            delta_sum = sum((m["price_delta"] for m in mods_rows), Decimal("0.00"))
            unit_price = d2(base_price + delta_sum)
            mods_view = [ModifierView(id=m["id"], name=m["name"], price_delta=m["price_delta"]) for m in mods_rows]
        except ServiceError as e:
            blocked = True
            reason = e.message
            unit_price = base_price
            mods_view = []
        except Exception:
            blocked = True
            reason = "Позиция сейчас недоступна."
            unit_price = base_price
            mods_view = []

        line_total = money_mul(unit_price, qty) if not blocked else money_mul(unit_price, qty)
        if not blocked:
            total = d2(total + line_total)

        lines.append(
            CartLineView(
                cart_item_id=cart_item_id,
                product_id=product_id,
                product_name=product_name,
                qty=qty,
                base_price=base_price,
                modifiers=mods_view,
                unit_price=unit_price,
                line_total=line_total,
                modifiers_key=modifiers_key,
                blocked=blocked,
                blocked_reason=reason,
            )
        )

    return CartView(cafe_id=cafe_id, lines=lines, total=total)


# ----------------------------
# Checkout / order creation (balance-based)
# ----------------------------

async def _assert_cart_ready_for_checkout(session: AsyncSession, settings, *, user_id: int, cafe_id: int) -> CartView:
    await ensure_can_order_from_cafe(session, settings, cafe_id)
    cart = await get_cart_view(session, settings, user_id=user_id, cafe_id=cafe_id)

    if not cart.lines:
        raise CartEmpty("Корзина пустая.")

    # block if any line blocked
    blocked = [ln for ln in cart.lines if ln.blocked]
    if blocked:
        # максимально честно: нельзя оформить пока есть недоступное
        raise Stoplisted("В корзине есть недоступные позиции. Удали их и попробуй снова.")

    if cart.total <= Decimal("0.00"):
        raise Validation("Некорректная сумма заказа.")

    return cart


async def get_balance(session: AsyncSession, user_id: int) -> Decimal:
    """Returns user balance as Decimal("0.00")."""
    await db.ensure_user(session, user_id)

    # Prefer db helper if exists
    if hasattr(db, "get_user_balance"):
        bal = await db.get_user_balance(session, user_id)
        return d2(bal)

    # Fallback: direct read from users table (requires `users.balance`)
    q = select(db.users.c.balance).where(db.users.c.id == user_id)
    val = (await session.execute(q)).scalar_one_or_none()
    return d2(val or 0)


async def create_topup_request(session: AsyncSession, settings, *, user_id: int, amount: Decimal) -> int:
    """Creates topup record in DB and returns topup_id. Payment creation happens in payments.py."""
    await db.ensure_user(session, user_id)

    amount = d2(amount)
    if amount <= Decimal("0.00"):
        raise Validation("Сумма должна быть больше 0.")

    min_amt = d2(getattr(settings, "MIN_TOPUP_AMOUNT", Decimal("10.00")))
    max_amt = d2(getattr(settings, "MAX_TOPUP_AMOUNT", Decimal("100000.00")))
    if amount < min_amt:
        raise Validation(f"Минимальная сумма пополнения: {min_amt:.2f} ₽")
    if amount > max_amt:
        raise Validation(f"Максимальная сумма пополнения: {max_amt:.2f} ₽")

    if not hasattr(db, "create_topup"):
        raise ServiceError("В db.py нет функции create_topup() для пополнений.")

    topup_id = await db.create_topup(session, user_id=user_id, amount=float(amount))
    return int(topup_id)


async def _debit_balance(session: AsyncSession, user_id: int, amount: Decimal) -> None:
    """Atomically decreases user balance."""
    amount = d2(amount)

    # Prefer db helper if exists
    if hasattr(db, "debit_user_balance"):
        await db.debit_user_balance(session, user_id=user_id, amount=float(amount))
        return

    if hasattr(db, "change_user_balance"):
        await db.change_user_balance(session, user_id=user_id, delta=float(-amount))
        return

    # Fallback: direct update (requires `users.balance`)
    now = db.utcnow() if hasattr(db, "utcnow") else None
    upd = db.users.update().where(db.users.c.id == user_id).values(
        balance=db.users.c.balance - float(amount),
        updated_at=now if now is not None else db.users.c.updated_at,
    )
    await session.execute(upd)


async def checkout_with_balance(
    session: AsyncSession,
    settings,
    *,
    user_id: int,
    cafe_id: int,
    pickup_type: str = "asap",
    pickup_at=None,
) -> CheckoutResult:
    """
    Creates PAID order by charging user's internal balance.
    - validates cart (stoplist/overload)
    - checks balance >= cart.total
    - creates order + snapshots
    - debits balance
    - clears cart
    """

    cart = await _assert_cart_ready_for_checkout(session, settings, user_id=user_id, cafe_id=cafe_id)

    balance = await get_balance(session, user_id)
    if balance < cart.total:
        need = d2(cart.total - balance)
        raise InsufficientBalance(f"Недостаточно средств. Не хватает: {need:.2f} ₽")

    # generate unique code + insert order (retry only on code collision)
    last_err: Exception | None = None
    order_id: int | None = None
    code: str | None = None

    for _ in range(10):
        code_try = settings.make_order_code()
        try:
            order_id = await db.create_order(
                session,
                code=code_try,
                user_id=user_id,
                cafe_id=cafe_id,
                status="paid",
                total=float(cart.total),
                pickup_type=pickup_type,
                pickup_at=pickup_at,
            )
            code = code_try
            break

        except IntegrityError as e:
            last_err = e

            # ✅ Ретраим ТОЛЬКО при конфликте уникальности кода
            msg = str(e).lower()
            is_code_collision = ("orders.code" in msg) and ("unique" in msg or "constraint" in msg)
            if is_code_collision:
                continue

            raise ServiceError(f"Ошибка базы при создании заказа: {e}") from e

    if order_id is None or code is None:
        raise ServiceError("Не удалось создать заказ (конфликт кода).") from last_err

    # write order items with snapshots
    for ln in cart.lines:
        modifier_ids = [int(x) for x in ln.modifiers_key.split(",") if x.strip()] if ln.modifiers_key else []

        mods_rows = await validate_modifier_selection(
            session,
            product_id=ln.product_id,
            modifier_ids=modifier_ids,
        )

        delta_sum = sum((m["price_delta"] for m in mods_rows), Decimal("0.00"))
        unit_price = d2(ln.base_price + delta_sum)
        line_total = money_mul(unit_price, ln.qty)

        order_item_id = await db.add_order_item(
            session,
            order_id=order_id,
            product_id=ln.product_id,
            title_snapshot=ln.product_name,
            base_price_snapshot=float(ln.base_price),
            modifiers_key=ln.modifiers_key,
            qty=ln.qty,
            line_total=float(line_total),
        )

        for m in mods_rows:
            await db.add_order_item_modifier(
                session,
                order_item_id=order_item_id,
                modifier_id=m["id"],
                title_snapshot=m["name"],
                price_delta_snapshot=float(m["price_delta"]),
            )

    # debit balance (same transaction)
    await _debit_balance(session, user_id, cart.total)

    # clear cart after order created
    await db.clear_cart(session, user_id, cafe_id)

    return CheckoutResult(
        order_id=int(order_id),
        code=str(code),
        total=cart.total,
        status="paid",
        payment_url=None,
    )

# ----------------------------
# Profile
# ----------------------------

async def get_profile(session: AsyncSession, user_id: int) -> dict:
    await db.ensure_user(session, user_id)
    return await db.get_user_stats(session, user_id)


# ----------------------------
# Admin/dev helpers (optional for seeding)
# ----------------------------

async def seed_minimal_demo_data(session: AsyncSession) -> None:
    """
    Нужен только для локального старта, если у тебя пустая БД.
    Ничего не ломает, если уже есть данные (по именам могут быть конфликты).
    Лучше вызывать вручную в dev.
    """
    now = db.utcnow()

    # if cafes exist - do nothing
    exists = (await session.execute(select(func.count()).select_from(db.cafes))).scalar_one()
    if int(exists) > 0:
        return

    # cafe
    cafe_id = int(
        (await session.execute(
            db.cafes.insert().values(name="Кофейня у дома", is_active=True, is_overloaded=False, created_at=now, updated_at=now
            ).returning(db.cafes.c.id)
        )).scalar_one()
    )

    # category
    cat_id = int(
        (await session.execute(
            db.categories.insert().values(cafe_id=cafe_id, name="Кофе", sort=0, is_active=True, created_at=now, updated_at=now
            ).returning(db.categories.c.id)
        )).scalar_one()
    )

    # product
    latte_id = int(
        (await session.execute(
            db.products.insert().values(
                cafe_id=cafe_id,
                category_id=cat_id,
                name="Латте",
                description="Классический латте",
                base_price=Decimal("220.00"),
                is_active=True,
                created_at=now,
                updated_at=now,
            ).returning(db.products.c.id)
        )).scalar_one()
    )

    # modifier group
    milk_group_id = int(
        (await session.execute(
            db.modifier_groups.insert().values(
                product_id=latte_id,
                name="Молоко",
                required=True,
                min_select=1,
                max_select=1,
                sort=0,
                is_active=True,
                created_at=now,
                updated_at=now,
            ).returning(db.modifier_groups.c.id)
        )).scalar_one()
    )

    # modifiers
    await session.execute(
        db.modifiers.insert(),
        [
            dict(group_id=milk_group_id, name="Обычное", price_delta=Decimal("0.00"), is_active=True, sort=0, created_at=now, updated_at=now),
            dict(group_id=milk_group_id, name="Овсяное", price_delta=Decimal("40.00"), is_active=True, sort=1, created_at=now, updated_at=now),
        ],
    )


async def build_order_receipt(session: AsyncSession, order_id: int) -> dict:
    """
    Возвращает данные для чека:
    {
      order: {...},
      cafe: {...},
      items: [
        {title, qty, line_total, modifiers:[{title, delta},...]}
      ]
    }
    """
    order = await db.get_order(session, order_id)
    if not order:
        raise NotFound("Заказ не найден.")

    cafe = await db.get_cafe(session, int(order["cafe_id"]))
    if not cafe:
        raise NotFound("Кофейня не найдена.")

    # items
    q_items = select(
        db.order_items.c.id,
        db.order_items.c.title_snapshot,
        db.order_items.c.qty,
        db.order_items.c.line_total,
        db.order_items.c.modifiers_key,
    ).where(db.order_items.c.order_id == order_id)

    items_rows = (await session.execute(q_items)).mappings().all()

    items: list[dict] = []
    for it in items_rows:
        item_id = int(it["id"])
        # modifiers snapshots
        q_mods = select(
            db.order_item_modifiers.c.title_snapshot,
            db.order_item_modifiers.c.price_delta_snapshot,
        ).where(db.order_item_modifiers.c.order_item_id == item_id)

        mods_rows = (await session.execute(q_mods)).mappings().all()
        mods = []
        for m in mods_rows:
            mods.append(
                {
                    "title": str(m["title_snapshot"]),
                    "delta": d2(m["price_delta_snapshot"]),
                }
            )

        items.append(
            {
                "title": str(it["title_snapshot"]),
                "qty": int(it["qty"]),
                "line_total": d2(it["line_total"]),
                "modifiers": mods,
            }
        )

    return {"order": order, "cafe": cafe, "items": items}


def format_receipt(receipt: dict) -> str:
    order = receipt["order"]
    cafe = receipt["cafe"]
    items = receipt["items"]

    order_id = int(order["id"])
    code = str(order["code"])
    total = d2(order["total"])
    cafe_name = str(cafe["name"])

    lines = []
    lines.append(f"✅ <b>Заказ создан</b> • <b>#{order_id}</b>")
    lines.append(f"🏪 Кофейня: <b>{cafe_name}</b>")
    lines.append(f"🔑 Код выдачи: <b>{code}</b>")
    pickup_type = str(order.get("pickup_type") or "asap")
    pickup_at = order.get("pickup_at")

    if pickup_type == "asap":
        lines.append("⏱️ Время: <b>как можно скорее</b>")
    else:
        # pickup_at может быть datetime или строка — покажем HH:MM
        try:
            hhmm = pickup_at.strftime("%H:%M")
        except Exception:
            hhmm = str(pickup_at)[:5]
        lines.append(f"🕒 Время: <b>к {hhmm}</b>")
    lines.append("")
    lines.append("🧾 <b>Состав заказа:</b>")

    for it in items:
        title = it["title"]
        qty = it["qty"]
        line_total = it["line_total"]
        if it["modifiers"]:
            mods = ", ".join(
                f"{m['title']}{f' +{m['delta']:.2f}₽' if m['delta'] != 0 else ''}".replace(".00", "")
                for m in it["modifiers"]
            )
            lines.append(f"• {title} ×{qty} — <b>{line_total:.2f} ₽</b>\n  <i>{mods}</i>")
        else:
            lines.append(f"• {title} ×{qty} — <b>{line_total:.2f} ₽</b>")

    lines.append("")
    lines.append(f"💰 Итого: <b>{total:.2f} ₽</b>")
    lines.append("Приходи, показывай код — и забирай ☕")


    return "\n".join(lines)