import asyncio
from datetime import date, timedelta
from io import BytesIO
from uuid import uuid4

import pytest
from PIL import Image

from app.config import settings
from app.media import process_media
from tests.conftest import TEXT, account, approve, ok
from tests.test_registry import MemoryBucket

pytestmark = pytest.mark.integration


def upload(client, headers, path="/me/media"):
    output = BytesIO()
    Image.new("RGB", (320, 240), (140, 80, 90)).save(output, "JPEG")
    record = ok(
        client.post(path, headers=headers, files={"file": ("gallery.jpeg", output.getvalue(), "image/jpeg")}),
        202,
    )
    asyncio.run(process_media({}, record["id"]))
    return record["id"]


def test_ac20_ac22_ac23_ac26_ac27_ac30(client, org_context, monkeypatch):
    admin, reviewer, reviewer_id, analyst, curator, alice, bob, bob_user, profiles, district, center, _ = (
        org_context
    )
    (first, slug), (second, _) = profiles
    today = date.today()
    filters = {"start": today.isoformat(), "end": today.isoformat(), "district_id": district}
    summary = ok(client.get("/analytics/dashboard", headers=analyst, params=filters))
    listing = ok(client.get("/analytics/creators", headers=analyst, params=filters))
    assert summary["totals"]["creators"] == listing["total"] == 2
    assert {r["id"] for r in listing["items"]} == {first, second}
    funnel = ok(client.get("/analytics/funnel", headers=analyst, params=filters))
    assert funnel["started"] == funnel["submitted"] == funnel["approved"] == 2
    preset = ok(
        client.put(
            "/analytics/presets",
            headers=analyst,
            json={
                "name": "District",
                "role": "analyst",
                "filters": filters,
            },
        )
    )
    assert ok(client.get("/analytics/presets", headers=analyst))["items"][0]["id"] == preset["id"]
    ok(client.delete(f"/analytics/presets/{preset['id']}", headers=analyst), 204)
    report = client.get("/analytics/export/csv", headers=analyst, params=filters)
    assert report.status_code == 200 and "totals,creators,2" in report.text
    assert "email" not in report.text and "phone" not in report.text and first not in report.text
    assert client.get("/admin/users", headers=analyst).status_code == 403

    req = ok(
        client.post(
            "/me/residencies",
            headers=alice,
            json={
                "center_id": center,
                "starts_on": today.isoformat(),
                "ends_on": (today + timedelta(days=5)).isoformat(),
            },
        ),
        201,
    )["id"]
    assert ok(client.get(f"/creators/{slug}"))["residency"] == "pending"
    assert client.post(f"/admin/residencies/{req}/confirm", headers=admin).status_code == 409
    ok(client.post(f"/center/residencies/{req}/recommend", headers=curator), 204)
    assert ok(client.get(f"/creators/{slug}"))["residency"] == "pending"
    ok(client.post(f"/admin/residencies/{req}/confirm", headers=admin), 204)
    assert ok(client.get(f"/creators/{slug}"))["residency"] == "active"

    org = ok(
        client.post(
            "/me/organizations", headers=alice, json={"legal_type": "legal_entity", "display_name": TEXT}
        ),
        201,
    )["id"]
    ok(client.post(f"/admin/organizations/{org}/confirm", headers=admin), 204)
    inv = ok(
        client.post(f"/me/organizations/{org}/invitations", headers=alice, json={"user_id": bob_user}), 201
    )["id"]
    ok(client.put(f"/me/organization-invitations/{inv}/accept", headers=bob), 204)
    assert len(ok(client.get(f"/me/organizations/{org}/members", headers=alice))["items"]) == 2
    project = ok(
        client.post(
            "/me/projects",
            headers=alice,
            json={
                "title": TEXT,
                "creator_ids": [second],
                "organization_id": org,
            },
        ),
        201,
    )["project_id"]
    ok(client.post(f"/me/items/projects/{project}/submit", headers=alice), 202)
    assert client.get(f"/projects/{project}").status_code == 404
    approve(client, reviewer, first, "project")
    assert ok(client.get(f"/projects/{project}"))["creator_ids"] == [first]
    ok(client.post(f"/me/projects/{project}/confirm-participation", headers=bob), 204)
    assert set(ok(client.get(f"/projects/{project}"))["creator_ids"]) == {first, second}
    assert ok(client.get("/projects", params={"creator_id": second}))["total"] == 1

    bucket = MemoryBucket()
    monkeypatch.setattr("app.media.s3", lambda: bucket)
    avatar = upload(client, admin, f"/admin/creators/{first}/media")
    ok(
        client.put(
            f"/admin/creators/{first}/avatar",
            headers=admin,
            json={"avatar_id": avatar, "reason": "Photo update"},
        ),
        202,
    )
    assert ok(client.get(f"/creators/{slug}"))["avatar_id"] is None
    approve(client, reviewer, first)
    assert ok(client.get(f"/creators/{slug}"))["avatar_id"] == avatar
    image = client.get(f"/media/{avatar}/320")
    assert image.status_code == 200 and "no-store" in image.headers["cache-control"]
    ok(
        client.request(
            "DELETE", f"/admin/users/{reviewer_id}/roles", headers=admin, json={"role": "moderator"}
        ),
        204,
    )
    assert client.get("/moderation/cases", headers=reviewer).status_code == 403


