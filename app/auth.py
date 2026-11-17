import hashlib
from datetime import UTC, datetime, timedelta
from secrets import token_urlsafe
from typing import Annotated, Literal
from uuid import UUID

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pwdlib import PasswordHash
from redis.exceptions import RedisError
from starlette.concurrency import run_in_threadpool

from app.config import settings
from app.db import DB, Conn, audit, many, one
from app.schemas import (
    AccessToken,
    ConsentInput,
    ConsentType,
    Email,
    EmailToken,
    Id,
    LocaleInput,
    Login,
    Registration,
    Reset,
    Role,
    User,
)

router = APIRouter(tags=["auth"])
bearer = HTTPBearer(auto_error=False)
passwords = PasswordHash.recommended()
dummy_hash = passwords.hash(token_urlsafe(32))
STAFF = {Role.admin, Role.superadmin}


class Actor(User):
    scopes: dict[Role, set[UUID]]
    auth_version: int


def secret() -> str:
    value = settings.jwt_secret.get_secret_value()
    if len(value.encode()) < 32:
        raise HTTPException(503, "authentication unavailable")
    return value


async def actor(db: DB, auth: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)]) -> Actor:
    error = HTTPException(401, "invalid or expired session", headers={"WWW-Authenticate": "Bearer"})
    if not auth:
        raise error
    try:
        claims = jwt.decode(
            auth.credentials,
            secret(),
            algorithms=["HS256"],
            issuer="creative-shygys",
            audience="creative-shygys-api",
            options={"require": ["sub", "exp", "iat", "ver"]},
        )
        user_id = UUID(claims["sub"])
    except (jwt.InvalidTokenError, ValueError):
        raise error from None
    user = await one(
        db,
        """
        SELECT id,email,locale,auth_version FROM users
        WHERE id=%s AND verified_at IS NOT NULL AND disabled_at IS NULL
    """,
        (user_id,),
    )
    if not user or claims["ver"] != user["auth_version"]:
        raise error
    roles = await many(db, "SELECT role,scope_id FROM user_roles WHERE user_id=%s", (user_id,))
    scopes: dict[Role, set[UUID]] = {}
    for row in roles:
        if row["scope_id"]:
            scopes.setdefault(Role(row["role"]), set()).add(row["scope_id"])
    return Actor(**user, roles=sorted({row["role"] for row in roles}), scopes=scopes)


Authenticated = Annotated[Actor, Depends(actor)]


def require(*roles: Role):
    async def require_role(user: Authenticated) -> Actor:
        if not set(user.roles).intersection(roles):
            raise HTTPException(403, "insufficient role")
        return user

    return require_role


async def throttle(request: Request, name: str, limit: int, seconds: int, subject: str = ""):
    ip = request.client.host if request.client else "unknown"
    key = hashlib.sha256((subject or ip).encode()).hexdigest()
    try:
        allowed = await request.app.state.redis.eval(
            """
            local n = redis.call('INCR', KEYS[1])
            if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
            return n <= tonumber(ARGV[2]) and 1 or 0
        """,
            1,
            f"shygys:limit:{name}:{key}",
            seconds,
            limit,
        )
    except RedisError:
        raise HTTPException(503, "rate limiter unavailable") from None
    if not allowed:
        raise HTTPException(429, "too many requests", headers={"Retry-After": str(seconds)})


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def issue_email(db: Conn, user_id: UUID, email: str, purpose: str, locale: str):
    if not settings.smtp_host:
        raise HTTPException(503, "email delivery unavailable")
    token = token_urlsafe(40)
    await db.execute(
        """INSERT INTO email_tokens(digest,user_id,purpose,target_email,expires_at)
           VALUES (%s,%s,%s,%s,now()+interval '1 hour')""",
        (digest(token), user_id, purpose, email),
    )
    # Store delivery in the same transaction as the token.
    from psycopg.types.json import Jsonb

    await db.execute(
        """
        INSERT INTO notifications(user_id,template_id,locale,context)
        VALUES (%s,%s,%s,%s)
    """,
        (user_id, f"email:{purpose}", locale, Jsonb({"email": email, "token": token})),
    )


