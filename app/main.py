"""FastAPI application entrypoint."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from app import config


@asynccontextmanager
async def lifespan(app: FastAPI):
    # DB init + settings seeding are wired in the next build stage.
    yield


app = FastAPI(title=config.APP_NAME, version=config.APP_VERSION, lifespan=lifespan)


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
