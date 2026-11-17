from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from psycopg.types.json import Jsonb
from pydantic import Field, model_validator

from app.auth import Actor, Admin, require
from app.config import settings
from app.db import DB, Conn, audit, many, one
from app.registry import (
    CREATOR_COLUMNS,
    PUBLIC_POINT,
    catalog_where,
    grouped_markers,
    map_bounds,
    marker_cell,
    marker_source,
    paginated,
    public_creator,
)
from app.schemas import (
    Accuracy,
    CatalogFilters,
    Id,
    Localized,
    MapFilters,
    MapResults,
    Model,
    Page,
    PageQuery,
    PublicCreator,
    Role,
)

router = APIRouter(tags=["geography"])
GeoExporter = Annotated[Actor, Depends(require(Role.analyst, Role.admin, Role.superadmin))]
PlaceKind = Literal["studio", "workshop", "gallery", "coworking", "center"]


class Coordinates(Model):
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-85.05, le=85.05)


class ReferenceInput(Coordinates):
    labels: Localized
    code: str = Field(min_length=1, max_length=64)
    district_id: UUID | None = None


class PlaceInput(Coordinates):
    labels: Localized
    locality_id: UUID
    kind: PlaceKind
    is_public: bool = False


class Place(PlaceInput):
    id: UUID


class PlaceFilters(PageQuery):
    kind: PlaceKind | None = None
    locality_id: UUID | None = None


@router.get("/map/config")
async def map_config() -> dict[str, str]:
    return {
        "style_url": settings.tile_style_url,
        "attribution": "© OpenStreetMap contributors",
        "attribution_url": "https://www.openstreetmap.org/copyright",
    }


@router.post("/admin/references/{kind}", status_code=201)
async def geo_reference(
    kind: Literal["districts", "localities"], data: ReferenceInput, user: Admin, db: DB
) -> Id:
    params = (data.code, Jsonb(data.labels.model_dump()), data.longitude, data.latitude)
    if kind == "localities" and data.district_id:
        row = await one(
            db,
            """
            INSERT INTO localities(code,labels,centroid,district_id)
            VALUES (%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),4326),%s) RETURNING id
        """,
            params + (data.district_id,),
        )
    elif kind == "districts" and not data.district_id:
        row = await one(
            db,
            """
            INSERT INTO districts(code,labels,centroid)
            VALUES (%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),4326)) RETURNING id
        """,
            params,
        )
    else:
        raise HTTPException(422, "only a locality requires district_id")
    await audit(db, user.id, f"{kind}.created", row["id"])
    return Id(**row)


@router.post("/admin/places", status_code=201)
async def place(data: PlaceInput, user: Admin, db: DB) -> Place:
    row = await one(
        db,
        """
        INSERT INTO places(locality_id,labels,kind,geo_point,is_public,verified_at)
        VALUES (%s,%s,%s,ST_SetSRID(ST_MakePoint(%s,%s),4326),%s,
                CASE WHEN %s THEN now() END) RETURNING id
    """,
        (
            data.locality_id,
            Jsonb(data.labels.model_dump()),
            data.kind,
            data.longitude,
            data.latitude,
            data.is_public,
            data.is_public,
        ),
    )
    await audit(db, user.id, "place.created", row["id"], public=data.is_public)
    return Place(**data.model_dump(), **row)


@router.get("/spaces")
@router.get("/map/places")
async def places(f: Annotated[PlaceFilters, Query()], db: DB) -> Page[Place]:
    where = "is_public AND verified_at IS NOT NULL"
    params = []
    for key in ("kind", "locality_id"):
        if (value := getattr(f, key)) is not None:
            where += f" AND {key}=%s"
            params.append(value)
    return Page[Place](
        **await paginated(
            db,
            "id,locality_id,labels,kind,is_public,ST_X(geo_point) AS longitude,ST_Y(geo_point) AS latitude",
            "places",
            where,
            tuple(params),
            f,
            "id",
        )
    )


Longitude = Annotated[float, Field(ge=-180, le=180)]
Latitude = Annotated[float, Field(ge=-90, le=90)]
Position = tuple[Longitude, Latitude]
Ring = Annotated[list[Position], Field(min_length=4, max_length=30000)]
Polygon = Annotated[list[Ring], Field(min_length=1, max_length=100)]


class MultiPolygon(Model):
    type: Literal["MultiPolygon"]
    coordinates: list[Polygon] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def closed_rings(self):
        count = 0
        for polygon in self.coordinates:
            for ring in polygon:
                count += len(ring)
                if ring[0] != ring[-1]:
                    raise ValueError("geometry rings must be closed")
        if count > 100000:
            raise ValueError("geometry exceeds the vertex limit")
        return self


