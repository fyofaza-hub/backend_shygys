from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response
from psycopg.types.json import Jsonb
from pydantic import Field, model_validator
from starlette.concurrency import run_in_threadpool

from app.analytics import REPORT_TYPES, table_bytes
from app.auth import Admin, Editor
from app.content import EVENT_FIELDS, OPPORTUNITY_FIELDS, record_cms
from app.db import DB, Conn, many, one
from app.geo import Place, PlaceInput, detach_place
from app.registry import paginated, pattern
from app.schemas import Event, Localized, Model, Opportunity, Page, PageQuery, Publication, TextSearch

router = APIRouter(tags=["cms"])
ContentKind = Literal["articles", "events", "opportunities"]
Dictionary = Literal["categories", "districts", "tags", "oked"]
DICTIONARY_TABLES = {
    "categories": "creative_categories",
    "districts": "districts",
    "tags": "tags",
    "oked": "oked",
}
PLACE_COLUMNS = (
    "id,labels,locality_id,kind,is_public,ST_X(geo_point) AS longitude,"
    "ST_Y(geo_point) AS latitude,archived_at"
)


class ContentFilters(TextSearch):
    status: Publication | None = None


async def cms_list(db: Conn, table: str, fields: str, f: ContentFilters):
    where, params = ["true"], []
    if f.status is not None:
        where.append("status=%s")
        params.append(f.status)
    if f.q:
        where.append("to_tsvector('simple', fields::text) @@ websearch_to_tsquery('simple',%s)")
        params.append(f.q)
    return await paginated(db, fields, table, " AND ".join(where), tuple(params), f, "id")


@router.get("/editor/events")
async def cms_events(f: Annotated[ContentFilters, Query()], user: Editor, db: DB) -> Page[Event]:
    return Page[Event](**await cms_list(db, "events", EVENT_FIELDS, f))


@router.get("/editor/opportunities")
async def cms_opportunities(f: Annotated[ContentFilters, Query()], user: Editor, db: DB) -> Page[Opportunity]:
    return Page[Opportunity](**await cms_list(db, "opportunities", OPPORTUNITY_FIELDS, f))


@router.get("/editor/events/{entity}")
async def cms_event(entity: UUID, user: Editor, db: DB) -> Event:
    value = await one(db, f"SELECT {EVENT_FIELDS} FROM events WHERE id=%s", (entity,))
    if not value:
        raise HTTPException(404, "event not found")
    return Event(**value)


@router.get("/editor/opportunities/{entity}")
async def cms_opportunity(entity: UUID, user: Editor, db: DB) -> Opportunity:
    value = await one(db, f"SELECT {OPPORTUNITY_FIELDS} FROM opportunities WHERE id=%s", (entity,))
    if not value:
        raise HTTPException(404, "opportunity not found")
    return Opportunity(**value)


async def archive_content(db: Conn, kind: ContentKind, entity: UUID, user_id: UUID):
    value = await one(
        db, f"UPDATE {kind} SET status='archived' WHERE id=%s AND status<>'archived' RETURNING *", (entity,)
    )
    if not value:
        raise HTTPException(404, "active material not found")
    if kind == "articles":
        await db.execute(
            """
            UPDATE translation_jobs SET status='stale',updated_at=now()
            WHERE entity_type='article' AND entity_id=%s AND status IN ('queued','running')
        """,
            (entity,),
        )
    await record_cms(db, user_id, f"{kind}.archived", entity, value)


@router.delete("/editor/articles/{entity}", status_code=204)
async def archive_article(entity: UUID, user: Editor, db: DB):
    await archive_content(db, "articles", entity, user.id)


@router.delete("/editor/events/{entity}", status_code=204)
async def archive_event(entity: UUID, user: Editor, db: DB):
    await archive_content(db, "events", entity, user.id)


@router.delete("/editor/opportunities/{entity}", status_code=204)
async def archive_opportunity(entity: UUID, user: Editor, db: DB):
    await archive_content(db, "opportunities", entity, user.id)


