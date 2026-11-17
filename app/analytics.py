import csv
import io
from datetime import UTC, date, datetime
from typing import Annotated, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from openpyxl import Workbook
from psycopg.types.json import Jsonb
from pydantic import AliasChoices, ConfigDict, Field, model_validator
from starlette.concurrency import run_in_threadpool

from app.auth import Actor, require, throttle
from app.config import settings
from app.db import DB, Conn, audit, many, one
from app.registry import ITEM_PUBLIC, PUBLIC, RESIDENCY, catalog_where, item_source, paginated
from app.schemas import CatalogFilters, LegalStatus, Model, Page, PageQuery, Role

router = APIRouter(tags=["analytics"])
Analyst = Annotated[Actor, Depends(require(Role.analyst, Role.admin, Role.superadmin))]


class AnalyticsFilters(CatalogFilters):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    start: date = Field(validation_alias=AliasChoices("date_from", "start"))
    end: date = Field(validation_alias=AliasChoices("date_to", "end"))
    grain: Literal["day", "week", "month"] = "month"

    @model_validator(mode="after")
    def valid_period(self):
        if not 0 <= (self.end - self.start).days <= 1096:
            raise ValueError("period must span 0 to 1096 days")
        return self


class Totals(Model):
    creators: int
    published_profiles: int
    pending_cases: int
    residents: int
    resident_individuals: int
    resident_sole_proprietors: int
    resident_legal_entities: int
    organizations: int
    resident_organizations: int
    articles: int
    events: int
    active_authors_30d: int
    works: int
    projects: int


class Bucket(Model):
    key: str
    count: int
    unstable_sample: bool = False

    @model_validator(mode="after")
    def flag_small_sample(self):
        self.unstable_sample = self.count < 5
        return self


class Dashboard(Model):
    start: date
    end: date
    generated_at: datetime
    date_from: date
    date_to: date
    totals: Totals
    legal_status: list[Bucket]
    districts: list[Bucket]
    categories: list[Bucket]
    oked: list[Bucket]
    registrations: list[Bucket]
    average_moderation_hours: float | None
    median_moderation_hours: float | None
    approval_rate: float | None
    sample_size: int
    unstable_sample: bool
    minimum_sample_size: int = 5