def test_ac18_ac28(client, monkeypatch):
    _, editor = account(client, "content_editor")
    bucket = MemoryBucket()
    monkeypatch.setattr("app.media.s3", lambda: bucket)
    monkeypatch.setattr(settings, "alemai_base_url", "")
    images = [upload(client, editor) for _ in range(10)]
    slug = f"gallery-{uuid4().hex}"
    source = {"title": {"kk": "", "ru": "Выставка", "en": ""}}
    row = ok(
        client.post(
            "/editor/articles",
            headers=editor,
            json={
                "slug": slug,
                "source_locale": "ru",
                "fields": source,
                "media_ids": images,
            },
        ),
        201,
    )
    route = f"/translations/article/{row['id']}"
    assert (
        client.post(
            route + "/jobs",
            headers=editor,
            json={
                "source_locale": "ru",
                "external_processing_approved": True,
            },
        ).status_code
        == 503
    )
    for locale, title in (("kk", "Көрме"), ("en", "Exhibition")):
        ok(
            client.put(
                route,
                headers=editor,
                json={
                    "source_locale": "ru",
                    "locale": locale,
                    "fields_json": {"title": title, "summary": "", "body": "", "seo_description": ""},
                },
            ),
            204,
        )
    ok(client.post(f"/editor/articles/{row['id']}/publish", headers=editor, json={}))
    article = ok(client.get(f"/articles/{slug}"))
    assert article["media_ids"] == images and article["fields"]["title"]["kk"] == "Көрме"
    assert all(client.get(f"/media/{image}/320").status_code == 200 for image in images)


def test_export_and_duplicate_merge(client, org_context):
    admin, _, _, _, _, alice, bob, _, profiles, _, _, _ = org_context
    first, second = [pair[0] for pair in profiles]
    payload = {"phone": "+77001112233"}
    for headers in (alice, bob):
        ok(client.put("/me/creator/contacts", headers=headers, json=payload))
    pair = {"primary_id": first, "duplicate_id": second}
    preview = ok(client.post("/admin/creator-merges/preview", headers=admin, json=pair))
    assert preview["primary_id"] == first and "phone" not in preview["differences"]
    ok(client.post("/admin/creator-merges", headers=admin, json={**pair, "reason": "Verified duplicate"}))
    assert client.get("/creators/" + profiles[1][1]).status_code == 404
    request = ok(client.post("/me/data-requests/export", headers=alice), 202)["id"]
    download = f"/me/data-requests/{request}/download"
    assert client.get(download, headers=alice).status_code == 404
    ok(
        client.post(f"/admin/data-requests/{request}/complete", headers=admin, json={"reason": "Verified"}),
        204,
    )
    assert client.get(download, headers=bob).status_code == 404
    data = ok(client.get(download, headers=alice))
    assert data["profile"]["phone"] == payload["phone"]
    assert "password_hash" not in data["account"] and "auth_version" not in data["account"]


