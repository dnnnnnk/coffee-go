from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from ..config import get_settings
from .routes import router as web_router


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(title="Coffee-Go Web")

    # templates live in src/web/templates
    templates_dir = Path(__file__).resolve().parent / "templates"
    app.state.templates = Jinja2Templates(directory=str(templates_dir))

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.WEB_SESSION_SECRET,
        session_cookie=settings.WEB_COOKIE_NAME,
        same_site=settings.WEB_COOKIE_SAMESITE,
        https_only=settings.WEB_COOKIE_HTTPS_ONLY,
        max_age=int(settings.WEB_SESSION_TTL_MIN) * 60,
    )

    app.include_router(web_router)
    return app


app = create_app()