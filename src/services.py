# src/services.py
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from sqlalchemy import and_, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from passlib.hash import bcrypt
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
# Checkout / order creation (yookassa split)
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


# New order creation function for yookassa split (pending_payment)
async def _create_order_from_cart(
    session: AsyncSession,
    settings,
    *,
    user_id: int,
    cafe_id: int,
    status: str,
    pickup_type: str = "asap",
    pickup_at=None,
) -> CheckoutResult:
    """Creates an order from current cart and snapshots items/modifiers.

    NOTE: Payment is created separately in payments.py.
    """
    cart = await _assert_cart_ready_for_checkout(session, settings, user_id=user_id, cafe_id=cafe_id)

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
                status=status,
                total=float(cart.total),
                pickup_type=pickup_type,
                pickup_at=pickup_at,
            )
            code = code_try
            break

        except IntegrityError as e:
            last_err = e

            # retry ONLY if it's a unique collision on orders.code
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

    # clear cart after order created
    await db.clear_cart(session, user_id, cafe_id)

    return CheckoutResult(order_id=int(order_id), code=str(code), total=cart.total, status=status, payment_url=None)


# Public service function to create pending_payment order
async def checkout_pending_payment(
    session: AsyncSession,
    settings,
    *,
    user_id: int,
    cafe_id: int,
    pickup_type: str = "asap",
    pickup_at=None,
) -> CheckoutResult:
    """Creates an order with status=pending_payment (payment is created later in payments.py)."""
    return await _create_order_from_cart(
        session,
        settings,
        user_id=user_id,
        cafe_id=cafe_id,
        status="pending_payment",
        pickup_type=pickup_type,
        pickup_at=pickup_at,
    )

# ----------------------------
# Profile
# ----------------------------


async def get_profile(session: AsyncSession, user_id: int) -> dict:
    await db.ensure_user(session, user_id)
    return await db.get_user_stats(session, user_id)


# ----------------------------
# Web panel services (pure, no HTML)
# ----------------------------

def _verify_password(password: str, password_hash: str | None) -> bool:
    if not password_hash:
        return False

    # bcrypt hashes usually start with $2a$ / $2b$ / $2y$
    if password_hash.startswith("$2"):
        try:
            import bcrypt  # type: ignore

            return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
        except Exception:
            return False

    # fallback: plain string compare (dev only)
    return password == password_hash


async def web_auth_login(session: AsyncSession, *, login: str, password: str) -> int | None:
    """Return cafe_id if credentials are valid, otherwise None."""
    q = select(
        db.cafes.c.id,
        db.cafes.c.web_password_hash,
        db.cafes.c.is_active,
    ).where(db.cafes.c.web_login == login)

    row = (await session.execute(q)).mappings().first()
    if not row:
        return None

    if not bool(row.get("is_active", True)):
        return None

    if not _verify_password(password, row.get("web_password_hash")):
        return None

    return int(row["id"])


async def web_admin_unlock(session: AsyncSession, *, cafe_id: int, admin_password: str) -> bool:
    """Verifies admin password for a cafe."""
    q = select(db.cafes.c.admin_password_hash).where(db.cafes.c.id == cafe_id)
    row = (await session.execute(q)).mappings().first()
    if not row:
        return False
    return _verify_password(admin_password, row.get("admin_password_hash"))


