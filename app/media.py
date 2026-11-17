import io
from datetime import datetime
from pathlib import PurePosixPath
from typing import Annotated, Literal
from uuid import UUID, uuid4

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from PIL import Image, ImageOps, UnidentifiedImageError
from starlette.concurrency import run_in_threadpool

from app.antivirus import MalwareFound, scan_image
from app.auth import Actor, Admin, Authenticated, throttle
from app.config import settings
from app.db import DB, Conn, audit, one, worker_db
from app.registry import ITEM_PUBLIC, PUBLIC
from app.schemas import Model

router = APIRouter(tags=["media"])
FORMAT_MIME = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
MAX_PIXELS = 24_000_000


class MediaVariants(Model):
    original: str
    thumbnail: str
    medium: str


class Media(Model):
    id: UUID
    mime: str
    bytes: int
    status: Literal["pending", "ready", "failed"]
    created_at: datetime
    rights: str | None = None
    width: int | None = None
    height: int | None = None
    variants: MediaVariants


MEDIA_COLUMNS = ",".join(key for key in Media.model_fields if key != "variants")


def media_response(row: dict, creator: UUID | None = None) -> Media:
    base = f"/api/v1/me/media/{row['id']}"
    if creator:
        base = f"/api/v1/admin/creators/{creator}/media/{row['id']}"
    return Media(
        **row,
        variants=MediaVariants(original=f"{base}/original", thumbnail=f"{base}/320", medium=f"{base}/960"),
    )


def s3():
    if (
        not settings.s3_endpoint
        or not settings.s3_access_key
        or not settings.s3_secret_key.get_secret_value()
    ):
        raise HTTPException(503, "media storage unavailable")
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
        region_name=settings.s3_region,
        config=Config(
            signature_version="s3v4", connect_timeout=5, read_timeout=30, s3={"addressing_style": "path"}
        ),
    )


def check_image(data: bytes, mime: str | None) -> tuple[int, int]:
    with Image.open(io.BytesIO(data)) as image:
        if FORMAT_MIME.get(image.format) != mime or mime not in FORMAT_MIME.values():
            raise ValueError("invalid image MIME")
        if image.width * image.height > MAX_PIXELS or getattr(image, "n_frames", 1) != 1:
            raise ValueError("unsupported dimensions or animation")
        size = image.size
        image.verify()
    # Decode all pixels as well: a valid header alone is insufficient.
    with Image.open(io.BytesIO(data)) as decoded:
        decoded.load()
    return size


@router.post("/media/upload", status_code=202)
@router.post("/me/media", status_code=202)
async def upload(
    file: Annotated[UploadFile, File()],
    request: Request,
    user: Authenticated,
    db: DB,
    rights: Annotated[str | None, Form(max_length=1000)] = None,
) -> Media:
    if not {"creator", "org_rep", "content_editor", "moderator", "admin", "superadmin"} & set(user.roles):
        raise HTTPException(403, "media upload is unavailable for this role")
    return await store_image(file, request, user, db, user.id, rights)


@router.post("/admin/creators/{entity}/media", status_code=202)
async def upload_for_creator(
    entity: UUID,
    file: Annotated[UploadFile, File()],
    request: Request,
    user: Admin,
    db: DB,
    rights: Annotated[str | None, Form(max_length=1000)] = None,
) -> Media:
    creator = await one(db, "SELECT user_id FROM creators WHERE id=%s", (entity,))
    if not creator:
        raise HTTPException(404, "profile not found")
    saved = await store_image(file, request, user, db, creator["user_id"], rights)
    return media_response(saved.model_dump(exclude={"variants"}), entity)


@router.get("/admin/creators/{entity}/media/{asset_id}")
async def delegated_media_status(entity: UUID, asset_id: UUID, user: Admin, db: DB) -> Media:
    cols = ",".join(f"a.{key}" for key in MEDIA_COLUMNS.split(","))
    row = await one(
        db,
        f"""SELECT {cols} FROM media_assets a JOIN creators c ON c.user_id=a.owner_id
                           WHERE a.id=%s AND c.id=%s""",
        (asset_id, entity),
    )
    if not row:
        raise HTTPException(404, "image not found")
    return media_response(row, entity)


