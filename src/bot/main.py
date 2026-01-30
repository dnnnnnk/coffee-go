# src/main.py
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from src.config import get_settings
from src.db import get_engine, init_db, session_scope
from src.bot.handlers import router as main_router
from src import payments
from src import services
from src.seeds.demo import seed_minimal_demo_data
from src.bot.outbox import run_outbox_worker

logger = logging.getLogger("easycoffee")



def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


def build_dispatcher() -> Dispatcher:
    dp = Dispatcher()
    dp.include_router(main_router)
    return dp


def build_bot(token: str) -> Bot:
    return Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


async def run_bot(settings) -> None:
    bot = build_bot(settings.BOT_TOKEN)
    dp = build_dispatcher()

    outbox_task = asyncio.create_task(run_outbox_worker(settings, bot), name="outbox")

    try:
        await dp.start_polling(bot)
    finally:
        outbox_task.cancel()
        with suppress(Exception):
            await outbox_task
        with suppress(Exception):
            await bot.session.close()


def build_http_app(settings):
    from fastapi import FastAPI, Request
    from fastapi.responses import JSONResponse

    app = FastAPI(title="EasyCoffee", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post(settings.YOOKASSA_WEBHOOK_PATH)
    async def yookassa_webhook(request: Request):
        # secret check (query ?secret=... or header X-Webhook-Secret)
        try:
            payments.verify_webhook_secret(
                settings=settings,
                query_params=dict(request.query_params),
                headers=dict(request.headers),
            )
        except payments.PaymentError as e:
            return JSONResponse(status_code=401, content={"ok": False, "error": e.message})

        try:
            payload = await request.json()
        except Exception:
            return JSONResponse(status_code=400, content={"ok": False, "error": "Invalid JSON"})

        try:
            async with session_scope(settings) as s:
                res = await payments.process_yookassa_webhook(s, settings, payload=payload)
            return JSONResponse(status_code=200, content=res)
        except payments.PaymentError as e:
            # не 500, чтобы ЮKassa не долбила ретраями бесконечно из-за “логических” ошибок
            return JSONResponse(status_code=400, content={"ok": False, "error": e.message})
        except Exception as e:
            logger.exception("Webhook processing error")
            return JSONResponse(status_code=500, content={"ok": False, "error": "Internal error"})

    return app


async def run_http(settings) -> None:
    import uvicorn

    app = build_http_app(settings)
    config = uvicorn.Config(
        app,
        host=settings.HTTP_HOST,
        port=settings.HTTP_PORT,
        log_level=str(settings.LOG_LEVEL).lower(),
        loop="asyncio",
    )
    server = uvicorn.Server(config)
    await server.serve()


async def main() -> None:
    settings = get_settings()
    _setup_logging(settings.LOG_LEVEL)

    await init_db(settings)

    # автосид для локального теста: если БД пустая — добавим минимальные данные
    if settings.ENV in {"dev", "test"}:
        async with session_scope(settings) as s:
            await seed_minimal_demo_data(s)

    engine = get_engine(settings)
    try:
        if settings.WEBHOOKS_ENABLED:
            bot_task = asyncio.create_task(run_bot(settings), name="bot")
            http_task = asyncio.create_task(run_http(settings), name="http")

            done, pending = await asyncio.wait(
                {bot_task, http_task},
                return_when=asyncio.FIRST_EXCEPTION,
            )

            # if one crashed/canceled — stop the other
            for t in pending:
                t.cancel()
            for t in done:
                # propagate exception if any
                t.result()
        else:
            await run_bot(settings)
    finally:
        with suppress(Exception):
            await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