async def use_token(db: Conn, token: str, purpose: str) -> dict:
    row = await one(
        db,
        """
        UPDATE email_tokens SET used_at=now() WHERE digest=%s AND purpose=%s
        AND expires_at > now() AND used_at IS NULL
        RETURNING user_id,target_email
    """,
        (digest(token), purpose),
    )
    if not row:
        raise HTTPException(400, "invalid or expired email token")
    return row


@router.post("/auth/register", status_code=202)
async def register(data: Registration, request: Request, db: DB) -> Response:
    await throttle(request, "register", 8, 3600)
    email = data.email.lower()
    await throttle(request, "register-account", 3, 3600, email)
    hashed = await run_in_threadpool(passwords.hash, data.password)
    user = await one(
        db,
        """
        INSERT INTO users(email,password_hash,locale) VALUES (%s,%s,%s)
        ON CONFLICT(email) DO NOTHING RETURNING id
    """,
        (email, hashed, data.locale),
    )
    if user:
        await db.execute("INSERT INTO user_roles(user_id,role) VALUES (%s,'creator')", (user["id"],))
        await db.execute(
            """
            INSERT INTO consents(user_id,type,policy_version) VALUES (%s,'data_processing',%s)
        """,
            (user["id"], data.policy_version),
        )
        await issue_email(db, user["id"], email, "verify", data.locale)
    return Response(status_code=202)


@router.post("/auth/verify-email", status_code=204)
async def verify(data: EmailToken, request: Request, db: DB):
    await throttle(request, "verify", 20, 900)
    row = await use_token(db, data.token, "verify")
    await db.execute(
        "UPDATE users SET verified_at=now() WHERE id=%s AND verified_at IS NULL", (row["user_id"],)
    )


@router.post("/auth/login")
async def login(data: Login, request: Request, db: DB) -> AccessToken:
    email = data.email.lower()
    await throttle(request, "login", 30, 900)
    await throttle(request, "login-account", 8, 900, email)
    user = await one(db, "SELECT * FROM users WHERE email=%s", (email,))
    valid = await run_in_threadpool(
        passwords.verify, data.password, user["password_hash"] if user else dummy_hash
    )
    if not valid or not user or not user["verified_at"] or user["disabled_at"]:
        raise HTTPException(401, "invalid credentials or unavailable account")
    now = datetime.now(UTC)
    token = jwt.encode(
        {
            "sub": str(user["id"]),
            "ver": user["auth_version"],
            "iss": "creative-shygys",
            "aud": "creative-shygys-api",
            "iat": now,
            "exp": now + timedelta(minutes=settings.token_minutes),
        },
        secret(),
        algorithm="HS256",
    )
    return AccessToken(access_token=token, expires_in=settings.token_minutes * 60)


@router.post("/auth/forgot-password", status_code=202)
async def forgot(data: Email, request: Request, db: DB):
    await throttle(request, "forgot", 10, 3600)
    await throttle(request, "forgot-account", 3, 3600, data.email.lower())
    user = await one(db, "SELECT * FROM users WHERE email=%s AND disabled_at IS NULL", (data.email.lower(),))
    if user:
        await issue_email(
            db, user["id"], user["email"], "reset" if user["verified_at"] else "verify", user["locale"]
        )


@router.post("/auth/reset-password", status_code=204)
async def reset(data: Reset, request: Request, db: DB):
    await throttle(request, "reset", 10, 900)
    row = await use_token(db, data.token, "reset")
    hashed = await run_in_threadpool(passwords.hash, data.password)
    await db.execute(
        """
        UPDATE users SET password_hash=%s,auth_version=auth_version+1 WHERE id=%s AND email=%s
    """,
        (hashed, row["user_id"], row["target_email"]),
    )
    await db.execute(
        "UPDATE email_tokens SET used_at=now() WHERE user_id=%s AND purpose='reset' AND used_at IS NULL",
        (row["user_id"],),
    )


@router.post("/auth/change-email", status_code=202)
async def change_email(data: Email, request: Request, user: Authenticated, db: DB):
    await throttle(request, "change-email", 5, 3600, str(user.id))
    await issue_email(db, user.id, data.email.lower(), "change", user.locale)