class SpaceEditor(Place):
    archived_at: datetime | None


@router.get("/editor/spaces")
async def space_drafts(f: Annotated[TextSearch, Query()], user: Editor, db: DB) -> Page[SpaceEditor]:
    where, params = ("labels::text ILIKE %s", (pattern(f.q),)) if f.q else ("true", ())
    return Page[SpaceEditor](**await paginated(db, PLACE_COLUMNS, "places", where, params, f, "id"))


@router.get("/editor/spaces/{entity}")
async def space_draft(entity: UUID, user: Editor, db: DB) -> SpaceEditor:
    row = await one(db, f"SELECT {PLACE_COLUMNS} FROM places WHERE id=%s", (entity,))
    if not row:
        raise HTTPException(404, "space not found")
    return SpaceEditor(**row)


@router.post("/editor/spaces", status_code=201)
async def create_space(data: PlaceInput, user: Editor, db: DB) -> SpaceEditor:
    if data.is_public:
        raise HTTPException(422, "create a draft before publishing a space")
    row = await one(
        db,
        f"""
      INSERT INTO places(labels,locality_id,kind,geo_point)
      VALUES(%s,%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),4326)) RETURNING {PLACE_COLUMNS}
    """,
        (Jsonb(data.labels.model_dump()), data.locality_id, data.kind, data.longitude, data.latitude),
    )
    await record_cms(db, user.id, "space.created", row["id"], row)
    return SpaceEditor(**row)


@router.put("/editor/spaces/{entity}")
async def edit_space(entity: UUID, data: PlaceInput, user: Editor, db: DB) -> SpaceEditor:
    row = await one(
        db,
        f"""
      UPDATE places SET labels=%s,locality_id=%s,kind=%s,geo_point=ST_SetSRID(ST_MakePoint(%s,%s),4326),
         is_public=false,verified_at=NULL WHERE id=%s AND archived_at IS NULL RETURNING {PLACE_COLUMNS}
    """,
        (Jsonb(data.labels.model_dump()), data.locality_id, data.kind, data.longitude, data.latitude, entity),
    )
    if not row:
        raise HTTPException(404, "space not found")
    await detach_place(db, entity)
    await record_cms(db, user.id, "space.edited", entity, row)
    return SpaceEditor(**row)


@router.post("/editor/spaces/{entity}/publish")
async def publish_space(entity: UUID, user: Editor, db: DB) -> SpaceEditor:
    row = await one(
        db,
        f"""
      UPDATE places SET is_public=true,verified_at=now() WHERE id=%s AND archived_at IS NULL
      AND labels->>'kk'<>'' AND labels->>'ru'<>'' AND labels->>'en'<>'' RETURNING {PLACE_COLUMNS}
    """,
        (entity,),
    )
    if not row:
        raise HTTPException(409, "space must exist and have all three labels")
    await record_cms(db, user.id, "space.published", entity, row)
    return SpaceEditor(**row)


@router.delete("/editor/spaces/{entity}", status_code=204)
async def archive_space(entity: UUID, user: Editor, db: DB):
    row = await one(
        db,
        f"""
      UPDATE places SET is_public=false,verified_at=NULL,archived_at=now()
      WHERE id=%s AND archived_at IS NULL RETURNING {PLACE_COLUMNS}
    """,
        (entity,),
    )
    if not row:
        raise HTTPException(404, "space not found")
    await detach_place(db, entity)
    await record_cms(db, user.id, "space.archived", entity, row)


class DictionaryEntry(Model):
    code: str = Field(min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9_.-]+$")
    labels: Localized
    longitude: float | None = Field(None, ge=-180, le=180)
    latitude: float | None = Field(None, ge=-85.05, le=85.05)

    @model_validator(mode="after")
    def complete_labels(self):
        if not self.labels.complete():
            raise ValueError("all three labels are required")
        return self


class DictionaryRecord(Model):
    id: UUID
    code: str
    labels: Localized
    longitude: float | None = None
    latitude: float | None = None
    retired_at: datetime | None
    version: str | None = None


