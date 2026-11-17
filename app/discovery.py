from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Request
from psycopg.types.json import Jsonb
from pydantic import Field, model_validator

from app.auth import Authenticated, Editor, throttle
from app.db import DB, Conn, audit, one
from app.registry import ITEM_PUBLIC, PUBLIC, item_source, paginated
from app.schemas import ContentFields, Id, Model, Page, PageQuery, Publication, Reason, Slug

router = APIRouter(tags=["discovery"])
CollectionKind = Literal["exhibition", "collection", "route"]


class CollectionInput(Model):
    slug: Slug
    kind: CollectionKind
    fields: ContentFields
    work_ids: list[UUID] = Field(default_factory=list, max_length=100)
    place_ids: list[UUID] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def members(self):
        if len(set(self.work_ids)) != len(self.work_ids) or len(set(self.place_ids)) != len(self.place_ids):
            raise ValueError("duplicate entry")
        if self.kind == "route" and (self.work_ids or len(self.place_ids) < 2):
            raise ValueError("a route requires at least two places and only place entries")
        if self.kind != "route" and (self.place_ids or not self.work_ids):
            raise ValueError("a collection requires work entries")
        return self


class Collection(CollectionInput):
    id: UUID
    status: Publication


class CollectionFilters(PageQuery):
    kind: CollectionKind | None = None


WORKS = f"""ARRAY(SELECT i.id FROM unnest(b.work_ids) WITH ORDINALITY AS w(id,n)
    JOIN {item_source("portfolio_item")} ON i.id=w.id
    WHERE {PUBLIC} AND {ITEM_PUBLIC} ORDER BY w.n)"""
PLACES = """ARRAY(SELECT p.id FROM unnest(b.place_ids) WITH ORDINALITY AS x(id,n)
    JOIN places p ON p.id=x.id WHERE p.is_public AND p.verified_at IS NOT NULL ORDER BY x.n)"""
COLUMNS = "id,slug,kind,fields,work_ids,place_ids,status"
PUBLIC_COLUMNS = f"b.id,b.slug,b.kind,b.fields,{WORKS} AS work_ids,{PLACES} AS place_ids,b.status"


async def check_collection(db: Conn, data: CollectionInput):
    works = await one(
        db,
        f"""SELECT count(*) AS n FROM {item_source("portfolio_item")}
      WHERE i.id=ANY(%s) AND {PUBLIC} AND {ITEM_PUBLIC}""",
        (data.work_ids,),
    )
    places = await one(
        db,
        """SELECT count(*) AS n FROM places
      WHERE id=ANY(%s) AND is_public AND verified_at IS NOT NULL""",
        (data.place_ids,),
    )
    if works["n"] != len(data.work_ids) or places["n"] != len(data.place_ids):
        raise HTTPException(422, "entries must be publicly available")


@router.post("/editor/collections", status_code=201)
async def make_collection(data: CollectionInput, user: Editor, db: DB) -> Collection:
    row = await one(
        db,
        f"""
        INSERT INTO editorial_collections(slug,kind,fields,work_ids,place_ids,author_id)
        VALUES(%s,%s,%s,%s,%s,%s) RETURNING {COLUMNS}
    """,
        (data.slug, data.kind, Jsonb(data.fields.model_dump()), data.work_ids, data.place_ids, user.id),
    )
    await audit(db, user.id, "collection.created", row["id"])
    return Collection(**row)


@router.put("/editor/collections/{entity}")
async def edit_collection(entity: UUID, data: CollectionInput, user: Editor, db: DB) -> Collection:
    row = await one(
        db,
        f"""
        UPDATE editorial_collections SET slug=%s,kind=%s,fields=%s,work_ids=%s,place_ids=%s,
        status='unpublished',updated_at=now() WHERE id=%s RETURNING {COLUMNS}
    """,
        (data.slug, data.kind, Jsonb(data.fields.model_dump()), data.work_ids, data.place_ids, entity),
    )
    if not row:
        raise HTTPException(404, "collection not found")
    await audit(db, user.id, "collection.updated", entity)
    return Collection(**row)


@router.post("/editor/collections/{entity}/publish", status_code=204)
async def publish_collection(entity: UUID, user: Editor, db: DB):
    row = await one(db, f"SELECT {COLUMNS} FROM editorial_collections WHERE id=%s FOR UPDATE", (entity,))
    if not row:
        raise HTTPException(404, "collection not found")
    data = Collection(**row)
    if not data.fields.title.complete():
        raise HTTPException(409, "all title locales required")
    await check_collection(db, data)
    await db.execute("UPDATE editorial_collections SET status='published' WHERE id=%s", (entity,))
    await audit(db, user.id, "collection.published", entity)


class PublicCollection(Model):
    id: UUID
    slug: Slug
    kind: CollectionKind
    fields: ContentFields
    work_ids: list[UUID]
    place_ids: list[UUID]
    status: Publication


@router.get("/collections")
async def collections(f: Annotated[CollectionFilters, Query()], db: DB) -> Page[PublicCollection]:
    where = "b.status='published'"
    params = ()
    if f.kind:
        where += " AND b.kind=%s"
        params = (f.kind,)
    return Page[PublicCollection](
        **await paginated(db, PUBLIC_COLUMNS, "editorial_collections b", where, params, f, "slug,id")
    )


@router.get("/collections/{slug}")
async def collection_detail(slug: Slug, db: DB) -> PublicCollection:
    row = await one(
        db,
        f"SELECT {PUBLIC_COLUMNS} FROM editorial_collections b WHERE slug=%s AND status='published'",
        (slug,),
    )
    if not row:
        raise HTTPException(404, "collection not found")
    return PublicCollection(**row)


class Collaboration(Model):
    message: Reason


@router.post("/creators/{slug}/collaboration", status_code=202)
async def collaborate(slug: Slug, data: Collaboration, request: Request, user: Authenticated, db: DB) -> Id:
    await throttle(request, "collaboration", 5, 3600, str(user.id))
    creator = await one(
        db,
        f"SELECT c.id,c.user_id FROM creators c WHERE c.slug=%s AND {PUBLIC} "
        "AND c.available_for_collaboration FOR UPDATE OF c",
        (slug,),
    )
    if not creator or creator["user_id"] == user.id:
        raise HTTPException(404, "available collaborator not found")
    if await one(
        db,
        """
        SELECT id FROM collaboration_requests WHERE sender_id=%s AND creator_id=%s AND message=%s
        AND created_at>now()-interval '24 hours'
    """,
        (user.id, creator["id"], data.message),
    ):
        raise HTTPException(409, "request already delivered")
    row = await one(
        db,
        """
        INSERT INTO collaboration_requests(sender_id,creator_id,message) VALUES (%s,%s,%s) RETURNING id
    """,
        (user.id, creator["id"], data.message),
    )
    await db.execute(
        """
        INSERT INTO notifications(user_id,template_id,locale,context)
        SELECT id,'collaboration.received',locale,%s FROM users WHERE id=%s
    """,
        (
            Jsonb({"request_id": str(row["id"]), "message": data.message, "sender_id": str(user.id)}),
            creator["user_id"],
        ),
    )
    return Id(**row)
