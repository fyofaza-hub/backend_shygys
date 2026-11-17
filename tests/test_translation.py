import asyncio
from datetime import date
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from pydantic import SecretStr

from app.auth import passwords
from app.config import settings
from app.translation import process_translation
from tests.conftest import PASSWORD, TEXT, credentials, ok

pytestmark = pytest.mark.integration


def test_ac14_17_ac19_ac21(client, monkeypatch):
    email = f"editor-{uuid4().hex}@example.com"
    with psycopg.connect(settings.database_url) as db:
        actor = db.execute(
            """
            INSERT INTO users(email,password_hash,verified_at) VALUES (%s,%s,now()) RETURNING id
        """,
            (email, passwords.hash(PASSWORD)),
        ).fetchone()[0]
        db.execute("INSERT INTO user_roles(user_id,role) VALUES (%s,'admin')", (actor,))
    editor = credentials(client, email)
    monkeypatch.setattr(settings, "alemai_base_url", "https://provider.example.com/v1")
    monkeypatch.setattr(settings, "alemai_api_key", SecretStr("mock-only"))
    monkeypatch.setattr(settings, "alemai_translation_model", "gemma-4-31b-it")
    slug = f"article-{uuid4().hex}"
    article = ok(
        client.post(
            "/editor/articles",
            headers=editor,
            json={
                "slug": slug,
                "source_locale": "ru",
                "fields": {"title": {"kk": "", "ru": "Тест", "en": ""}},
            },
        ),
        201,
    )
    match = f"/translations/article/{article['id']}"
    request = {"source_locale": "ru", "external_processing_approved": True}
    jobs = ok(client.post(match + "/jobs", headers=editor, json=request), 202)
    assert {j["target_locale"] for j in jobs} == {"kk", "en"}
    assert {j["id"] for j in ok(client.post(match + "/jobs", headers=editor, json=request), 202)} == {
        j["id"] for j in jobs
    }
    ids = {j["target_locale"]: j["id"] for j in jobs}

    async def limited(*args):
        req = httpx.Request("POST", "https://provider.example.com")
        raise httpx.HTTPStatusError("mock 429", request=req, response=httpx.Response(429, request=req))

    monkeypatch.setattr("app.translation.chat", limited)
    asyncio.run(process_translation({}, ids["en"]))
    state = ok(client.get(match + "/jobs", headers=editor))["items"]
    retry = next(j for j in state if j["id"] == ids["en"])
    assert (retry["status"], retry["error_code"], retry["attempts"]) == ("queued", "provider_429", 1)
    with psycopg.connect(settings.database_url) as db:
        db.execute("UPDATE translation_jobs SET available_at=now() WHERE id=%s", (UUID(ids["en"]),))

    async def translated(model, system, payload):
        title = TEXT[payload["target"]]
        return '{"title":"' + title + '","summary":"","body":"","seo_description":""}'

    monkeypatch.setattr("app.translation.chat", translated)
    for job_id in ids.values():
        asyncio.run(process_translation({}, job_id))

    publish = f"/editor/articles/{article['id']}/publish"
    ok(client.post(publish, headers=editor, json={}), 409)
    for locale in ("kk", "en"):
        ok(client.post(f"/editor/articles/{article['id']}/review/{locale}", headers=editor), 204)
    ok(client.post(publish, headers=editor, json={}))
    assert ok(client.get(f"/articles/{slug}"))["fields"]["title"] == TEXT
    manual = {
        "source_locale": "ru",
        "locale": "en",
        "fields_json": {"title": "Human title", "summary": "", "body": "", "seo_description": ""},
    }
    ok(client.put(match, headers=editor, json=manual), 204)
    assert {j["id"] for j in ok(client.post(match + "/jobs", headers=editor, json=request), 202)} == set(
        ids.values()
    )
    asyncio.run(process_translation({}, ids["en"]))
    draft = ok(client.get(f"/editor/articles/{article['id']}", headers=editor))
    assert draft["fields"]["title"]["en"] == "Human title"
    draft["fields"]["title"]["ru"] = "Новый исходник"
    edited = {k: draft[k] for k in ("type", "source_locale", "fields", "media_ids")}
    ok(client.put(f"/editor/articles/{article['id']}", headers=editor, json=edited))
    review = ok(client.get(f"/editor/articles/{article['id']}/translation-status", headers=editor))
    assert {r["locale"]: r["state"] for r in review} == {"kk": "stale", "ru": "original", "en": "stale"}
    assert client.post(publish, headers=editor, json={}).status_code == 409
    assert ok(client.get(f"/articles/{slug}"))["fields"]["title"] == TEXT

    today = date.today().isoformat()
    f = {"start": today, "end": today}
    current = ok(client.get("/analytics/dashboard", headers=editor, params=f))
    assert current["start"] == current["end"] == today
    historical = ok(
        client.get(
            "/analytics/dashboard", headers=editor, params={"start": "2000-01-01", "end": "2000-01-31"}
        )
    )
    assert historical["start"] == "2000-01-01" and historical["registrations"] == []
    assert historical["totals"]["articles"] == 0 and current["totals"]["articles"] >= 1
    report = client.get("/analytics/export/xlsx", headers=editor, params=f)
    assert report.status_code == 200, report.text
    assert report.content[:2] == b"PK"