class DictionaryFilters(PageQuery):
    version: str | None = None
    include_retired: bool = False


def dictionary_columns(kind: Dictionary) -> str:
    columns = "id,code,labels,retired_at"
    if kind == "districts":
        columns += ",ST_X(centroid) AS longitude,ST_Y(centroid) AS latitude"
    if kind == "oked":
        columns += ",version"
    return columns


@router.get("/editor/dictionaries/{kind}")
async def dictionary(
    kind: Dictionary, f: Annotated[DictionaryFilters, Query()], user: Editor, db: DB
) -> Page[DictionaryRecord]:
    where, params = ["true"], []
    if not f.include_retired:
        where.append("retired_at IS NULL")
    if kind == "oked" and f.version:
        where.append("version=%s")
        params.append(f.version)
    return Page[DictionaryRecord](
        **await paginated(
            db,
            dictionary_columns(kind),
            DICTIONARY_TABLES[kind],
            " AND ".join(where),
            tuple(params),
            f,
            "code,id",
        )
    )


@router.get("/editor/dictionaries/{kind}/{entity}")
async def dictionary_item(kind: Dictionary, entity: UUID, user: Editor, db: DB) -> DictionaryRecord:
    row = await one(
        db, f"SELECT {dictionary_columns(kind)} FROM {DICTIONARY_TABLES[kind]} WHERE id=%s", (entity,)
    )
    if not row:
        raise HTTPException(404, "dictionary item not found")
    return DictionaryRecord(**row)


MutableDictionary = Literal["categories", "districts", "tags"]


@router.post("/editor/dictionaries/{kind}", status_code=201)
async def add_item(kind: MutableDictionary, data: DictionaryEntry, user: Editor, db: DB) -> DictionaryRecord:
    cols, args, holders = "code,labels", (data.code, Jsonb(data.labels.model_dump())), "%s,%s"
    if kind == "districts":
        if data.longitude is None or data.latitude is None:
            raise HTTPException(422, "district centroid is required")
        cols += ",centroid"
        args += (data.longitude, data.latitude)
        holders += ",ST_SetSRID(ST_MakePoint(%s,%s),4326)"
    row = await one(
        db,
        f"""
      INSERT INTO {DICTIONARY_TABLES[kind]}({cols}) VALUES ({holders}) RETURNING {dictionary_columns(kind)}
    """,
        args,
    )
    await record_cms(db, user.id, f"dictionary.{kind}.created", row["id"], row)
    return DictionaryRecord(**row)


@router.put("/editor/dictionaries/{kind}/{entity}")
async def edit_dictionary(
    kind: MutableDictionary, entity: UUID, data: DictionaryEntry, user: Editor, db: DB
) -> DictionaryRecord:
    assignments, args = "code=%s,labels=%s", (data.code, Jsonb(data.labels.model_dump()))
    if kind == "districts":
        if data.longitude is None or data.latitude is None:
            raise HTTPException(422, "district centroid is required")
        assignments += ",centroid=ST_SetSRID(ST_MakePoint(%s,%s),4326)"
        args += (data.longitude, data.latitude)
    row = await one(
        db,
        f"""
       UPDATE {DICTIONARY_TABLES[kind]} SET {assignments} WHERE id=%s AND retired_at IS NULL
       RETURNING {dictionary_columns(kind)}
    """,
        args + (entity,),
    )
    if not row:
        raise HTTPException(404, "active dictionary entry not found")
    if kind == "districts":
        await db.execute(
            """
            UPDATE creators c SET public_point=d.centroid FROM districts d
            WHERE d.id=%s AND d.id=c.district_id AND c.accuracy='district'
        """,
            (entity,),
        )
    await record_cms(db, user.id, f"dictionary.{kind}.edited", entity, row)
    return DictionaryRecord(**row)


