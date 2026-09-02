"""FastAPI application entrypoint."""
from __future__ import annotations

import base64
import binascii
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from app import config
from app.api import accounts as accounts_api
from app.api import booking as booking_api
from app.api import dashboard as dashboard_api
from app.api import events as events_api
from app.api import imports as imports_api
from app.api import settings as settings_api
from app.api import tickets as tickets_api
from app.api import treks as treks_api
from app.api import trekkers as trekkers_api
from app.db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title=config.APP_NAME, version=config.APP_VERSION, lifespan=lifespan)


def _unauthorized() -> Response:
    return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Karavan"'})


@app.middleware("http")
async def _basic_auth(request, call_next):
    # Shared-login gate for hosted deployments. Off by default (local dev,
    # and any deployment that doesn't set BB_AUTH_USER/BB_AUTH_PASS) so
    # nothing changes for the existing local-first workflow. The health
    # check stays open for the hosting platform's own liveness probes.
    if config.AUTH_USER and request.url.path != "/api/health":
        header = request.headers.get("authorization", "")
        ok = False
        if header.startswith("Basic "):
            try:
                raw = base64.b64decode(header[6:]).decode("utf-8")
                user, _, pw = raw.partition(":")
                ok = (secrets.compare_digest(user, config.AUTH_USER)
                      and secrets.compare_digest(pw, config.AUTH_PASS))
            except (binascii.Error, UnicodeDecodeError, ValueError):
                ok = False
        if not ok:
            return _unauthorized()
    return await call_next(request)


@app.middleware("http")
async def _no_store_static(request, call_next):
    # The UI is a single small bundle; never let a browser serve stale JS/CSS.
    response = await call_next(request)
    if request.url.path.startswith("/static") or request.url.path == "/":
        response.headers["Cache-Control"] = "no-store"
    return response


app.include_router(imports_api.router)
app.include_router(settings_api.router)
app.include_router(accounts_api.router)
app.include_router(treks_api.router)
app.include_router(trekkers_api.router)
app.include_router(events_api.router)
app.include_router(booking_api.router)
app.include_router(tickets_api.router)
app.include_router(dashboard_api.router)


@app.get("/api/health")
def health() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "app": config.APP_NAME,
            "version": config.APP_VERSION,
            "dry_run": config.DRY_RUN,
        }
    )


_STATIC = Path(__file__).resolve().parent / "static"


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


app.mount("/static", StaticFiles(directory=_STATIC), name="static")
