# src/config.py
from __future__ import annotations

import os
import secrets
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
    # Database (SQLite)
    # ----------------------------
    DB_PATH: Path = Field(default=BASE_DIR / "data" / "easycoffee.sqlite3", description="Путь к SQLite файлу")
    DB_TIMEOUT_SEC: int = Field(default=30, ge=1, le=120, description="busy_timeout для SQLite")
    DB_WAL: bool = Field(default=True, description="Включить WAL режим (рекомендуется)")
    DB_FOREIGN_KEYS: bool = Field(default=True, description="PRAGMA foreign_keys = ON")
    DB_POOL_SIZE: int = Field(default=5, ge=1, le=50, description="Размер пула (для aiosqlite условно)")
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
    # Payments (YooKassa) + test mode
    # ----------------------------
    PAYMENTS_ENABLED: bool = Field(default=False, description="Включить оплату. Если False — тестовый флоу без оплаты.")
    PAYMENT_PROVIDER: Literal["yookassa"] = Field(default="yookassa", description="Провайдер оплаты (пока один)")
    CURRENCY: str = Field(default="RUB", min_length=3, max_length=3, description="Валюта заказов")

    # ЮKassa creds (обязательны только если PAYMENTS_ENABLED=1)
    YOOKASSA_SHOP_ID: str | None = Field(default=None, description="ЮKassa shopId")
    YOOKASSA_SECRET_KEY: str | None = Field(default=None, description="ЮKassa secret key")

    # URL, куда ЮKassa вернёт пользователя после оплаты (на сайт/страницу/заглушку)
    PAYMENT_RETURN_URL: str | None = Field(default=None, description="Return URL после оплаты (confirmation return_url)")

    # Параметры платежа
    PAYMENT_CAPTURE: bool = Field(default=True, description="capture=True (обычно да)")
    PAYMENT_DESCRIPTION_PREFIX: str = Field(default="EasyCoffee order", description="Префикс описания платежа")

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

        # DB directory
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
                # без return_url можно жить, но UX и флоу разваливаются (особенно на мобиле)
                raise ValueError("PAYMENTS_ENABLED=1 requires PAYMENT_RETURN_URL")

            # Webhooks strongly recommended if payments enabled
            if not self.WEBHOOKS_ENABLED:
               pass

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
        # absolute path avoids surprises when running from different cwd
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
