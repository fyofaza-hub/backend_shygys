from io import BytesIO
from typing import Annotated, Literal
from uuid import UUID

import qrcode
from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from psycopg.types.json import Jsonb
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from app.auth import Actor, Creator, Reviewer, has_consent, require, throttle
from app.config import settings
from app.db import DB, Conn, audit, many, one
from app.schemas import (
    Assignment,
    Case,
    CatalogFilters,
    Contacts,
    CreatorEditorView,
    CreatorPage,
    Decision,
    ItemFilters,
    MapFilters,
    MapResults,
    ModerationFilters,
    Oked,
    Page,
    PageQuery,
    PortfolioPins,
    ProfileCreate,
    ProfileInput,
    ProfilePatch,
    ProjectCreate,
    ProjectInput,
    ProjectInvitation,
    ProjectPatch,
    PublicCreator,
    PublicProject,
    PublicWork,
    ReasonInput,
    Reference,
    Role,
    Slug,
    Version,
    WorkInput,
    WorkPatch,
)

router = APIRouter(tags=["registry"])
CenterReviewer = Annotated[
    Actor, Depends(require(Role.moderator, Role.admin, Role.superadmin, Role.center_curator))
]
GLOBAL_REVIEWERS = {Role.moderator, Role.admin, Role.superadmin}
CENTER_SCOPE = """EXISTS(SELECT 1 FROM residencies r WHERE r.creator_id={creator}
    AND r.center_id=ANY(%s::uuid[]) AND r.status IN ('pending','active')
    AND r.ends_on >= (now() AT TIME ZONE 'Asia/Almaty')::date)"""


def center_scope(user: Actor) -> tuple[list[UUID], ...]:
    if GLOBAL_REVIEWERS.intersection(user.roles):
        return ()
    return (list(user.scopes.get(Role.center_curator, set())),)


Kind = Literal["creator", "portfolio_item", "project"]
TARGETS = {
    "creator": ("creators", None),
    "portfolio_item": ("portfolio_items", "portfolio_item_id"),
    "project": ("projects", "project_id"),
}

RESIDENCY = """
COALESCE((SELECT (CASE WHEN status='active' AND ends_on < (now() AT TIME ZONE 'Asia/Almaty')::date
                       THEN 'expired'::residency_status ELSE status END)
          FROM residencies r WHERE r.creator_id=c.id
          AND (r.status <> 'active' OR starts_on <= (now() AT TIME ZONE 'Asia/Almaty')::date)
          ORDER BY (r.status='active' AND r.ends_on >= (now() AT TIME ZONE 'Asia/Almaty')::date) DESC,
                   r.starts_on DESC, r.id LIMIT 1), 'none'::residency_status)
"""
PUBLIC = """c.publication_status='published' AND c.moderation_status='approved' AND c.approved IS NOT NULL
AND EXISTS (SELECT 1 FROM users u WHERE u.id=c.user_id AND u.disabled_at IS NULL)
AND EXISTS (SELECT 1 FROM consents s WHERE s.user_id=c.user_id AND s.type='data_processing'
            AND s.withdrawn_at IS NULL)"""
CONTACT_CONSENT = """EXISTS (SELECT 1 FROM consents s WHERE s.user_id=c.user_id
                            AND s.type='public_contacts' AND s.withdrawn_at IS NULL)"""
PUBLIC_POINT = """CASE WHEN c.accuracy='exact' AND NOT EXISTS (
    SELECT 1 FROM places p WHERE p.id=c.place_id AND p.is_public AND p.verified_at IS NOT NULL
    AND ST_Equals(p.geo_point,c.public_point)
) THEN NULL ELSE c.public_point END"""


def catalog_where(f: CatalogFilters, public: bool = True) -> tuple[str, tuple]:
    predicates = [PUBLIC if public else "true"]
    params = []
    for field in ("district_id", "locality_id", "legal_status", "available_for_collaboration"):
        value = getattr(f, field)
        if value is not None:
            predicates.append(f"c.{field}=%s")
            params.append(value)
    if f.residency:
        predicates.append(f"{RESIDENCY}=%s")
        params.append(f.residency)
    if public and f.locality_id is not None:
        predicates.append("c.accuracy IS DISTINCT FROM 'district'")
    if f.open_to_collab is not None:
        predicates.append("c.available_for_collaboration=%s")
        params.append(f.open_to_collab)
    if f.is_resident is not None:
        predicates.append(f"({RESIDENCY}='active')=%s")
        params.append(f.is_resident)
    if f.category or f.category_id:
        predicates.append(
            "EXISTS(SELECT 1 FROM creator_categories WHERE creator_id=c.id AND category_id=ANY(%s))"
        )
        params.append(f.category + f.category_id)
    if f.oked or f.oked_code:
        predicates.append("""EXISTS(SELECT 1 FROM creator_oked co JOIN oked o ON o.id=co.oked_id
                             WHERE co.creator_id=c.id AND o.code=ANY(%s))""")
        params.append(f.oked + f.oked_code)
    if f.q:
        predicates.append(f"""(
            to_tsvector('simple',c.search_text) @@ websearch_to_tsquery('simple',%s)
            OR c.id IN (
                SELECT pc.creator_id FROM projects p
                JOIN project_creators pc ON pc.project_id=p.id
                JOIN creators c ON c.id=p.owner_creator_id
                WHERE p.publication_status='published' AND p.moderation_status='approved'
                  AND pc.confirmed_at IS NOT NULL AND {PUBLIC}
                  AND EXISTS(SELECT 1 FROM consents a WHERE a.user_id=c.user_id
                             AND a.type='portfolio' AND a.withdrawn_at IS NULL)
                  AND to_tsvector('simple',coalesce(p.approved->'title'->>'kk','') || ' ' ||
                      coalesce(p.approved->'title'->>'ru','') || ' ' ||
                      coalesce(p.approved->'title'->>'en','')) @@ websearch_to_tsquery('simple',%s)
            ))""")
        params.extend((f.q, f.q))
    return " AND ".join(predicates), tuple(params)


