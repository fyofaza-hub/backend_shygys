from datetime import date, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from psycopg.types.json import Jsonb
from pydantic import Field, model_validator

from app.auth import Actor, Admin, Creator, require, throttle
from app.db import DB, audit, one
from app.registry import latest, owner_profile, paginated, save_version
from app.schemas import (
    Id,
    LegalStatus,
    Localized,
    Model,
    Page,
    PageQuery,
    ProfileInput,
    ReasonInput,
    Residency,
    Role,
    Version,
)

router = APIRouter(tags=["administration"])
Superadmin = Annotated[Actor, Depends(require(Role.superadmin))]


class RoleGrant(Model):
    role: Role
    scope_id: UUID | None = None

    @model_validator(mode="after")
    def scope(self):
        if (self.role == Role.center_curator) != (self.scope_id is not None):
            raise ValueError("center_curator requires a center scope; other roles are global")
        return self


@router.put("/admin/users/{user_id}/roles", status_code=204)
async def grant(user_id: UUID, data: RoleGrant, user: Superadmin, db: DB):
    if data.scope_id and not await one(
        db, "SELECT id FROM places WHERE id=%s AND kind='center'", (data.scope_id,)
    ):
        raise HTTPException(422, "unknown center")
    await db.execute(
        "INSERT INTO user_roles(user_id,role,scope_id) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
        (user_id, data.role, data.scope_id),
    )
    await audit(
        db,
        user.id,
        "role.granted",
        user_id,
        role=data.role,
        scope=str(data.scope_id) if data.scope_id else None,
    )


@router.delete("/admin/users/{user_id}/roles", status_code=204)
async def revoke(user_id: UUID, data: RoleGrant, user: Superadmin, db: DB):
    if user_id == user.id and data.role == Role.superadmin:
        raise HTTPException(409, "cannot revoke own superadmin role")
    await db.execute(
        "DELETE FROM user_roles WHERE user_id=%s AND role=%s AND scope_id IS NOT DISTINCT FROM %s",
        (user_id, data.role, data.scope_id),
    )
    await audit(
        db,
        user.id,
        "role.revoked",
        user_id,
        role=data.role,
        scope=str(data.scope_id) if data.scope_id else None,
    )


class AuditEvent(Model):
    id: UUID
    actor_id: UUID | None
    action: str
    entity_id: UUID | None
    context: dict
    created_at: datetime


@router.get("/admin/audit")
async def audit_history(f: Annotated[PageQuery, Query()], user: Superadmin, db: DB) -> Page[AuditEvent]:
    return Page[AuditEvent](
        **await paginated(
            db, ",".join(AuditEvent.model_fields), "audit_events", "true", (), f, "created_at DESC,id"
        )
    )


class ResidencyRequest(Model):
    center_id: UUID
    starts_on: date
    ends_on: date

    @model_validator(mode="after")
    def date_order(self):
        if self.ends_on < self.starts_on:
            raise ValueError("invalid residency period")
        return self


class ResidencyItem(ResidencyRequest):
    id: UUID
    creator_id: UUID | None
    org_id: UUID | None
    status: Residency
    curator_id: UUID | None
    admin_id: UUID | None


@router.post("/me/residencies", status_code=201)
async def apply_residency(data: ResidencyRequest, request: Request, user: Creator, db: DB) -> ResidencyItem:
    await throttle(request, "residency", 6, 3600, str(user.id))
    creator = await owner_profile(db, user.id, lock=True)
    if not await one(
        db,
        "SELECT id FROM places WHERE id=%s AND kind='center' AND is_public AND verified_at IS NOT NULL",
        (data.center_id,),
    ):
        raise HTTPException(422, "unknown public center")
    if await one(
        db,
        "SELECT id FROM residencies WHERE creator_id=%s AND center_id=%s AND status='pending'",
        (creator["id"], data.center_id),
    ):
        raise HTTPException(409, "residency request already exists")
    return ResidencyItem(
        **await one(
            db,
            """
        INSERT INTO residencies(creator_id,center_id,starts_on,ends_on) VALUES (%s,%s,%s,%s) RETURNING *
    """,
            (creator["id"], data.center_id, data.starts_on, data.ends_on),
        )
    )


