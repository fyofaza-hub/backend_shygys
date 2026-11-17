from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from psycopg.types.json import Jsonb

from app.auth import Editor
from app.db import DB, Conn, audit, many, one
from app.registry import check_media, paginated, pattern
from app.schemas import (
    Article,
    ArticleCreate,
    ArticleFilters,
    ArticleInput,
    Event,
    EventInput,
    LegalStatus,
    Model,
    Opportunity,
    OpportunityInput,
    Page,
    Publish,
    ReasonInput,
    TextSearch,
)

router = APIRouter(tags=["content"])
ARTICLE_FIELDS = "id,slug,type,source_locale,fields,media_ids,status,published_at,cover_id,meta_tags,gallery"


@router.get("/articles")
async def articles(f: Annotated[ArticleFilters, Query()], db: DB) -> Page[Article]:
    where = ["status='published'", "published_at<=now()"]
    params = []
    if f.type:
        where.append("approved->>'type'=%s")
        params.append(f.type)
    if f.q:
        where.append("(approved->'fields'->'title')::text ILIKE %s")
        params.append(pattern(f.q))
    page = await paginated(
        db,
        "id,slug,approved,status,published_at",
        "articles",
        " AND ".join(where),
        tuple(params),
        f,
        "published_at DESC,id",
    )
    return Page[Article](**{**page, "items": [public_article(r) for r in page["items"]]})


def public_article(row: dict) -> Article:
    return Article(**row["approved"], **{k: row[k] for k in ("id", "slug", "status", "published_at")})


@router.get("/articles/{slug}")
async def article(slug: str, db: DB) -> Article:
    row = await one(
        db,
        """
        SELECT id,slug,approved,status,published_at FROM articles
        WHERE slug=%s AND status='published' AND published_at<=now()
    """,
        (slug,),
    )
    if not row:
        raise HTTPException(404, "article not found")
    return public_article(row)


def source_fields(fields: dict, locale: str) -> dict[str, str]:
    return {key: value[locale] for key, value in fields.items()}


def source_hash(fields: dict, locale: str) -> str:
    import hashlib
    import json

    encoded = json.dumps(source_fields(fields, locale), sort_keys=True, ensure_ascii=False).encode()
    return hashlib.sha256(encoded).hexdigest()


async def record_translation(
    db: Conn,
    entity: UUID,
    locale: str,
    digest: str,
    fields: dict,
    state: str,
    author: UUID | None,
    kind: str = "article",
):
    await db.execute(
        """
        INSERT INTO translations(entity_type,entity_id,locale,source_hash,fields_json,status,author_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
    """,
        (kind, entity, locale, digest, Jsonb(fields), state, author),
    )


async def current_article(db: Conn, article_id: UUID, lock: bool = False) -> dict:
    row = await one(
        db,
        f"SELECT {ARTICLE_FIELDS},author_id FROM articles WHERE id=%s" + (" FOR UPDATE" if lock else ""),
        (article_id,),
    )
    if not row:
        raise HTTPException(404, "article not found")
    return row


async def record_cms(db: Conn, user: UUID, action: str, entity: UUID, snapshot: dict, **details):
    from fastapi.encoders import jsonable_encoder

    await audit(db, user, action, entity, snapshot=jsonable_encoder(snapshot), **details)


def article_assets(data: ArticleInput) -> list[UUID]:
    return data.media_ids + ([data.cover_id] if data.cover_id is not None else [])


@router.post("/editor/articles", status_code=201)
async def create_article(data: ArticleCreate, user: Editor, db: DB) -> Article:
    await check_media(db, user.id, article_assets(data))
    fields = data.fields.model_dump()
    if not fields["title"][data.source_locale]:
        raise HTTPException(422, "source title is required")
    row = await one(
        db,
        f"""
        INSERT INTO articles(author_id,slug,type,source_locale,fields,media_ids,cover_id,meta_tags,gallery)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING {ARTICLE_FIELDS}
    """,
        (
            user.id,
            data.slug,
            data.type,
            data.source_locale,
            Jsonb(fields),
            data.media_ids,
            data.cover_id,
            Jsonb(data.meta_tags.model_dump()),
            Jsonb(data.model_dump(mode="json")["gallery"]),
        ),
    )
    if not row:
        raise HTTPException(409, "slug already exists")
    await record_translation(
        db,
        row["id"],
        data.source_locale,
        source_hash(fields, data.source_locale),
        source_fields(fields, data.source_locale),
        "original",
        user.id,
    )
    await record_cms(db, user.id, "article.created", row["id"], row)
    return Article(**row)


@router.get("/editor/articles/{article_id}")
async def edit_article_detail(article_id: UUID, user: Editor, db: DB) -> Article:
    row = await current_article(db, article_id)
    row.pop("author_id")
    return Article(**row)