def pattern(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def paginated(
    db: Conn,
    columns: str,
    source: str,
    where: str,
    params: tuple,
    f: PageQuery,
    order: str,
    projection_params: tuple = (),
) -> dict:
    offset = (f.page - 1) * f.limit
    result = await one(
        db,
        f"""
        WITH selected AS (
          SELECT {columns} FROM {source} WHERE {where} ORDER BY {order} OFFSET %s LIMIT %s
        )
        SELECT ARRAY(SELECT to_jsonb(selected) FROM selected ORDER BY {order}) AS items,
               (SELECT count(*) FROM {source} WHERE {where}) AS total
    """,
        projection_params + params + (offset, f.limit) + params,
    )
    return {**result, "page": f.page, "limit": f.limit}


CREATOR_COLUMNS = f"""
    c.id AS id,c.slug AS slug,c.approved,c.legal_verified,c.moderation_status,c.publication_status,{RESIDENCY} AS residency,
    CASE WHEN c.phone_public AND {CONTACT_CONSENT} THEN c.phone END AS phone,
    CASE WHEN c.email_public AND {CONTACT_CONSENT} THEN c.email END AS email,
    ST_X({PUBLIC_POINT}) AS longitude, ST_Y({PUBLIC_POINT}) AS latitude, c.accuracy
"""


def public_creator(row: dict) -> PublicCreator:
    location = None
    if row["longitude"] is not None:
        location = {k: row[k] for k in ("longitude", "latitude", "accuracy")}
    approved = dict(row["approved"])
    approved["accuracy"] = location["accuracy"] if location else None
    if approved["accuracy"] != "exact":
        approved["place_id"] = None
    if approved["accuracy"] == "district":
        approved["locality_id"] = None
    return PublicCreator(
        **approved,
        location=location,
        **{
            key: row[key]
            for key in (
                "id",
                "slug",
                "legal_verified",
                "moderation_status",
                "publication_status",
                "residency",
                "phone",
                "email",
            )
        },
    )


@router.get("/creators")
async def catalog(f: Annotated[CatalogFilters, Query()], db: DB) -> Page[PublicCreator]:
    where, params = catalog_where(f)
    page = await paginated(db, CREATOR_COLUMNS, "creators c", where, params, f, "slug,id")
    page["items"] = [public_creator(row) for row in page["items"]]
    return Page[PublicCreator](**page)


@router.get("/creators/{slug}")
async def creator_detail(slug: str, db: DB, response: Response) -> CreatorPage:
    row = await one(db, f"SELECT {CREATOR_COLUMNS} FROM creators c WHERE {PUBLIC} AND slug=%s", (slug,))
    if not row:
        raise HTTPException(404, "profile not found")
    response.headers["Cache-Control"] = "no-store"
    pinned = await many(
        db,
        f"""
      SELECT i.* FROM portfolio_pins pin JOIN portfolio_items i ON i.id=pin.work_id
      JOIN creators c ON c.id=i.creator_id WHERE pin.creator_id=%s AND {PUBLIC} AND {ITEM_PUBLIC}
      ORDER BY pin.slot
    """,
        (row["id"],),
    )
    projects_page = await items_feed(db, "project", ItemFilters(creator_id=row["id"]))
    return CreatorPage(
        **public_creator(row).model_dump(),
        pinned_works=[public_item(item, "portfolio_item") for item in pinned],
        projects=Page[PublicProject](**projects_page),
    )


def encode_qr(url: str) -> bytes:
    output = BytesIO()
    qrcode.make(url).save(output)
    return output.getvalue()


@router.get("/creators/{slug}/qr", response_class=Response, responses={200: {"content": {"image/png": {}}}})
async def creator_qr(slug: Slug, db: DB) -> Response:
    if not await one(db, f"SELECT c.id FROM creators c WHERE c.slug=%s AND {PUBLIC}", (slug,)):
        raise HTTPException(404, "profile not found")
    url = f"{settings.frontend_url.rstrip('/')}/creators/{slug}"
    return Response(
        await run_in_threadpool(encode_qr, url), media_type="image/png", headers={"Cache-Control": "no-store"}
    )


def map_bounds(bbox: str) -> tuple[float, float, float, float]:
    west, south, east, north = map(float, bbox.split(","))
    if not (-180 <= west < east <= 180 and -85.05 <= south < north <= 85.05):
        raise HTTPException(422, "bbox must be west,south,east,north in WGS84")
    return west, south, east, north


def marker_cell(zoom: int) -> float:
    return 40075016.68557849 / (4 * 2**zoom)


def marker_source(f: MapFilters) -> tuple[str, tuple]:
    bounds = map_bounds(f.bbox)
    where, params = catalog_where(f)
    cell = marker_cell(f.zoom)
    source = f"""(
        SELECT ST_SnapToGrid(ST_Transform(c.public_point,3857),%s) AS cell,
               c.public_point,c.id,c.slug,c.accuracy
        FROM creators c WHERE {where} AND {PUBLIC_POINT} IS NOT NULL
        AND c.public_point && ST_MakeEnvelope(%s,%s,%s,%s,4326)
    ) points"""
    return source, (cell,) + params + bounds


@router.get("/map/creators")
async def map_creators(f: Annotated[MapFilters, Query()], db: DB) -> MapResults:
    source, params = marker_source(f)
    return await grouped_markers(db, f, source, params)


async def grouped_markers(db: Conn, f: MapFilters, source: str, params: tuple) -> MapResults:
    grouped = f"""(
        SELECT cell, count(*) AS count, ST_Centroid(ST_Collect(public_point)) AS center,
          CASE WHEN count(*)=1 THEN min(id::text)::uuid END AS creator_id,
          CASE WHEN count(*)=1 THEN min(slug) END AS slug,
          CASE WHEN count(*)=1 THEN min(accuracy) END AS accuracy
        FROM {source} GROUP BY cell
    ) clusters"""
    page = await paginated(
        db,
        """
      COALESCE(creator_id::text,encode(ST_AsEWKB(cell),'hex')) AS id, count,creator_id,slug,accuracy,
      ST_X(center) AS longitude,ST_Y(center) AS latitude
    """,
        grouped,
        "true",
        params,
        f,
        "count DESC,id",
    )
    return MapResults(**page, zoom=f.zoom, empty_reason="no_matching_markers" if page["total"] == 0 else None)


@router.get("/map/creators/clusters/{cluster_id}")
async def cluster_members(
    cluster_id: Annotated[str, Path(min_length=20, max_length=100, pattern="^[a-f0-9]+$")],
    f: Annotated[MapFilters, Query()],
    db: DB,
) -> Page[PublicCreator]:
    source, params = marker_source(f)
    page = await paginated(
        db,
        CREATOR_COLUMNS,
        f"{source} JOIN creators c ON c.id=points.id",
        "encode(ST_AsEWKB(points.cell),'hex')=%s",
        params + (cluster_id,),
        f,
        "slug,id",
    )
    return Page[PublicCreator](**{**page, "items": [public_creator(row) for row in page["items"]]})


@router.get("/me/creator/contacts")
async def get_contacts(user: Creator, db: DB) -> Contacts:
    profile = await owner_profile(db, user.id)
    return Contacts(**{key: profile[key] for key in Contacts.model_fields})


@router.put("/me/creator/contacts")
async def update_contacts(data: Contacts, user: Creator, db: DB) -> Contacts:
    profile = await owner_profile(db, user.id, lock=True)
    return await persist_contacts(db, user.id, profile["id"], data)


async def persist_contacts(db: Conn, owner: UUID, creator: UUID, data: Contacts) -> Contacts:
    if (data.email_public or data.phone_public) and not await has_consent(db, owner, "public_contacts"):
        raise HTTPException(409, "public contact consent is required")
    normalized = data.model_copy(update={"email": data.email.lower() if data.email else None})
    await db.execute(
        "UPDATE creators SET phone=%s,email=%s,phone_public=%s,email_public=%s WHERE id=%s",
        (normalized.phone, normalized.email, normalized.phone_public, normalized.email_public, creator),
    )
    return normalized


async def owner_profile(db: Conn, user_id: UUID, lock: bool = False) -> dict:
    row = await one(
        db, "SELECT * FROM creators WHERE user_id=%s" + (" FOR UPDATE" if lock else ""), (user_id,)
    )
    if not row:
        raise HTTPException(404, "profile not found")
    return row


async def check_media(db: Conn, owner: UUID, ids: list[UUID]):
    if ids:
        count = await one(
            db,
            "SELECT count(*) AS n FROM media_assets WHERE owner_id=%s AND id=ANY(%s) AND status='ready'",
            (owner, ids),
        )
        if count["n"] != len(set(ids)):
            raise HTTPException(422, "media must belong to author and be ready")


async def validate_profile(db: Conn, data: ProfileInput, owner: UUID, complete: bool):
    if complete and (
        (data.accuracy == "exact" and not data.place_id)
        or (data.accuracy == "locality" and not data.locality_id)
    ):
        raise HTTPException(422, "selected geographic precision requires its reference")
    if data.locality_id and not await one(
        db, "SELECT id FROM localities WHERE id=%s AND district_id=%s", (data.locality_id, data.district_id)
    ):
        raise HTTPException(422, "locality does not belong to district")
    if data.district_id and not await one(db, "SELECT id FROM districts WHERE id=%s", (data.district_id,)):
        raise HTTPException(422, "unknown district")
    if data.place_id and not await one(
        db,
        """
        SELECT id FROM places WHERE id=%s AND locality_id=%s AND is_public AND verified_at IS NOT NULL
    """,
        (data.place_id, data.locality_id),
    ):
        raise HTTPException(422, "place must be verified and public within the locality")
    for table, ids in (("creative_categories", data.category_ids), ("oked", data.oked_ids)):
        if ids:
            count = await one(db, f"SELECT count(*) AS n FROM {table} WHERE id=ANY(%s)", (ids,))
            if count["n"] != len(set(ids)):
                raise HTTPException(422, "unknown category or OKED")
    await check_media(db, owner, [item for item in (data.avatar_id, data.cover_id) if item is not None])
    if complete and not (
        data.public_name
        and data.district_id
        and data.category_ids
        and data.oked_ids
        and data.description.complete()
    ):
        raise HTTPException(
            422, "name, district, categories, OKED and descriptions in all three locales are required"
        )


async def validate_work(db: Conn, data: WorkInput | ProjectInput, owner: UUID, complete: bool):
    await check_media(db, owner, data.media_ids + ([data.cover_id] if data.cover_id else []))
    if data.category_id and not await one(
        db, "SELECT id FROM creative_categories WHERE id=%s", (data.category_id,)
    ):
        raise HTTPException(422, "unknown category")
    if complete and not data.title.complete():
        raise HTTPException(422, "titles in all three locales are required")
    if isinstance(data, ProjectInput):
        if data.organization_id and not await one(
            db,
            """
            SELECT org_id FROM organization_members WHERE org_id=%s AND user_id=%s AND verified_at IS NOT NULL
        """,
            (data.organization_id, owner),
        ):
            raise HTTPException(403, "verified membership required")
        if data.creator_ids:
            count = await one(db, "SELECT count(*) AS n FROM creators WHERE id=ANY(%s)", (data.creator_ids,))
            if count["n"] != len(set(data.creator_ids)):
                raise HTTPException(422, "unknown project creator")


def version(row: dict) -> Version:
    schema = ProjectInput if row["project_id"] else WorkInput if row["portfolio_item_id"] else ProfileInput
    return Version(
        **{k: row[k] for k in Version.model_fields if k not in ("payload", "project_slug")},
        payload=schema.model_validate(row["payload"]),
        project_slug=row.get("project_slug"),
    )


def target_ids(kind: Kind, creator_id: UUID, target: UUID) -> tuple:
    return creator_id, target if kind == "portfolio_item" else None, target if kind == "project" else None


VERSION_WHERE = (
    "creator_id=%s AND portfolio_item_id IS NOT DISTINCT FROM %s AND project_id IS NOT DISTINCT FROM %s"
)


async def latest(db: Conn, kind: Kind, creator_id: UUID, target: UUID) -> dict:
    row = await one(
        db,
        f"SELECT * FROM creator_versions WHERE {VERSION_WHERE} ORDER BY created_at DESC,id DESC LIMIT 1",
        target_ids(kind, creator_id, target),
    )
    if not row:
        raise HTTPException(404, "version not found")
    return row


async def save_version(
    db: Conn,
    kind: Kind,
    profile: dict,
    entity: dict,
    data: ProfileInput | WorkInput,
    actor_id: UUID,
    submit: bool = False,
    submitted_by: UUID | None = None,
) -> Version:
    if entity["publication_status"] == "archived":
        raise HTTPException(409, "archived entity cannot be edited")
    if await one(
        db,
        f"SELECT id FROM creator_versions WHERE {VERSION_WHERE} AND status='pending_review'",
        target_ids(kind, profile["id"], entity["id"]),
    ):
        raise HTTPException(409, "a version is already pending")
    if kind == "creator":
        await validate_profile(db, data, actor_id, submit)
    else:
        await validate_work(db, data, actor_id, submit)
    if submit and not await has_consent(db, actor_id, "data_processing"):
        raise HTTPException(409, "data processing consent is required")
    if submit and kind in ("portfolio_item", "project") and not await has_consent(db, actor_id, "portfolio"):
        raise HTTPException(409, "portfolio publication consent is required")
    payload = data.model_dump(mode="json")
    previous = entity["approved"] or {}
    diff = {
        key: {"before": previous.get(key), "after": value}
        for key, value in payload.items()
        if previous.get(key) != value
    }
    state = "pending_review" if submit else "draft"
    row = await one(
        db,
        """
        INSERT INTO creator_versions(creator_id,portfolio_item_id,project_id,payload,diff,status,submitted_by)
        VALUES (%s,%s,%s,%s,%s,%s,%s) RETURNING *
    """,
        target_ids(kind, profile["id"], entity["id"])
        + (Jsonb(payload), Jsonb(diff), state, submitted_by or actor_id),
    )
    if submit:
        await db.execute(
            """INSERT INTO notifications(user_id,template_id,locale,context)
            SELECT id,'moderation.submitted',locale,%s FROM users WHERE id=%s""",
            (Jsonb({"version_id": str(row["id"]), "kind": kind}), actor_id),
        )
        await db.execute("INSERT INTO moderation_cases(version_id) VALUES (%s)", (row["id"],))
        await db.execute(
            "INSERT INTO analytics_events(event_type,object_id) VALUES (%s,%s)",
            (f"{kind}.submitted", entity["id"]),
        )
    if entity["approved"] is None:
        table = TARGETS[kind][0]
        await db.execute(f"UPDATE {table} SET moderation_status=%s WHERE id=%s", (state, entity["id"]))
    return version(row)


@router.post("/me/creator", status_code=201)
async def make_creator(data: ProfileCreate, request: Request, user: Creator, db: DB) -> Version:
    await throttle(request, "new-profile", 6, 3600, str(user.id))
    profile = await one(
        db,
        """
        INSERT INTO creators(user_id,slug) VALUES (%s,%s) ON CONFLICT DO NOTHING RETURNING *
    """,
        (user.id, data.slug),
    )
    if not profile:
        raise HTTPException(409, "slug or profile already exists")
    await db.execute(
        "INSERT INTO analytics_events(event_type,object_id) VALUES ('creator.started',%s)", (profile["id"],)
    )
    return await save_version(
        db, "creator", profile, profile, ProfileInput(**data.model_dump(exclude={"slug"})), user.id
    )


@router.get("/me/creator")
async def my_creator(user: Creator, db: DB) -> CreatorEditorView:
    row = await owner_profile(db, user.id)
    return editor_view(row, version(await latest(db, "creator", row["id"], row["id"])))


def editor_view(row: dict, current: Version, contacts: Contacts | None = None) -> CreatorEditorView:
    contacts = contacts or Contacts(**{key: row[key] for key in Contacts.model_fields})
    return CreatorEditorView(**current.model_dump(), slug=row["slug"], contacts=contacts)


@router.patch("/me/creator")
async def patch_creator(data: ProfilePatch, user: Creator, db: DB) -> CreatorEditorView:
    row = await owner_profile(db, user.id, lock=True)
    raw = await latest(db, "creator", row["id"], row["id"])
    changes = data.model_dump(mode="json", exclude_unset=True, exclude={"contacts"})
    payload = {**raw["payload"], **changes}
    for key in ("description", "skills", "experience", "services"):
        if isinstance(changes.get(key), dict):
            payload[key] = {**raw["payload"][key], **changes[key]}
    contacts = None
    if data.contacts is not None:
        current = {key: row[key] for key in Contacts.model_fields}
        try:
            patched_contacts = Contacts.model_validate(
                {**current, **data.contacts.model_dump(exclude_unset=True)}
            )
        except ValidationError:
            raise HTTPException(422, "invalid contact fields") from None
        contacts = await persist_contacts(db, user.id, row["id"], patched_contacts)
    result = version(raw)
    if changes:
        try:
            updated = ProfileInput.model_validate(payload)
        except ValidationError:
            raise HTTPException(422, "invalid profile fields") from None
        result = await save_version(db, "creator", row, row, updated, user.id)
    return editor_view(row, result, contacts)


@router.put("/me/creator")
async def edit_creator(data: ProfileInput, user: Creator, db: DB) -> Version:
    row = await owner_profile(db, user.id, lock=True)
    return await save_version(db, "creator", row, row, data, user.id, submit=row["approved"] is not None)


@router.post("/me/creator/submit", status_code=202)
async def submit_creator(request: Request, user: Creator, db: DB) -> Version:
    await throttle(request, "submit", 15, 3600, str(user.id))
    row = await owner_profile(db, user.id, lock=True)
    draft = version(await latest(db, "creator", row["id"], row["id"]))
    return await save_version(db, "creator", row, row, draft.payload, user.id, submit=True)


async def owned_target(db: Conn, kind: Kind, profile: dict, target: UUID, lock: bool = False) -> dict:
    table = TARGETS[kind][0]
    owner_column = "creator_id" if kind == "portfolio_item" else "owner_creator_id"
    row = await one(
        db,
        f"SELECT * FROM {table} WHERE id=%s AND {owner_column}=%s" + (" FOR UPDATE" if lock else ""),
        (target, profile["id"]),
    )
    if not row:
        raise HTTPException(404, "item not found")
    return row


@router.post("/me/portfolio", status_code=201)
async def create_work(data: WorkInput, user: Creator, db: DB) -> Version:
    profile = await owner_profile(db, user.id)
    entity = await one(
        db, "INSERT INTO portfolio_items(creator_id) VALUES (%s) RETURNING *", (profile["id"],)
    )
    return await save_version(db, "portfolio_item", profile, entity, data, user.id)


@router.post("/me/projects", status_code=201)
async def create_project(data: ProjectCreate, user: Creator, db: DB) -> Version:
    profile = await owner_profile(db, user.id)
    entity = await one(
        db,
        """
        INSERT INTO projects(owner_creator_id,slug) VALUES (%s,coalesce(%s,gen_random_uuid()::text)) RETURNING *
    """,
        (profile["id"], data.slug),
    )
    payload = ProjectInput.model_validate(data.model_dump(exclude={"slug"}))
    result = await save_version(db, "project", profile, entity, payload, user.id)
    return result.model_copy(update={"project_slug": entity["slug"]})


async def edit_item(kind: Kind, target: UUID, data: WorkInput, user: Creator, db: Conn) -> Version:
    profile = await owner_profile(db, user.id)
    entity = await owned_target(db, kind, profile, target, lock=True)
    result = await save_version(
        db, kind, profile, entity, data, user.id, submit=entity["approved"] is not None
    )
    return result.model_copy(update={"project_slug": entity.get("slug")})


@router.put("/me/portfolio/pins")
async def set_portfolio_pins(data: PortfolioPins, user: Creator, db: DB) -> PortfolioPins:
    profile = await owner_profile(db, user.id, lock=True)
    valid = await one(
        db,
        f"""
        SELECT count(*) AS n FROM portfolio_items i JOIN creators c ON c.id=i.creator_id
        WHERE c.id=%s AND i.id=ANY(%s) AND {PUBLIC} AND {ITEM_PUBLIC}
    """,
        (profile["id"], data.work_ids),
    )
    if valid["n"] != len(data.work_ids):
        raise HTTPException(422, "only your published works may be pinned")
    await db.execute("DELETE FROM portfolio_pins WHERE creator_id=%s", (profile["id"],))
    await db.execute(
        """
      INSERT INTO portfolio_pins(creator_id,work_id,slot)
      SELECT %s,id,slot FROM unnest(%s::uuid[]) WITH ORDINALITY AS x(id,slot)
    """,
        (profile["id"], data.work_ids),
    )
    return data


@router.get("/me/portfolio/pins")
async def portfolio_pins(user: Creator, db: DB) -> PortfolioPins:
    profile = await owner_profile(db, user.id)
    rows = await many(
        db, "SELECT work_id FROM portfolio_pins WHERE creator_id=%s ORDER BY slot", (profile["id"],)
    )
    return PortfolioPins(work_ids=[r["work_id"] for r in rows])


@router.put("/me/portfolio/{target}")
async def update_work(target: UUID, data: WorkInput, user: Creator, db: DB) -> Version:
    return await edit_item("portfolio_item", target, data, user, db)


@router.put("/me/projects/{target}")
async def update_project(target: UUID, data: ProjectInput, user: Creator, db: DB) -> Version:
    return await edit_item("project", target, data, user, db)


async def patch_item(kind: Kind, target: UUID, data: WorkPatch, user: Creator, db: Conn) -> Version:
    profile = await owner_profile(db, user.id)
    entity = await owned_target(db, kind, profile, target, lock=True)
    raw = await latest(db, kind, profile["id"], target)
    changes = data.model_dump(mode="json", exclude_unset=True)
    payload = {**raw["payload"], **changes}
    for key in ("title", "description"):
        if isinstance(changes.get(key), dict):
            payload[key] = {**raw["payload"].get(key, {}), **changes[key]}
    if "images" in changes and "media_ids" not in changes:
        payload["media_ids"] = []
    if "media_ids" in changes and "images" not in changes:
        payload["images"] = []
    schema = ProjectInput if kind == "project" else WorkInput
    try:
        edited = schema.model_validate(payload)
    except ValidationError:
        raise HTTPException(422, "invalid item fields") from None
    if edited == schema.model_validate(raw["payload"]):
        return version({**raw, "project_slug": entity.get("slug")})
    result = await save_version(
        db, kind, profile, entity, edited, user.id, submit=entity["approved"] is not None
    )
    return result.model_copy(update={"project_slug": entity.get("slug")})


@router.patch("/me/portfolio/{target}")
async def patch_work(target: UUID, data: WorkPatch, user: Creator, db: DB) -> Version:
    return await patch_item("portfolio_item", target, data, user, db)


@router.patch("/me/projects/{target}")
async def patch_project(target: UUID, data: ProjectPatch, user: Creator, db: DB) -> Version:
    return await patch_item("project", target, data, user, db)


def item_kind(collection: Literal["portfolio", "projects"]) -> Kind:
    return "portfolio_item" if collection == "portfolio" else "project"


@router.get("/me/items/{collection}/{target}")
async def my_item(
    collection: Literal["portfolio", "projects"], target: UUID, user: Creator, db: DB
) -> Version:
    profile = await owner_profile(db, user.id)
    kind = item_kind(collection)
    entity = await owned_target(db, kind, profile, target)
    if entity["publication_status"] == "archived":
        raise HTTPException(404, "item not found")
    row = await latest(db, kind, profile["id"], target)
    return version({**row, "project_slug": entity.get("slug")})


@router.post("/me/items/{collection}/{target}/submit", status_code=202)
async def submit_item(
    collection: Literal["portfolio", "projects"], target: UUID, request: Request, user: Creator, db: DB
) -> Version:
    await throttle(request, "submit", 15, 3600, str(user.id))
    profile = await owner_profile(db, user.id)
    kind = item_kind(collection)
    entity = await owned_target(db, kind, profile, target, lock=True)
    data = version(await latest(db, kind, profile["id"], target)).payload
    result = await save_version(db, kind, profile, entity, data, user.id, submit=True)
    return result.model_copy(update={"project_slug": entity.get("slug")})


@router.delete("/me/portfolio/{target}", status_code=204)
async def delete_draft(target: UUID, user: Creator, db: DB):
    profile = await owner_profile(db, user.id)
    entity = await owned_target(db, "portfolio_item", profile, target, lock=True)
    if entity["approved"] or entity["moderation_status"] == "pending_review":
        raise HTTPException(409, "only unsubmitted drafts may be removed")
    await db.execute("UPDATE portfolio_items SET publication_status='archived' WHERE id=%s", (target,))


@router.delete("/me/projects/{target}", status_code=204)
async def archive_project(target: UUID, user: Creator, db: DB):
    profile = await owner_profile(db, user.id)
    entity = await owned_target(db, "project", profile, target, lock=True)
    if entity["publication_status"] == "archived":
        return
    if await one(
        db, "SELECT id FROM creator_versions WHERE project_id=%s AND status='pending_review'", (target,)
    ):
        raise HTTPException(409, "resolve the pending project review before archiving")
    await db.execute("UPDATE projects SET publication_status='archived' WHERE id=%s", (target,))


async def publish_profile(db: Conn, creator_id: UUID, data: ProfileInput):
    point_place = data.place_id if data.accuracy in (None, "exact") else None
    point_locality = data.locality_id if data.accuracy in (None, "locality", "exact") else None
    await db.execute(
        """
      UPDATE creators c SET approved=%s,public_name=%s,
        legal_verified=CASE WHEN legal_status=%s THEN legal_verified ELSE false END,
        legal_status=%s,district_id=%s,locality_id=%s,place_id=%s,
        available_for_collaboration=%s,search_text=%s,
        public_point=COALESCE((SELECT geo_point FROM places WHERE id=%s AND is_public AND verified_at IS NOT NULL),
                             (SELECT centroid FROM localities WHERE id=%s),
                             (SELECT centroid FROM districts WHERE id=%s)),
        accuracy=CASE WHEN %s::uuid IS NOT NULL THEN 'exact' WHEN %s::uuid IS NOT NULL THEN 'locality'
                      ELSE 'district' END,
        moderation_status='approved',
        publication_status=CASE WHEN publication_status='hidden' THEN 'hidden'::publication_status ELSE 'published'::publication_status END
      WHERE c.id=%s
    """,
        (
            Jsonb(data.model_dump(mode="json")),
            data.public_name,
            data.legal_status,
            data.legal_status,
            data.district_id,
            data.locality_id,
            data.place_id,
            data.available_for_collaboration,
            " ".join(
                [
                    data.public_name,
                    *data.description.model_dump().values(),
                    *data.skills.model_dump().values(),
                    *data.services.model_dump().values(),
                ]
            ),
            point_place,
            point_locality,
            data.district_id,
            point_place,
            point_locality,
            creator_id,
        ),
    )
    for table, column, ids in (
        ("creator_categories", "category_id", data.category_ids),
        ("creator_oked", "oked_id", data.oked_ids),
    ):
        await db.execute(f"DELETE FROM {table} WHERE creator_id=%s", (creator_id,))
        await db.execute(
            f"INSERT INTO {table}(creator_id,{column}) SELECT %s,unnest(%s::uuid[])",
            (creator_id, list(set(ids))),
        )


async def publish_item(db: Conn, kind: Kind, entity: dict, data: WorkInput | ProjectInput, creator_id: UUID):
    table = TARGETS[kind][0]
    await db.execute(
        f"""
        UPDATE {table} SET approved=%s,moderation_status='approved',
        publication_status=CASE WHEN publication_status='hidden' THEN 'hidden'::publication_status ELSE 'published'::publication_status END WHERE id=%s
    """,
        (Jsonb(data.model_dump(mode="json")), entity["id"]),
    )
    if kind == "portfolio_item":
        await db.execute(
            "UPDATE portfolio_items SET category_id=%s,cover_id=%s WHERE id=%s",
            (data.category_id, data.cover_id, entity["id"]),
        )
    else:
        members = list({creator_id, *data.creator_ids})
        await db.execute(
            "UPDATE projects SET organization_id=%s WHERE id=%s", (data.organization_id, entity["id"])
        )
        await db.execute(
            "DELETE FROM project_creators WHERE project_id=%s AND NOT(creator_id=ANY(%s))",
            (entity["id"], members),
        )
        await db.execute(
            """
            INSERT INTO project_creators(project_id,creator_id,confirmed_at)
            SELECT %s, member, CASE WHEN member=%s THEN now() END FROM unnest(%s::uuid[]) member
            ON CONFLICT DO NOTHING
        """,
            (entity["id"], creator_id, members),
        )


CASE_SELECT = """
    m.id,m.assigned_to,m.reviewer_id,m.decision,m.reason,m.created_at,m.decided_at,v.diff,
    to_jsonb(v) AS version, ARRAY(SELECT d.id FROM creators d WHERE d.id<>c.id
        AND ((c.phone IS NOT NULL AND c.phone=d.phone) OR (c.email IS NOT NULL AND c.email=d.email))
        ORDER BY d.id LIMIT 30) AS possible_duplicates
"""
CASE_FROM = (
    "moderation_cases m JOIN creator_versions v ON v.id=m.version_id JOIN creators c ON c.id=v.creator_id"
)


def case_select(scope: tuple) -> str:
    if not scope:
        return CASE_SELECT
    secondary = CENTER_SCOPE.format(creator="d.id")
    return CASE_SELECT.replace("WHERE d.id<>c.id", f"WHERE d.id<>c.id AND {secondary}")


def as_case(row: dict) -> Case:
    return Case(**{**row, "version": version(row["version"])})


@router.get("/moderation/cases")
async def moderation_queue(
    f: Annotated[ModerationFilters, Query()], user: CenterReviewer, db: DB
) -> Page[Case]:
    scope = center_scope(user)
    where = ["true"]
    params = list(scope)
    if scope:
        where.extend(
            (CENTER_SCOPE.format(creator="c.id"), "v.portfolio_item_id IS NULL AND v.project_id IS NULL")
        )
    for column, value in (
        ("m.decision", f.decision),
        ("m.assigned_to", f.assigned_to),
        ("v.creator_id", f.creator_id),
        ("c.user_id", f.author_id),
    ):
        if value is not None:
            where.append(f"{column}=%s")
            params.append(value)
    if f.district_id:
        where.append("COALESCE(v.payload->>'district_id',c.district_id::text)=%s")
        params.append(str(f.district_id))
    for operator, value in ((">=", f.since), ("<", f.until)):
        if value:
            where.append(f"m.created_at{operator}%s")
            params.append(value)
    if f.reason:
        where.append("m.reason ILIKE %s")
        params.append(pattern(f.reason))
    if f.kind:
        where.append(
            {
                "creator": "v.portfolio_item_id IS NULL AND v.project_id IS NULL",
                "portfolio_item": "v.portfolio_item_id IS NOT NULL",
                "project": "v.project_id IS NOT NULL",
            }[f.kind]
        )
    page = await paginated(
        db,
        case_select(scope),
        CASE_FROM,
        " AND ".join(where),
        tuple(params),
        f,
        "created_at DESC,id",
        projection_params=scope,
    )
    page["items"] = [as_case(row) for row in page["items"]]
    return Page[Case](**page)


@router.get("/moderation/cases/{case_id}")
async def case_detail(case_id: UUID, user: CenterReviewer, db: DB) -> Case:
    scope = center_scope(user)
    where = "m.id=%s"
    if scope:
        where += " AND v.portfolio_item_id IS NULL AND v.project_id IS NULL AND " + CENTER_SCOPE.format(
            creator="c.id"
        )
    row = await one(
        db, f"SELECT {case_select(scope)} FROM {CASE_FROM} WHERE {where}", scope + (case_id,) + scope
    )
    if not row:
        raise HTTPException(404, "case not found")
    return as_case(row)


@router.put("/moderation/cases/{case_id}/assignee", status_code=204)
async def assign(case_id: UUID, data: Assignment, user: Reviewer, db: DB):
    if data.user_id and not await one(
        db,
        """
        SELECT u.id FROM users u JOIN user_roles r ON r.user_id=u.id
        WHERE u.id=%s AND u.disabled_at IS NULL AND u.verified_at IS NOT NULL
          AND r.role IN ('moderator','admin','superadmin')
    """,
        (data.user_id,),
    ):
        raise HTTPException(422, "assignee must be an active reviewer")
    if not await one(
        db,
        "UPDATE moderation_cases SET assigned_to=%s WHERE id=%s AND decided_at IS NULL RETURNING id",
        (data.user_id, case_id),
    ):
        raise HTTPException(409, "case is unavailable for assignment")
    await audit(
        db, user.id, "moderation.assigned", case_id, assignee=str(data.user_id) if data.user_id else None
    )


@router.post("/moderation/cases/{case_id}/decision")
async def decide(case_id: UUID, data: Decision, user: CenterReviewer, db: DB) -> Case:
    scope = center_scope(user)
    where = "m.id=%s"
    if scope:
        where += " AND v.portfolio_item_id IS NULL AND v.project_id IS NULL AND " + CENTER_SCOPE.format(
            creator="c.id"
        )
    case = await one(db, f"SELECT m.* FROM {CASE_FROM} WHERE {where} FOR UPDATE OF m", (case_id,) + scope)
    if not case:
        raise HTTPException(404, "case not found")
    if case["decided_at"]:
        raise HTTPException(409, "case already decided")
    if case["assigned_to"] not in (None, user.id) and not ({"admin", "superadmin"} & set(user.roles)):
        raise HTTPException(403, "case belongs to another reviewer")
    raw = await one(db, "SELECT * FROM creator_versions WHERE id=%s", (case["version_id"],))
    v = version(raw)
    c = await one(db, "SELECT * FROM creators WHERE id=%s FOR UPDATE", (v.creator_id,))
    if user.id in (c["user_id"], raw["submitted_by"]):
        raise HTTPException(403, "cannot review your own content")
    target = v.portfolio_item_id or v.project_id or v.creator_id
    kind: Kind = "portfolio_item" if v.portfolio_item_id else "project" if v.project_id else "creator"
    entity = c if kind == "creator" else await owned_target(db, kind, c, target, lock=True)
    if data.decision == "approved":
        enabled = await one(db, "SELECT id FROM users WHERE id=%s AND disabled_at IS NULL", (c["user_id"],))
        if not enabled or not await has_consent(db, c["user_id"], "data_processing"):
            raise HTTPException(409, "author is disabled or withdrew consent")
        if kind == "creator":
            await validate_profile(db, v.payload, c["user_id"], True)
            await publish_profile(db, v.creator_id, v.payload)
        else:
            if not await has_consent(db, c["user_id"], "portfolio"):
                raise HTTPException(409, "portfolio publication consent was withdrawn")
            await validate_work(db, v.payload, c["user_id"], True)
            await publish_item(db, kind, entity, v.payload, v.creator_id)
            if entity["approved"] is None:
                await db.execute(
                    "INSERT INTO analytics_events(event_type,object_id,properties) VALUES (%s,%s,%s)",
                    (f"{kind}.published", target, Jsonb({"creator_id": str(v.creator_id)})),
                )

    elif entity["approved"] is None:
        table = TARGETS[kind][0]
        await db.execute(f"UPDATE {table} SET moderation_status=%s WHERE id=%s", (data.decision, target))
    await db.execute(
        "UPDATE creator_versions SET status=%s,reason=%s,reviewer_id=%s WHERE id=%s",
        (data.decision, data.reason, user.id, v.id),
    )
    await db.execute(
        """
        UPDATE moderation_cases SET decision=%s,reason=%s,reviewer_id=%s,decided_at=now() WHERE id=%s
    """,
        (data.decision, data.reason, user.id, case_id),
    )
    await db.execute(
        """
        INSERT INTO notifications(user_id,template_id,locale,context)
        SELECT id,'moderation.decision',locale,%s FROM users WHERE id=%s
    """,
        (Jsonb({"case_id": str(case_id), "decision": data.decision, "reason": data.reason}), c["user_id"]),
    )
    await audit(
        db,
        user.id,
        "moderation.decided",
        target,
        case_id=str(case_id),
        decision=data.decision,
        reason=data.reason,
    )
    return await case_detail(case_id, user, db)


@router.post("/moderation/{kind}/{target}/hide", status_code=204)
async def hide(kind: Kind, target: UUID, data: ReasonInput, user: CenterReviewer, db: DB):
    scope = center_scope(user)
    if scope and kind != "creator":
        raise HTTPException(404, "published entity not found")
    where = "id=%s AND publication_status='published'"
    if scope:
        where += " AND " + CENTER_SCOPE.format(creator="creators.id")
    table = TARGETS[kind][0]
    if not await one(
        db,
        f"UPDATE {table} SET publication_status='hidden' WHERE {where} RETURNING id",
        (target,) + scope,
    ):
        raise HTTPException(404, "published entity not found")
    await audit(db, user.id, "publication.hidden", target, kind=kind, reason=data.reason)


def item_source(kind: Kind):
    return (
        "portfolio_items i JOIN creators c ON c.id=i.creator_id"
        if kind == "portfolio_item"
        else "projects i JOIN creators c ON c.id=i.owner_creator_id"
    )


PUBLIC_PARTICIPANTS = f"""ARRAY(
    SELECT c.id FROM project_creators pc JOIN creators c ON c.id=pc.creator_id
    WHERE pc.project_id=i.id AND pc.confirmed_at IS NOT NULL AND {PUBLIC}
    ORDER BY c.id
) AS participants"""


ITEM_PUBLIC = """i.publication_status='published' AND i.moderation_status='approved'
AND EXISTS(SELECT 1 FROM consents s WHERE s.user_id=c.user_id AND s.type='portfolio' AND s.withdrawn_at IS NULL)"""


async def items_feed(db: Conn, kind: Kind, f: ItemFilters) -> dict:
    where = [PUBLIC, ITEM_PUBLIC]
    params = []
    if f.creator_id:
        if kind == "portfolio_item":
            where.append("i.creator_id=%s")
        else:
            where.append(
                "EXISTS (SELECT 1 FROM project_creators pc JOIN creators participant ON participant.id=pc.creator_id "
                "WHERE pc.project_id=i.id AND pc.creator_id=%s AND pc.confirmed_at IS NOT NULL "
                "AND participant.publication_status='published')"
            )
        params.append(f.creator_id)
    if f.category_id:
        where.append("i.approved->>'category_id'=%s")
        params.append(str(f.category_id))
    if f.q:
        where.append("(i.approved->'title')::text ILIKE %s")
        params.append(pattern(f.q))
    owner = "creator_id" if kind == "portfolio_item" else "owner_creator_id"
    sort_columns = ""
    order = "created_at DESC,id"
    if f.sort == "author":
        sort_columns = ",coalesce((i.approved->>'sort_order')::int,0) AS author_order"
        order = "author_order,created_at DESC,id"
    if f.sort == "popular":
        event = "project.view" if kind == "project" else "portfolio_item.view"
        sort_columns = f",(SELECT count(*) FROM analytics_events a WHERE a.event_type='{event}' AND a.object_id=i.id) AS views"
        order = "views DESC,created_at DESC,id"
    page = await paginated(
        db,
        f"i.id,i.{owner},i.approved,i.moderation_status,i.publication_status,i.created_at"
        + (f",i.slug,{PUBLIC_PARTICIPANTS}" if kind == "project" else "")
        + sort_columns,
        item_source(kind),
        " AND ".join(where),
        tuple(params),
        f,
        order,
    )
    page["items"] = [public_item(row, kind) for row in page["items"]]
    return page


def public_item(row: dict, kind: Kind) -> PublicWork | PublicProject:
    schema = PublicWork if kind == "portfolio_item" else PublicProject
    owner = "creator_id" if kind == "portfolio_item" else "owner_creator_id"
    payload = row["approved"].copy()
    if kind == "project":
        payload["creator_ids"] = row["participants"]
        payload["slug"] = row["slug"]
    return schema(
        **payload, **{key: row[key] for key in ("id", owner, "moderation_status", "publication_status")}
    )


@router.get("/portfolio")
async def portfolio(f: Annotated[ItemFilters, Query()], db: DB) -> Page[PublicWork]:
    return Page[PublicWork](**await items_feed(db, "portfolio_item", f))


@router.get("/projects")
async def projects(f: Annotated[ItemFilters, Query()], db: DB) -> Page[PublicProject]:
    return Page[PublicProject](**await items_feed(db, "project", f))


async def item_detail(db: Conn, kind: Kind, target: UUID | str) -> PublicWork | PublicProject:
    columns = f"i.*,{PUBLIC_PARTICIPANTS}" if kind == "project" else "i.*"
    where, params = "i.id=%s", (target,)
    if kind == "project" and isinstance(target, str):
        try:
            uuid = UUID(target)
        except ValueError:
            where = "i.slug=%s"
        else:
            where, params = "(i.slug=%s OR i.id=%s)", (target, uuid)
    row = await one(
        db,
        f"SELECT {columns} FROM {item_source(kind)} WHERE {PUBLIC} AND {ITEM_PUBLIC} AND {where}",
        params,
    )
    if not row:
        raise HTTPException(404, "item not found")
    return public_item(row, kind)


@router.get("/portfolio/{target}")
async def work_detail(target: UUID, db: DB) -> PublicWork:
    return await item_detail(db, "portfolio_item", target)


@router.get("/projects/{slug}")
async def project_detail(slug: Slug, db: DB) -> PublicProject:
    return await item_detail(db, "project", slug)


@router.post("/me/projects/{project_id}/confirm-participation", status_code=204)
async def confirm_participation(project_id: UUID, user: Creator, db: DB):
    profile = await owner_profile(db, user.id)
    if not await one(
        db,
        f"""UPDATE project_creators pc SET confirmed_at=coalesce(pc.confirmed_at,now())
        WHERE pc.project_id=%s AND pc.creator_id=%s AND EXISTS(
            SELECT 1 FROM {item_source("project")} WHERE i.id=pc.project_id AND {PUBLIC} AND {ITEM_PUBLIC}
        ) RETURNING pc.project_id""",
        (project_id, profile["id"]),
    ):
        raise HTTPException(404, "invitation not found")


@router.get("/me/project-invitations")
async def project_invitations(
    f: Annotated[PageQuery, Query()], user: Creator, db: DB
) -> Page[ProjectInvitation]:
    profile = await owner_profile(db, user.id)
    source = f"{item_source('project')} JOIN project_creators pc ON pc.project_id=i.id"
    return Page[ProjectInvitation](
        **await paginated(
            db,
            "i.id AS project_id,i.slug,i.approved->'title' AS title,pc.confirmed_at",
            source,
            f"pc.creator_id=%s AND i.owner_creator_id<>pc.creator_id AND {PUBLIC} AND {ITEM_PUBLIC}",
            (profile["id"],),
            f,
            "slug,project_id",
        )
    )


class ReferenceFilters(PageQuery):
    district_id: UUID | None = None
    version: str | None = None


@router.get("/references/{kind}", response_model=Page[Reference] | Page[Oked])
async def reference(
    kind: Literal["categories", "oked", "districts", "localities", "tags"],
    f: Annotated[ReferenceFilters, Query()],
    db: DB,
):
    table = "creative_categories" if kind == "categories" else kind
    where, params = ("true" if kind == "localities" else "retired_at IS NULL"), ()
    if kind == "localities" and f.district_id:
        where, params = "district_id=%s", (f.district_id,)
    if kind == "oked" and f.version:
        where, params = "retired_at IS NULL AND version=%s", (f.version,)
    columns = "id,code,labels,version" if kind == "oked" else "id,code,labels"
    page = await paginated(db, columns, table, where, params, f, "code,id")
    return Page[Oked](**page) if kind == "oked" else Page[Reference](**page)


@router.get("/me/items/{collection}")
async def my_items(
    collection: Literal["portfolio", "projects"], f: Annotated[PageQuery, Query()], user: Creator, db: DB
) -> Page[Version]:
    profile = await owner_profile(db, user.id)
    kind = item_kind(collection)
    table, column = TARGETS[kind]
    page = await paginated(
        db,
        "latest.*" + (",item.slug AS project_slug" if kind == "project" else ""),
        f"""
        {table} item JOIN LATERAL (
            SELECT v.* FROM creator_versions v WHERE v.{column}=item.id
            ORDER BY v.created_at DESC,v.id DESC LIMIT 1
        ) latest ON true
    """,
        "latest.creator_id=%s AND item.publication_status<>'archived'",
        (profile["id"],),
        f,
        "created_at DESC,id",
    )
    return Page[Version](**{**page, "items": [version(r) for r in page["items"]]})


@router.get("/me/portfolio")
async def my_portfolio(f: Annotated[PageQuery, Query()], user: Creator, db: DB) -> Page[Version]:
    return await my_items("portfolio", f, user, db)


@router.get("/me/projects")
async def my_projects(f: Annotated[PageQuery, Query()], user: Creator, db: DB) -> Page[Version]:
    return await my_items("projects", f, user, db)


@router.get("/me/portfolio/{target}")
async def my_portfolio_item(target: UUID, user: Creator, db: DB) -> Version:
    return await my_item("portfolio", target, user, db)


@router.get("/me/projects/{target}")
async def my_project(target: UUID, user: Creator, db: DB) -> Version:
    return await my_item("projects", target, user, db)


@router.post("/me/portfolio/{target}/submit", status_code=202)
async def submit_portfolio_item(target: UUID, request: Request, user: Creator, db: DB) -> Version:
    return await submit_item("portfolio", target, request, user, db)


@router.post("/me/projects/{target}/submit", status_code=202)
async def submit_project(target: UUID, request: Request, user: Creator, db: DB) -> Version:
    return await submit_item("projects", target, request, user, db)


@router.post("/moderation/{kind}/{target}/restore", status_code=204)
async def restore(kind: Kind, target: UUID, data: ReasonInput, user: Reviewer, db: DB):
    table = TARGETS[kind][0]
    if not await one(
        db,
        f"""
        UPDATE {table} SET publication_status='published' WHERE id=%s AND publication_status='hidden'
            AND moderation_status='approved' AND approved IS NOT NULL RETURNING id
    """,
        (target,),
    ):
        raise HTTPException(404, "hidden approved entity not found")
    await audit(db, user.id, "publication.restored", target, kind=kind, reason=data.reason)