@router.get("/me/residencies")
async def my_residencies(f: Annotated[PageQuery, Query()], user: Creator, db: DB) -> Page[ResidencyItem]:
    c = await owner_profile(db, user.id)
    return Page[ResidencyItem](
        **await paginated(db, "*", "residencies", "creator_id=%s", (c["id"],), f, "starts_on DESC,id")
    )


@router.post("/center/residencies/{entity}/recommend", status_code=204)
async def recommend(entity: UUID, user: Annotated[Actor, Depends(require(Role.center_curator))], db: DB):
    row = await one(db, "SELECT * FROM residencies WHERE id=%s AND status='pending' FOR UPDATE", (entity,))
    if not row or row["center_id"] not in user.scopes.get(Role.center_curator, set()):
        raise HTTPException(404, "pending residency not found for your center")
    await db.execute("UPDATE residencies SET curator_id=%s WHERE id=%s", (user.id, entity))
    await audit(db, user.id, "residency.recommended", entity)


@router.post("/admin/residencies/{entity}/confirm", status_code=204)
async def confirm(entity: UUID, user: Admin, db: DB):
    if not await one(
        db,
        """
        UPDATE residencies SET status='active',admin_id=%s WHERE id=%s AND status='pending'
        AND curator_id IS NOT NULL AND curator_id<>%s
        AND ends_on>=(now() AT TIME ZONE 'Asia/Almaty')::date RETURNING id
    """,
        (user.id, entity, user.id),
    ):
        raise HTTPException(409, "separate curator recommendation and valid dates required")
    await audit(db, user.id, "residency.confirmed", entity)


@router.post("/admin/residencies/{entity}/revoke", status_code=204)
async def revoke_residency(entity: UUID, data: ReasonInput, user: Admin, db: DB):
    if not await one(
        db, "UPDATE residencies SET status='revoked' WHERE id=%s AND status='active' RETURNING id", (entity,)
    ):
        raise HTTPException(409, "no active residency")
    await audit(db, user.id, "residency.revoked", entity, reason=data.reason)


class OrganizationInput(Model):
    legal_type: Literal["sole_proprietor", "legal_entity"]
    display_name: Localized


class Organization(OrganizationInput):
    id: UUID
    verification_state: Literal["pending_review", "approved", "rejected"]


@router.post("/me/organizations", status_code=201)
async def create_org(data: OrganizationInput, request: Request, user: Creator, db: DB) -> Organization:
    await throttle(request, "organization", 6, 3600, str(user.id))
    row = await one(
        db,
        """
        INSERT INTO organizations(legal_type,display_name) VALUES (%s,%s)
        RETURNING id,legal_type,display_name,verification_state
    """,
        (data.legal_type, Jsonb(data.display_name.model_dump())),
    )
    await db.execute(
        "INSERT INTO organization_members(org_id,user_id,role) VALUES (%s,%s,'owner')", (row["id"], user.id)
    )
    return Organization(**row)


@router.get("/me/organizations")
async def my_organizations(f: Annotated[PageQuery, Query()], user: Creator, db: DB) -> Page[Organization]:
    return Page[Organization](
        **await paginated(
            db,
            "o.id,o.legal_type,o.display_name,o.verification_state",
            "organizations o JOIN organization_members m ON m.org_id=o.id",
            "m.user_id=%s",
            (user.id,),
            f,
            "id",
        )
    )


@router.post("/admin/organizations/{entity}/confirm", status_code=204)
async def confirm_org(entity: UUID, user: Admin, db: DB):
    if not await one(
        db, "UPDATE organizations SET verification_state='approved' WHERE id=%s RETURNING id", (entity,)
    ):
        raise HTTPException(404, "organization not found")
    await db.execute(
        "UPDATE organization_members SET verified_at=now() WHERE org_id=%s AND role='owner'", (entity,)
    )
    await db.execute(
        """
        INSERT INTO user_roles(user_id,role) SELECT user_id,'org_rep' FROM organization_members
        WHERE org_id=%s AND role='owner' ON CONFLICT DO NOTHING
    """,
        (entity,),
    )
    await audit(db, user.id, "organization.confirmed", entity)


