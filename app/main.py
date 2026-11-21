"""FastAPI application entrypoint."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import settings
from .db import init_db
from .routers import auth, devices, discovery

log = logging.getLogger("gremote")

STATIC_DIR =Path(__file__).resolve().parent / "static"


@asynccontextmanager
async def lifespan(_: FastAPI):
    if "change-me" in (settings.secret_key, settings.admin_password):
        log.warning(
            "GREMOTE_SECRET_KEY / GREMOTE_ADMIN_PASSWORD are still the default 'change-me'; "
            "set real values in .env before exposing this."
        )
    init_db()
    yield


app = FastAPI(title=settings.app_title, lifespan=lifespan)

app.include_router(auth.router)
app.include_router(devices.router)
app.include_router(discovery.router)

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "index.html")