async def store_image(
    file: UploadFile, request: Request, user: Actor, db: Conn, owner_id: UUID, rights: str | None
) -> Media:
    await throttle(request, "upload", 40, 3600, str(user.id))
    # Allowlist every suffix: reject double extensions such as shell.php.jpg.
    name = (file.filename or "").replace("\\", "/")
    suffixes = PurePosixPath(name).suffixes
    if not suffixes or any(suffix.lower() not in ALLOWED_EXTENSIONS for suffix in suffixes):
        raise HTTPException(422, "unsupported file extension")
    data = await file.read(settings.media_max_bytes + 1)
    if not data or len(data) > settings.media_max_bytes:
        raise HTTPException(413, "invalid image size")
    try:
        width, height = await run_in_threadpool(check_image, data, file.content_type)
    except (OSError, ValueError, UnidentifiedImageError, Image.DecompressionBombError):
        raise HTTPException(422, "invalid or unsupported image") from None
    try:
        await run_in_threadpool(scan_image, data)
    except MalwareFound:
        raise HTTPException(422, "file rejected by media scanner") from None
    except OSError:
        raise HTTPException(503, "media scanner unavailable") from None
    media_id = uuid4()
    path = f"originals/{owner_id}/{media_id}"
    row = await one(
        db,
        f"""
        INSERT INTO media_assets(id,owner_id,mime,path,bytes,rights,width,height) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
        RETURNING {MEDIA_COLUMNS}
    """,
        (media_id, owner_id, file.content_type, path, len(data), rights, width, height),
    )
    try:
        await run_in_threadpool(
            lambda: s3().put_object(
                Bucket=settings.s3_bucket, Key=path, Body=data, ContentType=file.content_type
            )
        )
    except (ClientError, BotoCoreError):
        raise HTTPException(503, "media upload failed") from None
    if {"content_editor", "moderator", "admin", "superadmin"} & set(user.roles):
        await audit(db, user.id, "media.uploaded", media_id, owner_id=str(owner_id))
    return media_response(row)


@router.get("/me/media/{asset_id}")
async def media_status(asset_id: UUID, user: Authenticated, db: DB) -> Media:
    row = await one(
        db, f"SELECT {MEDIA_COLUMNS} FROM media_assets WHERE id=%s AND owner_id=%s", (asset_id, user.id)
    )
    if not row:
        raise HTTPException(404, "asset not found")
    return media_response(row)