async def dashboard(db: Conn, f: AnalyticsFilters) -> Dashboard:
    where, params = catalog_where(f, public=False)
    window = "c.created_at<((%s::date+1)::timestamp AT TIME ZONE 'Asia/Almaty')"
    args = params + (f.end,)
    matching = f"WITH filtered AS MATERIALIZED (SELECT c.* FROM creators c WHERE {where} AND {window})"
    totals = await one(
        db,
        f"""{matching}
        SELECT count(*) FILTER (WHERE c.moderation_status='approved') AS creators,
          count(*) FILTER (WHERE {PUBLIC}) AS published_profiles,
          count(*) FILTER (WHERE {RESIDENCY}='active' AND c.legal_status<>'legal_entity') AS residents,
          count(*) FILTER (WHERE {RESIDENCY}='active' AND c.legal_status='individual') AS resident_individuals,
          count(*) FILTER (WHERE {RESIDENCY}='active' AND c.legal_status='sole_proprietor') AS resident_sole_proprietors,
          count(*) FILTER (WHERE {RESIDENCY}='active' AND c.legal_status='legal_entity') AS resident_legal_entities,
          (SELECT count(*) FROM moderation_cases m JOIN creator_versions v ON v.id=m.version_id
           JOIN filtered x ON x.id=v.creator_id WHERE m.decision='pending_review') AS pending_cases,
          (SELECT count(DISTINCT m.org_id) FROM organization_members m JOIN filtered x ON x.user_id=m.user_id
           WHERE m.verified_at IS NOT NULL) AS organizations,
          (SELECT count(DISTINCT r.org_id) FROM residencies r JOIN organization_members m ON m.org_id=r.org_id
           JOIN filtered x ON x.user_id=m.user_id WHERE r.status='active' AND m.verified_at IS NOT NULL
           AND (now() AT TIME ZONE 'Asia/Almaty')::date BETWEEN r.starts_on AND r.ends_on)
           AS resident_organizations,
          (SELECT count(*) FROM articles WHERE status='published' AND published_at<=now()
           AND (published_at AT TIME ZONE 'Asia/Almaty')::date BETWEEN %s AND %s) AS articles,
          (SELECT count(*) FROM events WHERE status='published' AND (publish_at IS NULL OR publish_at<=now())
           AND (starts_at AT TIME ZONE 'Asia/Almaty')::date BETWEEN %s AND %s) AS events,
          (SELECT count(DISTINCT a.properties->>'creator_id') FROM analytics_events a
           JOIN filtered x ON x.id::text=a.properties->>'creator_id'
           WHERE a.event_type IN ('portfolio_item.published','project.published')
           AND a.created_at>=(((now() AT TIME ZONE 'Asia/Almaty')::date-29)::timestamp AT TIME ZONE 'Asia/Almaty')
           AND a.created_at<=now() AND (
             (a.event_type='portfolio_item.published' AND EXISTS(
               SELECT 1 FROM portfolio_items i WHERE i.id=a.object_id AND i.creator_id=x.id
               AND i.publication_status='published' AND i.moderation_status='approved'))
             OR (a.event_type='project.published' AND EXISTS(
               SELECT 1 FROM projects i WHERE i.id=a.object_id AND i.owner_creator_id=x.id
               AND i.publication_status='published' AND i.moderation_status='approved'))))

           AS active_authors_30d,
          (SELECT count(*) FROM {item_source("portfolio_item")}
           JOIN filtered x ON x.id=c.id WHERE {PUBLIC} AND {ITEM_PUBLIC}) AS works,
          (SELECT count(*) FROM {item_source("project")}
           JOIN filtered x ON x.id=c.id WHERE {PUBLIC} AND {ITEM_PUBLIC}) AS projects
        FROM filtered c
    """,
        args + (f.start, f.end, f.start, f.end),
    )
    timing = await one(
        db,
        f"""{matching}
        SELECT now() AS generated_at, avg(hours) AS average_moderation_hours,
          percentile_cont(0.5) WITHIN GROUP (ORDER BY hours) AS median_moderation_hours,
          count(*) FILTER (WHERE decision='approved')::float /
          nullif(count(*) FILTER (WHERE decision IN ('approved','rejected')),0) AS approval_rate
        FROM (SELECT extract(epoch FROM m.decided_at-m.created_at)/3600 AS hours,m.decision
              FROM moderation_cases m JOIN creator_versions v ON v.id=m.version_id
              JOIN filtered x ON x.id=v.creator_id
              WHERE (m.decided_at AT TIME ZONE 'Asia/Almaty')::date BETWEEN %s AND %s
                AND v.project_id IS NULL AND v.portfolio_item_id IS NULL) d
    """,
        args + (f.start, f.end),
    )
    group_specs = {
        "legal_status": ("c.legal_status", "filtered c"),
        "districts": ("c.district_id", "filtered c"),
        "categories": ("cc.category_id", "filtered c JOIN creator_categories cc ON cc.creator_id=c.id"),
        "oked": (
            "o.code",
            "filtered c JOIN creator_oked co ON co.creator_id=c.id JOIN oked o ON o.id=co.oked_id",
        ),
    }
    groups = {}
    for key, (field, source) in group_specs.items():
        groups[key] = await many(
            db,
            f"""{matching}
            SELECT {field}::text AS key,count(DISTINCT c.id) AS count FROM {source}
            WHERE c.moderation_status='approved' AND {field} IS NOT NULL GROUP BY {field} HAVING count(DISTINCT c.id)>=5 ORDER BY count DESC,key
        """,
            args,
        )
    registrations = await many(
        db,
        f"""{matching}
        SELECT date_trunc(%s,c.created_at AT TIME ZONE 'Asia/Almaty')::date::text AS key,count(*) AS count
        FROM filtered c WHERE (c.created_at AT TIME ZONE 'Asia/Almaty')::date BETWEEN %s AND %s
        GROUP BY 1 ORDER BY 1
    """,
        args + (f.grain, f.start, f.end),
    )
    return Dashboard(
        start=f.start,
        end=f.end,
        date_from=f.start,
        date_to=f.end,
        totals=totals,
        registrations=registrations,
        sample_size=totals["creators"],
        unstable_sample=totals["creators"] < 5,
        **groups,
        **timing,
    )


