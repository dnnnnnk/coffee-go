from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from .. import db


# --- Password hashing -------------------------------------------------------
# Prefer using a dedicated helper if present.
try:
    # optional module, if you created it earlier
    from ..utils.security import hash_password  # type: ignore
except Exception:  # pragma: no cover
    hash_password = None  # type: ignore


def _hash_password(p: str) -> str:
    """Hash password for seed accounts.

    Tries project helper first; otherwise falls back to bcrypt.
    """
    if hash_password is not None:
        return hash_password(p)  # type: ignore[misc]

    # Local fallback: use bcrypt directly (passlib has issues with bcrypt 4.x)
    import bcrypt  # type: ignore

    return bcrypt.hashpw(p.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


@dataclass(frozen=True)
class SeedCafe:
    name: str
    login: str
    password: str
    admin_password: str


async def seed_minimal_demo_data(session: AsyncSession) -> None:
    """Сиды для локального старта (если БД пустая).

    Создаёт 3 кофейни, категории, товары и модификаторы.
    Если cafes уже есть — ничего не делает.

    Важно: этот модуль не должен импортировать `services.py`.
    """

    now = db.utcnow()

    # If cafes exist - do nothing
    exists = (await session.execute(select(func.count()).select_from(db.cafes))).scalar_one()
    if int(exists) > 0:
        return

    # ---------------------- helpers ----------------------
    async def ins_cafe(c: SeedCafe) -> int:
        now_local = db.utcnow()
        values: dict[str, Any] = {
            "name": c.name,
            "is_active": True,
            "is_overloaded": False,
            "created_at": now_local,
            "updated_at": now_local,
        }

        # optional web auth columns
        if "web_login" in db.cafes.c:
            values["web_login"] = c.login
        if "web_password_hash" in db.cafes.c:
            values["web_password_hash"] = _hash_password(c.password)
        if "admin_password_hash" in db.cafes.c:
            values["admin_password_hash"] = _hash_password(c.admin_password)

        return int(
            (
                await session.execute(
                    db.cafes.insert().values(**values).returning(db.cafes.c.id)
                )
            ).scalar_one()
        )

    async def ins_cat(cafe_id: int, name: str, sort: int = 0) -> int:
        values: dict[str, Any] = {
            "cafe_id": cafe_id,
            "name": name,
            "is_active": True,
            "created_at": now,
            "updated_at": now,
        }
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
            if "sort" in db.modifiers.c:
                row["sort"] = idx
            payload.append(row)

        await session.execute(db.modifiers.insert(), payload)

    # ---------------------- seed data ----------------------
    cafes = [
        SeedCafe("Кофейня у дома", login="cafe1", password="1234", admin_password="admin1"),
        SeedCafe("Roast Lab", login="cafe2", password="1234", admin_password="admin2"),
        SeedCafe("Drip & Drop", login="cafe3", password="1234", admin_password="admin3"),
    ]

    # -------------------------------------------------------
    # 1) Кофейня у дома
    # -------------------------------------------------------
    cafe1 = await ins_cafe(cafes[0])
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
    cafe2 = await ins_cafe(cafes[1])
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
    cafe3 = await ins_cafe(cafes[2])
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
