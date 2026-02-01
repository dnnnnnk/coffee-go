# src/config.py
from __future__ import annotations

import os
import secrets
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# src/ -> project root is one level up
BASE_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """
    Единственный источник правды по настройкам.
    - Читает .env (по умолчанию в корне проекта)
    - Валидирует критичные зависимости (оплата/вебхук)
    - Даёт готовые computed values (db_url, webhook_path и т.п.)
    """

    model_config = SettingsConfigDict(
        env_file=os.getenv("ENV_FILE", str(BASE_DIR / ".env")),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ----------------------------
    # Runtime
    # ----------------------------
    ENV: Literal["dev", "test", "prod"] = Field(default="dev", description="Среда запуска")
    TIMEZONE: str = Field(default="Europe/Berlin", description="Таймзона проекта/кофеен (по умолчанию)")

    # ----------------------------
    # Telegram bot
    # ----------------------------
    BOT_TOKEN: str = Field(..., description="Telegram Bot API token")
    BOT_NAME: str = Field(default="EasyCoffee", description="Имя для логов/метрик")
    ADMINS: list[int] = Field(default_factory=list, description="Telegram user_id админов")

    # Ограничения/UX
    MENU_PAGE_SIZE: int = Field(default=8, ge=4, le=20, description="Сколько позиций показываем за раз в списках")
    MAX_CART_ITEMS: int = Field(default=30, ge=1, le=200, description="Лимит разных позиций в корзине на юзера")
    MAX_QTY_PER_ITEM: int = Field(default=20, ge=1, le=200, description="Лимит количества для одной позиции")
    ORDER_CODE_LENGTH: int = Field(default=6, ge=4, le=10, description="Длина кода получения")
    ORDER_CODE_ALPHABET: str = Field(
        default="ABCDEFGHJKLMNPQRSTUVWXYZ23456789",
        description="Алфавит для кода (без 0/O/1/I)",
    )

    # ----------------------------
    # Database
    # ----------------------------
    # Можно задать DB_URL напрямую (лучший вариант). Если DB_URL пустой —
    # используем сборку из параметров под выбранный бекенд.
    DB_BACKEND: Literal["sqlite", "postgres"] = Field(default="sqlite", description="Бекенд БД")
    DB_URL: str | None = Field(default=None, description="Полный SQLAlchemy async URL (sqlite+aiosqlite:// или postgresql+asyncpg://)")

    # SQLite
    DB_PATH: Path = Field(default=BASE_DIR / "data" / "easycoffee.sqlite3", description="Путь к SQLite файлу")
    DB_TIMEOUT_SEC: int = Field(default=30, ge=1, le=120, description="busy_timeout для SQLite")
    DB_WAL: bool = Field(default=True, description="Включить WAL режим (рекомендуется)")
    DB_FOREIGN_KEYS: bool = Field(default=True, description="PRAGMA foreign_keys = ON")

    # Postgres
    PG_HOST: str = Field(default="localhost", description="Postgres host")
    PG_PORT: int = Field(default=5432, ge=1, le=65535, description="Postgres port")
    PG_DB: str = Field(default="easycoffee", description="Postgres database")
    PG_USER: str = Field(default="postgres", description="Postgres user")
    PG_PASSWORD: str = Field(default="postgres", description="Postgres password")

    # Общие параметры
    DB_POOL_SIZE: int = Field(default=5, ge=1, le=50, description="Размер пула соединений")
    DB_ECHO_SQL: bool = Field(default=False, description="Логировать SQL (в dev удобно, в prod обычно нет)")

    # ----------------------------
    # Stoplist/Overload behavior
    # ----------------------------
    # Перегрузка может быть как ручной флаг is_overloaded в кафе, так и авто-лимит.
    OVERLOAD_AUTO_ENABLED: bool = Field(default=False, description="Авто-перегрузка по числу активных заказов")
    OVERLOAD_ACTIVE_ORDERS_LIMIT: int = Field(
        default=25, ge=1, le=500, description="Лимит активных заказов на кафе (для авто-перегрузки)"
    )
    OVERLOAD_ACTIVE_STATUSES: set[str] = Field(
        default_factory=lambda: {"paid", "accepted", "in_progress"},
        description="Какие статусы считаем активными для перегрузки",
    )

    # ----------------------------
    # Payments (YooKassa) — split payments (маркетплейс)
    # ----------------------------
    PAYMENTS_MODE: str = "yookassa"  # "yookassa" | "mock"
    PAYMENTS_ENABLED: bool = Field(default=True, description="Включить оплату через YooKassa")
    CURRENCY: str = Field(default="RUB", min_length=3, max_length=3, description="Валюта заказов")

    # Split: комиссия в наш магазин, основной платёж — в магазин кофейни
    YOOKASSA_SPLIT_ENABLED: bool = Field(default=True, description="Включить split-платежи (комиссия платформы + перевод кофейне)")

    # Комиссия платформы (берём сверху с пользователя)
    PLATFORM_FEE_MODE: Literal["fixed", "percent", "none"] = Field(
        default="fixed",
        description="Как считать комиссию платформы: fixed/percent/none",
    )
    PLATFORM_FEE_AMOUNT: Decimal = Field(default=Decimal("15.00"), description="Фикс комиссия, RUB")
    PLATFORM_FEE_PERCENT: Decimal = Field(default=Decimal("0.00"), description="Процент комиссии (0..100)")

    # YooKassa creds платформы (нашего магазина) — обязательны если PAYMENTS_ENABLED=1
    YOOKASSA_SHOP_ID: str | None = Field(default=None, description="ЮKassa shopId")
    YOOKASSA_SECRET_KEY: str | None = Field(default=None, description="ЮKassa secret key")

    # URL, куда ЮKassa вернёт пользователя после оплаты (на сайт/страницу/заглушку)
    PAYMENT_RETURN_URL: str | None = Field(default=None, description="Return URL после оплаты (confirmation return_url)")

    # Параметры платежа
    PAYMENT_CAPTURE: bool = Field(default=True, description="capture=True (обычно да)")
    PAYMENT_DESCRIPTION_PREFIX: str = Field(default="EasyCoffee order", description="Префикс описания платежа")

    # ----------------------------
    # Web panel (sessions/auth)
    # ----------------------------
    WEB_BASE_URL: str = Field(
        default="http://localhost:8080",
        description="Базовый URL веб-панели (нужен для ссылок/redirects)",
    )

    # Где лежат HTML-шаблоны
    WEB_TEMPLATES_DIR: Path = Field(default=BASE_DIR / "src" / "web" / "templates", description="Папка с Jinja2 templates")


    # Session cookies
    WEB_SESSION_SECRET: str | None = Field(default=None, description="Секрет подписи сессий веб-панели")
    WEB_SESSION_TTL_MIN: int = Field(default=60 * 24 * 7, ge=1, le=60 * 24 * 30, description="TTL сессии в минутах")

    WEB_COOKIE_NAME: str = Field(default="coffee_go_session", description="Имя cookie для сессии")

    # Secure/HTTPS. Оставляем оба флага ради совместимости, но нормализуем ниже.
    WEB_COOKIE_SECURE: bool = Field(default=False, description="Cookie Secure (true для https)")
    WEB_COOKIE_HTTPS_ONLY: bool | None = Field(default=None, description="Alias для WEB_COOKIE_SECURE (если задан в env — имеет приоритет)")

    WEB_COOKIE_HTTPONLY: bool = Field(default=True, description="Cookie HttpOnly")
    WEB_COOKIE_SAMESITE: Literal["lax", "strict", "none"] = Field(default="lax", description="SameSite политика для cookie")

    # Admin unlock: доп-пароль внутри веба
    WEB_ADMIN_UNLOCK_TTL_MIN: int = Field(default=30, ge=1, le=1440, description="Сколько минут действует admin-unlock")


    # ----------------------------
    # Webhook (для подтверждения оплаты и веб-панели позже)
    # ----------------------------
    # Даже если сейчас тестишь без оплаты — лучше уже иметь настройки на будущее.
    WEBHOOKS_ENABLED: bool = Field(default=False, description="Поднимать HTTP сервер (FastAPI) для webhooks/API")
    HTTP_HOST: str = Field(default="0.0.0.0", description="Host для FastAPI/uvicorn")
    HTTP_PORT: int = Field(default=8080, ge=1, le=65535, description="Port для FastAPI/uvicorn")

    # Секреты для webhook'ов (минимальная защита, особенно на публичном хостинге)
    WEBHOOK_SECRET: str | None = Field(default=None, description="Секрет для подписи/валидации webhooks")
    YOOKASSA_WEBHOOK_PATH: str = Field(default="/webhooks/yookassa", description="Путь для webhook ЮKassa")
    API_PREFIX: str = Field(default="/api", description="Префикс для будущего API (веб-панель)")

    # ----------------------------
    # Logging
    # ----------------------------
    LOG_LEVEL: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(default="INFO")
    LOG_SQL_ERRORS: bool = Field(default=True, description="Логировать ошибки БД подробно (без токенов)")

    # ----------------------------
    # Validators / derived
    # ----------------------------
    @field_validator("DB_PATH", mode="before")
    @classmethod
    def _coerce_db_path(cls, v):
        # allow passing as string in env
        return Path(v) if isinstance(v, str) else v

    @field_validator("WEB_TEMPLATES_DIR", mode="before")
    @classmethod
    def _coerce_templates_dir(cls, v):
        return Path(v) if isinstance(v, str) else v

    @field_validator("ADMINS", mode="before")
    @classmethod
    def _parse_admins(cls, v):
        # allow "1,2,3" or "1 2 3"
        if v is None:
            return []
        if isinstance(v, list):
            return [int(x) for x in v]
        if isinstance(v, str):
            raw = v.replace(",", " ").split()
            return [int(x) for x in raw if x.strip()]
        return v

    @field_validator("OVERLOAD_ACTIVE_STATUSES", mode="before")
    @classmethod
    def _parse_statuses(cls, v):
        if v is None:
            return {"paid", "accepted", "in_progress"}
        if isinstance(v, set):
            return v
        if isinstance(v, list):
            return {str(x).strip() for x in v if str(x).strip()}
        if isinstance(v, str):
            raw = v.replace(",", " ").split()
            return {x.strip() for x in raw if x.strip()}
        return v

    @model_validator(mode="after")
    def _validate_dependencies(self):
        # BOT_TOKEN
        if not self.BOT_TOKEN or ":" not in self.BOT_TOKEN:
            raise ValueError("BOT_TOKEN is missing or looks invalid")

        # DB directory only for sqlite
        if (self.DB_BACKEND == "sqlite") and (self.DB_URL is None or self.DB_URL.startswith("sqlite")):
            self.DB_PATH.parent.mkdir(parents=True, exist_ok=True)

        # Order code sanity
        if len(set(self.ORDER_CODE_ALPHABET)) < len(self.ORDER_CODE_ALPHABET):
            raise ValueError("ORDER_CODE_ALPHABET must not contain duplicate characters")
        if self.ORDER_CODE_LENGTH > len(self.ORDER_CODE_ALPHABET):
            # не строго обязательно, но нам нужен нормальный space для кодов
            raise ValueError("ORDER_CODE_LENGTH is too large for provided ORDER_CODE_ALPHABET")

        # Payments requirements
        if self.PAYMENTS_ENABLED:
            if not self.YOOKASSA_SHOP_ID or not self.YOOKASSA_SECRET_KEY:
                raise ValueError("PAYMENTS_ENABLED=1 requires YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY")
            if not self.PAYMENT_RETURN_URL:
                raise ValueError("PAYMENTS_ENABLED=1 requires PAYMENT_RETURN_URL")

            if self.PLATFORM_FEE_MODE not in {"fixed", "percent", "none"}:
                raise ValueError("PLATFORM_FEE_MODE must be fixed/percent/none")
            if self.PLATFORM_FEE_MODE == "fixed" and self.PLATFORM_FEE_AMOUNT < 0:
                raise ValueError("PLATFORM_FEE_AMOUNT must be >= 0")
            if self.PLATFORM_FEE_MODE == "percent":
                if self.PLATFORM_FEE_PERCENT < 0 or self.PLATFORM_FEE_PERCENT > 100:
                    raise ValueError("PLATFORM_FEE_PERCENT must be between 0 and 100")

            # Webhooks strongly recommended if payments enabled
            if not self.WEBHOOKS_ENABLED:
                pass

        # Web sessions secret
        if not self.WEB_SESSION_SECRET:
            # в prod не генерим молча — чтобы не сбрасывать сессии при перезапуске
            if self.ENV == "prod":
                raise ValueError("WEB_SESSION_SECRET is required in prod")
            self.WEB_SESSION_SECRET = secrets.token_urlsafe(32)

        # cookie settings sanity
        # нормализуем WEB_COOKIE_HTTPS_ONLY (alias) -> WEB_COOKIE_SECURE
        if self.WEB_COOKIE_HTTPS_ONLY is None:
            self.WEB_COOKIE_HTTPS_ONLY = bool(self.WEB_COOKIE_SECURE)
        else:
            self.WEB_COOKIE_SECURE = bool(self.WEB_COOKIE_HTTPS_ONLY)

        if self.WEB_COOKIE_SAMESITE == "none" and not self.WEB_COOKIE_SECURE:
            # браузеры требуют Secure при SameSite=None
            raise ValueError("WEB_COOKIE_SAMESITE='none' requires WEB_COOKIE_SECURE=1")

        # Webhooks requirements
        if self.WEBHOOKS_ENABLED:
            if not self.WEBHOOK_SECRET:
                # автогенерация допустима в dev/test, но в prod лучше явно
                if self.ENV == "prod":
                    raise ValueError("WEBHOOKS_ENABLED=1 in prod requires WEBHOOK_SECRET")
                self.WEBHOOK_SECRET = secrets.token_urlsafe(32)

            if not self.YOOKASSA_WEBHOOK_PATH.startswith("/"):
                raise ValueError("YOOKASSA_WEBHOOK_PATH must start with '/'")
            if not self.API_PREFIX.startswith("/"):
                raise ValueError("API_PREFIX must start with '/'")

        return self

    @property
    def db_url(self) -> str:
        # SQLAlchemy async URL
        # 1) Если задан DB_URL — используем его, но аккуратно нормализуем популярные схемы
        if self.DB_URL:
            u = self.DB_URL.strip()
            # allow user to pass sync-style urls
            if u.startswith("postgres://"):
                u = "postgresql+asyncpg://" + u[len("postgres://"):]
            elif u.startswith("postgresql://"):
                u = "postgresql+asyncpg://" + u[len("postgresql://"):]
            elif u.startswith("postgresql+psycopg2://"):
                u = "postgresql+asyncpg://" + u[len("postgresql+psycopg2://"):]
            return u

        # 2) Иначе собираем по бекенду
        if self.DB_BACKEND == "postgres":
            return (
                f"postgresql+asyncpg://{self.PG_USER}:{self.PG_PASSWORD}"
                f"@{self.PG_HOST}:{self.PG_PORT}/{self.PG_DB}"
            )

        # default: sqlite
        p = self.DB_PATH.resolve()
        return f"sqlite+aiosqlite:///{p}"

    @property
    def yookassa_webhook_url_path(self) -> str:
        # used by FastAPI router
        return self.YOOKASSA_WEBHOOK_PATH

    def make_order_code(self) -> str:
        # генерация кода выдачи (без БД-проверки уникальности — это сделает сервис/транзакция)
        return "".join(secrets.choice(self.ORDER_CODE_ALPHABET) for _ in range(self.ORDER_CODE_LENGTH))


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