@router.get("/analytics/dashboard")
async def dashboard_endpoint(f: Annotated[AnalyticsFilters, Query()], user: Analyst, db: DB) -> Dashboard:
    return await dashboard(db, f)


@router.get("/analytics/metrics")
async def metrics(user: Analyst) -> dict[str, str]:
    return {
        "creators": "Distinct currently approved creator profiles created by the end of the selected period, regardless of publication.",
        "published_profiles": "Matching creators currently published, approved and consenting to data processing.",
        "residents": "Currently valid confirmed resident individual/sole-proprietor profiles; organizations and legal entities are reported separately.",
        "organizations": "Distinct organizations with verified membership by matching creators.",
        "pending_cases": "Currently pending moderation cases for matching creators.",
        "average_moderation_hours": "Mean elapsed hours from case creation to first decision.",
        "median_moderation_hours": "Median elapsed hours from case creation to first decision.",
        "registrations": "Profile creations within the selected period, grouped in Asia/Almaty.",
        "segment_threshold": "District, legal status, category and OKED groups below 5 are suppressed.",
    }


def report_rows(data: Dashboard) -> list[tuple]:
    rows = [
        ("metadata", "date_from", data.start.isoformat()),
        ("metadata", "date_to", data.end.isoformat()),
        ("metadata", "generated_at", data.generated_at.isoformat()),
        ("metadata", "unstable_sample", data.unstable_sample),
    ]
    rows.extend(("totals", key, value) for key, value in data.totals.model_dump().items())
    rows.extend(
        ("moderation", key, getattr(data, key))
        for key in ("approval_rate", "average_moderation_hours", "median_moderation_hours")
    )
    for name in ("legal_status", "districts", "categories", "oked", "registrations"):
        rows.extend((name, b.key, b.count) for b in getattr(data, name))
    return rows


def tabular_report(data: Dashboard, fmt: str) -> bytes:
    return table_bytes([("series", "key", "value"), *report_rows(data)], fmt)


def table_bytes(rows: list[tuple], fmt: str) -> bytes:
    if fmt == "csv":
        output = io.StringIO(newline="")
        csv.writer(output).writerows(rows)
        return output.getvalue().encode("utf-8-sig")
    output = io.BytesIO()
    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("Creative Shygys")
    for row in rows:
        sheet.append(row)
    workbook.save(output)
    return output.getvalue()


async def render_report(data: Dashboard, fmt: str) -> bytes:
    if not settings.browserless_url:
        raise HTTPException(503, "Browserless unavailable")
    # Only server-defined metric keys and numeric values are inserted into the HTML.
    highest = max(1, max(data.totals.model_dump().values()))
    bars = "".join(
        f"<div>{k} ({v})<div style='width:{v / highest * 480:.1f}px;height:16px;background:#36577a'></div></div>"
        for k, v in data.totals.model_dump().items()
    )
    html = (
        f"<!doctype html><meta charset='utf-8'><title>Creative Shygys</title>"
        f"<h1>Creative Shygys</h1><p>{data.start} – {data.end}</p>{bars}"
    )
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            endpoint = "pdf" if fmt == "pdf" else "screenshot"
            result = await client.post(
                f"{settings.browserless_url.rstrip('/')}/{endpoint}",
                params={"token": settings.browserless_token.get_secret_value()},
                json={"html": html},
            )
            result.raise_for_status()
            signature = b"%PDF-" if fmt == "pdf" else b"\x89PNG\r\n\x1a\n"
            if not result.content.startswith(signature):
                raise ValueError("invalid render")
            return result.content
    except (httpx.HTTPError, ValueError):
        raise HTTPException(502, "report renderer unavailable") from None


REPORT_TYPES = {
    "csv": "text/csv",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "pdf": "application/pdf",
    "png": "image/png",
}


@router.get(
    "/analytics/export/{fmt}", responses={200: {"content": {mime: {} for mime in REPORT_TYPES.values()}}}
)
async def export(
    fmt: Literal["csv", "xlsx", "pdf", "png"],
    f: Annotated[AnalyticsFilters, Query()],
    request: Request,
    user: Analyst,
    db: DB,
) -> Response:
    await throttle(request, "report", 25, 3600, str(user.id))
    data = await dashboard(db, f)
    if fmt in ("pdf", "png"):
        content = await render_report(data, fmt)
    else:
        content = await run_in_threadpool(tabular_report, data, fmt)
    await audit(
        db, user.id, "dashboard.exported", format=fmt, start=f.start.isoformat(), end=f.end.isoformat()
    )
    return Response(
        content,
        media_type=REPORT_TYPES[fmt],
        headers={
            "Content-Disposition": f'attachment; filename="creative-shygys.{fmt}"',
            "Cache-Control": "private, no-store",
        },
    )


