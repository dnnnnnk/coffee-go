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


# ----------------------------
# Public API used by handlers/services
# ----------------------------


def get_yookassa_client(settings) -> YooKassaClient:
    if not getattr(settings, "YOOKASSA_SHOP_ID", None) or not getattr(settings, "YOOKASSA_SECRET_KEY", None):
        raise PaymentError("Не настроены ключи ЮKassa.")
    return YooKassaClient(
        shop_id=str(settings.YOOKASSA_SHOP_ID),
        secret_key=str(settings.YOOKASSA_SECRET_KEY),
        timeout_sec=int(getattr(settings, "YOOKASSA_TIMEOUT_SEC", 15)),
    )


def build_topup_description(settings, *, topup_id: int) -> str:
    prefix = getattr(settings, "TOPUP_DESCRIPTION_PREFIX", "EasyCoffee topup")
    return f"{prefix} #{topup_id}"


async def create_payment_for_topup(
    session: AsyncSession,
    settings,
    *,
    topup_id: int,
    amount: Decimal,
    user_id: int,
) -> PaymentCreateResult:
    """Создаёт платеж в YooKassa для пополнения баланса (topup)."""

    return_url = getattr(settings, "PAYMENT_RETURN_URL", None)
    if not return_url:
        raise PaymentError("PAYMENT_RETURN_URL не задан.")

    client = get_yookassa_client(settings)

    # stable idempotence key: topup id
    idem = f"topup:{topup_id}"

    metadata = {"topup_id": str(topup_id), "user_id": str(user_id)}

    notification_url = None
    if getattr(settings, "WEBHOOKS_ENABLED", False):
        base = getattr(settings, "PUBLIC_BASE_URL", None)
        if base:
            path = getattr(settings, "yookassa_webhook_url_path", "/webhooks/yookassa")
            secret = getattr(settings, "WEBHOOK_SECRET", None)
            if secret:
                notification_url = f"{base.rstrip('/')}{path}?secret={secret}"
            else:
                notification_url = f"{base.rstrip('/')}{path}"

    res = await client.create_payment(
        amount=amount,
        currency=str(getattr(settings, "CURRENCY", "RUB")),
        description=build_topup_description(settings, topup_id=topup_id),
        return_url=str(return_url),
        capture=bool(getattr(settings, "PAYMENT_CAPTURE", True)),
        idempotence_key=idem,
        metadata=metadata,
        notification_url=notification_url,
    )

    # persist provider info into topup row
    await db.set_topup_provider_payment(
        session,
        topup_id=topup_id,
        provider_payment_id=res.payment_id,
        confirmation_url=res.confirmation_url,
        status=str(res.status),
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
    """Идемпотентная обработка webhook YooKassa для пополнений (topups).

    Делает:
      - определяет topup_id из metadata (лучший вариант)
      - иначе пытается найти topup по provider_payment_id
      - обновляет topup.status и сохраняет payload
    """

    if not isinstance(payload, dict):
        raise PaymentError("Webhook payload должен быть JSON-объектом.")

    event = str(payload.get("event", "")).strip()
    pay_obj = _extract_payment_object(payload)

    payment_id = str(pay_obj.get("id") or "").strip()
    if not payment_id:
        raise PaymentError("Некорректный webhook: нет payment id.")

    status = _event_to_status(event, str(pay_obj.get("status") or "").strip() or None)

    # 1) try metadata
    topup_id: int | None = None
    md = pay_obj.get("metadata")
    if isinstance(md, dict) and "topup_id" in md:
        try:
            topup_id = int(str(md["topup_id"]))
        except Exception:
            topup_id = None

    # 2) fallback by provider_payment_id
    if topup_id is None:
        q = select(db.topups.c.id).where(
            and_(db.topups.c.provider == "yookassa", db.topups.c.provider_payment_id == payment_id)
        )
        found = (await session.execute(q)).scalar_one_or_none()
        if found is not None:
            topup_id = int(found)

    if topup_id is None:
        raise PaymentError("Не удалось определить topup_id для webhook.")

    confirmation_url = None
    conf = pay_obj.get("confirmation")
    if isinstance(conf, dict):
        confirmation_url = conf.get("confirmation_url")

    # Save provider info + status
    await db.set_topup_provider_payment(
        session,
        topup_id=topup_id,
        provider_payment_id=payment_id,
        confirmation_url=confirmation_url,
        status=str(status),
        payload_json=payload,
    )

    # Update topup status
    await db.set_topup_status(session, topup_id, str(status))

    return {"ok": True, "topup_id": topup_id, "payment_id": payment_id, "status": status}


# ----------------------------
# Reconciliation utility
# ----------------------------


async def refresh_topup_status_from_provider(
    session: AsyncSession,
    settings,
    *,
    topup_id: int,
) -> dict[str, Any]:
    """Если webhook не дошёл: вручную дернуть статус платежа по topup."""

    topup = await db.get_topup(session, topup_id)
    if not topup:
        raise PaymentError("Пополнение не найдено.")

    provider_payment_id = str(topup.get("provider_payment_id") or "").strip()
    if not provider_payment_id:
        raise PaymentError("У пополнения нет provider_payment_id.")

    client = get_yookassa_client(settings)
    payload = await client.get_payment(provider_payment_id)

    status = str(payload.get("status", "pending"))
    confirmation_url = None
    conf = payload.get("confirmation")
    if isinstance(conf, dict):
        confirmation_url = conf.get("confirmation_url")

    await db.set_topup_provider_payment(
        session,
        topup_id=topup_id,
        provider_payment_id=provider_payment_id,
        confirmation_url=confirmation_url,
        status=status,
        payload_json=payload,
    )

    await db.set_topup_status(session, topup_id, status)

    return {"topup_id": topup_id, "provider_payment_id": provider_payment_id, "status": status}
