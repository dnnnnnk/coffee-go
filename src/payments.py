# src/payments.py
from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, Mapping, Optional

import aiohttp
from sqlalchemy.ext.asyncio import AsyncSession

import db


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

PaymentStatus = Literal[
    "pending", "waiting_for_capture", "succeeded", "canceled"
]

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
    """
    Минимальный, но боевой клиент YooKassa через HTTP API v3.

    Важно:
      - idempotence key обязателен для create_payment/capture (иначе дубли при ретраях)
      - таймауты и обработка ошибок должны быть явными
    """

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
                    # YooKassa returns JSON even on errors
                    try:
                        payload = json.loads(text) if text else {}
                    except json.JSONDecodeError:
                        payload = {"_raw": text}

                    if resp.status >= 400:
                        # Typical payload: { "type": "...", "id": "...", "code": "...", "description": "...", ... }
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
        # receipt: optional, not used now, but left for future compliance requirements
        receipt: dict[str, Any] | None = None,
        # notification_url may be configured in YooKassa dashboard; passing here is optional
        notification_url: str | None = None,
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

    async def capture_payment(self, payment_id: str, *, amount: Decimal, currency: str, idempotence_key: str) -> dict[str, Any]:
        body = {"amount": {"value": money_str(amount), "currency": currency}}
        return await self._request("POST", f"/payments/{payment_id}/capture", idempotence_key=idempotence_key, json_body=body)


# ----------------------------
# Public API used by handlers/services
# ----------------------------

def build_payment_description(settings, *, order_id: int) -> str:
    prefix = getattr(settings, "PAYMENT_DESCRIPTION_PREFIX", "EasyCoffee order")
    return f"{prefix} #{order_id}"


def get_yookassa_client(settings) -> YooKassaClient:
    if not getattr(settings, "YOOKASSA_SHOP_ID", None) or not getattr(settings, "YOOKASSA_SECRET_KEY", None):
        raise PaymentError("Не настроены ключи ЮKassa.")
    return YooKassaClient(
        shop_id=str(settings.YOOKASSA_SHOP_ID),
        secret_key=str(settings.YOOKASSA_SECRET_KEY),
        timeout_sec=int(getattr(settings, "YOOKASSA_TIMEOUT_SEC", 15)),
    )