@router.put("/editor/articles/{article_id}")
async def edit_article(article_id: UUID, data: ArticleInput, user: Editor, db: DB) -> Article:
    old = await current_article(db, article_id, lock=True)
    if old["status"] == "archived":
        raise HTTPException(409, "article is archived")
    retained = set(old["media_ids"]) | {old["cover_id"]}
    await check_media(db, user.id, [asset for asset in article_assets(data) if asset not in retained])
    fields = data.fields.model_dump()
    old_hash = source_hash(old["fields"], old["source_locale"])
    digest = source_hash(fields, data.source_locale)
    if not fields["title"][data.source_locale]:
        raise HTTPException(422, "source title is required")
    row = await one(
        db,
        f"""
        UPDATE articles SET type=%s,source_locale=%s,fields=%s,media_ids=%s,cover_id=%s,meta_tags=%s,gallery=%s
        WHERE id=%s RETURNING {ARTICLE_FIELDS}
    """,
        (
            data.type,
            data.source_locale,
            Jsonb(fields),
            data.media_ids,
            data.cover_id,
            Jsonb(data.meta_tags.model_dump()),
            Jsonb(data.model_dump(mode="json")["gallery"]),
            article_id,
        ),
    )
    for locale in ("kk", "ru", "en"):
        new = source_fields(fields, locale)
        prior = source_fields(old["fields"], locale)
        if new != prior or (locale == data.source_locale and data.source_locale != old["source_locale"]):
            await record_translation(
                db,
                article_id,
                locale,
                digest,
                new,
                "original" if locale == data.source_locale else "human_reviewed",
                user.id,
            )
    if old_hash != digest or data.source_locale != old["source_locale"]:
        await db.execute(
            """
            UPDATE translation_jobs SET status='stale',updated_at=now() WHERE entity_type='article'
              AND entity_id=%s AND (source_hash<>%s OR source_locale<>%s) AND status IN ('queued','running','complete','protected')
        """,
            (article_id, digest, data.source_locale),
        )
    await record_cms(db, user.id, "article.edited", article_id, row)
    return Article(**row)


@router.post("/editor/articles/{article_id}/publish")
async def publish_article(article_id: UUID, data: Publish, user: Editor, db: DB) -> Article:
    current = await current_article(db, article_id, lock=True)
    model = Article(**{k: current[k] for k in Article.model_fields})
    if model.status == "archived":
        raise HTTPException(409, "article is archived")
    digest = source_hash(current["fields"], model.source_locale)
    rows = await many(
        db,
        """
        SELECT DISTINCT ON(locale) locale, status, source_hash FROM translations
        WHERE entity_type='article' AND entity_id=%s ORDER BY locale,created_at DESC,id DESC
    """,
        (article_id,),
    )
    reviewed = {
        r["locale"]
        for r in rows
        if r["source_hash"] == digest and r["status"] in ("human_reviewed", "published")
    }
    required = {key for key, val in current["fields"].items() if val[model.source_locale]}
    complete = all(all(current["fields"][key][lang] for lang in ("kk", "ru", "en")) for key in required)
    approved = complete and {"kk", "ru", "en"} - {model.source_locale} <= reviewed
    if not approved and not (data.override_reason and ({"admin", "superadmin"} & set(user.roles))):
        raise HTTPException(409, "missing or unreviewed translations")
    snapshot = model.model_dump(mode="json", include=set(ArticleInput.model_fields))
    row = await one(
        db,
        f"""
        UPDATE articles SET status='published',approved=%s,published_at=coalesce(%s,now()) WHERE id=%s RETURNING {ARTICLE_FIELDS}
    """,
        (Jsonb(snapshot), data.publish_at, article_id),
    )
    for locale in ("kk", "ru", "en"):
        if current["fields"]["title"][locale] and (locale == model.source_locale or locale in reviewed):
            await record_translation(
                db, article_id, locale, digest, source_fields(current["fields"], locale), "published", user.id
            )
    await record_cms(db, user.id, "article.published", article_id, row, override_reason=data.override_reason)
    return Article(**row)


@router.post("/editor/articles/{article_id}/review/{locale}", status_code=204)
async def review_article_locale(article_id: UUID, locale: Literal["kk", "ru", "en"], user: Editor, db: DB):
    row = await current_article(db, article_id, lock=True)
    if not row["fields"]["title"][locale]:
        raise HTTPException(422, "translation title is required")
    await record_translation(
        db,
        article_id,
        locale,
        source_hash(row["fields"], row["source_locale"]),
        source_fields(row["fields"], locale),
        "human_reviewed",
        user.id,
    )
    await audit(db, user.id, "article.translation_reviewed", article_id, locale=locale)