@router.post("/admin/creators/{entity}/verify-legal-status", status_code=204)
async def verify_legal_status(entity: UUID, user: Admin, db: DB):
    if not await one(
        db,
        """
        UPDATE creators SET legal_verified=true WHERE id=%s AND approved IS NOT NULL
        AND legal_status<>%s RETURNING id
    """,
        (entity, LegalStatus.individual),
    ):
        raise HTTPException(409, "no approved business profile to verify")
    await audit(db, user.id, "creator.legal_status_verified", entity)


class CategoryInput(Model):
    code: str = Field(min_length=1, max_length=64)
    labels: Localized


@router.post("/admin/references/categories", status_code=201)
async def category(data: CategoryInput, user: Admin, db: DB) -> Id:
    row = await one(
        db,
        "INSERT INTO creative_categories(code,labels) VALUES (%s,%s) RETURNING id",
        (data.code, Jsonb(data.labels.model_dump())),
    )
    await audit(db, user.id, "category.created", row["id"])
    return Id(**row)


class CenterFilters(PageQuery):
    center_id: UUID | None = None
    status: Residency | None = None


@router.get("/center/residencies")
async def center_queue(
    f: Annotated[CenterFilters, Query()],
    user: Annotated[Actor, Depends(require(Role.center_curator, Role.admin, Role.superadmin))],
    db: DB,
) -> Page[ResidencyItem]:
    ids = list(user.scopes.get(Role.center_curator, set()))
    where, params = [], []
    if not {"admin", "superadmin"} & set(user.roles):
        where.append("center_id=ANY(%s::uuid[])")
        params.append(ids)
    if f.center_id is not None:
        where.append("center_id=%s")
        params.append(f.center_id)
    if f.status is not None:
        where.append("status=%s")
        params.append(f.status)
    return Page[ResidencyItem](
        **await paginated(
            db, "*", "residencies", " AND ".join(where) or "true", tuple(params), f, "starts_on DESC,id"
        )
    )


class UserFilters(PageQuery):
    role: Role | None = None
    disabled: bool | None = None


class StaffUser(Model):
    id: UUID
    email: str
    locale: str
    verified_at: datetime | None
    disabled_at: datetime | None
    roles: list[Role]


@router.get("/admin/users")
async def users(f: Annotated[UserFilters, Query()], user: Admin, db: DB) -> Page[StaffUser]:
    where, params = ["true"], []
    if f.role:
        where.append("EXISTS(SELECT 1 FROM user_roles r WHERE r.user_id=u.id AND r.role=%s)")
        params.append(f.role)
    if f.disabled is not None:
        where.append("(u.disabled_at IS NOT NULL)=%s")
        params.append(f.disabled)
    return Page[StaffUser](
        **await paginated(
            db,
            """
        u.id,u.email,u.locale,u.verified_at,u.disabled_at,
        ARRAY(SELECT DISTINCT r.role FROM user_roles r WHERE r.user_id=u.id ORDER BY r.role) AS roles
    """,
            "users u",
            " AND ".join(where),
            tuple(params),
            f,
            "id",
        )
    )


class AvatarUpdate(ReasonInput):
    avatar_id: UUID


@router.put("/admin/creators/{entity}/avatar", status_code=202)
async def replace_avatar(entity: UUID, data: AvatarUpdate, user: Admin, db: DB) -> Version:
    row = await one(db, "SELECT * FROM creators WHERE id=%s FOR UPDATE", (entity,))
    if not row:
        raise HTTPException(404, "profile not found")
    current = await latest(db, "creator", entity, entity)
    profile = ProfileInput.model_validate({**current["payload"], "avatar_id": data.avatar_id})
    version = await save_version(
        db,
        "creator",
        row,
        row,
        profile,
        row["user_id"],
        submit=row["approved"] is not None,
        submitted_by=user.id,
    )
    await audit(db, user.id, "creator.avatar_changed", entity, reason=data.reason, version_id=str(version.id))
    return version