@router.post("/auth/confirm-email-change", status_code=204)
async def confirm_change(data: EmailToken, request: Request, db: DB):
    await throttle(request, "confirm-change", 15, 900)
    row = await use_token(db, data.token, "change")
    await db.execute(
        "UPDATE users SET email=%s,auth_version=auth_version+1 WHERE id=%s",
        (row["target_email"], row["user_id"]),
    )
    await db.execute(
        "UPDATE email_tokens SET used_at=now() WHERE user_id=%s AND used_at IS NULL", (row["user_id"],)
    )


@router.get("/me")
async def me(user: Authenticated) -> User:
    return User(id=user.id, email=user.email, locale=user.locale, roles=user.roles)


@router.patch("/me/locale", status_code=204)
async def set_locale(data: LocaleInput, user: Authenticated, db: DB):
    await db.execute("UPDATE users SET locale=%s WHERE id=%s", (data.locale, user.id))


@router.post("/me/consents", status_code=204)
async def consent(data: ConsentInput, user: Authenticated, db: DB):
    await db.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (user.id,))
    await db.execute(
        """
        UPDATE consents SET withdrawn_at=now() WHERE user_id=%s AND type=%s
        AND withdrawn_at IS NULL AND policy_version<>%s
    """,
        (user.id, data.type, data.policy_version),
    )
    await db.execute(
        """
        INSERT INTO consents(user_id,type,policy_version) VALUES (%s,%s,%s)
        ON CONFLICT(user_id,type) WHERE withdrawn_at IS NULL DO NOTHING
    """,
        (user.id, data.type, data.policy_version),
    )


@router.delete("/me/consents/{kind}", status_code=204)
async def withdraw(kind: ConsentType, user: Authenticated, db: DB):
    await db.execute("SELECT id FROM users WHERE id=%s FOR UPDATE", (user.id,))
    await db.execute(
        "UPDATE consents SET withdrawn_at=now() WHERE user_id=%s AND type=%s AND withdrawn_at IS NULL",
        (user.id, kind),
    )


@router.post("/me/data-requests/{kind}", status_code=202)
async def request_data(kind: Literal["export", "close"], request: Request, user: Authenticated, db: DB) -> Id:
    await throttle(request, "data-request", 3, 3600, str(user.id))
    return Id(
        **await one(
            db, "INSERT INTO profile_requests(user_id,kind) VALUES (%s,%s) RETURNING id", (user.id, kind)
        )
    )


async def has_consent(db: Conn, user_id: UUID, kind: ConsentType) -> bool:
    return bool(
        await one(
            db,
            """
        SELECT id FROM consents WHERE user_id=%s AND type=%s AND withdrawn_at IS NULL
    """,
            (user_id, kind),
        )
    )


@router.post("/auth/logout", status_code=204)
async def logout(user: Authenticated, db: DB):
    await db.execute("UPDATE users SET auth_version=auth_version+1 WHERE id=%s", (user.id,))


Admin = Annotated[Actor, Depends(require(Role.admin, Role.superadmin))]
Reviewer = Annotated[Actor, Depends(require(Role.moderator, Role.admin, Role.superadmin))]
Editor = Annotated[Actor, Depends(require(Role.content_editor, Role.admin, Role.superadmin))]
Creator = Annotated[Actor, Depends(require(Role.creator, Role.org_rep))]


@router.post("/admin/users/{user_id}/disable", status_code=204)
async def disable(user_id: UUID, user: Admin, db: DB):
    if user_id == user.id:
        raise HTTPException(409, "cannot disable own account")
    query = "SELECT role FROM user_roles WHERE user_id=%s AND role IN ('admin','superadmin')"
    if Role.superadmin not in user.roles and await one(db, query, (user_id,)):
        raise HTTPException(403, "cannot disable an administrator")
    if not await one(
        db,
        "UPDATE users SET disabled_at=now(),auth_version=auth_version+1 WHERE id=%s RETURNING id",
        (user_id,),
    ):
        raise HTTPException(404, "user not found")
    await db.execute("UPDATE creators SET publication_status='hidden' WHERE user_id=%s", (user_id,))
    await audit(db, user.id, "user.disabled", user_id)