class Visit(Model):
    kind: Literal["creator", "project", "portfolio_item"]
    id: UUID


@router.post("/analytics/visits", status_code=204)
async def track_visit(data: Visit, request: Request, db: DB):
    agent = request.headers.get("user-agent", "").lower()
    if not agent or any(word in agent for word in ("bot", "spider", "crawler", "headless")):
        return
    await throttle(request, "visit", 100, 3600)
    if data.kind == "creator":
        row = await one(db, f"SELECT c.id FROM creators c WHERE c.id=%s AND {PUBLIC}", (data.id,))
    else:
        row = await one(
            db,
            f"SELECT i.id FROM {item_source(data.kind)} WHERE i.id=%s AND {PUBLIC} AND {ITEM_PUBLIC}",
            (data.id,),
        )
    if row:
        await db.execute(
            "INSERT INTO analytics_events(event_type,object_id) VALUES (%s,%s)",
            (f"{data.kind}.view", data.id),
        )


class ComparisonFilters(AnalyticsFilters):
    baseline_start: date = Field(validation_alias=AliasChoices("baseline_date_from", "baseline_start"))
    baseline_end: date = Field(validation_alias=AliasChoices("baseline_date_to", "baseline_end"))

    @model_validator(mode="after")
    def baseline_period(self):
        if not 0 <= (self.baseline_end - self.baseline_start).days <= 1096:
            raise ValueError("baseline period must span 0 to 1096 days")
        return self


class Delta(Model):
    metric: str
    baseline: int
    current: int
    difference: int
    percent: float | None
    generated_at: datetime
    unstable_sample: bool


@router.get("/analytics/compare")
async def compare(f: Annotated[ComparisonFilters, Query()], user: Analyst, db: DB) -> list[Delta]:
    filters = f.model_dump(exclude={"baseline_start", "baseline_end"})
    current = (await dashboard(db, AnalyticsFilters(**filters))).totals.model_dump()
    baseline = (
        await dashboard(db, AnalyticsFilters(**{**filters, "start": f.baseline_start, "end": f.baseline_end}))
    ).totals.model_dump()
    return [
        Delta(
            metric=key,
            baseline=baseline[key],
            current=value,
            difference=value - baseline[key],
            percent=(value - baseline[key]) * 100 / baseline[key] if baseline[key] else None,
            generated_at=datetime.now(UTC),
            unstable_sample=baseline[key] < 5 or value < 5,
        )
        for key, value in current.items()
    ]


class SegmentCreator(Model):
    id: UUID
    slug: str
    public_name: str | None
    district_id: UUID | None
    legal_status: LegalStatus


@router.get("/analytics/creators")
async def creator_segment(
    f: Annotated[AnalyticsFilters, Query()], user: Analyst, db: DB
) -> Page[SegmentCreator]:
    where, params = catalog_where(f, public=False)
    where += """ AND c.moderation_status='approved'
      AND c.created_at<((%s::date+1)::timestamp AT TIME ZONE 'Asia/Almaty')"""
    return Page[SegmentCreator](
        **await paginated(
            db,
            f"c.id,c.slug,CASE WHEN {PUBLIC} THEN c.public_name END AS public_name,c.district_id,c.legal_status",
            "creators c",
            where,
            params + (f.end,),
            f,
            "slug,id",
        )
    )


class Funnel(Model):
    started: int
    submitted: int
    pending_review: int
    approved: int
    rejected: int
    submission_rate: float | None
    generated_at: datetime
    unstable_sample: bool


