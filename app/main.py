"""FastAPI application entrypoint."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app import config
from app.api import accounts as accounts_api
from app.api import booking as booking_api
from app.api import events as events_api
from app.api import imports as imports_api
from app.api import settings as settings_api
from app.api import treks as treks_api
from app.api import trekkers as trekkers_api
from app.db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title=config.APP_NAME, version=config.APP_VERSION, lifespan=lifespan)

app.include_router(imports_api.router)
app.include_router(settings_api.router)
app.include_router(accounts_api.router)
app.include_router(treks_api.router)
app.include_router(trekkers_api.router)
app.include_router(events_api.router)
app.include_router(booking_api.router)


@app.get("/api/health")
def health() -> JSONResponse:
    return JSONResponse(
        {
            "status": "ok",
            "app": config.APP_NAME,
            "version": config.APP_VERSION,
            "data_dir": str(config.DATA_DIR),
        }
    )