class Boundary(Model):
    id: UUID
    code: str
    labels: Localized
    geometry: MultiPolygon


@router.put("/admin/references/{kind}/{entity}/boundary", status_code=204)
async def set_boundary(
    kind: Literal["districts", "localities"], entity: UUID, data: MultiPolygon, user: Admin, db: DB
):
    raw = data.model_dump_json()
    if not await one(db, "SELECT id FROM " + kind + " WHERE id=%s FOR UPDATE", (entity,)):
        raise HTTPException(404, "geographic reference not found")
    result = await one(db, "SELECT ST_IsValid(ST_GeomFromGeoJSON(%s)) AS valid", (raw,))
    if not result["valid"]:
        raise HTTPException(422, "invalid polygon topology")
    await db.execute(f"UPDATE {kind} SET geom=ST_GeomFromGeoJSON(%s) WHERE id=%s", (raw, entity))
    await audit(db, user.id, f"{kind}.boundary_updated", entity)


@router.get("/map/boundaries/{kind}")
async def boundaries(
    kind: Literal["districts", "localities"], f: Annotated[PageQuery, Query()], db: DB
) -> Page[Boundary]:
    return Page[Boundary](
        **await paginated(
            db,
            "id,code,labels,ST_AsGeoJSON(geom,15)::jsonb AS geometry",
            kind,
            "geom IS NOT NULL",
            (),
            f,
            "code,id",
        )
    )


class PlaceVisibility(Model):
    is_public: bool


@router.put("/admin/places/{entity}/visibility", status_code=204)
async def place_visibility(entity: UUID, data: PlaceVisibility, user: Admin, db: DB):
    if not await one(
        db,
        """
        UPDATE places SET is_public=%s,verified_at=CASE WHEN %s THEN coalesce(verified_at,now()) END
        WHERE id=%s AND archived_at IS NULL RETURNING id
    """,
        (data.is_public, data.is_public, entity),
    ):
        raise HTTPException(404, "place not found")
    if not data.is_public:
        await detach_place(db, entity)
    await audit(db, user.id, "place.visibility_changed", entity, public=data.is_public)


async def detach_place(db: Conn, entity: UUID):
    await db.execute(
        """
      UPDATE creators c SET place_id=NULL,approved=c.approved || '{"place_id":null}'::jsonb,
        public_point=CASE WHEN c.accuracy='exact' THEN
          coalesce((SELECT centroid FROM localities WHERE id=c.locality_id),
                   (SELECT centroid FROM districts WHERE id=c.district_id)) ELSE c.public_point END,
        accuracy=CASE WHEN c.accuracy='exact' THEN
          CASE WHEN c.locality_id IS NOT NULL THEN 'locality' ELSE 'district' END ELSE c.accuracy END
      WHERE place_id=%s
    """,
        (entity,),
    )


@router.get("/analytics/gis-export", responses={200: {"content": {"application/geo+json": {}}}})
async def export_gis(f: Annotated[CatalogFilters, Query()], user: GeoExporter, db: DB) -> Response:
    where, params = catalog_where(f)
    rows = await many(
        db,
        f"""
        SELECT c.slug,c.accuracy,ST_AsGeoJSON({PUBLIC_POINT})::jsonb AS geometry
        FROM creators c WHERE {where} AND {PUBLIC_POINT} IS NOT NULL AND EXISTS (
            SELECT 1 FROM consents s WHERE s.user_id=c.user_id AND s.type='gis_export' AND s.withdrawn_at IS NULL
        ) ORDER BY c.slug LIMIT 10001
    """,
        params,
    )
    if len(rows) > 10000:
        raise HTTPException(422, "narrow your filters before export")
    await audit(db, user.id, "gis.exported", count=len(rows))
    import json

    data = {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": r["geometry"],
                "properties": {"slug": r["slug"], "accuracy": r["accuracy"]},
            }
            for r in rows
        ],
    }
    return Response(
        json.dumps(data), media_type="application/geo+json", headers={"Cache-Control": "private, no-store"}
    )


class MapEvent(Coordinates):
    id: UUID
    title: Localized
    starts_at: datetime
    ends_at: datetime


@router.get("/map/events")
async def event_markers(f: Annotated[PageQuery, Query()], db: DB) -> Page[MapEvent]:
    return Page[MapEvent](
        **await paginated(
            db,
            "e.id,e.fields->'title' AS title,e.starts_at,e.ends_at,ST_X(p.geo_point) AS longitude,"
            "ST_Y(p.geo_point) AS latitude",
            "events e JOIN places p ON p.id=e.place_id",
            "e.status='published' AND (e.publish_at IS NULL OR e.publish_at<=now()) AND p.is_public AND p.verified_at IS NOT NULL",
            (),
            f,
            "starts_at,id",
        )
    )