def test_editorial_collection_hides_removed_works(client, org_context):
    admin, reviewer, _, _, _, alice, _, _, profiles, _, _, _ = org_context
    creator_id, creator_slug = profiles[0]
    work = ok(client.post("/me/portfolio", headers=alice, json={"title": TEXT}), 201)["portfolio_item_id"]
    ok(client.post(f"/me/items/portfolio/{work}/submit", headers=alice), 202)
    approve(client, reviewer, creator_id, "portfolio_item")
    slug = f"exhibit-{uuid4().hex}"
    item = ok(
        client.post(
            "/editor/collections",
            headers=admin,
            json={
                "slug": slug,
                "kind": "exhibition",
                "fields": {"title": TEXT},
                "work_ids": [work],
            },
        ),
        201,
    )["id"]
    ok(client.post(f"/editor/collections/{item}/publish", headers=admin), 204)
    assert ok(client.get(f"/collections/{slug}"))["work_ids"] == [work]
    assert ok(client.get("/collections", params={"kind": "exhibition"}))["total"] >= 1
    ok(
        client.post(f"/moderation/creator/{creator_id}/hide", headers=reviewer, json={"reason": "Removed"}),
        204,
    )
    assert client.get(f"/creators/{creator_slug}").status_code == 404
    assert ok(client.get(f"/collections/{slug}"))["work_ids"] == []


def test_content_calendar_and_support_filters(client, org_context):
    admin, *_, district, center, _ = org_context
    start = "2030-12-12T10:00:00+05:00"
    end = "2030-12-12T12:00:00+05:00"
    event = {
        "fields": {"title": TEXT},
        "organizer": TEXT,
        "starts_at": start,
        "ends_at": end,
        "place_id": center,
    }
    entity = ok(client.post("/editor/events", headers=admin, json=event), 201)["id"]
    ok(client.put(f"/editor/events/{entity}", headers=admin, json=event))
    ok(client.post(f"/editor/events/{entity}/publish", headers=admin), 204)
    assert ok(client.get(f"/events/{entity}"))["organizer"] == TEXT
    assert entity in {r["id"] for r in ok(client.get("/events", params={"calendar": "upcoming"}))["items"]}
    assert entity in {r["id"] for r in ok(client.get("/map/events"))["items"]}
    grant = {
        "fields": {"title": TEXT},
        "eligibility": TEXT,
        "kind": "grant",
        "district_id": district,
        "legal_status": "sole_proprietor",
        "source_url": "https://example.com/funding",
    }
    entity = ok(client.post("/editor/opportunities", headers=admin, json=grant), 201)["id"]
    ok(client.put(f"/editor/opportunities/{entity}", headers=admin, json=grant))
    ok(client.post(f"/editor/opportunities/{entity}/publish", headers=admin), 204)
    assert ok(client.get(f"/opportunities/{entity}"))["source_url"] == grant["source_url"]
    filters = {"district_id": district, "active": True, "kind": "grant", "legal_status": "sole_proprietor"}
    assert entity in {r["id"] for r in ok(client.get("/opportunities", params=filters))["items"]}
    filters["legal_status"] = "individual"
    assert entity not in {r["id"] for r in ok(client.get("/opportunities", params=filters))["items"]}


