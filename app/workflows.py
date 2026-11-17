from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from psycopg.types.json import Jsonb
from pydantic import Field

from app.auth import Admin, Authenticated
from app.db import DB, Conn, audit, many, one
from app.registry import latest, paginated, save_version
from app.schemas import Change, Id, Model, Page, PageQuery, ProfileInput, ReasonInput, Version

router = APIRouter(tags=["workflows"])


async def org_owner(db: Conn, org_id: UUID, user_id: UUID):
    if not await one(
        db,
        """
        SELECT o.id FROM organizations o JOIN organization_members m ON m.org_id=o.id
        WHERE o.id=%s AND o.verification_state='approved' AND m.user_id=%s
        AND m.role='owner' AND m.verified_at IS NOT NULL FOR UPDATE OF o
    """,
        (org_id, user_id),
    ):
        raise HTTPException(404, "verified organization membership required")


class Invite(Model):
    user_id: UUID


class Invitation(Model):
    id: UUID
    org_id: UUID
    invited_by: UUID
    created_at: datetime
    resolved_at: datetime | None
    accepted: bool | None


@router.post("/me/organizations/{org_id}/invitations", status_code=201)
async def invite(org_id: UUID, data: Invite, user: Authenticated, db: DB) -> Id:
    await org_owner(db, org_id, user.id)
    if not await one(
        db,
        "SELECT id FROM users WHERE id=%s AND verified_at IS NOT NULL AND disabled_at IS NULL",
        (data.user_id,),
    ):
        raise HTTPException(404, "active invitee not found")
    if await one(
        db, "SELECT user_id FROM organization_members WHERE org_id=%s AND user_id=%s", (org_id, data.user_id)
    ):
        raise HTTPException(409, "user is already a member")
    row = await one(
        db,
        """
        INSERT INTO organization_invitations(org_id,user_id,invited_by) VALUES (%s,%s,%s)
        ON CONFLICT(org_id,user_id) WHERE resolved_at IS NULL DO NOTHING RETURNING id
    """,
        (org_id, data.user_id, user.id),
    )
    if not row:
        raise HTTPException(409, "invitation already pending")
    await db.execute(
        """
        INSERT INTO notifications(user_id,template_id,locale,context)
        SELECT id,'organization.invited',locale,%s FROM users WHERE id=%s
    """,
        (Jsonb({"invitation_id": str(row["id"]), "organization_id": str(org_id)}), data.user_id),
    )
    return Id(**row)


@router.get("/me/organization-invitations")
async def invitations(f: Annotated[PageQuery, Query()], user: Authenticated, db: DB) -> Page[Invitation]:
    return Page[Invitation](
        **await paginated(
            db,
            ",".join(Invitation.model_fields),
            "organization_invitations",
            "user_id=%s",
            (user.id,),
            f,
            "created_at DESC,id",
        )
    )


@router.put("/me/organization-invitations/{entity}/{decision}", status_code=204)
async def answer(entity: UUID, decision: Literal["accept", "decline"], user: Authenticated, db: DB):
    row = await one(
        db,
        """
        UPDATE organization_invitations SET resolved_at=now(),accepted=%s
        WHERE id=%s AND user_id=%s AND resolved_at IS NULL RETURNING org_id
    """,
        (decision == "accept", entity, user.id),
    )
    if not row:
        raise HTTPException(404, "pending invitation not found")
    if decision == "accept":
        await db.execute(
            """
            INSERT INTO organization_members(org_id,user_id,role,verified_at)
            VALUES (%s,%s,'member',now()) ON CONFLICT DO NOTHING
        """,
            (row["org_id"], user.id),
        )


@router.delete("/me/organizations/{org_id}/members/{member}", status_code=204)
async def remove_member(org_id: UUID, member: UUID, user: Authenticated, db: DB):
    await org_owner(db, org_id, user.id)
    if not await one(
        db,
        """
        DELETE FROM organization_members WHERE org_id=%s AND user_id=%s AND role='member' RETURNING user_id
    """,
        (org_id, member),
    ):
        raise HTTPException(404, "team member not found")


class TeamMember(Model):
    user_id: UUID
    role: Literal["owner", "member"]
    verified_at: datetime | None


@router.get("/me/organizations/{org_id}/members")
async def team(
    org_id: UUID, f: Annotated[PageQuery, Query()], user: Authenticated, db: DB
) -> Page[TeamMember]:
    if not await one(
        db, "SELECT user_id FROM organization_members WHERE org_id=%s AND user_id=%s", (org_id, user.id)
    ):
        raise HTTPException(404, "organization membership required")
    return Page[TeamMember](
        **await paginated(
            db, "user_id,role,verified_at", "organization_members", "org_id=%s", (org_id,), f, "role,user_id"
        )
    )


class MergePair(Model):
    primary_id: UUID
    duplicate_id: UUID


MergeField = Literal["description", "skills", "experience", "services", "links", "category_ids", "oked_ids"]


class MergeRequest(MergePair, ReasonInput):
    copy_fields: list[MergeField] = Field(default_factory=list, max_length=7)