class RadiusFilters(CatalogFilters):
    longitude: float = Field(ge=-180, le=180)
    latitude: float = Field(ge=-85.05, le=85.05)
    radius_m: int = Field(ge=50, le=250000)


@router.get("/map/creators/nearby")
async def nearby_creators(f: Annotated[RadiusFilters, Query()], db: DB) -> Page[PublicCreator]:
    where, params = catalog_where(f)
    where += f""" AND c.accuracy='exact' AND {PUBLIC_POINT} IS NOT NULL
        AND ST_DWithin(c.public_point::geography,ST_SetSRID(ST_MakePoint(%s,%s),4326)::geography,%s)"""
    page = await paginated(
        db, CREATOR_COLUMNS, "creators c", where, params + (f.longitude, f.latitude, f.radius_m), f, "slug,id"
    )
    return Page[PublicCreator](**{**page, "items": [public_creator(row) for row in page["items"]]})


MapLayer = Literal["creators", "spaces", "workshops", "galleries", "events"]


class LayerFilters(MapFilters):
    layer: MapLayer = "creators"
    zoom: int = Field(12, ge=0, le=22)


class LayerPoint(Coordinates):
    id: UUID
    layer: MapLayer
    slug: str | None
    accuracy: Accuracy
    labels: Localized | None


def layer_source(f: LayerFilters) -> tuple[str, tuple]:
    if f.layer == "creators":
        source, params = marker_source(f)
        return f"(SELECT points.*,null::jsonb AS labels FROM {source}) layer_points", params
    bounds = map_bounds(f.bbox)
    where = ["p.is_public", "p.verified_at IS NOT NULL", "p.geo_point && ST_MakeEnvelope(%s,%s,%s,%s,4326)"]
    params = list(bounds)
    if f.locality_id:
        where.append("p.locality_id=%s")
        params.append(f.locality_id)
    if f.district_id:
        where.append("l.district_id=%s")
        params.append(f.district_id)
    if f.layer in ("workshops", "galleries"):
        where.append("p.kind=%s")
        params.append("workshop" if f.layer == "workshops" else "gallery")
    id_column, labels, source = "p.id", "p.labels", "places p JOIN localities l ON l.id=p.locality_id"
    if f.layer == "events":
        id_column, labels = "e.id", "e.fields->'title'"
        source += " JOIN events e ON e.place_id=p.id"
        where.extend(
            ("e.status='published'", "(e.publish_at IS NULL OR e.publish_at<=now())", "e.ends_at>=now()")
        )
    return (
        f"""(
        SELECT {id_column} AS id, null::text AS slug, 'exact' AS accuracy, {labels} AS labels,
               p.geo_point AS public_point, ST_SnapToGrid(ST_Transform(p.geo_point,3857),%s) AS cell
        FROM {source} WHERE {" AND ".join(where)}
    ) layer_points""",
        (marker_cell(f.zoom), *params),
    )


@router.get("/map/points")
async def map_points(f: Annotated[LayerFilters, Query()], db: DB) -> Page[LayerPoint]:
    source, params = layer_source(f)
    raw = await paginated(
        db,
        "id,slug,accuracy,labels,ST_X(public_point) AS longitude,ST_Y(public_point) AS latitude",
        source,
        "true",
        params,
        f,
        "id",
    )
    return Page[LayerPoint](**{**raw, "items": [LayerPoint(**row, layer=f.layer) for row in raw["items"]]})


@router.get("/map/clusters")
async def map_clusters(f: Annotated[LayerFilters, Query()], db: DB) -> MapResults:
    source, params = layer_source(f)
    grouped = await grouped_markers(db, f, source, params)
    if f.layer != "creators":
        # Layer object IDs are venue/event IDs. Only creator markers provide creator_id.
        for item in grouped.items:
            item.creator_id = None
    return grouped


class DensityCell(Coordinates):
    id: str
    count: int


@router.get("/map/heatmap")
async def heatmap(f: Annotated[MapFilters, Query()], user: GeoExporter, db: DB) -> Page[DensityCell]:
    source, params = marker_source(f)
    grouped = f"""(
        SELECT encode(ST_AsEWKB(cell),'hex') AS id,count(*) AS count,
            ST_Transform(cell,4326) AS center
        FROM {source} GROUP BY cell HAVING count(*)>=5
    ) density"""
    return Page[DensityCell](
        **await paginated(
            db,
            "id,count,ST_X(center) AS longitude,ST_Y(center) AS latitude",
            grouped,
            "true",
            params,
            f,
            "count DESC,id",
        )
    )
