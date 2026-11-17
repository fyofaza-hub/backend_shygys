import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from psycopg import Error
from psycopg.errors import CheckViolation, ForeignKeyViolation, UniqueViolation
from psycopg_pool import PoolTimeout
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app import (
    admin,
    analytics,
    auth,
    cms,
    content,
    discovery,
    geo,
    media,
    notifications,
    registry,
    search,
    translation,
    workflows,
)
from app.config import settings
from app.db import make_pool


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.db = make_pool()
    app.state.redis = Redis.from_url(settings.redis_url, socket_timeout=3)
    await app.state.db.open()
    try:
        yield
    finally:
        await app.state.redis.aclose()
        await app.state.db.close()


app = FastAPI(
    title="Creative Shygys",
    version="0.1.0",
    lifespan=lifespan,
    debug=False,
    docs_url=None if settings.env == "production" else "/docs",
    redoc_url=None if settings.env == "production" else "/redoc",
    openapi_url=None if settings.env == "production" else "/openapi.json",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)
for module in (
    auth,
    registry,
    cms,
    content,
    translation,
    media,
    admin,
    geo,
    analytics,
    search,
    notifications,
    discovery,
    workflows,
):
    app.include_router(module.router)
    app.include_router(module.router, prefix="/api/v1")


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"detail": [{key: error[key] for key in ("loc", "msg", "type")} for error in exc.errors()]},
    )


@app.exception_handler(UniqueViolation)
async def conflict(request: Request, exc: UniqueViolation):
    return JSONResponse(status_code=409, content={"detail": "record already exists"})


@app.exception_handler(ForeignKeyViolation)
@app.exception_handler(CheckViolation)
async def invalid_reference(request: Request, exc: Exception):
    return JSONResponse(status_code=422, content={"detail": "invalid reference or state"})


@app.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
async def ready(request: Request) -> JSONResponse:
    try:
        async with request.app.state.db.connection(timeout=3) as db:
            await db.execute("SELECT 1 FROM alembic_version")
        await request.app.state.redis.ping()
    except (Error, PoolTimeout, RedisError):
        return JSONResponse(status_code=503, content={"status": "unavailable"})
    return JSONResponse(content={"status": "ok"})


@app.exception_handler(Exception)
async def internal_error(request: Request, exc: Exception):
    logging.getLogger(__name__).error("Unhandled server error", exc_info=(type(exc), exc, exc.__traceback__))
    return JSONResponse(status_code=500, content={"detail": "internal server error"})