async def web_list_orders(
    session: AsyncSession,
    *,
    cafe_id: int,
    status_filter: str | None = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    """List orders for cafe. Status filter is optional."""
    q = select(
        db.orders.c.id,
        db.orders.c.code,
        db.orders.c.user_id,
        db.orders.c.cafe_id,
        db.orders.c.status,
        db.orders.c.total,
        db.orders.c.pickup_type,
        db.orders.c.pickup_at,
        db.orders.c.created_at,
        db.orders.c.updated_at,
    ).where(db.orders.c.cafe_id == cafe_id)

    if status_filter:
        q = q.where(db.orders.c.status == status_filter)

    q = q.order_by(db.orders.c.created_at.desc()).limit(int(limit)).offset(int(offset))
    rows = (await session.execute(q)).mappings().all()
    return [dict(r) for r in rows]


async def web_update_order_status(
    session: AsyncSession,
    *,
    cafe_id: int,
    order_id: int,
    new_status: str,
) -> bool:
    """Updates order status if order belongs to cafe."""
    now = db.utcnow()

    q = (
        db.orders.update()
        .where(and_(db.orders.c.id == order_id, db.orders.c.cafe_id == cafe_id))
        .values(status=new_status, updated_at=now)
        .returning(db.orders.c.id)
    )

    row = (await session.execute(q)).first()
    return row is not None


async def web_get_stoplist(session: AsyncSession, *, cafe_id: int) -> dict:
    """Return stoplist ids for products and modifiers."""
    qp = select(db.stoplist_products.c.product_id).where(db.stoplist_products.c.cafe_id == cafe_id)
    qm = select(db.stoplist_modifiers.c.modifier_id).where(db.stoplist_modifiers.c.cafe_id == cafe_id)

    products = (await session.execute(qp)).scalars().all()
    modifiers = (await session.execute(qm)).scalars().all()

    return {
        "products": [int(x) for x in products],
        "modifiers": [int(x) for x in modifiers],
    }


async def web_set_stoplist_product(
    session: AsyncSession,
    *,
    cafe_id: int,
    product_id: int,
    enabled: bool,
) -> None:
    now = db.utcnow()

    if enabled:
        # insert if not exists
        q = db.stoplist_products.insert().values(
            cafe_id=cafe_id,
            product_id=product_id,
            created_at=now,
        )
        try:
            await session.execute(q)
        except Exception:
            # ignore duplicates
            pass
    else:
        q = db.stoplist_products.delete().where(
            and_(
                db.stoplist_products.c.cafe_id == cafe_id,
                db.stoplist_products.c.product_id == product_id,
            )
        )
        await session.execute(q)


async def web_set_stoplist_modifier(
    session: AsyncSession,
    *,
    cafe_id: int,
    modifier_id: int,
    enabled: bool,
) -> None:
    now = db.utcnow()

    if enabled:
        q = db.stoplist_modifiers.insert().values(
            cafe_id=cafe_id,
            modifier_id=modifier_id,
            created_at=now,
        )
        try:
            await session.execute(q)
        except Exception:
            pass
    else:
        q = db.stoplist_modifiers.delete().where(
            and_(
                db.stoplist_modifiers.c.cafe_id == cafe_id,
                db.stoplist_modifiers.c.modifier_id == modifier_id,
            )
        )
        await session.execute(q)


async def web_menu_read(session: AsyncSession, *, cafe_id: int) -> dict:
    """Read-only menu dump for cafe (categories -> products -> groups -> modifiers)."""
    cafe = await db.get_cafe(session, cafe_id)
    if not cafe:
        raise NotFound("Кофейня не найдена.")

    categories = await db.list_categories(session, cafe_id)
    products = await db.list_products(session, cafe_id, category_id=None)

    # group products by category
    prods_by_cat: dict[int, list[dict]] = {}
    for p in products:
        cid = int(p["category_id"])
        prods_by_cat.setdefault(cid, []).append(p)

    # load modifier groups/modifiers per product
    groups_by_prod: dict[int, list[dict]] = {}
    mods_by_group: dict[int, list[dict]] = {}

    for p in products:
        pid = int(p["id"])
        groups = await db.list_modifier_groups(session, pid)
        groups_by_prod[pid] = groups
        for g in groups:
            gid = int(g["id"])
            mods_by_group[gid] = await db.list_modifiers(session, gid)

    out_categories: list[dict] = []
    for c in categories:
        cid = int(c["id"])
        out_products: list[dict] = []
        for p in prods_by_cat.get(cid, []):
            pid = int(p["id"])
            out_groups: list[dict] = []
            for g in groups_by_prod.get(pid, []):
                gid = int(g["id"])
                out_groups.append({**g, "modifiers": mods_by_group.get(gid, [])})
            out_products.append({**p, "modifier_groups": out_groups})

        out_categories.append({**c, "products": out_products})

    return {
        "cafe": cafe,
        "categories": out_categories,
    }


# ----------------------------
# Menu CRUD (v1)
# ----------------------------

async def web_menu_crud_create_category(
    session: AsyncSession,
    *,
    cafe_id: int,
    name: str,
    sort: int = 0,
    is_active: bool = True,
) -> int:
    now = db.utcnow()
    values: dict[str, Any] = {
        "cafe_id": cafe_id,
        "name": name,
        "is_active": bool(is_active),
        "created_at": now,
        "updated_at": now,
    }
    if "sort" in db.categories.c:
        values["sort"] = int(sort)

    row = (
        await session.execute(
            db.categories.insert().values(**values).returning(db.categories.c.id)
        )
    ).scalar_one()
    return int(row)


async def web_menu_crud_update_category(
    session: AsyncSession,
    *,
    cafe_id: int,
    category_id: int,
    name: str | None = None,
    sort: int | None = None,
    is_active: bool | None = None,
) -> bool:
    now = db.utcnow()
    values: dict[str, Any] = {"updated_at": now}
    if name is not None:
        values["name"] = name
    if sort is not None and "sort" in db.categories.c:
        values["sort"] = int(sort)
    if is_active is not None:
        values["is_active"] = bool(is_active)

    q = (
        db.categories.update()
        .where(and_(db.categories.c.id == category_id, db.categories.c.cafe_id == cafe_id))
        .values(**values)
        .returning(db.categories.c.id)
    )
    return (await session.execute(q)).first() is not None


async def web_menu_crud_create_product(
    session: AsyncSession,
    *,
    cafe_id: int,
    category_id: int,
    name: str,
    base_price: Decimal,
    description: str | None = None,
    sort: int = 0,
    is_active: bool = True,
) -> int:
    now = db.utcnow()
    values: dict[str, Any] = {
        "cafe_id": cafe_id,
        "category_id": category_id,
        "name": name,
        "description": description,
        "base_price": d2(base_price),
        "is_active": bool(is_active),
        "created_at": now,
        "updated_at": now,
    }
    if "sort" in db.products.c:
        values["sort"] = int(sort)

    row = (
        await session.execute(
            db.products.insert().values(**values).returning(db.products.c.id)
        )
    ).scalar_one()
    return int(row)


async def web_menu_crud_update_product(
    session: AsyncSession,
    *,
    cafe_id: int,
    product_id: int,
    name: str | None = None,
    description: str | None = None,
    base_price: Decimal | None = None,
    category_id: int | None = None,
    sort: int | None = None,
    is_active: bool | None = None,
) -> bool:
    now = db.utcnow()
    values: dict[str, Any] = {"updated_at": now}

    if name is not None:
        values["name"] = name
    if description is not None:
        values["description"] = description
    if base_price is not None:
        values["base_price"] = d2(base_price)
    if category_id is not None:
        values["category_id"] = int(category_id)
    if sort is not None and "sort" in db.products.c:
        values["sort"] = int(sort)
    if is_active is not None:
        values["is_active"] = bool(is_active)

    q = (
        db.products.update()
        .where(and_(db.products.c.id == product_id, db.products.c.cafe_id == cafe_id))
        .values(**values)
        .returning(db.products.c.id)
    )
    return (await session.execute(q)).first() is not None


async def web_menu_crud_create_modifier_group(
    session: AsyncSession,
    *,
    product_id: int,
    name: str,
    required: bool,
    min_select: int,
    max_select: int,
    sort: int = 0,
    is_active: bool = True,
) -> int:
    now = db.utcnow()
    values: dict[str, Any] = {
        "product_id": int(product_id),
        "name": name,
        "required": bool(required),
        "min_select": int(min_select),
        "max_select": int(max_select),
        "is_active": bool(is_active),
        "created_at": now,
        "updated_at": now,
    }
    if "sort" in db.modifier_groups.c:
        values["sort"] = int(sort)

    row = (
        await session.execute(
            db.modifier_groups.insert().values(**values).returning(db.modifier_groups.c.id)
        )
    ).scalar_one()
    return int(row)


async def web_menu_crud_update_modifier_group(
    session: AsyncSession,
    *,
    group_id: int,
    name: str | None = None,
    required: bool | None = None,
    min_select: int | None = None,
    max_select: int | None = None,
    sort: int | None = None,
    is_active: bool | None = None,
) -> bool:
    now = db.utcnow()
    values: dict[str, Any] = {"updated_at": now}

    if name is not None:
        values["name"] = name
    if required is not None:
        values["required"] = bool(required)
    if min_select is not None:
        values["min_select"] = int(min_select)
    if max_select is not None:
        values["max_select"] = int(max_select)
    if sort is not None and "sort" in db.modifier_groups.c:
        values["sort"] = int(sort)
    if is_active is not None:
        values["is_active"] = bool(is_active)

    q = (
        db.modifier_groups.update()
        .where(db.modifier_groups.c.id == group_id)
        .values(**values)
        .returning(db.modifier_groups.c.id)
    )
    return (await session.execute(q)).first() is not None


async def web_menu_crud_create_modifier(
    session: AsyncSession,
    *,
    group_id: int,
    name: str,
    price_delta: Decimal,
    sort: int = 0,
    is_active: bool = True,
) -> int:
    now = db.utcnow()
    values: dict[str, Any] = {
        "group_id": int(group_id),
        "name": name,
        "price_delta": d2(price_delta),
        "is_active": bool(is_active),
        "created_at": now,
        "updated_at": now,
    }
    if "sort" in db.modifiers.c:
        values["sort"] = int(sort)

    row = (
        await session.execute(
            db.modifiers.insert().values(**values).returning(db.modifiers.c.id)
        )
    ).scalar_one()
    return int(row)


async def web_menu_crud_update_modifier(
    session: AsyncSession,
    *,
    modifier_id: int,
    name: str | None = None,
    price_delta: Decimal | None = None,
    sort: int | None = None,
    is_active: bool | None = None,
) -> bool:
    now = db.utcnow()
    values: dict[str, Any] = {"updated_at": now}

    if name is not None:
        values["name"] = name
    if price_delta is not None:
        values["price_delta"] = d2(price_delta)
    if sort is not None and "sort" in db.modifiers.c:
        values["sort"] = int(sort)
    if is_active is not None:
        values["is_active"] = bool(is_active)

    q = (
        db.modifiers.update()
        .where(db.modifiers.c.id == modifier_id)
        .values(**values)
        .returning(db.modifiers.c.id)
    )
    return (await session.execute(q)).first() is not None


# ----------------------------
# Admin/dev helpers (optional for seeding)
# ----------------------------

async def seed_minimal_demo_data(session: AsyncSession) -> None:
    """
    Сиды для локального старта (если БД пустая).
    Создаёт 3 кофейни, категории, товары и модификаторы.
    Если cafes уже есть — ничего не делает.
    """
    now = db.utcnow()

    exists = (await session.execute(select(func.count()).select_from(db.cafes))).scalar_one()
    if int(exists) > 0:
        return

    async def ins_cafe(name: str, login: str, password: str, admin_password: str = "admin") -> int:
        now = db.utcnow()
        return int(
            (await session.execute(
                db.cafes.insert().values(
                    name=name,
                    is_active=True,
                    is_overloaded=False,
                    created_at=now,
                    updated_at=now,

                    web_login=login,
                    web_password_hash=bcrypt.hash(password),
                    admin_password_hash=bcrypt.hash(admin_password),
                ).returning(db.cafes.c.id)
            )).scalar_one()
        )

    async def ins_cat(cafe_id: int, name: str, sort: int = 0) -> int:
        values: dict[str, Any] = {
            "cafe_id": cafe_id,
            "name": name,
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        }
        # optional column
        if "sort" in db.categories.c:
            values["sort"] = int(sort)

        return int(
            (
                await session.execute(
                    db.categories.insert().values(**values).returning(db.categories.c.id)
                )
            ).scalar_one()
        )

    async def ins_prod(
        cafe_id: int,
        category_id: int,
        name: str,
        base_price: str,
        description: str | None = None,
        sort: int = 0,
    ) -> int:
        values: dict[str, Any] = {
            "cafe_id": cafe_id,
            "category_id": category_id,
            "name": name,
            "description": description,
            "base_price": Decimal(base_price),
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        }
        # optional column
        if "sort" in db.products.c:
            values["sort"] = int(sort)

        return int(
            (
                await session.execute(
                    db.products.insert().values(**values).returning(db.products.c.id)
                )
            ).scalar_one()
        )

    async def ins_group(
        product_id: int,
        name: str,
        *,
        required: bool,
        min_select: int,
        max_select: int,
        sort: int = 0,
    ) -> int:
        values: dict[str, Any] = {
            "product_id": product_id,
            "name": name,
            "required": required,
            "min_select": min_select,
            "max_select": max_select,
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        }
        # optional column
        if "sort" in db.modifier_groups.c:
            values["sort"] = int(sort)

        return int(
            (
                await session.execute(
                    db.modifier_groups.insert().values(**values).returning(db.modifier_groups.c.id)
                )
            ).scalar_one()
        )

    async def ins_mods(group_id: int, mods: list[tuple[str, str]]) -> None:
        payload: list[dict[str, Any]] = []
        for idx, (n, d) in enumerate(mods):
            row: dict[str, Any] = {
                "group_id": group_id,
                "name": n,
                "price_delta": Decimal(d),
                "is_active": True,
                "created_at": now,
                "updated_at": now,
            }
            # optional column
            if "sort" in db.modifiers.c:
                row["sort"] = idx
            payload.append(row)

        await session.execute(db.modifiers.insert(), payload)

    # -------------------------------------------------------
    # 1) Кофейня у дома
    # -------------------------------------------------------
    cafe1 = await ins_cafe("Кофейня у дома", login="cafe1", password="1234", admin_password="admin1")
    c1_coffee = await ins_cat(cafe1, "Кофе", sort=0)
    c1_food = await ins_cat(cafe1, "Еда", sort=1)

    espresso = await ins_prod(cafe1, c1_coffee, "Эспрессо", "160.00", "Крепко и быстро")
    capp = await ins_prod(cafe1, c1_coffee, "Капучино", "210.00", "Классика на каждый день")
    latte = await ins_prod(cafe1, c1_coffee, "Латте", "230.00", "Мягкий, молочный")
    raf = await ins_prod(cafe1, c1_coffee, "Раф", "290.00", "Сливочный и сладкий")

    croissant = await ins_prod(cafe1, c1_food, "Круассан", "190.00", "Свежий, хрустящий")
    cheesecake = await ins_prod(cafe1, c1_food, "Чизкейк", "260.00", "Классический")

    # latte modifiers
    g_milk_latte = await ins_group(latte, "Молоко", required=True, min_select=1, max_select=1)
    await ins_mods(g_milk_latte, [("Обычное", "0.00"), ("Безлактозное", "20.00"), ("Овсяное", "40.00")])

    g_size_latte = await ins_group(latte, "Размер", required=True, min_select=1, max_select=1)
    await ins_mods(g_size_latte, [("S", "0.00"), ("M", "40.00"), ("L", "80.00")])

    g_syrup_latte = await ins_group(latte, "Сироп", required=False, min_select=0, max_select=2)
    await ins_mods(g_syrup_latte, [("Ваниль", "30.00"), ("Карамель", "30.00"), ("Лесной орех", "30.00")])

    # capp modifiers
    g_milk_capp = await ins_group(capp, "Молоко", required=True, min_select=1, max_select=1)
    await ins_mods(g_milk_capp, [("Обычное", "0.00"), ("Овсяное", "40.00")])

    g_temp_capp = await ins_group(capp, "Температура", required=True, min_select=1, max_select=1)
    await ins_mods(g_temp_capp, [("Обычная", "0.00"), ("Горячее", "0.00"), ("Тёплое", "0.00")])

    # raf modifiers
    g_size_raf = await ins_group(raf, "Размер", required=True, min_select=1, max_select=1)
    await ins_mods(g_size_raf, [("M", "0.00"), ("L", "70.00")])

    g_add_raf = await ins_group(raf, "Добавки", required=False, min_select=0, max_select=2)
    await ins_mods(g_add_raf, [("Корица", "10.00"), ("Шоколад", "20.00"), ("Взбитые сливки", "40.00")])

    # -------------------------------------------------------
    # 2) Roast Lab
    # -------------------------------------------------------
    cafe2 = await ins_cafe("Roast Lab", login="cafe2", password="1234", admin_password="admin2")
    c2_coffee = await ins_cat(cafe2, "Кофе", sort=0)
    c2_alt = await ins_cat(cafe2, "Альтернатива", sort=1)

    americano = await ins_prod(cafe2, c2_coffee, "Американо", "190.00", "Лонг кофе")
    flat = await ins_prod(cafe2, c2_coffee, "Флэт-уайт", "250.00", "Плотный и крепкий")
    v60 = await ins_prod(cafe2, c2_alt, "V60", "320.00", "Фильтр воронка")
    batch = await ins_prod(cafe2, c2_alt, "Фильтр-кофе", "240.00", "Batch brew")

    # americano modifiers
    g_size_amer = await ins_group(americano, "Размер", required=True, min_select=1, max_select=1)
    await ins_mods(g_size_amer, [("S", "0.00"), ("M", "30.00"), ("L", "60.00")])

    g_shot_amer = await ins_group(americano, "Шоты", required=False, min_select=0, max_select=2)
    await ins_mods(g_shot_amer, [("Доп. шот", "50.00")])

    # flat modifiers
    g_milk_flat = await ins_group(flat, "Молоко", required=True, min_select=1, max_select=1)
    await ins_mods(g_milk_flat, [("Обычное", "0.00"), ("Овсяное", "40.00")])

    g_size_flat = await ins_group(flat, "Размер", required=True, min_select=1, max_select=1)
    await ins_mods(g_size_flat, [("S", "0.00"), ("M", "40.00")])

    # v60 modifiers
    g_grind = await ins_group(v60, "Помол", required=True, min_select=1, max_select=1)
    await ins_mods(g_grind, [("Стандарт", "0.00"), ("Поуже", "0.00"), ("Пошире", "0.00")])

    g_profile = await ins_group(v60, "Профиль", required=True, min_select=1, max_select=1)
    await ins_mods(g_profile, [("Фрукты", "0.00"), ("Шоколад", "0.00"), ("Орехи", "0.00")])

    # batch modifiers
    g_size_batch = await ins_group(batch, "Размер", required=True, min_select=1, max_select=1)
    await ins_mods(g_size_batch, [("M", "0.00"), ("L", "50.00")])

    # -------------------------------------------------------
    # 3) Drip & Drop
    # -------------------------------------------------------
    cafe3 = await ins_cafe("Drip & Drop", login="cafe3", password="1234", admin_password="admin3")
    c3_coffee = await ins_cat(cafe3, "Кофе", sort=0)
    c3_special = await ins_cat(cafe3, "Спешл", sort=1)

    matcha = await ins_prod(cafe3, c3_special, "Матча латте", "310.00", "Японская матча")
    iced = await ins_prod(cafe3, c3_special, "Айс латте", "260.00", "Лёд + латте")
    cocoa = await ins_prod(cafe3, c3_special, "Какао", "240.00", "Шоколадный напиток")
    espresso_tonic = await ins_prod(cafe3, c3_coffee, "Эспрессо-тоник", "290.00", "Освежает")

    # matcha modifiers
    g_milk_matcha = await ins_group(matcha, "Молоко", required=True, min_select=1, max_select=1)
    await ins_mods(g_milk_matcha, [("Обычное", "0.00"), ("Кокосовое", "50.00"), ("Овсяное", "40.00")])

    g_sweet_matcha = await ins_group(matcha, "Сладость", required=True, min_select=1, max_select=1)
    await ins_mods(g_sweet_matcha, [("Без сахара", "0.00"), ("Обычно", "0.00"), ("Сладко", "0.00")])

    # iced latte modifiers
    g_milk_iced = await ins_group(iced, "Молоко", required=True, min_select=1, max_select=1)
    await ins_mods(g_milk_iced, [("Обычное", "0.00"), ("Овсяное", "40.00")])

    g_size_iced = await ins_group(iced, "Размер", required=True, min_select=1, max_select=1)
    await ins_mods(g_size_iced, [("M", "0.00"), ("L", "60.00")])

    # cocoa modifiers
    g_add_cocoa = await ins_group(cocoa, "Добавки", required=False, min_select=0, max_select=2)
    await ins_mods(g_add_cocoa, [("Маршмеллоу", "30.00"), ("Корица", "10.00"), ("Взбитые сливки", "40.00")])

    # espresso tonic modifiers
    g_tonic = await ins_group(espresso_tonic, "Тоник", required=True, min_select=1, max_select=1)
    await ins_mods(g_tonic, [("Обычный", "0.00"), ("Грейпфрут", "20.00"), ("Лайм", "20.00")])

    await session.commit()

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