from contextlib import asynccontextmanager
from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, Request
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool

from app.config import settings

Conn = AsyncConnection[dict[str, Any]]


def make_pool() -> AsyncConnectionPool:
    return AsyncConnectionPool(settings.database_url, kwargs={"row_factory": dict_row}, open=False)


async def connection(request: Request):
    async with request.app.state.db.connection() as conn:
        yield conn


DB = Annotated[Conn, Depends(connection, scope="function")]


async def one(conn: Conn, query: str, params: tuple = ()) -> dict | None:
    return await (await conn.execute(query, params)).fetchone()


async def many(conn: Conn, query: str, params: tuple = ()) -> list[dict]:
    return await (await conn.execute(query, params)).fetchall()


@asynccontextmanager
async def worker_db():
    async with await AsyncConnection.connect(settings.database_url, row_factory=dict_row) as db:
        yield db


async def audit(db: Conn, actor: UUID, action: str, entity: UUID | None = None, **context: Any):
    await db.execute(
        "INSERT INTO audit_events(actor_id, action, entity_id, context) VALUES (%s,%s,%s,%s)",
        (actor, action, entity, Jsonb(context)),
    )
