import asyncio
from io import BytesIO
from uuid import uuid4

import psycopg
import pytest
from PIL import Image
from psycopg.types.json import Jsonb

from app.auth import passwords
from app.config import settings
from app.media import process_media
from tests.conftest import PASSWORD, TEXT, credentials, ok

pytestmark = pytest.mark.integration


def test_ac01_13_ac24_25_ac29(client, monkeypatch):
    prefix = uuid4().hex
    email = prefix + "@example.com"
    staff = "moderator-" + prefix + "@example.com"
    ok(
        client.post(
            "/auth/register", json={"email": email, "password": PASSWORD, "policy_version": "2026-11"}
        ),
        202,
    )
    with psycopg.connect(settings.database_url) as db:
        token = db.execute(
            """
            SELECT context->>'token' FROM notifications n JOIN users u ON n.user_id=u.id
            WHERE u.email=%s AND template_id='email:verify'
        """,
            (email,),
        ).fetchone()[0]
        reviewer = db.execute(
            """
            INSERT INTO users(email,password_hash,verified_at) VALUES (%s,%s,now()) RETURNING id
        """,
            (staff, passwords.hash(PASSWORD)),
        ).fetchone()[0]
        db.execute("INSERT INTO user_roles(user_id,role) VALUES (%s,'moderator')", (reviewer,))
        district = db.execute(
            """
          INSERT INTO districts(code,labels,centroid) VALUES (%s,%s,ST_SetSRID(ST_MakePoint(82.6,49.9),4326)) RETURNING id
        """,
            (prefix, Jsonb(TEXT)),
        ).fetchone()[0]
        locality = db.execute(
            """
          INSERT INTO localities(code,labels,district_id,centroid)
          VALUES (%s,%s,%s,ST_SetSRID(ST_MakePoint(82.61,49.91),4326)) RETURNING id
        """,
            (prefix, Jsonb(TEXT), district),
        ).fetchone()[0]
        category = db.execute(
            "INSERT INTO creative_categories(code,labels) VALUES (%s,%s) RETURNING id", (prefix, Jsonb(TEXT))
        ).fetchone()[0]
        oked = db.execute(
            "INSERT INTO oked(code,labels,version) VALUES ('90.01',%s,%s) RETURNING id", (Jsonb(TEXT), prefix)
        ).fetchone()[0]
    ok(client.post("/auth/verify-email", json={"token": token}), 204)
    creator = credentials(client, email)
    assert ok(client.get("/me", headers=creator))["email"] == email
    moderator = credentials(client, staff)
    slug = "test-" + prefix
    draft = ok(client.post("/me/creator", headers=creator, json={"slug": slug}), 201)
    profile_id = draft["creator_id"]
    assert draft["status"] == "draft"
    assert ok(client.get("/me/creator", headers=creator))["payload"]["public_name"] == ""
    assert client.get(f"/creators/{slug}").status_code == 404
    payload = {
        "public_name": "Published name",
        "legal_status": "individual",
        "district_id": str(district),
        "locality_id": str(locality),
        "category_ids": [str(category)],
        "oked_ids": [str(oked)],
        "description": TEXT,
    }
    ok(client.put("/me/creator", headers=creator, json=payload))
    pending = ok(client.post("/me/creator/submit", headers=creator), 202)
    assert pending["status"] == "pending_review"
    assert client.get(f"/creators/{slug}").status_code == 404
    assert client.get("/moderation/cases", headers=creator).status_code == 403
    queue = ok(client.get("/moderation/cases", headers=moderator, params={"creator_id": profile_id}))
    case = queue["items"][0]["id"]
    ok(
        client.post(
            f"/moderation/cases/{case}/decision",
            headers=moderator,
            json={"decision": "changes_requested", "reason": "Please check."},
        )
    )
    assert ok(client.get("/me/creator", headers=creator))["reason"] == "Please check."
    ok(client.put("/me/creator", headers=creator, json=payload))
    ok(client.post("/me/creator/submit", headers=creator), 202)
    queue = ok(
        client.get(
            "/moderation/cases",
            headers=moderator,
            params={"creator_id": profile_id, "decision": "pending_review"},
        )
    )
    ok(
        client.post(
            f"/moderation/cases/{queue['items'][0]['id']}/decision",
            headers=moderator,
            json={"decision": "approved"},
        )
    )

    ok(client.put("/me/creator/contacts", headers=creator, json={"phone": "+77001234567", "email": email}))
    public = ok(client.get(f"/creators/{slug}"))
    assert public["phone"] is None and public["email"] is None
    assert "+77001234567" not in client.get(f"/creators/{slug}").text
    assert public["legal_status"] == "individual" and public["residency"] == "none"
    assert public["district_id"] == str(district) and public["oked_ids"] == [str(oked)]
    assert profile_id in {row["id"] for row in ok(client.get("/creators", params={"limit": 100}))["items"]}
    payload["legal_status"] = "sole_proprietor"
    ok(client.put("/me/creator", headers=creator, json=payload))
    pending_cases = ok(
        client.get(
            "/moderation/cases",
            headers=moderator,
            params={"creator_id": profile_id, "decision": "pending_review"},
        )
    )["items"]
    ok(
        client.post(
            f"/moderation/cases/{pending_cases[0]['id']}/decision",
            headers=moderator,
            json={"decision": "approved"},
        )
    )

    assert public["location"] == {"longitude": 82.61, "latitude": 49.91, "accuracy": "locality"}
    q = {
        "district_id": str(district),
        "category": str(category),
        "legal_status": "sole_proprietor",
        "q": "Published",
    }
    assert ok(client.get("/creators", params=q))["total"] == 1
    hits = ok(client.get("/search", params={"q": "Published", "kind": "creator", "locale": "en"}))
    assert profile_id in {hit["id"] for hit in hits["items"]}
    refs = ok(client.get("/references/localities", params={"district_id": str(district), "limit": 1}))
    assert refs["items"][0]["id"] == str(locality)
    map_params = {**q, "bbox": "81,49,84,51", "zoom": 12}
    markers = ok(client.get("/map/creators", params=map_params))["items"]
    catalog = ok(client.get("/creators", params=q))["items"]
    assert {point["creator_id"] for point in markers} == {c["id"] for c in catalog} == {profile_id}
    assert ok(client.get("/creators/" + markers[0]["slug"]))["id"] == profile_id
    assert markers[0]["accuracy"] == "locality"

    ok(
        client.post("/me/consents", headers=creator, json={"type": "portfolio", "policy_version": "2026-11"}),
        204,
    )
    media_workflow(client, creator, moderator, profile_id, monkeypatch)

    payload["public_name"] = "Revision"
    change = ok(client.put("/me/creator", headers=creator, json=payload))
    assert change["status"] == "pending_review"
    assert ok(client.get(f"/creators/{slug}"))["public_name"] == "Published name"
    ok(
        client.post(
            f"/moderation/creator/{profile_id}/hide", headers=moderator, json={"reason": "Take down"}
        ),
        204,
    )
    assert client.get(f"/creators/{slug}").status_code == 404
    assert ok(client.get("/creators", params=q))["total"] == 0
    empty = ok(client.get("/map/creators", params=map_params))
    assert empty["items"] == [] and empty["total"] == 0 and empty["empty_reason"] == "no_matching_markers"
    assert profile_id not in {r["id"] for r in ok(client.get("/search", params={"q": "Published"}))["items"]}
    with psycopg.connect(settings.database_url) as db, pytest.raises(psycopg.Error, match="append-only"):
        db.execute("DELETE FROM audit_events WHERE entity_id=%s", (profile_id,))


