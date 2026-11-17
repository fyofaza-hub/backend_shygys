import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import SecretStr

from app.config import settings
from app.translation import process_translation
from tests.conftest import TEXT, account, approve, ok

pytestmark = pytest.mark.integration
ROOT = "/api/v1"


def test_map_cms_and_gis_consent(client, org_context):
    admin, reviewer, _, analyst, _, alice, _, _, profiles, district, _, _ = org_context
    creator_id, creator_slug = profiles[0]
    profile = ok(client.get(f"{ROOT}/creators/{creator_slug}"))
    _, editor = account(client, "content_editor")
    params = {
        "district_id": district,
        "category_id": profile["category_ids"][0],
        "oked_code": "90.01",
        "q": "Creator",
        "is_resident": False,
        "open_to_collab": False,
    }
    catalog = ok(client.get(ROOT + "/creators", params=params))
    viewport = {**params, "bbox": "81,49,84,51", "zoom": 10}
    points = ok(client.get(ROOT + "/map/points", params=viewport))
    clusters = ok(client.get(ROOT + "/map/clusters", params=viewport))
    assert {p["id"] for p in points["items"]} == {p["id"] for p in catalog["items"]}
    assert sum(c["count"] for c in clusters["items"]) == points["total"] == 2
    assert {point["accuracy"] for point in points["items"]} == {"locality"}
    cluster = clusters["items"][0]["id"]
    assert ok(client.get(ROOT + f"/map/creators/clusters/{cluster}", params=viewport))["total"] == 2
    assert ok(client.get(ROOT + "/map/heatmap", params=viewport, headers=analyst))["total"] == 0

    place = ok(
        client.post(
            ROOT + "/editor/spaces",
            headers=editor,
            json={
                "labels": TEXT,
                "locality_id": profile["locality_id"],
                "kind": "workshop",
                "longitude": 82.65,
                "latitude": 49.93,
            },
        ),
        201,
    )["id"]
    workshops = {**viewport, "layer": "workshops"}
    assert ok(client.get(ROOT + "/map/points", params=workshops))["total"] == 0
    ok(client.post(f"{ROOT}/editor/spaces/{place}/publish", headers=editor))
    assert ok(client.get(ROOT + "/map/points", params=workshops))["items"][0]["id"] == place
    ok(client.patch(ROOT + "/me/creator", headers=alice, json={"accuracy": "exact", "place_id": place}))
    ok(client.post(ROOT + "/me/creator/submit", headers=alice), 202)
    approve(client, reviewer, creator_id)
    nearby = {**params, "longitude": 82.65, "latitude": 49.93, "radius_m": 500}
    assert ok(client.get(ROOT + "/map/creators/nearby", params=nearby))["total"] == 1

    export = ROOT + "/analytics/gis-export"
    assert ok(client.get(export, headers=analyst, params=params))["features"] == []
    ok(
        client.post(
            ROOT + "/me/consents", headers=alice, json={"type": "gis_export", "policy_version": "v1"}
        ),
        204,
    )
    assert len(ok(client.get(export, headers=analyst, params=params))["features"]) == 1
    ok(client.delete(ROOT + "/me/consents/gis_export", headers=alice), 204)
    assert ok(client.get(export, headers=analyst, params=params))["features"] == []

    now = datetime.now(UTC)
    later = now + timedelta(days=3)
    event = ok(
        client.post(
            ROOT + "/editor/events",
            headers=editor,
            json={
                "fields": {"title": TEXT},
                "starts_at": later.isoformat(),
                "ends_at": (later + timedelta(hours=2)).isoformat(),
                "place_id": place,
            },
        ),
        201,
    )["id"]
    schedule = (now + timedelta(days=2)).isoformat()
    ok(
        client.post(f"{ROOT}/editor/events/{event}/publish", headers=editor, json={"publish_at": schedule}),
        204,
    )
    assert client.get(f"{ROOT}/events/{event}").status_code == 404
    assert ok(client.get(ROOT + "/map/points", params={**viewport, "layer": "events"}))["total"] == 0
    ok(client.post(f"{ROOT}/editor/events/{event}/publish", headers=editor), 204)
    assert (
        ok(client.get(ROOT + "/map/points", params={**viewport, "layer": "events"}))["items"][0]["id"]
        == event
    )
    ok(client.delete(f"{ROOT}/editor/events/{event}", headers=editor), 204)
    assert client.get(f"{ROOT}/events/{event}").status_code == 404
    assert (
        ok(client.get(f"{ROOT}/editor/history/{event}", headers=editor))["items"][0]["snapshot"]["status"]
        == "archived"
    )

    ok(client.delete(f"{ROOT}/editor/spaces/{place}", headers=editor), 204)
    assert ok(client.get(ROOT + "/map/points", params=workshops))["total"] == 0
    assert ok(client.get(f"{ROOT}/creators/{creator_slug}"))["accuracy"] == "locality"
    assert (
        client.put(
            f"{ROOT}/admin/places/{place}/visibility", headers=admin, json={"is_public": True}
        ).status_code
        == 404
    )

    tag = ok(
        client.post(
            ROOT + "/editor/dictionaries/tags",
            headers=editor,
            json={
                "code": "tag-" + uuid4().hex,
                "labels": TEXT,
            },
        ),
        201,
    )
    assert (
        ok(client.get(ROOT + f"/editor/dictionaries/tags/{tag['id']}", headers=editor))["code"] == tag["code"]
    )
    ok(client.delete(ROOT + f"/editor/dictionaries/tags/{tag['id']}", headers=editor), 204)
    assert ok(client.get(ROOT + f"/editor/dictionaries/tags/{tag['id']}", headers=editor))["retired_at"]
    layout = {"sections": ["events", "creators"]}
    assert client.put(ROOT + "/editor/home/layout", headers=editor, json=layout).status_code == 403
    ok(client.put(ROOT + "/editor/home/layout", headers=admin, json=layout))
    assert ok(client.get(ROOT + "/home/layout")) == layout
    preset = ok(
        client.put(
            ROOT + "/editor/filter-presets",
            headers=editor,
            json={"name": "Archive", "kind": "events", "status": "archived"},
        )
    )
    assert ok(client.get(ROOT + "/editor/filter-presets", headers=editor))["items"][0]["id"] == preset["id"]
    export_result = client.get(
        ROOT + "/editor/content/events/export/csv", headers=editor, params={"status": "archived"}
    )
    assert export_result.status_code == 200 and event in export_result.text
    version = "test-" + uuid4().hex
    entries = [{"code": f"13.{n:02}", "labels": TEXT} for n in range(43)]
    ok(
        client.post(
            ROOT + "/editor/oked-versions", headers=editor, json={"version": version, "entries": entries}
        ),
        201,
    )
    assert ok(client.get(ROOT + "/references/oked", params={"version": version}))["total"] == 43
    ok(client.delete(ROOT + f"/editor/oked-versions/{version}", headers=editor), 204)
    assert ok(client.get(ROOT + "/references/oked", params={"version": version}))["total"] == 0