@router.delete("/editor/dictionaries/{kind}/{entity}", status_code=204)
async def retire_dictionary(kind: MutableDictionary, entity: UUID, user: Editor, db: DB):
    row = await one(
        db,
        f"""
      UPDATE {DICTIONARY_TABLES[kind]} SET retired_at=now() WHERE id=%s AND retired_at IS NULL
      RETURNING {dictionary_columns(kind)}
    """,
        (entity,),
    )
    if not row:
        raise HTTPException(404, "active dictionary entry not found")
    await record_cms(db, user.id, f"dictionary.{kind}.retired", entity, row)


class OkedEntry(Model):
    code: str = Field(pattern=r"^\d{2}(?:\.?\d){0,8}$")
    labels: Localized


class OkedVersion(Model):
    version: str = Field(pattern=r"^[a-zA-Z0-9_.-]+$", min_length=1, max_length=64)
    entries: list[OkedEntry] = Field(min_length=43, max_length=43)

    @model_validator(mode="after")
    def distinct_codes(self):
        if len({e.code.replace(".", "") for e in self.entries}) != 43 or not all(
            e.labels.complete() for e in self.entries
        ):
            raise ValueError("43 distinct codes with three labels each are required")
        return self


@router.post("/editor/oked-versions", status_code=201)
async def add_oked(data: OkedVersion, user: Editor, db: DB) -> OkedVersion:
    await db.execute("LOCK TABLE oked IN SHARE ROW EXCLUSIVE MODE")
    if await one(db, "SELECT id FROM oked WHERE version=%s LIMIT 1", (data.version,)):
        raise HTTPException(409, "dictionary version already exists")
    await db.execute(
        """
        INSERT INTO oked(code,labels,version)
        SELECT code,labels,%s FROM jsonb_to_recordset(%s) AS x(code text,labels jsonb)
    """,
        (data.version, Jsonb([item.model_dump() for item in data.entries])),
    )
    await record_cms(db, user.id, "dictionary.oked.version_created", user.id, data.model_dump())
    return data


@router.delete("/editor/oked-versions/{version}", status_code=204)
async def retire_oked(version: str, user: Editor, db: DB):
    if not await one(
        db,
        "UPDATE oked SET retired_at=now() WHERE version=%s AND retired_at IS NULL RETURNING id",
        (version,),
    ):
        raise HTTPException(404, "active dictionary version not found")
    await record_cms(db, user.id, "dictionary.oked.version_retired", user.id, {"version": version})


class ChangeRecord(Model):
    id: UUID
    actor_id: UUID | None
    action: str
    created_at: datetime
    snapshot: dict


@router.get("/editor/history/{entity}")
async def history(entity: UUID, f: Annotated[PageQuery, Query()], user: Editor, db: DB) -> Page[ChangeRecord]:
    return Page[ChangeRecord](
        **await paginated(
            db,
            "id,actor_id,action,created_at,context->'snapshot' AS snapshot",
            "audit_events",
            "entity_id=%s AND context ? 'snapshot'",
            (entity,),
            f,
            "created_at DESC,id",
        )
    )


class ArchiveBatch(Model):
    ids: list[UUID] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_ids(self):
        if len(set(self.ids)) != len(self.ids):
            raise ValueError("IDs must be unique")
        return self


@router.post("/editor/content/{kind}/archive-batch", status_code=204)
async def archive_batch(kind: ContentKind, data: ArchiveBatch, user: Editor, db: DB):
    for entity in sorted(data.ids):
        await archive_content(db, kind, entity, user.id)


class HomeLayout(Model):
    sections: list[Literal["creators", "projects", "articles", "events", "opportunities", "spaces"]] = Field(
        default_factory=lambda: ["creators", "projects", "articles", "events", "opportunities", "spaces"],
        max_length=6,
    )

    @model_validator(mode="after")
    def unique_sections(self):
        if len(set(self.sections)) != len(self.sections):
            raise ValueError("sections must be unique")
        return self


@router.get("/home/layout")
async def home_layout(db: DB) -> HomeLayout:
    row = await one(db, "SELECT value FROM cms_settings WHERE key='home_layout'")
    return HomeLayout(**row["value"]) if row else HomeLayout()