def previews(row: dict):
    client = s3()
    bucket = settings.s3_bucket
    body = client.get_object(Bucket=bucket, Key=row["path"])["Body"]
    try:
        data = body.read(settings.media_max_bytes + 1)
    finally:
        body.close()
    if len(data) > settings.media_max_bytes:
        raise ValueError("original exceeds limit")
    check_image(data, row["mime"])
    with Image.open(io.BytesIO(data)) as image:
        oriented = ImageOps.exif_transpose(image)
        clean = oriented.convert(
            "RGBA" if "A" in oriented.getbands() or "transparency" in oriented.info else "RGB"
        )
        result = {}
        for size in (320, 960):
            preview = clean.copy()
            preview.thumbnail((size, size), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            preview.save(output, "WEBP", quality=82, method=5)
            path = f"previews/{row['id']}/{size}.webp"
            client.put_object(Bucket=bucket, Key=path, Body=output.getvalue(), ContentType="image/webp")
            result[str(size)] = path
        return result


async def process_media(ctx, asset_id: str):
    from psycopg.types.json import Jsonb

    async with worker_db() as db:
        row = await one(
            db,
            "SELECT * FROM media_assets WHERE id=%s AND status='pending' FOR UPDATE SKIP LOCKED",
            (UUID(asset_id),),
        )
        if not row:
            return
        try:
            variants = await run_in_threadpool(previews, row)
        except (OSError, ValueError, Image.DecompressionBombError):
            await db.execute("UPDATE media_assets SET status='failed' WHERE id=%s", (row["id"],))
            return
        except (ClientError, BotoCoreError, HTTPException):
            await db.execute(
                """
                UPDATE media_assets SET attempts=attempts+1,
                  status=CASE WHEN attempts>=4 THEN 'failed' ELSE status END,
                  retry_at=now()+make_interval(secs=>30*(attempts+1))
                WHERE id=%s
            """,
                (row["id"],),
            )
            return
        await db.execute(
            "UPDATE media_assets SET status='ready',variants=%s WHERE id=%s", (Jsonb(variants), row["id"])
        )


@router.get("/media/{asset_id}/{size}", responses={200: {"content": {"image/webp": {}}}})
async def public_preview(asset_id: UUID, size: Literal["320", "960"], db: DB):
    row = await one(
        db,
        f"""
        SELECT variants FROM media_assets a WHERE a.id=%s AND a.status='ready' AND (
          EXISTS(SELECT 1 FROM creators c WHERE {PUBLIC} AND a.id::text IN (c.approved->>'avatar_id',c.approved->>'cover_id'))
          OR EXISTS(SELECT 1 FROM portfolio_items i JOIN creators c ON c.id=i.creator_id
                    WHERE {PUBLIC} AND {ITEM_PUBLIC}
                      AND ((i.approved->'media_ids') ? a.id::text OR i.approved->>'cover_id'=a.id::text))
          OR EXISTS(SELECT 1 FROM projects i JOIN creators c ON c.id=i.owner_creator_id
                    WHERE {PUBLIC} AND {ITEM_PUBLIC}
                      AND ((i.approved->'media_ids') ? a.id::text OR i.approved->>'cover_id'=a.id::text))
          OR EXISTS(SELECT 1 FROM articles
                    WHERE status='published' AND published_at<=now() AND ((approved->'media_ids') ? a.id::text OR approved->>'cover_id'=a.id::text))
        )
    """,
        (asset_id,),
    )
    if not row or not row["variants"].get(size):
        raise HTTPException(404, "image not found")
    return await preview_response(row["variants"][size])


async def private_media(db: Conn, asset_id: UUID, size: str, owner_id: UUID):
    row = await one(
        db,
        "SELECT variants,path,mime,status FROM media_assets WHERE id=%s AND owner_id=%s",
        (asset_id, owner_id),
    )
    if not row:
        raise HTTPException(404, "image not found")
    if size == "original":
        return await preview_response(row["path"], row["mime"])
    if row["status"] != "ready" or not row["variants"].get(size):
        raise HTTPException(404, "preview not ready")
    return await preview_response(row["variants"][size])


@router.get(
    "/me/media/{asset_id}/{size}",
    response_class=StreamingResponse,
    responses={200: {"content": {mime: {} for mime in FORMAT_MIME.values()}}},
)
async def owner_preview(asset_id: UUID, size: Literal["320", "960", "original"], user: Authenticated, db: DB):
    return await private_media(db, asset_id, size, user.id)


@router.get(
    "/admin/creators/{entity}/media/{asset_id}/{size}",
    response_class=StreamingResponse,
    responses={200: {"content": {mime: {} for mime in FORMAT_MIME.values()}}},
)
async def delegated_preview(
    entity: UUID, asset_id: UUID, size: Literal["320", "960", "original"], user: Admin, db: DB
):
    creator = await one(db, "SELECT user_id FROM creators WHERE id=%s", (entity,))
    if not creator:
        raise HTTPException(404, "profile not found")
    return await private_media(db, asset_id, size, creator["user_id"])


async def preview_response(path: str, mime: str = "image/webp"):
    try:
        obj = await run_in_threadpool(lambda: s3().get_object(Bucket=settings.s3_bucket, Key=path))
    except (BotoCoreError, ClientError):
        raise HTTPException(503, "media storage unavailable") from None

    def chunks():
        stream = obj["Body"]
        try:
            yield from stream.iter_chunks(chunk_size=65536)
        finally:
            stream.close()

    return StreamingResponse(
        chunks(),
        media_type=mime,
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )
