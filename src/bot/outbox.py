import asyncio
from contextlib import suppress

from aiogram import Bot

from src.db import session_scope, fetch_pending_order_status_events, mark_order_status_event_sent, mark_order_status_event_failed

STATUS_LABEL = {
    "pending_payment": "ожидает оплаты",
    "paid": "оплачен",
    "accepted": "в работе",
    "in_progress": "готов",
    "done": "выдан",
}

def format_status_msg(order_id: int, new_status: str) -> str:
    st = STATUS_LABEL.get(new_status, new_status)
    if new_status == "accepted":
        return f"☕️ Заказ <b>#{order_id}</b> принят в работу."
    if new_status == "in_progress":
        return f"✅ Заказ <b>#{order_id}</b> готов."
    if new_status == "done":
        return f"🎁 Заказ <b>#{order_id}</b> выдан. Спасибо!"
    return f"ℹ️ Статус заказа <b>#{order_id}</b>: <b>{st}</b>."

async def run_outbox_worker(settings, bot: Bot) -> None:
    while True:
        try:
            async with session_scope(settings) as s:
                events = await fetch_pending_order_status_events(s, limit=50)

                for ev in events:
                    event_id = int(ev["id"])
                    order_id = int(ev["order_id"])
                    user_id = int(ev["user_id"])
                    new_status = str(ev["new_status"])

                    try:
                        await bot.send_message(user_id, format_status_msg(order_id, new_status))
                        await mark_order_status_event_sent(s, event_id)
                    except Exception as e:
                        await mark_order_status_event_failed(s, event_id, str(e))
        except Exception:
            # не падаем навсегда из-за одной проблемы
            pass

        await asyncio.sleep(1.5)