@router.put("/editor/home/layout")
async def set_home_layout(data: HomeLayout, user: Admin, db: DB) -> HomeLayout:
    await db.execute(
        """
        INSERT INTO cms_settings(key,value) VALUES ('home_layout',%s)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
    """,
        (Jsonb(data.model_dump()),),
    )
    await record_cms(db, user.id, "home.layout_changed", user.id, data.model_dump())
    return data


class SavedFilter(Model):
    name: str = Field(min_length=1, max_length=80)
    kind: ContentKind
    q: str | None = Field(None, min_length=1, max_length=200)
    status: Publication | None = None


class CmsPreset(SavedFilter):
    id: UUID


@router.put("/editor/filter-presets")
async def save_filter(data: SavedFilter, user: Editor, db: DB) -> CmsPreset:
    row = await one(
        db,
        """
      INSERT INTO cms_presets(user_id,kind,name,filters) VALUES (%s,%s,%s,%s)
      ON CONFLICT(user_id,kind,name) DO UPDATE SET filters=excluded.filters RETURNING id
    """,
        (user.id, data.kind, data.name, Jsonb(data.model_dump(mode="json", include={"q", "status"}))),
    )
    await record_cms(db, user.id, "filter.saved", row["id"], data.model_dump(mode="json"))
    return CmsPreset(**data.model_dump(), **row)


@router.get("/editor/filter-presets")
async def filters(f: Annotated[PageQuery, Query()], user: Editor, db: DB) -> Page[CmsPreset]:
    return Page[CmsPreset](
        **await paginated(
            db,
            "id,kind,name,filters->>'q' AS q,filters->>'status' AS status",
            "cms_presets",
            "user_id=%s",
            (user.id,),
            f,
            "name,id",
        )
    )


@router.delete("/editor/filter-presets/{entity}", status_code=204)
async def delete_filter(entity: UUID, user: Editor, db: DB):
    if not await one(
        db, "DELETE FROM cms_presets WHERE id=%s AND user_id=%s RETURNING id", (entity, user.id)
    ):
        raise HTTPException(404, "preset not found")
    await record_cms(db, user.id, "filter.removed", entity, {})


def spreadsheet_cell(value: str) -> str:
    return (
        "'" + value
        if value.lstrip().startswith(("=", "+", "-", "@")) or value.startswith(("\t", "\r", "\n"))
        else value
    )


@router.get("/editor/content/{kind}/export/{fmt}", response_class=Response)
async def export_content(
    kind: ContentKind,
    fmt: Literal["csv", "xlsx"],
    user: Editor,
    db: DB,
    q: str | None = None,
    status: Publication | None = None,
) -> Response:
    where, params = ["true"], []
    if q:
        if len(q) > 200:
            raise HTTPException(422, "search query is too long")
        where.append("to_tsvector('simple',fields::text) @@ websearch_to_tsquery('simple',%s)")
        params.append(q)
    if status is not None:
        where.append("status=%s")
        params.append(status)
    rows = await many(
        db,
        f"""
      SELECT id,status,fields->'title' AS title FROM {kind} WHERE {" AND ".join(where)}
      ORDER BY id LIMIT 10001
    """,
        tuple(params),
    )
    if len(rows) > 10000:
        raise HTTPException(422, "export exceeds 10000 rows, narrow the filters")
    cells = [("id", "status", "title_kk", "title_ru", "title_en")]
    cells.extend(
        (
            str(row["id"]),
            row["status"],
            *(spreadsheet_cell(row["title"][lang]) for lang in ("kk", "ru", "en")),
        )
        for row in rows
    )
    content = await run_in_threadpool(table_bytes, cells, fmt)
    await record_cms(
        db, user.id, "content.exported", user.id, {"kind": kind, "format": fmt, "rows": len(rows)}
    )
    return Response(
        content,
        media_type=REPORT_TYPES[fmt],
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": f'attachment; filename="{kind}.{fmt}"',
        },
    )
