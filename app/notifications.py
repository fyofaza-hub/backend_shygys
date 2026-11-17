from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query

from app.auth import Authenticated
from app.db import DB, one
from app.registry import paginated
from app.schemas import Locale, Model, Page, PageQuery

router = APIRouter(tags=["notifications"])


class Notification(Model):
    id: UUID
    template_id: str
    locale: Locale
    context: dict
    read_at: datetime | None
    created_at: datetime


class NotificationQuery(PageQuery):
    unread: bool = False


@router.get("/me/notifications")
async def notifications(
    f: Annotated[NotificationQuery, Query()], user: Authenticated, db: DB
) -> Page[Notification]:
    # SMTP delivery records contain single-use credentials and are never exposed in this feed.
    where = "user_id=%s AND left(template_id,6)<>'email:'"
    if f.unread:
        where += " AND read_at IS NULL"
    return Page[Notification](
        **await paginated(
            db,
            ",".join(Notification.model_fields),
            "notifications",
            where,
            (user.id,),
            f,
            "created_at DESC,id",
        )
    )


@router.put("/me/notifications/{entity}/read", status_code=204)
async def read_notification(entity: UUID, user: Authenticated, db: DB):
    if not await one(
        db,
        """
        UPDATE notifications SET read_at=coalesce(read_at,now())
        WHERE id=%s AND user_id=%s AND left(template_id,6)<>'email:' RETURNING id
    """,
        (entity, user.id),
    ):
        raise HTTPException(404, "notification not found")


class SubscriptionInput(Model):
    category_id: UUID
    kind: Literal["events", "opportunities"]


@router.put("/me/subscriptions", status_code=204)
async def subscribe(data: SubscriptionInput, user: Authenticated, db: DB):
    await db.execute(
        """
      INSERT INTO notification_subscriptions(user_id,category_id,kind) VALUES(%s,%s,%s)
      ON CONFLICT DO NOTHING
    """,
        (user.id, data.category_id, data.kind),
    )


@router.get("/me/subscriptions")
async def subscriptions(
    f: Annotated[PageQuery, Query()], user: Authenticated, db: DB
) -> Page[SubscriptionInput]:
    return Page[SubscriptionInput](
        **await paginated(
            db,
            "category_id,kind",
            "notification_subscriptions",
            "user_id=%s",
            (user.id,),
            f,
            "kind,category_id",
        )
    )


@router.delete("/me/subscriptions/{kind}/{category_id}", status_code=204)
async def unsubscribe(
    kind: Literal["events", "opportunities"], category_id: UUID, user: Authenticated, db: DB
):
    await db.execute(
        "DELETE FROM notification_subscriptions WHERE user_id=%s AND kind=%s AND category_id=%s",
        (user.id, kind, category_id),
    )