class ObjectBody(BytesIO):
    def iter_chunks(self, chunk_size):
        while chunk := self.read(chunk_size):
            yield chunk


class MemoryBucket:
    def __init__(self):
        self.objects = {}

    def put_object(self, *, Bucket, Key, Body, ContentType):
        self.objects[Key] = (Body, ContentType)

    def get_object(self, *, Bucket, Key):
        return {"Body": ObjectBody(self.objects[Key][0])}


def media_workflow(client, creator, moderator, profile_id, monkeypatch):
    bucket = MemoryBucket()
    monkeypatch.setattr("app.media.s3", lambda: bucket)
    output = BytesIO()
    Image.new("RGB", (700, 400), (210, 100, 60)).save(output, "PNG")
    original = output.getvalue()
    ok(client.post("/me/media", headers=creator, files={"file": ("art.png", original, "image/jpeg")}), 422)
    ids = []
    for name in ("first.png", "second.png"):
        asset = ok(
            client.post("/me/media", headers=creator, files={"file": (name, original, "image/png")}), 202
        )
        ids.append(asset["id"])
        asyncio.run(process_media({}, asset["id"]))
        assert ok(client.get("/me/media/" + asset["id"], headers=creator))["status"] == "ready"
        assert client.get(f"/media/{asset['id']}/320").status_code == 404
        protected = client.get(f"/me/media/{asset['id']}/320", headers=creator)
        assert protected.status_code == 200
        assert protected.headers["content-type"] == "image/webp"
    assert sum(value[0] == original for value in bucket.objects.values()) == 2

    post = {"title": TEXT, "media_ids": ids, "cover_id": ids[0]}
    draft = ok(client.post("/me/portfolio", headers=creator, json=post), 201)
    work_id = draft["portfolio_item_id"]
    ok(client.post(f"/me/items/portfolio/{work_id}/submit", headers=creator), 202)
    assert client.get(f"/portfolio/{work_id}").status_code == 404
    queue = ok(
        client.get(
            "/moderation/cases",
            headers=moderator,
            params={"kind": "portfolio_item", "creator_id": profile_id, "decision": "pending_review"},
        )
    )
    ok(
        client.post(
            f"/moderation/cases/{queue['items'][0]['id']}/decision",
            headers=moderator,
            json={"decision": "approved"},
        )
    )
    assert ok(client.get(f"/portfolio/{work_id}"))["media_ids"] == ids
    preview = client.get(f"/media/{ids[0]}/320")
    assert preview.status_code == 200
    assert preview.content[:4] == b"RIFF"