async def create_payment_for_order(
    session: AsyncSession,
    settings,
    *,
    order_id: int,
    amount: Decimal,
    user_id: int,
    cafe_id: int,
) -> PaymentCreateResult:
    """
    Создаёт платеж в ЮKassa.
    Важно: запись в БД по платежу делаем сразу (upsert_payment) — чтобы вебхук мог матчиться даже без metadata.
    """
    if not getattr(settings, "PAYMENTS_ENABLED", False):
        raise PaymentError("Оплата отключена настройками.")

    return_url = getattr(settings, "PAYMENT_RETURN_URL", None)
    if not return_url:
        raise PaymentError("PAYMENT_RETURN_URL не задан.")

    client = get_yookassa_client(settings)

    # idempotence key: order_id is perfect stable key
    idem = str(order_id)

    # metadata helps link in webhooks
    metadata = {"order_id": str(order_id), "user_id": str(user_id), "cafe_id": str(cafe_id)}

    # notification_url: optional (often configured in dashboard)
    notification_url = None
    if getattr(settings, "WEBHOOKS_ENABLED", False):
        # If user provided PUBLIC_BASE_URL in env, we can build absolute URL.
        base = getattr(settings, "PUBLIC_BASE_URL", None)
        if base:
            path = settings.yookassa_webhook_url_path
            secret = getattr(settings, "WEBHOOK_SECRET", None)
            if secret:
                notification_url = f"{base.rstrip('/')}{path}?secret={secret}"
            else:
                notification_url = f"{base.rstrip('/')}{path}"

    res = await client.create_payment(
        amount=amount,
        currency=str(getattr(settings, "CURRENCY", "RUB")),
        description=build_payment_description(settings, order_id=order_id),
        return_url=str(return_url),
        capture=bool(getattr(settings, "PAYMENT_CAPTURE", True)),
        idempotence_key=idem,
        metadata=metadata,
        notification_url=notification_url,
    )

    # persist immediately (idempotent)
    await db.upsert_payment(
        session,
        order_id=order_id,
        provider=res.provider,
        payment_id=res.payment_id,
        status=res.status,
        confirmation_url=res.confirmation_url,
        payload_json=res.raw,
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
    """
    YooKassa notifications do NOT support custom headers.
    Поэтому нормальный вариант — вешать секрет в query (?secret=...),
    либо закрывать endpoint на уровне сети/прокси (лучше).
    """
    secret = getattr(settings, "WEBHOOK_SECRET", None)
    if not secret:
        return  # nothing to verify

    qp = query_params or {}
    if qp.get("secret") == secret:
        return

    # as fallback (if you route through proxy and add header yourself)
    hs = headers or {}
    if hs.get("X-Webhook-Secret") == secret:
        return

    raise PaymentError("Неверный webhook secret.")


def _extract_payment_object(payload: dict[str, Any]) -> dict[str, Any]:
    """
    Expected YooKassa notification:
      { "type": "notification", "event": "payment.succeeded", "object": { ...payment... } }
    """
    obj = payload.get("object")
    if not isinstance(obj, dict):
        raise PaymentError("Некорректный webhook: нет object.")
    return obj


def _event_to_status(event: str, payment_status: str | None) -> PaymentStatus:
    # Prefer payment.status if present, else map from event
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
    """
    Идемпотентная обработка webhook.
    Делает:
      - вытаскивает order_id из metadata (лучше) или пытается сопоставить по payment_id (если ранее сохранили)
      - upsert payments
      - переводит order.status:
          succeeded -> paid
          canceled  -> canceled (если ещё не paid)
    """
    if not isinstance(payload, dict):
        raise PaymentError("Webhook payload должен быть JSON-объектом.")

    event = str(payload.get("event", "")).strip()
    pay_obj = _extract_payment_object(payload)

    payment_id = str(pay_obj.get("id") or "").strip()
    if not payment_id:
        raise PaymentError("Некорректный webhook: нет payment id.")

    status = _event_to_status(event, str(pay_obj.get("status") or "").strip() or None)

    # order_id from metadata
    order_id: int | None = None
    md = pay_obj.get("metadata")
    if isinstance(md, dict) and "order_id" in md:
        try:
            order_id = int(str(md["order_id"]))
        except Exception:
            order_id = None

    # If metadata absent/broken — try to find payment row by provider+payment_id
    if order_id is None:
        q = select(db.payments.c.order_id).where(
            and_(db.payments.c.provider == "yookassa", db.payments.c.payment_id == payment_id)
        )
        found = (await session.execute(q)).scalar_one_or_none()
        if found is not None:
            order_id = int(found)

    if order_id is None:
        # Nothing we can safely update (avoid corrupting random order)
        raise PaymentError("Не удалось определить order_id для webhook.")

    # Persist payment status/payload
    confirmation_url = None
    conf = pay_obj.get("confirmation")
    if isinstance(conf, dict):
        confirmation_url = conf.get("confirmation_url")

    await db.upsert_payment(
        session,
        order_id=order_id,
        provider="yookassa",
        payment_id=payment_id,
        status=status,
        confirmation_url=confirmation_url,
        payload_json=payload,
    )

    # Update order status with safety:
    order = await db.get_order(session, order_id)
    if not order:
        raise PaymentError("Заказ для webhook не найден.")

    cur_status = str(order["status"])
    if status == "succeeded":
        if cur_status != "paid":
            # only transition from pending_payment/created to paid
            if cur_status in {"pending_payment", "created"}:
                await db.set_order_status(session, order_id, "paid")
            # If already canceled/done/etc — do not overwrite silently
    elif status == "canceled":
        # do not cancel already paid orders
        if cur_status in {"pending_payment", "created"}:
            await db.set_order_status(session, order_id, "canceled")

    return {"ok": True, "order_id": order_id, "payment_id": payment_id, "status": status}


# ----------------------------
# Optional: reconciliation utility
# ----------------------------

async def refresh_payment_status_from_provider(session: AsyncSession, settings, *, order_id: int) -> dict[str, Any]:
    """
    На случай, если webhook не дошёл: можно вручную дернуть статус платежа.
    """
    row = await db.get_payment_by_order(session, order_id)
    if not row:
        raise PaymentError("Платёж для заказа не найден.")

    if row["provider"] != "yookassa":
        raise PaymentError("Неизвестный провайдер оплаты.")

    client = get_yookassa_client(settings)
    provider_payment_id = str(row["payment_id"])
    payload = await client.get_payment(provider_payment_id)

    status = str(payload.get("status", "pending"))
    confirmation_url = None
    conf = payload.get("confirmation")
    if isinstance(conf, dict):
        confirmation_url = conf.get("confirmation_url")

    await db.upsert_payment(
        session,
        order_id=order_id,
        provider="yookassa",
        payment_id=provider_payment_id,
        status=status,
        confirmation_url=confirmation_url,
        payload_json=payload,
    )

    # Sync order status carefully
    if status == "succeeded":
        order = await db.get_order(session, order_id)
        if order and str(order["status"]) in {"pending_payment", "created"}:
            await db.set_order_status(session, order_id, "paid")

    return {"order_id": order_id, "provider_payment_id": provider_payment_id, "status": status}