def test_stage_two_versioned_catalog_patch_and_center_actions(client, org_context):
    admin, reviewer, _, _, curator, alice, _, _, profiles, district, center, _ = org_context
    creator, slug = profiles[0]
    root = "/api/v1"
    url = root + "/me/creator"
    published = root + "/creators/" + slug
    payload = {
        "public_name": "Stage Two Creative",
        "description": {"kk": "Жаңа сипаттама"},
        "accuracy": "district",
        "available_for_collaboration": True,
        "contacts": {"phone": "+77000009999", "phone_public": False},
    }
    profile = ok(client.patch(url, headers=alice, json=payload))
    assert profile["status"] == "draft"
    assert profile["payload"]["description"] == {**TEXT, "kk": "Жаңа сипаттама"}
    assert profile["contacts"]["phone"] == "+77000009999"
    assert ok(client.get(published))["public_name"] == "Creator 0"
    draft = ok(client.post(url + "/submit", headers=alice), 202)
    assert draft["status"] == "pending_review"
    path = root + "/moderation/cases"
    params = {"creator_id": creator, "decision": "pending_review"}
    assert ok(client.get(path, headers=curator, params=params))["total"] == 0
    case = ok(client.get(path, headers=reviewer, params=params))["items"][0]
    assert case["diff"]["public_name"] == {"before": "Creator 0", "after": "Stage Two Creative"}
    assert ok(client.get(published))["accuracy"] == "locality"
    ok(client.post(f"{path}/{case['id']}/decision", headers=reviewer, json={"decision": "approved"}))
    public = ok(client.get(published))
    assert public["location"]["accuracy"] == "district" and public["phone"] is None
    assert public["locality_id"] is None
    qr = client.get(published + "/qr")
    assert qr.status_code == 200 and qr.content.startswith(b"\x89PNG\r\n\x1a\n")

    f = {
        "q": "stage creative",
        "district_id": district,
        "open_to_collab": True,
        "is_resident": False,
        "category_id": public["category_ids"][0],
        "oked_code": "90.01",
        "legal_status": "individual",
    }
    results = ok(client.get(root + "/creators", params=f))
    assert results["total"] == 1 and results["items"][0]["id"] == creator
    f["q"] = "сипаттама"
    assert ok(client.get(root + "/creators", params=f))["total"] == 1
    f["q"] = "stage creative"
    f["is_resident"] = True
    assert ok(client.get(root + "/creators", params=f))["total"] == 0
    today = date.today()
    entity = ok(
        client.post(
            root + "/me/residencies",
            headers=alice,
            json={
                "center_id": center,
                "starts_on": today.isoformat(),
                "ends_on": (today + timedelta(days=2)).isoformat(),
            },
        ),
        201,
    )["id"]
    ok(client.post(f"{root}/center/residencies/{entity}/recommend", headers=curator), 204)
    ok(client.post(f"{root}/admin/residencies/{entity}/confirm", headers=admin), 204)
    assert ok(client.get(root + "/creators", params=f))["total"] == 1
    ok(client.patch(url, headers=alice, json={"public_name": "Stage Creative Updated"}))
    ok(client.post(url + "/submit", headers=alice), 202)
    scoped = ok(client.get(path, headers=curator, params=params))["items"]
    assert len(scoped) == 1
    ok(
        client.post(
            f"{path}/{scoped[0]['id']}/decision",
            headers=curator,
            json={"decision": "changes_requested", "reason": "Please review the title"},
        )
    )
    ok(
        client.post(
            f"{root}/center/residencies/{entity}/revoke", headers=curator, json={"reason": "Terminated"}
        ),
        204,
    )
    assert ok(client.get(root + "/creators", params=f))["total"] == 0


