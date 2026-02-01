# src/payments.py
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Mapping

import aiohttp
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import db


# ----------------------------
# Errors
# ----------------------------


class PaymentError(Exception):
    def __init__(self, message: str, *, details: Any | None = None):
        super().__init__(message)
        self.message = message
        self.details = details


# ----------------------------
# Types / DTO
# ----------------------------

PaymentStatus = Literal["pending", "waiting_for_capture", "succeeded", "canceled"]


@dataclass(frozen=True)
class PaymentCreateResult:
    provider: str
    payment_id: str
    status: PaymentStatus
    confirmation_url: str | None
    raw: dict[str, Any]


# ----------------------------
# Money helpers
# ----------------------------

TWOPLACES = Decimal("0.01")


def money_str(x: Decimal | int | float | str) -> str:
    d = x if isinstance(x, Decimal) else Decimal(str(x))
    d = d.quantize(TWOPLACES, rounding=ROUND_HALF_UP)
    # YooKassa expects "123.00"
    return f"{d:.2f}"


# ----------------------------
# YooKassa HTTP client (async)
# ----------------------------


class YooKassaClient:
    """Минимальный, но боевой клиент YooKassa через HTTP API v3."""

    API_BASE = "https://api.yookassa.ru/v3"

    def __init__(self, shop_id: str, secret_key: str, *, timeout_sec: int = 15):
        self.shop_id = shop_id
        self.secret_key = secret_key
        self.timeout = aiohttp.ClientTimeout(total=timeout_sec)

    def _auth_header(self) -> str:
        token = base64.b64encode(f"{self.shop_id}:{self.secret_key}".encode("utf-8")).decode("ascii")
        return f"Basic {token}"

    async def _request(
        self,
        method: str,
        path: str,
        *,
        idempotence_key: str | None = None,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.API_BASE}{path}"
        headers = {
            "Authorization": self._auth_header(),
            "Content-Type": "application/json",
        }
        if idempotence_key:
            headers["Idempotence-Key"] = idempotence_key

        async with aiohttp.ClientSession(timeout=self.timeout) as session:
            try:
                async with session.request(method, url, headers=headers, json=json_body, params=params) as resp:
                    text = await resp.text()
                    try:
                        payload = json.loads(text) if text else {}
                    except json.JSONDecodeError:
                        payload = {"_raw": text}

                    if resp.status >= 400:
                        msg = payload.get("description") or payload.get("message") or f"YooKassa HTTP {resp.status}"
                        raise PaymentError(msg, details={"status": resp.status, "payload": payload})

                    if not isinstance(payload, dict):
                        raise PaymentError("Некорректный ответ YooKassa.", details=payload)

                    return payload
            except aiohttp.ClientError as e:
                raise PaymentError("Ошибка сети при обращении к YooKassa.", details=str(e)) from e

    async def create_payment(
        self,
        *,
        amount: Decimal,
        currency: str,
        description: str,
        return_url: str,
        capture: bool,
        idempotence_key: str,
        metadata: dict[str, Any] | None = None,
        receipt: dict[str, Any] | None = None,
        notification_url: str | None = None,
        transfers: list[dict[str, Any]] | None = None,
    ) -> PaymentCreateResult:
        body: dict[str, Any] = {
            "amount": {"value": money_str(amount), "currency": currency},
            "capture": bool(capture),
            "confirmation": {"type": "redirect", "return_url": return_url},
            "description": description,
        }
        if metadata:
            body["metadata"] = metadata
        if receipt:
            body["receipt"] = receipt
        if notification_url:
            body["notification_url"] = notification_url
        if transfers:
            body["transfers"] = transfers

        payload = await self._request(
            "POST",
            "/payments",
            idempotence_key=idempotence_key,
            json_body=body,
        )

        confirmation_url = None
        conf = payload.get("confirmation") or {}
        if isinstance(conf, dict):
            confirmation_url = conf.get("confirmation_url")

        return PaymentCreateResult(
            provider="yookassa",
            payment_id=str(payload.get("id")),
            status=str(payload.get("status", "pending")),  # type: ignore[assignment]
            confirmation_url=confirmation_url,
            raw=payload,
        )

    async def get_payment(self, payment_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/payments/{payment_id}")


# ----------------------------
# Public API used by handlers/services
# ----------------------------
async def refresh_order_status_from_provider(
    session: AsyncSession,
    settings,
    *,
    order_id: int,
) -> dict[str, Any]:
    row = (await session.execute(
        select(
            db.orders.c.payment_provider,
            db.orders.c.provider_payment_id,
            db.orders.c.payment_status,
        ).where(db.orders.c.id == order_id)
    )).mappings().one_or_none()

    if not row:
        raise PaymentError("Заказ не найден.")

    provider = str(row.get("payment_provider") or "").strip()

    # mock: всегда succeeded
    if provider == "mock" or is_mock_mode(settings):
        return {"status": "succeeded", "provider": "mock"}

    if provider != "yookassa":
        # если вдруг нет провайдера — просто вернём что есть
        return {"status": str(row.get("payment_status") or "pending"), "provider": provider or None}

    payment_id = str(row.get("provider_payment_id") or "").strip()
    if not payment_id:
        return {"status": str(row.get("payment_status") or "pending"), "provider": "yookassa"}

    client = get_yookassa_client(settings)
    pay = await client.get_payment(payment_id)
    status = str(pay.get("status") or "pending")

    await session.execute(
        db.orders.update()
        .where(db.orders.c.id == order_id)
        .values(payment_status=status, updated_at=db.utcnow())
    )
    if status == "succeeded":
        await session.execute(
            db.orders.update()
            .where(db.orders.c.id == order_id)
            .values(status="paid", paid_at=db.utcnow(), updated_at=db.utcnow())
        )

    return {"status": status, "provider": "yookassa", "raw": pay}


def is_mock_mode(settings) -> bool:
    return str(getattr(settings, "PAYMENTS_MODE", "yookassa")).lower() == "mock"

def get_yookassa_client(settings) -> YooKassaClient:
    if not getattr(settings, "YOOKASSA_SHOP_ID", None) or not getattr(settings, "YOOKASSA_SECRET_KEY", None):
        raise PaymentError("Не настроены ключи ЮKassa.")
    return YooKassaClient(
        shop_id=str(settings.YOOKASSA_SHOP_ID),
        secret_key=str(settings.YOOKASSA_SECRET_KEY),
        timeout_sec=int(getattr(settings, "YOOKASSA_TIMEOUT_SEC", 15)),
    )


def build_order_description(
    settings,
    *,
    cafe_name: str | None = None,
    total_amount: Decimal | None = None,
    order_id: int | None = None,
) -> str:
    """Описание платежа для ЮKassa (видно в ЛК).

    Требования:
    - коротко
    - включает кофейню и сумму
    - не превышает 128 символов
    """

    def _clean_name(name: str) -> str:
        s = " ".join((name or "").split())
        return s.strip()

    def _money_human(d: Decimal) -> str:
        # "1234.00" -> "1234"; "1234.50" -> "1234.50"
        s = money_str(d)
        return s[:-3] if s.endswith(".00") else s

    name = _clean_name(cafe_name or getattr(settings, "PAYMENT_SHOP_NAME", "") or "Кофейня")

    if total_amount is not None:
        amount_txt = f"{_money_human(total_amount)} ₽"
        desc = f"{name} • {amount_txt}"
    else:
        # fallback: как раньше
        prefix = getattr(
            settings,
            "PAYMENT_DESCRIPTION_PREFIX",
            getattr(settings, "ORDER_DESCRIPTION_PREFIX", "Take Cup order"),
        )
        if order_id is None:
            desc = str(prefix)
        else:
            desc = f"{prefix} #{order_id}"

    return desc[:128]


async def create_payment_for_order(
    session: AsyncSession,
    settings,
    *,
    order_id: int,
    user_id: int,
    return_url: str,
) -> PaymentCreateResult:

    if is_mock_mode(settings):
        mock_payment_id = f"mock-{order_id}"
        await session.execute(
            db.orders.update()
            .where(db.orders.c.id == order_id)
            .values(
                payment_provider="mock",
                provider_payment_id=mock_payment_id,
                payment_status="succeeded",
                payment_confirmation_url=None,
                status="paid",
                paid_at=db.utcnow(),
                updated_at=db.utcnow(),
            )
        )
        return PaymentCreateResult(
            provider="mock",
            payment_id=mock_payment_id,
            status="succeeded",
            confirmation_url=None,
            raw={"mock": True, "order_id": order_id, "user_id": user_id},
        )

    # --- обычная ЮKassa ---
    client = get_yookassa_client(settings)

    q = (
        select(
            db.orders.c.id,
            db.orders.c.total,
            db.orders.c.cafe_id,
            db.cafes.c.name.label("cafe_name"),
        )
        .select_from(db.orders.join(db.cafes, db.orders.c.cafe_id == db.cafes.c.id))
        .where(db.orders.c.id == order_id)
    )
    row = (await session.execute(q)).mappings().one_or_none()
    if not row:
        raise PaymentError("Заказ не найден.")

    coffee_amount = Decimal(str(row["total"]))

    fee_mode = str(getattr(settings, "PLATFORM_FEE_MODE", "fixed")).lower()
    fee_amount = Decimal(str(getattr(settings, "PLATFORM_FEE_AMOUNT", "0")))
    fee_percent = Decimal(str(getattr(settings, "PLATFORM_FEE_PERCENT", "0")))

    platform_fee = Decimal("0.00")
    if fee_mode == "fixed":
        platform_fee = fee_amount
    elif fee_mode == "percent":
        if fee_percent != 0:
            platform_fee = (coffee_amount * fee_percent / Decimal("100")).quantize(
                TWOPLACES, rounding=ROUND_HALF_UP
            )
    elif fee_mode == "none":
        platform_fee = Decimal("0.00")

    if platform_fee < 0:
        platform_fee = Decimal("0.00")

    total_amount = (coffee_amount + platform_fee).quantize(TWOPLACES, rounding=ROUND_HALF_UP)

    res = await client.create_payment(
        amount=total_amount,
        currency=str(getattr(settings, "CURRENCY", "RUB")),
        description=build_order_description(
            settings,
            cafe_name=str(row.get("cafe_name") or ""),
            total_amount=total_amount,
            order_id=order_id,
        ),
        return_url=return_url,
        capture=bool(getattr(settings, "PAYMENT_CAPTURE", True)),
        idempotence_key=f"order:{order_id}",
        metadata={
            "type": "order",
            "order_id": str(order_id),
            "user_id": str(user_id),
            "cafe_id": str(row["cafe_id"]),
            "platform_fee": money_str(platform_fee),
            "coffee_amount": money_str(coffee_amount),
        },
        notification_url=None,
        receipt=None,
    )

    await session.execute(
        db.orders.update()
        .where(db.orders.c.id == order_id)
        .values(
            payment_provider="yookassa",
            provider_payment_id=res.payment_id,
            payment_status=str(res.status),
            payment_confirmation_url=res.confirmation_url,
            updated_at=db.utcnow(),
        )
    )

    return res

# ----------------------------
# Webhook verification + processing
# ----------------------------


def verify_webhook_secret(
    *,
    settings,
    query_params: Mapping[str, str] | None = None,
    headers: Mapping[str, str] | None = None,
) -> None:
    """YooKassa notifications do NOT support custom headers.

    Поэтому нормальный вариант — вешать секрет в query (?secret=...),
    либо закрывать endpoint на уровне сети/прокси.
    """

    secret = getattr(settings, "WEBHOOK_SECRET", None)
    if not secret:
        return

    qp = query_params or {}
    if qp.get("secret") == secret:
        return

    hs = headers or {}
    if hs.get("X-Webhook-Secret") == secret:
        return

    raise PaymentError("Неверный webhook secret.")


def _extract_payment_object(payload: dict[str, Any]) -> dict[str, Any]:
    obj = payload.get("object")
    if not isinstance(obj, dict):
        raise PaymentError("Некорректный webhook: нет object.")
    return obj


def _event_to_status(event: str, payment_status: str | None) -> PaymentStatus:
    if payment_status in {"pending", "waiting_for_capture", "succeeded", "canceled"}:
        return payment_status  # type: ignore[return-value]

    if event == "payment.succeeded":
        return "succeeded"
    if event == "payment.waiting_for_capture":
        return "waiting_for_capture"
    if event == "payment.canceled":
        return "canceled"
    return "pending"


async def process_yookassa_webhook(
    session: AsyncSession,
    settings,
    *,
    payload: dict[str, Any],
) -> dict[str, Any]:
    """Идемпотентная обработка webhook YooKassa для заказов."""

    if not isinstance(payload, dict):
        raise PaymentError("Webhook payload должен быть JSON-объектом.")

    event = str(payload.get("event", "")).strip()
    pay_obj = _extract_payment_object(payload)

    payment_id = str(pay_obj.get("id") or "").strip()
    if not payment_id:
        raise PaymentError("Некорректный webhook: нет payment id.")

    status = _event_to_status(event, str(pay_obj.get("status") or "").strip() or None)

    # определить order_id
    order_id: int | None = None
    md = pay_obj.get("metadata")
    if isinstance(md, dict) and str(md.get("type") or "") == "order" and "order_id" in md:
        try:
            order_id = int(str(md["order_id"]))
        except Exception:
            order_id = None

    if order_id is None:
        q = select(db.orders.c.id).where(
            and_(db.orders.c.provider == "yookassa", db.orders.c.provider_payment_id == payment_id)
        )
        found = (await session.execute(q)).scalar_one_or_none()
        if found is not None:
            order_id = int(found)

    if order_id is None:
        raise PaymentError("Не удалось определить order_id для webhook.")

    confirmation_url = None
    conf = pay_obj.get("confirmation")
    if isinstance(conf, dict):
        confirmation_url = conf.get("confirmation_url")

    await session.execute(
        db.orders.update()
        .where(db.orders.c.id == order_id)
        .values(
            provider_payment_id=payment_id,
            payment_status=str(status),
            pay_url=confirmation_url,
            payment_payload=json.dumps(payload, ensure_ascii=False),
            updated_at=db.utcnow(),
        )
    )

    # если платеж успешен — помечаем заказ оплаченным
    if status == "succeeded":
        await session.execute(
            db.orders.update().where(db.orders.c.id == order_id).values(status="paid", updated_at=db.utcnow())
        )

    return {"ok": True, "order_id": order_id, "payment_id": payment_id, "status": status}