EVENT_FIELDS = "id,fields,starts_at,ends_at,place_id,registration_url,status,organizer,publish_at,category_id"
OPPORTUNITY_FIELDS = "id,fields,eligibility,deadline,url,status,kind,source_url,district_id,category_id,legal_status,publish_at"


@router.post("/editor/events", status_code=201)
async def create_event(data: EventInput, user: Editor, db: DB) -> Event:
    row = await one(
        db,
        f"""
        INSERT INTO events(author_id,fields,starts_at,ends_at,place_id,registration_url,organizer,category_id)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {EVENT_FIELDS}
    """,
        (
            user.id,
            Jsonb(data.fields.model_dump()),
            data.starts_at,
            data.ends_at,
            data.place_id,
            str(data.registration_url) if data.registration_url else None,
            Jsonb(data.organizer.model_dump()),
            data.category_id,
        ),
    )
    await record_cms(db, user.id, "event.created", row["id"], row)
    return Event(**row)


@router.post("/editor/opportunities", status_code=201)
async def create_opportunity(data: OpportunityInput, user: Editor, db: DB) -> Opportunity:
    row = await one(
        db,
        f"""
        INSERT INTO opportunities(author_id,fields,eligibility,deadline,url,kind,source_url,district_id,category_id,legal_status)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING {OPPORTUNITY_FIELDS}
    """,
        (
            user.id,
            Jsonb(data.fields.model_dump()),
            Jsonb(data.eligibility.model_dump()),
            data.deadline,
            str(data.url) if data.url else None,
            data.kind,
            str(data.source_url) if data.source_url else None,
            data.district_id,
            data.category_id,
            data.legal_status,
        ),
    )
    await record_cms(db, user.id, "opportunity.created", row["id"], row)
    return Opportunity(**row)


async def publish_content(
    kind: Literal["events", "opportunities"], entity: UUID, user: Editor, db: DB, data: Publish | None = None
):
    row = await one(db, f"SELECT fields,status FROM {kind} WHERE id=%s FOR UPDATE", (entity,))
    if not row:
        raise HTTPException(404, "content not found")
    if row["status"] == "archived":
        raise HTTPException(409, "content is archived")
    if any(any(field.values()) and not all(field.values()) for field in row["fields"].values()) or not all(
        row["fields"]["title"].values()
    ):
        raise HTTPException(409, "all used fields must be translated")
    saved = await one(
        db,
        f"UPDATE {kind} SET status='published',publish_at=%s WHERE id=%s RETURNING *",
        (data.publish_at if data else None, entity),
    )
    await record_cms(db, user.id, f"{kind}.published", entity, saved)


@router.post("/editor/events/{entity}/publish", status_code=204)
async def publish_event(entity: UUID, user: Editor, db: DB, data: Publish | None = None):
    await publish_content("events", entity, user, db, data)


@router.post("/editor/opportunities/{entity}/publish", status_code=204)
async def publish_opportunity(entity: UUID, user: Editor, db: DB, data: Publish | None = None):
    await publish_content("opportunities", entity, user, db, data)


class EventFilters(TextSearch):
    calendar: Literal["upcoming", "past", "all"] = "upcoming"


class OpportunityFilters(TextSearch):
    kind: Literal["grant", "contest", "accelerator", "program"] | None = None
    district_id: UUID | None = None
    category_id: UUID | None = None
    legal_status: LegalStatus | None = None
    active: bool | None = None


def content_search(f: TextSearch) -> tuple[list[str], list]:
    where, params = ["status='published' AND (publish_at IS NULL OR publish_at<=now())"], []
    if f.q:
        where.append("((fields->'title')::text ILIKE %s OR (fields->'summary')::text ILIKE %s)")
        params.extend((pattern(f.q), pattern(f.q)))
    return where, params


@router.get("/events")
async def events(f: Annotated[EventFilters, Query()], db: DB) -> Page[Event]:
    where, params = content_search(f)
    if f.calendar != "all":
        where.append("ends_at>=now()" if f.calendar == "upcoming" else "ends_at<now()")
    order = "starts_at DESC,id" if f.calendar == "past" else "starts_at,id"
    return Page[Event](
        **await paginated(db, EVENT_FIELDS, "events", " AND ".join(where), tuple(params), f, order)
    )


@router.get("/opportunities")
async def opportunities(f: Annotated[OpportunityFilters, Query()], db: DB) -> Page[Opportunity]:
    where, params = content_search(f)
    for key in ("kind", "district_id", "category_id", "legal_status"):
        if (value := getattr(f, key)) is not None:
            where.append(f"{key}=%s" if key == "kind" else f"({key}=%s OR {key} IS NULL)")
            params.append(value)
    if f.active is not None:
        where.append("(deadline IS NULL OR deadline>=%s)=%s")
        params.extend((datetime.now(UTC), f.active))
    return Page[Opportunity](
        **await paginated(
            db,
            OPPORTUNITY_FIELDS,
            "opportunities",
            " AND ".join(where),
            tuple(params),
            f,
            "deadline NULLS LAST,id",
        )
    )