def test_stage_three_media_portfolio_projects_and_spaces(client, org_context, monkeypatch):
    _, reviewer, _, _, _, alice, bob, _, profiles, _, center, _ = org_context
    owner, slug = profiles[0]
    participant, participant_slug = profiles[1]
    root = "/api/v1"
    monkeypatch.setattr("app.media.s3", lambda: bucket)
    bucket = MemoryBucket()
    output = BytesIO()
    Image.new("RGB", (640, 480), (140, 80, 90)).save(output, "JPEG")
    original = output.getvalue()
    files = {"file": ("portfolio.jpeg", original, "image/jpeg")}
    upload_url = root + "/media/upload"
    ok(
        client.post(upload_url, headers=alice, files={"file": ("fake.jpg", b"not an image", "image/jpeg")}),
        422,
    )
    with monkeypatch.context() as limit:
        limit.setattr(settings, "media_max_bytes", 100)
        ok(client.post(upload_url, headers=alice, files=files), 413)
    assets = [
        ok(client.post(upload_url, headers=alice, files=files, data={"rights": "CC BY 4.0"}), 202)
        for _ in range(2)
    ]
    for asset in assets:
        assert asset["width"] == 640 and asset["height"] == 480 and asset["rights"] == "CC BY 4.0"
        paths = asset["variants"]
        assert ok(client.get(paths["thumbnail"], headers=alice), 404)
        assert client.get(paths["original"], headers=alice).content == original
        assert client.get(paths["original"], headers=bob).status_code == 404
        asyncio.run(process_media({}, asset["id"]))
        assert client.get(paths["thumbnail"], headers=alice).content[:4] == b"RIFF"
        assert client.get(paths["medium"], headers=alice).content[:4] == b"RIFF"
        assert client.get(f"{root}/media/{asset['id']}/320").status_code == 404

    first, second = [asset["id"] for asset in assets]
    portfolio_url = root + "/me/portfolio"
    work = ok(
        client.post(
            portfolio_url,
            headers=alice,
            json={
                "title": TEXT,
                "cover_id": first,
                "author": "Studio",
                "license": "CC BY 4.0",
                "sort_order": 10,
                "images": [{"media_id": first, "sort_order": 20}, {"media_id": second, "sort_order": 10}],
                "video_urls": ["https://www.youtube.com/watch?v=example"],
            },
        ),
        201,
    )["portfolio_item_id"]
    work_url = f"{portfolio_url}/{work}"
    assert ok(client.get(work_url, headers=alice))["payload"]["media_ids"] == [second, first]
    assert ok(client.get(portfolio_url, headers=alice))["total"] == 1
    assert client.patch(work_url, headers=bob, json={"sort_order": 2}).status_code == 404
    assert ok(client.post(work_url + "/submit", headers=alice), 202)["status"] == "pending_review"
    public_work = f"{root}/portfolio/{work}"
    assert client.get(public_work).status_code == 404
    approve(client, reviewer, owner, "portfolio_item")
    assert ok(client.get(public_work))["images"][0] == {"media_id": second, "sort_order": 10}
    assert client.get(f"{root}/media/{first}/320").status_code == 200
    updated = ok(client.patch(work_url, headers=alice, json={"title": {"ru": "Новая редакция"}}))
    assert updated["status"] == "pending_review"
    assert ok(client.get(public_work))["title"] == TEXT
    approve(client, reviewer, owner, "portfolio_item")
    assert ok(client.get(public_work))["title"] == {**TEXT, "ru": "Новая редакция"}
    ok(client.put(portfolio_url + "/pins", headers=alice, json={"work_ids": [work]}))
    too_many = {"work_ids": [str(uuid4()) for _ in range(4)]}
    assert client.put(portfolio_url + "/pins", headers=alice, json=too_many).status_code == 422
    assert ok(client.get(root + "/creators/" + slug))["pinned_works"][0]["id"] == work

    other = ok(client.post(portfolio_url, headers=alice, json={"title": TEXT, "sort_order": 1}), 201)[
        "portfolio_item_id"
    ]
    ok(client.post(f"{portfolio_url}/{other}/submit", headers=alice), 202)
    approve(client, reviewer, owner, "portfolio_item")
    ok(client.post(root + "/analytics/visits", json={"kind": "portfolio_item", "id": work}), 204)
    for sort, expected in (("author", other), ("popular", work), ("newest", other)):
        page = ok(client.get(root + "/portfolio", params={"creator_id": owner, "sort": sort}))
        assert page["items"][0]["id"] == expected
    draft = ok(client.post(portfolio_url, headers=alice, json={"title": TEXT}), 201)["portfolio_item_id"]
    ok(client.delete(f"{portfolio_url}/{draft}", headers=alice), 204)
    assert client.get(f"{portfolio_url}/{draft}", headers=alice).status_code == 404

    project_slug = "shared-" + uuid4().hex
    project_url = root + "/me/projects"
    created = ok(
        client.post(
            project_url,
            headers=alice,
            json={
                "slug": project_slug,
                "title": TEXT,
                "creator_ids": [participant],
                "media_ids": [first],
            },
        ),
        201,
    )
    project_id = created["project_id"]
    assert created["project_slug"] == project_slug
    ok(client.post(f"{project_url}/{project_id}/submit", headers=alice), 202)
    approve(client, reviewer, owner, "project")
    details = root + "/projects/" + project_slug
    assert ok(client.get(details))["creator_ids"] == [owner]
    invites = ok(client.get(root + "/me/project-invitations", headers=bob))["items"]
    assert invites[0]["project_id"] == project_id and invites[0]["confirmed_at"] is None
    ok(client.post(f"{project_url}/{project_id}/confirm-participation", headers=bob), 204)
    page = ok(client.get(root + "/creators/" + participant_slug))
    assert page["projects"]["items"][0]["slug"] == project_slug
    assert set(ok(client.get(details))["creator_ids"]) == {owner, participant}
    ok(client.delete(f"{project_url}/{project_id}", headers=alice), 204)
    assert client.get(details).status_code == 404
    spaces = ok(client.get(root + "/spaces", params={"kind": "center", "limit": 100}))["items"]
    assert center in {space["id"] for space in spaces}