@router.get("/analytics/funnel")
async def registration_funnel(f: Annotated[AnalyticsFilters, Query()], user: Analyst, db: DB) -> Funnel:
    where, params = catalog_where(f, public=False)
    row = await one(
        db,
        f"""
      WITH cohort AS (
        SELECT c.id FROM creators c WHERE {where}
          AND (c.created_at AT TIME ZONE 'Asia/Almaty')::date BETWEEN %s AND %s
      ), submissions AS (
        SELECT DISTINCT ON(v.creator_id) v.creator_id,m.decision FROM creator_versions v JOIN moderation_cases m ON m.version_id=v.id
        JOIN cohort c ON c.id=v.creator_id WHERE v.project_id IS NULL AND v.portfolio_item_id IS NULL
        ORDER BY v.creator_id,m.created_at DESC,m.id DESC
      )
      SELECT (SELECT count(*) FROM cohort) AS started,count(DISTINCT creator_id) AS submitted,
      count(DISTINCT creator_id) FILTER (WHERE decision='pending_review') AS pending_review,
      count(DISTINCT creator_id) FILTER (WHERE decision='approved') AS approved,
      count(DISTINCT creator_id) FILTER (WHERE decision='rejected') AS rejected
      FROM submissions
    """,
        params + (f.start, f.end),
    )
    return Funnel(
        **row,
        submission_rate=row["submitted"] / row["started"] if row["started"] else None,
        generated_at=datetime.now(UTC),
        unstable_sample=row["started"] < 5,
    )


class PresetInput(Model):
    name: Annotated[str, Field(min_length=1, max_length=80)]
    role: Literal["analyst", "admin", "superadmin"]
    filters: AnalyticsFilters


class DashboardPreset(PresetInput):
    id: UUID
    updated_at: datetime


@router.put("/analytics/presets")
async def save_dashboard_preset(data: PresetInput, user: Analyst, db: DB) -> DashboardPreset:
    if data.role not in user.roles:
        raise HTTPException(403, "role is not assigned to caller")
    return DashboardPreset(
        **await one(
            db,
            """
      INSERT INTO dashboard_presets(user_id,role,name,filters) VALUES (%s,%s,%s,%s)
      ON CONFLICT(user_id,role,name) DO UPDATE SET filters=excluded.filters,updated_at=now()
      RETURNING id,role,name,filters,updated_at
    """,
            (user.id, data.role, data.name, Jsonb(data.filters.model_dump(mode="json"))),
        )
    )


@router.get("/analytics/presets")
async def dashboard_presets(f: Annotated[PageQuery, Query()], user: Analyst, db: DB) -> Page[DashboardPreset]:
    return Page[DashboardPreset](
        **await paginated(
            db,
            "id,role,name,filters,updated_at",
            "dashboard_presets",
            "user_id=%s AND role=ANY(%s::role[])",
            (user.id, [str(role) for role in user.roles]),
            f,
            "updated_at DESC,id",
        )
    )


@router.delete("/analytics/presets/{entity}", status_code=204)
async def delete_dashboard_preset(entity: UUID, user: Analyst, db: DB):
    if not await one(
        db,
        """
      DELETE FROM dashboard_presets WHERE id=%s AND user_id=%s AND role=ANY(%s::role[]) RETURNING id
    """,
        (entity, user.id, [str(role) for role in user.roles]),
    ):
        raise HTTPException(404, "preset not found")


@router.get("/analytics/overview")
async def overview(f: Annotated[AnalyticsFilters, Query()], user: Analyst, db: DB) -> Dashboard:
    return await dashboard(db, f)


class Dynamics(Model):
    date_from: date
    date_to: date
    grain: Literal["day", "week", "month"]
    generated_at: datetime
    unstable_sample: bool
    items: list[Bucket]


@router.get("/analytics/dynamics")
async def dynamics(f: Annotated[AnalyticsFilters, Query()], user: Analyst, db: DB) -> Dynamics:
    data = await dashboard(db, f)
    return Dynamics(
        date_from=f.start,
        date_to=f.end,
        grain=f.grain,
        generated_at=data.generated_at,
        unstable_sample=sum(b.count for b in data.registrations) < 5,
        items=data.registrations,
    )


class Geography(Model):
    date_from: date
    date_to: date
    generated_at: datetime
    minimum_sample_size: int = 5
    unstable_sample: bool
    items: list[Bucket]


@router.get("/analytics/geography")
async def geography(f: Annotated[AnalyticsFilters, Query()], user: Analyst, db: DB) -> Geography:
    data = await dashboard(db, f)
    return Geography(
        date_from=f.start,
        date_to=f.end,
        generated_at=data.generated_at,
        unstable_sample=data.unstable_sample,
        items=data.districts,
    )
