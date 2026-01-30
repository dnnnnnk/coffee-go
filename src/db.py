# src/db.py
from __future__ import annotations
import contextlib
from datetime import datetime, timezone
from typing import Iterable, Sequence

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    and_,
    text,
    delete,
    event,
    func,
    insert,
    or_,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool


# ----------------------------
# Schema / metadata
# ----------------------------
metadata = MetaData()

SCHEMA_VERSION = 4


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ----------------------------
# Tables
# ----------------------------

schema_meta = Table(
    "schema_meta",
    metadata,
    Column("id", Integer, primary_key=True),  # always 1
    Column("version", Integer, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

users = Table(
    "users",
    metadata,
    Column("id", Integer, primary_key=True),  # tg user id
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

cafes = Table(
    "cafes",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("name", String(128), nullable=False),
    Column("legal_name", String(256), nullable=True),
    Column("yookassa_account_id", String(128), nullable=True),
    Column("web_login", String(64), nullable=True),
    Column("web_password_hash", String(256), nullable=True),
    Column("admin_password_hash", String(256), nullable=True),
    Column("is_active", Boolean, nullable=False, server_default="1"),
    Column("is_overloaded", Boolean, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Index("ix_cafes_active", "is_active"),
)

categories = Table(
    "categories",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("cafe_id", Integer, ForeignKey("cafes.id", ondelete="CASCADE"), nullable=False),
    Column("name", String(128), nullable=False),
    Column("sort", Integer, nullable=False, server_default="0"),
    Column("is_active", Boolean, nullable=False, server_default="1"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("cafe_id", "name", name="uq_categories_cafe_name"),
    Index("ix_categories_cafe", "cafe_id"),
)

products = Table(
    "products",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("cafe_id", Integer, ForeignKey("cafes.id", ondelete="CASCADE"), nullable=False),
    Column("category_id", Integer, ForeignKey("categories.id", ondelete="SET NULL"), nullable=True),
    Column("name", String(128), nullable=False),
    Column("description", Text, nullable=True),
    Column("base_price", Numeric(10, 2), nullable=False),
    Column("sort", Integer, nullable=False, server_default="0"),
    Column("is_active", Boolean, nullable=False, server_default="1"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("cafe_id", "name", name="uq_products_cafe_name"),
    Index("ix_products_cafe", "cafe_id"),
    Index("ix_products_sort", "sort"),
    Index("ix_products_category", "category_id"),
    CheckConstraint("base_price >= 0", name="ck_products_base_price_nonneg"),
)

modifier_groups = Table(
    "modifier_groups",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("product_id", Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
    Column("name", String(128), nullable=False),
    Column("required", Boolean, nullable=False, server_default="0"),
    Column("min_select", Integer, nullable=False, server_default="0"),
    Column("max_select", Integer, nullable=False, server_default="1"),
    Column("sort", Integer, nullable=False, server_default="0"),
    Column("is_active", Boolean, nullable=False, server_default="1"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Index("ix_mod_groups_product", "product_id"),
    CheckConstraint("min_select >= 0", name="ck_mod_groups_min_nonneg"),
    CheckConstraint("max_select >= 0", name="ck_mod_groups_max_nonneg"),
    CheckConstraint("max_select >= min_select", name="ck_mod_groups_max_ge_min"),
)

modifiers = Table(
    "modifiers",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("group_id", Integer, ForeignKey("modifier_groups.id", ondelete="CASCADE"), nullable=False),
    Column("name", String(128), nullable=False),
    Column("price_delta", Numeric(10, 2), nullable=False, server_default="0"),
    Column("is_active", Boolean, nullable=False, server_default="1"),
    Column("sort", Integer, nullable=False, server_default="0"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("group_id", "name", name="uq_modifiers_group_name"),
    Index("ix_modifiers_group", "group_id"),
    CheckConstraint("price_delta >= -1000000", name="ck_modifiers_price_delta_sane"),
)

stoplist_products = Table(
    "stoplist_products",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("cafe_id", Integer, ForeignKey("cafes.id", ondelete="CASCADE"), nullable=False),
    Column("product_id", Integer, ForeignKey("products.id", ondelete="CASCADE"), nullable=False),
    Column("until_ts", DateTime(timezone=True), nullable=True),  # NULL = indefinitely
    Column("reason", String(256), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("cafe_id", "product_id", name="uq_stoplist_products"),
    Index("ix_stoplist_products_cafe", "cafe_id"),
)

stoplist_modifiers = Table(
    "stoplist_modifiers",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("cafe_id", Integer, ForeignKey("cafes.id", ondelete="CASCADE"), nullable=False),
    Column("modifier_id", Integer, ForeignKey("modifiers.id", ondelete="CASCADE"), nullable=False),
    Column("until_ts", DateTime(timezone=True), nullable=True),
    Column("reason", String(256), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint("cafe_id", "modifier_id", name="uq_stoplist_modifiers"),
    Index("ix_stoplist_modifiers_cafe", "cafe_id"),
)

cart_items = Table(
    "cart_items",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
    Column("cafe_id", Integer, ForeignKey("cafes.id", ondelete="CASCADE"), nullable=False),
    Column("product_id", Integer, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False),
    Column("modifiers_key", String(256), nullable=False, server_default=""),  # sorted modifier ids "1,5,9"
    Column("qty", Integer, nullable=False, server_default="1"),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    UniqueConstraint(
        "user_id", "cafe_id", "product_id", "modifiers_key", name="uq_cart_line_signature"
    ),
    Index("ix_cart_user_cafe", "user_id", "cafe_id"),
    CheckConstraint("qty >= 1", name="ck_cart_qty_ge_1"),
)

orders = Table(
    "orders",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("code", String(16), nullable=False),  # выдача по коду
    Column("user_id", Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
    Column("cafe_id", Integer, ForeignKey("cafes.id", ondelete="RESTRICT"), nullable=False),
    Column(
        "status",
        String(32),
        nullable=False,
        server_default="created",
        doc="created|pending_payment|paid|accepted|in_progress|done|canceled",
    ),
    Column("total", Numeric(10, 2), nullable=False, server_default="0"),
    Column("payment_provider", String(32), nullable=True),
    Column("provider_payment_id", String(128), nullable=True),
    Column("payment_status", String(32), nullable=False, server_default="pending"),
    Column("payment_confirmation_url", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("paid_at", DateTime(timezone=True), nullable=True),
    Column("pickup_type", String(16), nullable=False, server_default="asap"),  # asap | time
    Column("pickup_at", DateTime(timezone=False), nullable=True),
    Index("ix_orders_user", "user_id"),
    Index("ix_orders_cafe", "cafe_id"),
    Index("ix_orders_status", "status"),
    Index("ix_orders_payment", "provider_payment_id"),
    Index("ix_orders_payment_status", "payment_status"),
    UniqueConstraint("code", name="uq_orders_code"),
    CheckConstraint("total >= 0", name="ck_orders_total_nonneg"),
)

order_items = Table(
    "order_items",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("order_id", Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
    Column("product_id", Integer, ForeignKey("products.id", ondelete="RESTRICT"), nullable=False),
    Column("title_snapshot", String(160), nullable=False),
    Column("base_price_snapshot", Numeric(10, 2), nullable=False),
    Column("modifiers_key", String(256), nullable=False, server_default=""),
    Column("qty", Integer, nullable=False),
    Column("line_total", Numeric(10, 2), nullable=False),
    Index("ix_order_items_order", "order_id"),
    CheckConstraint("qty >= 1", name="ck_order_items_qty_ge_1"),
    CheckConstraint("line_total >= 0", name="ck_order_items_line_total_nonneg"),
)

order_item_modifiers = Table(
    "order_item_modifiers",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("order_item_id", Integer, ForeignKey("order_items.id", ondelete="CASCADE"), nullable=False),
    Column("modifier_id", Integer, ForeignKey("modifiers.id", ondelete="RESTRICT"), nullable=False),
    Column("title_snapshot", String(160), nullable=False),
    Column("price_delta_snapshot", Numeric(10, 2), nullable=False),
    Index("ix_order_item_mods_item", "order_item_id"),
)


# ----------------------------
# Order status events (outbox)
# ----------------------------
order_status_events = Table(
    "order_status_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("order_id", Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False),
    Column("user_id", Integer, ForeignKey("users.id", ondelete="RESTRICT"), nullable=False),
    Column("old_status", String(32), nullable=True),
    Column("new_status", String(32), nullable=False),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("last_error", Text, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("sent_at", DateTime(timezone=True), nullable=True),
    Index("ix_order_status_events_sent_at", "sent_at"),
    Index("ix_order_status_events_order", "order_id"),
    Index("ix_order_status_events_user", "user_id"),
)




# ----------------------------
# Engine / session
# ----------------------------
_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None

async def web_list_products_with_stoplist(session: AsyncSession, *, cafe_id: int) -> list[dict]:
    # все товары кофейни + флаг in_stoplist
    q = (
        select(
            products.c.id,
            products.c.name,
            products.c.is_active,
            (stoplist_products.c.id.is_not(None)).label("in_stoplist"),
        )
        .select_from(
            products.outerjoin(
                stoplist_products,
                and_(
                    stoplist_products.c.cafe_id == cafe_id,
                    stoplist_products.c.product_id == products.c.id,
                    or_(stoplist_products.c.until_ts.is_(None), stoplist_products.c.until_ts > utcnow()),
                ),
            )
        )
        .where(products.c.cafe_id == cafe_id)
        .order_by(products.c.sort.asc(), products.c.id.asc())
    )
    rows = (await session.execute(q)).mappings().all()
    return [dict(r) for r in rows]


async def web_list_modifiers_with_stoplist(session: AsyncSession, *, cafe_id: int) -> list[dict]:
    # все модификаторы кофейни (через product) + group_name/product_name + in_stoplist
    q = (
        select(
            modifiers.c.id,
            modifiers.c.name,
            modifiers.c.is_active,
            modifier_groups.c.name.label("group_name"),
            products.c.name.label("product_name"),
            (stoplist_modifiers.c.id.is_not(None)).label("in_stoplist"),
        )
        .select_from(
            modifiers
            .join(modifier_groups, modifier_groups.c.id == modifiers.c.group_id)
            .join(products, products.c.id == modifier_groups.c.product_id)
            .outerjoin(
                stoplist_modifiers,
                and_(
                    stoplist_modifiers.c.cafe_id == cafe_id,
                    stoplist_modifiers.c.modifier_id == modifiers.c.id,
                    or_(stoplist_modifiers.c.until_ts.is_(None), stoplist_modifiers.c.until_ts > utcnow()),
                ),
            )
        )
        .where(products.c.cafe_id == cafe_id)
        .order_by(products.c.id.asc(), modifier_groups.c.sort.asc(), modifiers.c.sort.asc(), modifiers.c.id.asc())
    )
    rows = (await session.execute(q)).mappings().all()
    return [dict(r) for r in rows]

def _apply_sqlite_pragmas(sync_engine: Engine, *, wal: bool, timeout_sec: int, foreign_keys: bool) -> None:
    @event.listens_for(sync_engine, "connect")
    def _on_connect(dbapi_connection, _):
        cur = dbapi_connection.cursor()
        try:
            # busy timeout (ms)
            cur.execute(f"PRAGMA busy_timeout = {int(timeout_sec * 1000)}")
            if wal:
                cur.execute("PRAGMA journal_mode = WAL")
                cur.execute("PRAGMA synchronous = NORMAL")
            if foreign_keys:
                cur.execute("PRAGMA foreign_keys = ON")
            # reasonable defaults
            cur.execute("PRAGMA temp_store = MEMORY")
        finally:
            cur.close()


def create_engine_and_sessionmaker(settings) -> tuple[AsyncEngine, async_sessionmaker[AsyncSession]]:
    """
    settings must provide:
      - db_url
      - DB_ECHO_SQL, DB_WAL, DB_TIMEOUT_SEC, DB_FOREIGN_KEYS
    """
    db_url = str(settings.db_url)
    is_sqlite = db_url.startswith("sqlite")

    engine_kwargs: dict = {
        "echo": bool(settings.DB_ECHO_SQL),
        "future": True,
    }

    # SQLite: no real pooling, and apply pragmas (WAL/timeout/fk)
    if is_sqlite:
        engine_kwargs["poolclass"] = NullPool
    else:
        # Postgres/MySQL etc
        engine_kwargs["pool_size"] = int(getattr(settings, "DB_POOL_SIZE", 5))
        engine_kwargs["pool_pre_ping"] = True

    engine = create_async_engine(settings.db_url, **engine_kwargs)

    if is_sqlite:
        _apply_sqlite_pragmas(
            engine.sync_engine,
            wal=bool(settings.DB_WAL),
            timeout_sec=int(settings.DB_TIMEOUT_SEC),
            foreign_keys=bool(settings.DB_FOREIGN_KEYS),
        )

    sm = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)
    return engine, sm


def get_engine(settings) -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None or _sessionmaker is None:
        _engine, _sessionmaker = create_engine_and_sessionmaker(settings)
    return _engine


def get_sessionmaker(settings) -> async_sessionmaker[AsyncSession]:
    global _engine, _sessionmaker
    if _engine is None or _sessionmaker is None:
        _engine, _sessionmaker = create_engine_and_sessionmaker(settings)
    return _sessionmaker


@contextlib.asynccontextmanager
async def session_scope(settings) -> AsyncSession:
    """
    Usage:
      async with session_scope(settings) as s:
          ...
    Commits on success, rollbacks on exception.
    """
    sm = get_sessionmaker(settings)
    async with sm() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ----------------------------
# Init / migrations
# ----------------------------
async def init_db(settings) -> None:
    """
    Creates schema and applies simple versioned migrations.
    For now: SCHEMA_VERSION=1 and we just create_all.
    """

    engine = get_engine(settings)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.create_all)

        # --- lightweight migrations for Postgres (older dev dbs) ---
        if conn.dialect.name == "postgresql":
            async def _pg_has_column(table: str, column: str) -> bool:
                q = text("""
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_schema = current_schema()
                      AND table_name = :table
                      AND column_name = :column
                    LIMIT 1
                """)
                res = await conn.execute(q, {"table": table, "column": column})
                return res.first() is not None

            # cafes: split payments / legal fields
            if not await _pg_has_column("cafes", "legal_name"):
                await conn.exec_driver_sql("ALTER TABLE cafes ADD COLUMN legal_name TEXT;")
            if not await _pg_has_column("cafes", "yookassa_account_id"):
                await conn.exec_driver_sql("ALTER TABLE cafes ADD COLUMN yookassa_account_id TEXT;")
            if not await _pg_has_column("cafes", "web_login"):
                await conn.exec_driver_sql("ALTER TABLE cafes ADD COLUMN web_login TEXT;")
            if not await _pg_has_column("cafes", "web_password_hash"):
                await conn.exec_driver_sql("ALTER TABLE cafes ADD COLUMN web_password_hash TEXT;")
            if not await _pg_has_column("cafes", "admin_password_hash"):
                await conn.exec_driver_sql("ALTER TABLE cafes ADD COLUMN admin_password_hash TEXT;")

            # orders: payment fields
            if not await _pg_has_column("orders", "payment_provider"):
                await conn.exec_driver_sql("ALTER TABLE orders ADD COLUMN payment_provider TEXT;")
            if not await _pg_has_column("orders", "provider_payment_id"):
                await conn.exec_driver_sql("ALTER TABLE orders ADD COLUMN provider_payment_id TEXT;")
            if not await _pg_has_column("orders", "payment_status"):
                await conn.exec_driver_sql("ALTER TABLE orders ADD COLUMN payment_status TEXT NOT NULL DEFAULT 'pending';")
            if not await _pg_has_column("orders", "payment_confirmation_url"):
                await conn.exec_driver_sql("ALTER TABLE orders ADD COLUMN payment_confirmation_url TEXT;")
            if not await _pg_has_column("orders", "paid_at"):
                await conn.exec_driver_sql("ALTER TABLE orders ADD COLUMN paid_at TIMESTAMPTZ;")
            if not await _pg_has_column("orders", "pickup_type"):
                await conn.exec_driver_sql("ALTER TABLE orders ADD COLUMN pickup_type TEXT NOT NULL DEFAULT 'asap';")
            if not await _pg_has_column("orders", "pickup_at"):
                await conn.exec_driver_sql("ALTER TABLE orders ADD COLUMN pickup_at TIMESTAMP;")

            # orders: paid_at
            if not await _pg_has_column("orders", "paid_at"):
                await conn.exec_driver_sql("ALTER TABLE orders ADD COLUMN paid_at TIMESTAMPTZ;")

            # orders: pickup fields
            if not await _pg_has_column("orders", "pickup_type"):
                await conn.exec_driver_sql("ALTER TABLE orders ADD COLUMN pickup_type TEXT NOT NULL DEFAULT 'asap';")
            if not await _pg_has_column("orders", "pickup_at"):
                await conn.exec_driver_sql("ALTER TABLE orders ADD COLUMN pickup_at TIMESTAMP;")

        # --- lightweight migrations for SQLite only (older dev dbs) ---
        if conn.dialect.name == "sqlite":
            res = await conn.exec_driver_sql("PRAGMA table_info(orders);")
            cols = {row[1] for row in res.fetchall()}  # row[1] = name
            if "created_at" not in cols:
                await conn.exec_driver_sql(
                    "ALTER TABLE orders ADD COLUMN created_at DATETIME NOT NULL DEFAULT '1970-01-01 00:00:00';"
                )
                await conn.exec_driver_sql(
                    "UPDATE orders SET created_at = CURRENT_TIMESTAMP WHERE created_at = '1970-01-01 00:00:00';"
                )
            if "pickup_type" not in cols:
                await conn.exec_driver_sql("ALTER TABLE orders ADD COLUMN pickup_type TEXT NOT NULL DEFAULT 'asap';")

            if "pickup_at" not in cols:
                await conn.exec_driver_sql("ALTER TABLE orders ADD COLUMN pickup_at DATETIME NULL;")

            # --- orders payment fields ---
            if "payment_provider" not in cols:
                await conn.exec_driver_sql("ALTER TABLE orders ADD COLUMN payment_provider TEXT NULL;")
            if "provider_payment_id" not in cols:
                await conn.exec_driver_sql("ALTER TABLE orders ADD COLUMN provider_payment_id TEXT NULL;")
            if "payment_status" not in cols:
                await conn.exec_driver_sql("ALTER TABLE orders ADD COLUMN payment_status TEXT NOT NULL DEFAULT 'pending';")
            if "payment_confirmation_url" not in cols:
                await conn.exec_driver_sql("ALTER TABLE orders ADD COLUMN payment_confirmation_url TEXT NULL;")

            res_c = await conn.exec_driver_sql("PRAGMA table_info(cafes);")
            ccols = {row[1] for row in res_c.fetchall()}
            if "legal_name" not in ccols:
                await conn.exec_driver_sql("ALTER TABLE cafes ADD COLUMN legal_name TEXT NULL;")
            if "yookassa_account_id" not in ccols:
                await conn.exec_driver_sql("ALTER TABLE cafes ADD COLUMN yookassa_account_id TEXT NULL;")
            if "web_login" not in ccols:
                await conn.exec_driver_sql("ALTER TABLE cafes ADD COLUMN web_login TEXT NULL;")
            if "web_password_hash" not in ccols:
                await conn.exec_driver_sql("ALTER TABLE cafes ADD COLUMN web_password_hash TEXT NULL;")
            if "admin_password_hash" not in ccols:
                await conn.exec_driver_sql("ALTER TABLE cafes ADD COLUMN admin_password_hash TEXT NULL;")

            # --- users updated_at column (safety for older dbs) ---
            res_u = await conn.exec_driver_sql("PRAGMA table_info(users);")
            ucols = {row[1] for row in res_u.fetchall()}
            if "updated_at" not in ucols:
                await conn.exec_driver_sql(
                    "ALTER TABLE users ADD COLUMN updated_at DATETIME NOT NULL DEFAULT '1970-01-01 00:00:00';"
                )
                await conn.exec_driver_sql(
                    "UPDATE users SET updated_at = CURRENT_TIMESTAMP WHERE updated_at = '1970-01-01 00:00:00';"
                )

            # --- products sort column (for menu ordering) ---
            res_p = await conn.exec_driver_sql("PRAGMA table_info(products);")
            pcols = {row[1] for row in res_p.fetchall()}
            if "sort" not in pcols:
                await conn.exec_driver_sql("ALTER TABLE products ADD COLUMN sort INTEGER NOT NULL DEFAULT 0;")

        # ensure schema_meta row
        res = await conn.execute(select(schema_meta.c.version).where(schema_meta.c.id == 1))
        row = res.first()
        now = utcnow()
        if row is None:
            await conn.execute(
                insert(schema_meta).values(id=1, version=SCHEMA_VERSION, updated_at=now)
            )
        else:
            db_ver = int(row[0])
            if db_ver > SCHEMA_VERSION:
                raise RuntimeError(f"DB schema version {db_ver} is newer than app {SCHEMA_VERSION}")
            if db_ver < SCHEMA_VERSION:
                # place for future migrations
                # apply_migrations(conn, db_ver, SCHEMA_VERSION)
                await conn.execute(
                    update(schema_meta)
                    .where(schema_meta.c.id == 1)
                    .values(version=SCHEMA_VERSION, updated_at=now)
                )


# ----------------------------
# Helpers
# ----------------------------
async def ensure_user(session: AsyncSession, user_id: int) -> None:
    res = await session.execute(select(users.c.id).where(users.c.id == user_id))
    if res.first() is None:
        now = utcnow()
        await session.execute(
            insert(users).values(id=user_id, created_at=now, updated_at=now)
        )


def normalize_modifiers_key(modifier_ids: Iterable[int] | None) -> str:
    if not modifier_ids:
        return ""
    uniq_sorted = sorted({int(x) for x in modifier_ids})
    return ",".join(str(x) for x in uniq_sorted)


def _not_expired(until_ts, at: datetime) -> bool:
    # not used directly in SQL; left for parity
    return until_ts is None or until_ts > at


# ----------------------------
# Cafes / menu read
# ----------------------------
async def list_active_cafes(session: AsyncSession) -> list[dict]:
    q = (
        select(cafes.c.id, cafes.c.name, cafes.c.is_overloaded)
        .where(cafes.c.is_active == True)  # noqa: E712
        .order_by(cafes.c.id.asc())
    )
    rows = (await session.execute(q)).mappings().all()
    return [dict(r) for r in rows]


async def get_cafe(session: AsyncSession, cafe_id: int) -> dict | None:
    q = select(cafes).where(cafes.c.id == cafe_id)
    row = (await session.execute(q)).mappings().first()
    return dict(row) if row else None


async def set_cafe_overload(session: AsyncSession, cafe_id: int, is_overloaded_flag: bool) -> bool:
    q = (
        update(cafes)
        .where(cafes.c.id == cafe_id)
        .values(is_overloaded=bool(is_overloaded_flag), updated_at=utcnow())
    )
    res = await session.execute(q)
    return res.rowcount > 0


async def list_categories(session: AsyncSession, cafe_id: int) -> list[dict]:
    q = (
        select(categories.c.id, categories.c.name, categories.c.sort)
        .where(and_(categories.c.cafe_id == cafe_id, categories.c.is_active == True))  # noqa: E712
        .order_by(categories.c.sort.asc(), categories.c.id.asc())
    )
    rows = (await session.execute(q)).mappings().all()
    return [dict(r) for r in rows]


async def list_products(
    session: AsyncSession,
    cafe_id: int,
    category_id: int | None = None,
) -> list[dict]:
    cond = [products.c.cafe_id == cafe_id, products.c.is_active == True]  # noqa: E712
    if category_id is not None:
        cond.append(products.c.category_id == category_id)
    q = (
        select(
            products.c.id,
            products.c.name,
            products.c.description,
            products.c.base_price,
            products.c.category_id,
        )
        .where(and_(*cond))
        .order_by(products.c.id.asc())
    )
    rows = (await session.execute(q)).mappings().all()
    return [dict(r) for r in rows]


async def get_product(session: AsyncSession, cafe_id: int, product_id: int) -> dict | None:
    q = select(products).where(and_(products.c.id == product_id, products.c.cafe_id == cafe_id))
    row = (await session.execute(q)).mappings().first()
    return dict(row) if row else None


async def list_modifier_groups(session: AsyncSession, product_id: int) -> list[dict]:
    q = (
        select(
            modifier_groups.c.id,
            modifier_groups.c.name,
            modifier_groups.c.required,
            modifier_groups.c.min_select,
            modifier_groups.c.max_select,
            modifier_groups.c.sort,
        )
        .where(and_(modifier_groups.c.product_id == product_id, modifier_groups.c.is_active == True))  # noqa: E712
        .order_by(modifier_groups.c.sort.asc(), modifier_groups.c.id.asc())
    )
    rows = (await session.execute(q)).mappings().all()
    return [dict(r) for r in rows]


async def list_modifiers(session: AsyncSession, group_id: int) -> list[dict]:
    q = (
        select(modifiers.c.id, modifiers.c.name, modifiers.c.price_delta, modifiers.c.sort)
        .where(and_(modifiers.c.group_id == group_id, modifiers.c.is_active == True))  # noqa: E712
        .order_by(modifiers.c.sort.asc(), modifiers.c.id.asc())
    )
    rows = (await session.execute(q)).mappings().all()
    return [dict(r) for r in rows]


async def get_modifiers_by_ids(session: AsyncSession, modifier_ids: Sequence[int]) -> list[dict]:
    if not modifier_ids:
        return []
    q = select(modifiers.c.id, modifiers.c.name, modifiers.c.price_delta, modifiers.c.group_id).where(
        modifiers.c.id.in_([int(x) for x in modifier_ids])
    )
    rows = (await session.execute(q)).mappings().all()
    return [dict(r) for r in rows]


# ----------------------------
# Stoplist checks
# ----------------------------
async def is_product_blocked(
    session: AsyncSession,
    cafe_id: int,
    product_id: int,
    at: datetime | None = None,
) -> bool:
    at = at or utcnow()
    q = select(stoplist_products.c.id).where(
        and_(
            stoplist_products.c.cafe_id == cafe_id,
            stoplist_products.c.product_id == product_id,
            or_(stoplist_products.c.until_ts.is_(None), stoplist_products.c.until_ts > at),
        )
    )
    return (await session.execute(q)).first() is not None


async def find_blocked_modifiers(
    session: AsyncSession,
    cafe_id: int,
    modifier_ids: Sequence[int],
    at: datetime | None = None,
) -> list[int]:
    if not modifier_ids:
        return []
    at = at or utcnow()
    q = select(stoplist_modifiers.c.modifier_id).where(
        and_(
            stoplist_modifiers.c.cafe_id == cafe_id,
            stoplist_modifiers.c.modifier_id.in_([int(x) for x in modifier_ids]),
            or_(stoplist_modifiers.c.until_ts.is_(None), stoplist_modifiers.c.until_ts > at),
        )
    )
    rows = (await session.execute(q)).scalars().all()
    return [int(x) for x in rows]


# ----------------------------
# Cart
# ----------------------------
async def count_cart_lines(session: AsyncSession, user_id: int, cafe_id: int) -> int:
    q = select(func.count()).select_from(cart_items).where(
        and_(cart_items.c.user_id == user_id, cart_items.c.cafe_id == cafe_id)
    )
    return int((await session.execute(q)).scalar_one())


async def get_cart(session: AsyncSession, user_id: int, cafe_id: int) -> list[dict]:
    q = (
        select(
            cart_items.c.id,
            cart_items.c.product_id,
            cart_items.c.modifiers_key,
            cart_items.c.qty,
            products.c.name.label("product_name"),
            products.c.base_price.label("base_price"),
        )
        .select_from(cart_items.join(products, products.c.id == cart_items.c.product_id))
        .where(and_(cart_items.c.user_id == user_id, cart_items.c.cafe_id == cafe_id))
        .order_by(cart_items.c.updated_at.desc(), cart_items.c.id.desc())
    )
    rows = (await session.execute(q)).mappings().all()
    return [dict(r) for r in rows]


async def upsert_cart_line_qty(
    session: AsyncSession,
    user_id: int,
    cafe_id: int,
    product_id: int,
    modifiers_key: str,
    qty_delta: int,
) -> int:
    """
    Adds qty_delta to existing line (signature: user+cafe+product+modifiers_key) or inserts new.
    Returns cart_item_id.
    """
    now = utcnow()
    # optimistic update
    upd = (
        update(cart_items)
        .where(
            and_(
                cart_items.c.user_id == user_id,
                cart_items.c.cafe_id == cafe_id,
                cart_items.c.product_id == product_id,
                cart_items.c.modifiers_key == modifiers_key,
            )
        )
        .values(qty=cart_items.c.qty + int(qty_delta), updated_at=now)
        .returning(cart_items.c.id)
    )
    res = await session.execute(upd)
    row = res.first()
    if row:
        return int(row[0])

    ins = (
        insert(cart_items)
        .values(
            user_id=user_id,
            cafe_id=cafe_id,
            product_id=product_id,
            modifiers_key=modifiers_key,
            qty=int(qty_delta),
            created_at=now,
            updated_at=now,
        )
        .returning(cart_items.c.id)
    )
    res2 = await session.execute(ins)
    return int(res2.scalar_one())


async def set_cart_line_qty(session: AsyncSession, cart_item_id: int, user_id: int, qty: int) -> bool:
    if qty <= 0:
        return await delete_cart_line(session, cart_item_id, user_id)
    q = (
        update(cart_items)
        .where(and_(cart_items.c.id == cart_item_id, cart_items.c.user_id == user_id))
        .values(qty=int(qty), updated_at=utcnow())
    )
    res = await session.execute(q)
    return res.rowcount > 0


async def delete_cart_line(session: AsyncSession, cart_item_id: int, user_id: int) -> bool:
    q = delete(cart_items).where(and_(cart_items.c.id == cart_item_id, cart_items.c.user_id == user_id))
    res = await session.execute(q)
    return res.rowcount > 0


async def clear_cart(session: AsyncSession, user_id: int, cafe_id: int) -> int:
    q = delete(cart_items).where(and_(cart_items.c.user_id == user_id, cart_items.c.cafe_id == cafe_id))
    res = await session.execute(q)
    return int(res.rowcount or 0)


async def count_active_orders(session: AsyncSession, cafe_id: int, active_statuses: set[str]) -> int:
    q = select(func.count()).select_from(orders).where(
        and_(orders.c.cafe_id == cafe_id, orders.c.status.in_(list(active_statuses)))
    )
    return int((await session.execute(q)).scalar_one())


async def create_order(
    session: AsyncSession,
    *,
    code: str,
    user_id: int,
    cafe_id: int,
    status: str,
    total: float,
    pickup_type: str = "asap",
    pickup_at=None,
    payment_provider: str | None = None,
    provider_payment_id: str | None = None,
    payment_status: str = "pending",
    payment_confirmation_url: str | None = None,
) -> int:
    now = utcnow()

    q = (
        orders.insert()
        .values(
            code=code,
            user_id=user_id,
            cafe_id=cafe_id,
            status=status,
            total=total,
            payment_provider=payment_provider,
            provider_payment_id=provider_payment_id,
            payment_status=payment_status,
            payment_confirmation_url=payment_confirmation_url,
            created_at=now,
            updated_at=now,
            pickup_type=pickup_type,
            pickup_at=pickup_at,
        )
        .returning(orders.c.id)
    )
    row = (await session.execute(q)).scalar_one()
    return int(row)


async def add_order_item(
    session: AsyncSession,
    *,
    order_id: int,
    product_id: int,
    title_snapshot: str,
    base_price_snapshot: float,
    modifiers_key: str,
    qty: int,
    line_total: float,
) -> int:
    q = (
        insert(order_items)
        .values(
            order_id=order_id,
            product_id=product_id,
            title_snapshot=title_snapshot,
            base_price_snapshot=base_price_snapshot,
            modifiers_key=modifiers_key,
            qty=qty,
            line_total=line_total,
        )
        .returning(order_items.c.id)
    )
    return int((await session.execute(q)).scalar_one())


async def add_order_item_modifier(
    session: AsyncSession,
    *,
    order_item_id: int,
    modifier_id: int,
    title_snapshot: str,
    price_delta_snapshot: float,
) -> None:
    await session.execute(
        insert(order_item_modifiers).values(
            order_item_id=order_item_id,
            modifier_id=modifier_id,
            title_snapshot=title_snapshot,
            price_delta_snapshot=price_delta_snapshot,
        )
    )


async def get_order(session: AsyncSession, order_id: int) -> dict | None:
    q = select(orders).where(orders.c.id == order_id)
    row = (await session.execute(q)).mappings().first()
    return dict(row) if row else None



async def set_order_status(session: AsyncSession, order_id: int, status: str) -> bool:
    now = utcnow()
    values = {"status": status, "updated_at": now}
    if status == "paid":
        values["paid_at"] = now
    q = update(orders).where(orders.c.id == order_id).values(**values)
    res = await session.execute(q)
    return res.rowcount > 0



async def set_order_payment(
    session: AsyncSession,
    *,
    order_id: int,
    payment_provider: str | None,
    provider_payment_id: str | None,
    payment_status: str,
    payment_confirmation_url: str | None = None,
) -> bool:
    now = utcnow()
    q = (
        update(orders)
        .where(orders.c.id == order_id)
        .values(
            payment_provider=payment_provider,
            provider_payment_id=provider_payment_id,
            payment_status=payment_status,
            payment_confirmation_url=payment_confirmation_url,
            updated_at=now,
        )
    )
    res = await session.execute(q)
    return bool(res.rowcount and res.rowcount > 0)


# ----------------------------
# Order status events (outbox)
# ----------------------------
async def enqueue_order_status_event(
    session: AsyncSession,
    *,
    order_id: int,
    user_id: int,
    old_status: str | None,
    new_status: str,
) -> int:
    """Creates an outbox event to be later sent to user via bot."""
    now = utcnow()
    q = (
        insert(order_status_events)
        .values(
            order_id=int(order_id),
            user_id=int(user_id),
            old_status=old_status,
            new_status=str(new_status),
            attempts=0,
            last_error=None,
            created_at=now,
            sent_at=None,
        )
        .returning(order_status_events.c.id)
    )
    return int((await session.execute(q)).scalar_one())


async def fetch_pending_order_status_events(
    session: AsyncSession,
    *,
    limit: int = 50,
) -> list[dict]:
    """Fetches unsent events (sent_at is NULL), oldest first."""
    q = (
        select(
            order_status_events.c.id,
            order_status_events.c.order_id,
            order_status_events.c.user_id,
            order_status_events.c.old_status,
            order_status_events.c.new_status,
            order_status_events.c.attempts,
            order_status_events.c.last_error,
            order_status_events.c.created_at,
        )
        .where(order_status_events.c.sent_at.is_(None))
        .order_by(order_status_events.c.created_at.asc(), order_status_events.c.id.asc())
        .limit(int(limit))
    )
    rows = (await session.execute(q)).mappings().all()
    return [dict(r) for r in rows]


async def mark_order_status_event_sent(session: AsyncSession, event_id: int) -> bool:
    q = (
        update(order_status_events)
        .where(order_status_events.c.id == int(event_id))
        .values(sent_at=utcnow(), last_error=None)
    )
    res = await session.execute(q)
    return bool(res.rowcount and res.rowcount > 0)


async def mark_order_status_event_failed(session: AsyncSession, event_id: int, error: str) -> bool:
    # keep sent_at = NULL so it can be retried later
    q = (
        update(order_status_events)
        .where(order_status_events.c.id == int(event_id))
        .values(
            attempts=order_status_events.c.attempts + 1,
            last_error=str(error)[:2000],
        )
    )
    res = await session.execute(q)
    return bool(res.rowcount and res.rowcount > 0)




# ----------------------------
# Profile stats (computed)
# ----------------------------
async def get_user_stats(session: AsyncSession, user_id: int) -> dict:
    """
    Returns:
      cups_total: int (sum qty in paid/done orders)
      favorite_cafe: {cafe_id, name, cups} | None
      favorite_product: {product_id, name, cups} | None
    """
    paid_like = ["paid", "accepted", "in_progress", "done"]

    # total cups
    q_total = (
        select(func.coalesce(func.sum(order_items.c.qty), 0))
        .select_from(order_items.join(orders, orders.c.id == order_items.c.order_id))
        .where(and_(orders.c.user_id == user_id, orders.c.status.in_(paid_like)))
    )
    cups_total = int((await session.execute(q_total)).scalar_one())

    # favorite cafe
    q_fc = (
        select(
            cafes.c.id.label("cafe_id"),
            cafes.c.name.label("name"),
            func.coalesce(func.sum(order_items.c.qty), 0).label("cups"),
        )
        .select_from(
            order_items.join(orders, orders.c.id == order_items.c.order_id).join(cafes, cafes.c.id == orders.c.cafe_id)
        )
        .where(and_(orders.c.user_id == user_id, orders.c.status.in_(paid_like)))
        .group_by(cafes.c.id, cafes.c.name)
        .order_by(func.sum(order_items.c.qty).desc())
        .limit(1)
    )
    fav_cafe = (await session.execute(q_fc)).mappings().first()
    favorite_cafe = dict(fav_cafe) if fav_cafe else None

    # favorite product
    q_fp = (
        select(
            products.c.id.label("product_id"),
            products.c.name.label("name"),
            func.coalesce(func.sum(order_items.c.qty), 0).label("cups"),
        )
        .select_from(
            order_items.join(orders, orders.c.id == order_items.c.order_id).join(
                products, products.c.id == order_items.c.product_id
            )
        )
        .where(and_(orders.c.user_id == user_id, orders.c.status.in_(paid_like)))
        .group_by(products.c.id, products.c.name)
        .order_by(func.sum(order_items.c.qty).desc())
        .limit(1)
    )
    fav_prod = (await session.execute(q_fp)).mappings().first()
    favorite_product = dict(fav_prod) if fav_prod else None

    return {
        "cups_total": cups_total,
        "favorite_cafe": favorite_cafe,
        "favorite_product": favorite_product,
    }
