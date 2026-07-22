"""FastAPI application entrypoint."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app import config
from app.api import imports as imports_api
from app.api import settings as settings_api
from app.db import init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title=config.APP_NAME, version=config.APP_VERSION, lifespan=lifespan)

app.include_router(imports_api.router)
app.include_router(settings_api.router)


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