@router.get("/events/{entity}")
async def event(entity: UUID, db: DB) -> Event:
    row = await one(
        db,
        f"SELECT {EVENT_FIELDS} FROM events WHERE id=%s AND status='published' AND (publish_at IS NULL OR publish_at<=now())",
        (entity,),
    )
    if not row:
        raise HTTPException(404, "event not found")
    return Event(**row)


@router.get("/opportunities/{entity}")
async def opportunity(entity: UUID, db: DB) -> Opportunity:
    row = await one(
        db,
        f"SELECT {OPPORTUNITY_FIELDS} FROM opportunities WHERE id=%s AND status='published' AND (publish_at IS NULL OR publish_at<=now())",
        (entity,),
    )
    if not row:
        raise HTTPException(404, "opportunity not found")
    return Opportunity(**row)


@router.get("/editor/articles")
async def article_drafts(f: Annotated[ArticleFilters, Query()], user: Editor, db: DB) -> Page[Article]:
    where, params = ["true"], []
    if f.type:
        where.append("type=%s")
        params.append(f.type)
    if f.q:
        where.append("to_tsvector('simple',fields::text) @@ websearch_to_tsquery('simple',%s)")
        params.append(f.q)
    return Page[Article](
        **await paginated(db, ARTICLE_FIELDS, "articles", " AND ".join(where), tuple(params), f, "id")
    )


@router.post("/editor/content/{kind}/{entity}/hide", status_code=204)
async def hide_content(
    kind: Literal["articles", "events", "opportunities"],
    entity: UUID,
    data: ReasonInput,
    user: Editor,
    db: DB,
):
    row = await one(
        db, f"UPDATE {kind} SET status='hidden' WHERE id=%s AND status='published' RETURNING *", (entity,)
    )
    if not row:
        raise HTTPException(404, "published content not found")
    await record_cms(db, user.id, f"{kind}.hidden", entity, row, reason=data.reason)


@router.put("/editor/events/{entity}")
async def edit_event(entity: UUID, data: EventInput, user: Editor, db: DB) -> Event:
    row = await one(
        db,
        f"""
        UPDATE events SET fields=%s,starts_at=%s,ends_at=%s,place_id=%s,registration_url=%s,organizer=%s,category_id=%s,status='unpublished'
        WHERE id=%s AND status<>'archived' RETURNING {EVENT_FIELDS}
    """,
        (
            Jsonb(data.fields.model_dump()),
            data.starts_at,
            data.ends_at,
            data.place_id,
            str(data.registration_url) if data.registration_url else None,
            Jsonb(data.organizer.model_dump()),
            data.category_id,
            entity,
        ),
    )
    if not row:
        raise HTTPException(404, "event not found")
    await record_cms(db, user.id, "event.edited", entity, row)
    return Event(**row)


@router.put("/editor/opportunities/{entity}")
async def edit_opportunity(entity: UUID, data: OpportunityInput, user: Editor, db: DB) -> Opportunity:
    row = await one(
        db,
        f"""
        UPDATE opportunities SET fields=%s,eligibility=%s,deadline=%s,url=%s,kind=%s,source_url=%s,district_id=%s,category_id=%s,legal_status=%s,
        status='unpublished',external_ai_search=false
        WHERE id=%s AND status<>'archived' RETURNING {OPPORTUNITY_FIELDS}
    """,
        (
            Jsonb(data.fields.model_dump()),
            Jsonb(data.eligibility.model_dump()),
            data.deadline,
            str(data.url) if data.url else None,
            data.kind,
            str(data.source_url) if data.source_url else None,
            data.district_id,
            data.category_id,
            data.legal_status,
            entity,
        ),
    )
    if not row:
        raise HTTPException(404, "opportunity not found")
    await record_cms(db, user.id, "opportunity.edited", entity, row)
    return Opportunity(**row)


class SearchConsent(Model):
    approved: bool


@router.put("/editor/opportunities/{entity}/ai-search-consent", status_code=204)
async def opportunity_ai_search(entity: UUID, data: SearchConsent, user: Editor, db: DB):
    if not await one(
        db, "UPDATE opportunities SET external_ai_search=%s WHERE id=%s RETURNING id", (data.approved, entity)
    ):
        raise HTTPException(404, "opportunity not found")
    await audit(db, user.id, "opportunity.ai_search_changed", entity, approved=data.approved)