def test_translate_generate_overwrite_and_private_input(client, monkeypatch):
    _, editor = account(client, "content_editor")
    monkeypatch.setattr(settings, "alemai_api_key", SecretStr("test-stage-four-only"))
    monkeypatch.setattr(settings, "alemai_translation_model", "qwen3-8")
    provider_calls = []

    async def chat(model, system, payload):
        provider_calls.append((model, payload))
        return '{"title":"AI title","summary":"","body":"","seo_description":""}'

    monkeypatch.setattr("app.translation.chat", chat)

    def create_article(title):
        return ok(
            client.post(
                ROOT + "/editor/articles",
                headers=editor,
                json={
                    "slug": "stage4-" + uuid4().hex,
                    "source_locale": "ru",
                    "fields": {"title": {"ru": title, "kk": "", "en": ""}},
                },
            ),
            201,
        )["id"]

    article = create_article("Мастерская")
    path = f"{ROOT}/translations/article/{article}"
    generate = ROOT + "/translations/generate"
    body = {"entity_id": article, "source_locale": "ru", "external_processing_approved": True}
    human = {"title": "Human title", "summary": "", "body": "", "seo_description": ""}
    operation = {"source_locale": "ru", "locale": "en", "fields_json": human}
    ok(client.put(path, headers=editor, json=operation), 204)
    jobs = ok(client.post(generate, headers=editor, json=body), 202)
    assert {j["target_locale"] for j in jobs} == {"kk", "en"}
    english = next(j["id"] for j in jobs if j["target_locale"] == "en")
    asyncio.run(process_translation({}, english))
    assert {j["target_locale"]: j["status"] for j in ok(client.get(path + "/jobs", headers=editor))["items"]}[
        "en"
    ] == "protected"

    allow = {**body, "overwrite_human_reviewed": True}
    ok(client.post(generate, headers=editor, json=allow), 202)
    # The confirmation must not replace a newer manual review.
    latest = {**operation, "fields_json": {**human, "title": "New human title"}}
    ok(client.put(path, headers=editor, json=latest), 204)
    asyncio.run(process_translation({}, english))
    draft = ok(client.get(f"{ROOT}/editor/articles/{article}", headers=editor))
    assert draft["fields"]["title"]["en"] == "New human title"

    ok(client.post(generate, headers=editor, json=allow), 202)
    asyncio.run(process_translation({}, english))
    history = ok(client.get(path, headers=editor))["items"]
    assert any(r["status"] == "ai_draft" and r["locale"] == "en" for r in history)
    assert any(
        r["status"] == "human_reviewed" and r["fields_json"]["title"] == "New human title" for r in history
    )
    assert provider_calls[-1][1]["glossary"]

    protected_id = create_article("Домашний адрес: улица Абая 21")
    private_jobs = ok(client.post(generate, headers=editor, json={**body, "entity_id": protected_id}), 202)
    prior_count = len(provider_calls)
    for job in private_jobs:
        asyncio.run(process_translation({}, job["id"]))
    assert len(provider_calls) == prior_count
    result = ok(client.get(f"{ROOT}/translations/article/{protected_id}/jobs", headers=editor))
    assert {job["error_code"] for job in result["items"]} == {"potential_private_contact"}