class MergePreview(Model):
    primary_id: UUID
    duplicate_id: UUID
    differences: dict[str, Change]


FIELDS = ("description", "skills", "experience", "services", "links", "category_ids", "oked_ids")


async def duplicate_pair(db: Conn, data: MergePair, lock: bool = False):
    rows = await many(
        db,
        "SELECT * FROM creators WHERE id=ANY(%s) ORDER BY id" + (" FOR UPDATE" if lock else ""),
        ([data.primary_id, data.duplicate_id],),
    )
    profiles = {row["id"]: row for row in rows}
    if len(profiles) != 2 or any(r["publication_status"] == "archived" for r in rows):
        raise HTTPException(404, "two distinct active profiles required")
    primary, duplicate = profiles[data.primary_id], profiles[data.duplicate_id]
    if not any(primary[k] and primary[k] == duplicate[k] for k in ("email", "phone")):
        raise HTTPException(409, "matching normalized contact required")
    return primary, duplicate


@router.post("/admin/creator-merges/preview")
async def preview_merge(data: MergePair, user: Admin, db: DB) -> MergePreview:
    primary, duplicate = await duplicate_pair(db, data)
    values = [(await latest(db, "creator", r["id"], r["id"]))["payload"] for r in (primary, duplicate)]
    return MergePreview(
        **data.model_dump(),
        differences={
            key: Change(before=values[0].get(key), after=values[1].get(key))
            for key in FIELDS
            if values[0].get(key) != values[1].get(key)
        },
    )


@router.post("/admin/creator-merges")
async def merge(data: MergeRequest, user: Admin, db: DB) -> Version:
    primary, duplicate = await duplicate_pair(db, data, lock=True)
    winner = await latest(db, "creator", primary["id"], primary["id"])
    secondary = await latest(db, "creator", duplicate["id"], duplicate["id"])
    if "pending_review" in (winner["status"], secondary["status"]):
        raise HTTPException(409, "resolve pending profile reviews before merging")
    changes = {key: secondary["payload"][key] for key in data.copy_fields}
    payload = ProfileInput.model_validate({**winner["payload"], **changes})
    result = await save_version(
        db,
        "creator",
        primary,
        primary,
        payload,
        primary["user_id"],
        submit=bool(primary["approved"]) and bool(changes),
        submitted_by=user.id,
    )
    await db.execute(
        "UPDATE creators SET publication_status='archived',merged_into=%s WHERE id=%s",
        (primary["id"], duplicate["id"]),
    )
    await audit(
        db,
        user.id,
        "creator.merged",
        primary["id"],
        reason=data.reason,
        duplicate_id=str(duplicate["id"]),
        fields=data.copy_fields,
        version_id=str(result.id),
    )
    return result


@router.get("/me/data-requests/{request_id}/download")
async def personal_export(request_id: UUID, user: Authenticated, db: DB) -> JSONResponse:
    if not await one(
        db,
        """
        SELECT id FROM profile_requests WHERE id=%s AND user_id=%s AND kind='export'
          AND processed_at IS NOT NULL
    """,
        (request_id, user.id),
    ):
        raise HTTPException(404, "completed export request not found")
    row = await one(
        db,
        """
        WITH subject AS (SELECT * FROM creators WHERE user_id=%s)
        SELECT jsonb_build_object(
          'account',(SELECT to_jsonb(u)-'password_hash'-'auth_version' FROM users u WHERE id=%s),
          'roles',(SELECT coalesce(jsonb_agg(to_jsonb(r)),'[]') FROM user_roles r WHERE user_id=%s),
          'profile',(SELECT to_jsonb(c)-'public_point'-'search_text' FROM subject c),
          'versions',(SELECT coalesce(jsonb_agg(to_jsonb(v)-'reviewer_id'-'submitted_by'),'[]')
                     FROM creator_versions v JOIN subject c ON c.id=v.creator_id),
          'media',(SELECT coalesce(jsonb_agg(to_jsonb(m)-'path'-'variants'),'[]')
                   FROM media_assets m WHERE owner_id=%s),
          'organization_memberships',(SELECT coalesce(jsonb_agg(to_jsonb(m)),'[]')
                                      FROM organization_members m WHERE user_id=%s),
          'residencies',(SELECT coalesce(jsonb_agg(to_jsonb(r)-'curator_id'-'admin_id'),'[]')
                          FROM residencies r JOIN subject c ON r.creator_id=c.id),
          'consents',(SELECT coalesce(jsonb_agg(to_jsonb(a)),'[]') FROM consents a WHERE user_id=%s),
          'requests',(SELECT coalesce(jsonb_agg(to_jsonb(p)),'[]') FROM profile_requests p WHERE user_id=%s)
        ) AS data
    """,
        (user.id,) * 7,
    )
    return JSONResponse(
        row["data"],
        headers={
            "Cache-Control": "private, no-store",
            "Content-Disposition": 'attachment; filename="creative-shygys-data.json"',
        },
    )
