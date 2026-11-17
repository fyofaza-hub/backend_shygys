from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient

from app.auth import passwords
from app.config import settings
from app.main import app

TEXT = {"kk": "Сынақ", "ru": "Тест", "en": "Test"}
PASSWORD = "temporary-test-password"


@pytest.fixture
def client(monkeypatch):
    from pydantic import SecretStr

    monkeypatch.setattr(settings, "jwt_secret", SecretStr("x" * 48))
    monkeypatch.setattr(settings, "smtp_host", "smtp.test")
    with TestClient(app, client=(f"test-{uuid4().hex}", 50000)) as cli:
        yield cli


def ok(response, status=200):
    assert response.status_code == status, response.text
    return response.json() if response.content else None


def credentials(client, email):
    token = ok(client.post("/auth/login", json={"email": email, "password": PASSWORD}))
    return {"Authorization": f"Bearer {token['access_token']}"}


def pytest_collection_modifyitems(config, items):
    import os

    if os.environ.get("SHYGYS_INTEGRATION") == "1":
        return
    skip = pytest.mark.skip(reason="requires isolated migrated PostGIS and Redis")
    for item in items:
        if item.get_closest_marker("integration"):
            item.add_marker(skip)


def account(client, *roles):
    email = f"{uuid4().hex}@example.com"
    with psycopg.connect(settings.database_url) as db:
        user_id = db.execute(
            """
            INSERT INTO users(email,password_hash,verified_at) VALUES (%s,%s,now()) RETURNING id
        """,
            (email, passwords.hash(PASSWORD)),
        ).fetchone()[0]
        for role in roles:
            db.execute("INSERT INTO user_roles(user_id,role) VALUES (%s,%s)", (user_id, role))
    return str(user_id), credentials(client, email)


def approve(client, reviewer, creator_id, kind="creator"):
    cases = ok(
        client.get(
            "/moderation/cases",
            headers=reviewer,
            params={"creator_id": creator_id, "kind": kind, "decision": "pending_review"},
        )
    )["items"]
    assert len(cases) == 1
    return ok(
        client.post(
            f"/moderation/cases/{cases[0]['id']}/decision", headers=reviewer, json={"decision": "approved"}
        )
    )


@pytest.fixture
def org_context(client):
    _, superadmin = account(client, "superadmin")
    moderator_id, moderator = account(client, "moderator")
    _, analyst = account(client, "analyst")
    locale_staff_id, locale_staff = account(client)
    alice_id, alice = account(client, "creator")
    bob_id, bob = account(client, "creator")
    prefix = uuid4().hex
    geography = {"labels": TEXT, "code": prefix, "longitude": 82.6, "latitude": 49.9}
    district = ok(client.post("/admin/references/districts", headers=superadmin, json=geography), 201)["id"]
    locality = ok(
        client.post(
            "/admin/references/localities", headers=superadmin, json={**geography, "district_id": district}
        ),
        201,
    )["id"]
    center = ok(
        client.post(
            "/admin/places",
            headers=superadmin,
            json={
                "labels": TEXT,
                "locality_id": locality,
                "kind": "center",
                "is_public": True,
                "longitude": 82.6,
                "latitude": 49.9,
            },
        ),
        201,
    )["id"]
    ok(
        client.put(
            f"/admin/users/{locale_staff_id}/roles",
            headers=superadmin,
            json={
                "role": "center_curator",
                "scope_id": center,
            },
        ),
        204,
    )
    category = ok(
        client.post(
            "/admin/references/categories", headers=superadmin, json={"code": prefix, "labels": TEXT}
        ),
        201,
    )["id"]
    with psycopg.connect(settings.database_url) as db:
        from psycopg.types.json import Jsonb

        oked = str(
            db.execute(
                "INSERT INTO oked(code,labels,version) VALUES ('90.01',%s,%s) RETURNING id",
                (Jsonb(TEXT), prefix),
            ).fetchone()[0]
        )
    profiles = []
    for index, token in enumerate((alice, bob)):
        for kind in ("data_processing", "portfolio"):
            ok(client.post("/me/consents", headers=token, json={"type": kind, "policy_version": "test"}), 204)
        slug = f"accept-{prefix}-{index}"
        draft = ok(
            client.post(
                "/me/creator",
                headers=token,
                json={
                    "slug": slug,
                    "public_name": f"Creator {index}",
                    "district_id": district,
                    "locality_id": locality,
                    "category_ids": [category],
                    "oked_ids": [oked],
                    "description": TEXT,
                },
            ),
            201,
        )
        ok(client.post("/me/creator/submit", headers=token), 202)
        approve(client, moderator, draft["creator_id"])
        profiles.append((draft["creator_id"], slug))
    return (
        superadmin,
        moderator,
        moderator_id,
        analyst,
        locale_staff,
        alice,
        bob,
        bob_id,
        profiles,
        district,
        center,
        alice_id,
    )