class PrivacyRequest(Model):
    id: UUID
    user_id: UUID
    kind: Literal["export", "close"]
    created_at: datetime
    processed_at: datetime | None


class PrivacyFilters(PageQuery):
    kind: Literal["export", "close"] | None = None
    pending: bool = True


@router.get("/admin/data-requests")
async def privacy_queue(f: Annotated[PrivacyFilters, Query()], user: Admin, db: DB) -> Page[PrivacyRequest]:
    where, params = ["true"], []
    if f.pending:
        where.append("processed_at IS NULL")
    if f.kind:
        where.append("kind=%s")
        params.append(f.kind)
    return Page[PrivacyRequest](
        **await paginated(
            db,
            ",".join(PrivacyRequest.model_fields),
            "profile_requests",
            " AND ".join(where),
            tuple(params),
            f,
            "created_at,id",
        )
    )


@router.post("/admin/data-requests/{entity}/complete", status_code=204)
async def resolve_privacy_request(entity: UUID, data: ReasonInput, user: Admin, db: DB):
    record = await one(
        db, "SELECT * FROM profile_requests WHERE id=%s AND processed_at IS NULL FOR UPDATE", (entity,)
    )
    if not record:
        raise HTTPException(404, "pending request not found")
    if record["kind"] == "close":
        if record["user_id"] == user.id:
            raise HTTPException(409, "cannot close own staff account")
        if Role.superadmin not in user.roles and await one(
            db,
            """
            SELECT id FROM user_roles WHERE user_id=%s AND role IN ('admin','superadmin')
        """,
            (record["user_id"],),
        ):
            raise HTTPException(403, "cannot close an administrator account")
        await db.execute(
            "UPDATE users SET disabled_at=now(),auth_version=auth_version+1 WHERE id=%s", (record["user_id"],)
        )
        await db.execute(
            "UPDATE creators SET publication_status='hidden' WHERE user_id=%s", (record["user_id"],)
        )
        await db.execute(
            "UPDATE consents SET withdrawn_at=now() WHERE user_id=%s AND withdrawn_at IS NULL",
            (record["user_id"],),
        )
    await db.execute("UPDATE profile_requests SET processed_at=now() WHERE id=%s", (entity,))
    await audit(db, user.id, "user.data_request_completed", entity, kind=record["kind"], reason=data.reason)


class StaffOverview(Model):
    pending_reviews: int
    pending_residencies: int
    pending_privacy_requests: int
    failed_translations: int


@router.get("/admin/overview")
async def admin_overview(user: Admin, db: DB) -> StaffOverview:
    return StaffOverview(
        **await one(
            db,
            """
      SELECT (SELECT count(*) FROM moderation_cases WHERE decision='pending_review') AS pending_reviews,
             (SELECT count(*) FROM residencies WHERE status='pending') AS pending_residencies,
             (SELECT count(*) FROM profile_requests WHERE processed_at IS NULL) AS pending_privacy_requests,
             (SELECT count(*) FROM translation_jobs WHERE status='failed') AS failed_translations
    """,
        )
    )


@router.post("/center/residencies/{entity}/revoke", status_code=204)
async def revoke_center_residency(
    entity: UUID, data: ReasonInput, user: Annotated[Actor, Depends(require(Role.center_curator))], db: DB
):
    scopes = list(user.scopes.get(Role.center_curator, set()))
    if not await one(
        db,
        """
        UPDATE residencies SET status='revoked' WHERE id=%s
        AND center_id=ANY(%s::uuid[]) AND status='active' RETURNING id
    """,
        (entity, scopes),
    ):
        raise HTTPException(404, "active residency not found for your center")
    await audit(db, user.id, "residency.revoked_by_center", entity, reason=data.reason)
