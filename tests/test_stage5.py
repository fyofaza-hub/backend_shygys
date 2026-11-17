import asyncio
import smtplib
from datetime import UTC, datetime, timedelta
from uuid import UUID

import psycopg
import pytest

from app.config import settings
from app.worker import deliver_decision, enqueue_decisions, mail_message, notify_subscriptions
from tests.conftest import TEXT, ok

pytestmark = pytest.mark.integration


def test_bi_decision_period_is_independent_of_registration_cohort(client, org_context):
    _, reviewer, _, analyst, _, alice, _, _, profiles, district, _, _ = org_context
    now = datetime.now(UTC)
    with psycopg.connect(settings.database_url) as db:
        db.execute(
            "UPDATE creators SET created_at=%s WHERE id=ANY(%s)",
            (now - timedelta(days=120), [UUID(pair[0]) for pair in profiles]),
        )
        db.execute("UPDATE creators SET publication_status='hidden' WHERE id=%s", (UUID(profiles[1][0]),))
        # Two profile application decisions within this month, despite old registrations.
        db.execute(
            """
          UPDATE moderation_cases m SET created_at=%s,decided_at=%s,decision='approved'
          FROM creator_versions v WHERE m.version_id=v.id AND v.creator_id=%s
        """,
            (now - timedelta(hours=3), now - timedelta(hours=1), UUID(profiles[0][0])),
        )
        db.execute(
            """
          UPDATE moderation_cases m SET created_at=%s,decided_at=%s,decision='rejected'
          FROM creator_versions v WHERE m.version_id=v.id AND v.creator_id=%s
        """,
            (now - timedelta(hours=7), now - timedelta(hours=1), UUID(profiles[1][0])),
        )
    period = {
        "date_from": (now - timedelta(days=10)).date().isoformat(),
        "date_to": now.date().isoformat(),
        "district_id": district,
    }
    overview = ok(client.get("/api/v1/analytics/overview", headers=analyst, params=period))
    assert overview["totals"]["creators"] == 2
    assert overview["totals"]["published_profiles"] == 1
    assert overview["approval_rate"] == 0.5
    assert overview["average_moderation_hours"] == overview["median_moderation_hours"] == 4
    assert overview["unstable_sample"] and overview["sample_size"] == 2
    assert overview["generated_at"] and overview["districts"] == []
    dynamics = ok(client.get("/api/v1/analytics/dynamics", headers=analyst, params=period))
    assert dynamics["date_from"] == period["date_from"] and dynamics["items"] == []
    geography = ok(client.get("/api/v1/analytics/geography", headers=analyst, params=period))
    assert geography["items"] == [] and geography["minimum_sample_size"] == 5
    funnel = ok(client.get("/api/v1/analytics/funnel", headers=analyst, params=period))
    assert funnel["started"] == funnel["submitted"] == 0 and funnel["submission_rate"] is None
    comparison = ok(
        client.get(
            "/api/v1/analytics/compare",
            headers=analyst,
            params={
                **period,
                "baseline_date_from": "2000-01-01",
                "baseline_date_to": "2000-01-31",
            },
        )
    )
    count = next(row for row in comparison if row["metric"] == "creators")
    assert (count["baseline"], count["current"], count["difference"], count["percent"]) == (0, 2, 2, None)
    for fmt in ("csv", "xlsx"):
        response = client.get("/api/v1/analytics/export/" + fmt, headers=analyst, params=period)
        assert response.status_code == 200
        assert b"+770" not in response.content and b"@example.com" not in response.content
    assert client.get("/api/v1/analytics/overview", headers=reviewer, params=period).status_code == 403
    assert client.get("/api/v1/analytics/overview", headers=alice, params=period).status_code == 403


def test_decision_mail_outbox_retry_and_subscription_isolation(client, org_context, monkeypatch):
    admin, _, _, _, _, alice, bob, _, profiles, _, _, _ = org_context
    creator_id, slug = profiles[0]
    profile = ok(client.get("/api/v1/creators/" + slug))
    category = profile["category_ids"][0]
    # Existing decision notifications were committed by the fixture.
    from app.db import worker_db

    async def prepare():
        async with worker_db() as db:
            await enqueue_decisions(db)

    asyncio.run(prepare())
    with psycopg.connect(settings.database_url) as db:
        row = db.execute(
            """
          SELECT e.id,n.locale FROM email_outbox e JOIN notifications n ON n.id=e.notification_id
          JOIN creators c ON c.user_id=e.user_id WHERE c.id=%s LIMIT 1
        """,
            (UUID(creator_id),),
        ).fetchone()
    assert row
    messages = []

    def failed_send(payload):
        raise smtplib.SMTPException("mock")

    monkeypatch.setattr("app.worker.send_email", failed_send)
    asyncio.run(deliver_decision({}, str(row[0])))
    with psycopg.connect(settings.database_url) as db:
        state = db.execute(
            "SELECT attempts,sent_at,last_error FROM email_outbox WHERE id=%s", (row[0],)
        ).fetchone()
        assert state == (1, None, "delivery_failed")
        db.execute("UPDATE email_outbox SET available_at=now() WHERE id=%s", (row[0],))
    monkeypatch.setattr("app.worker.send_email", lambda payload: messages.append(mail_message(payload)))
    asyncio.run(deliver_decision({}, str(row[0])))
    asyncio.run(deliver_decision({}, str(row[0])))
    assert len(messages) == 1
    for locale in ("kk", "ru", "en"):
        message = mail_message(
            {
                "locale": locale,
                "template_id": "email:decision",
                "context": {
                    "email": "demo@example.com",
                    "decision": "changes_requested",
                    "reason": "Please update the cover",
                },
            }
        )
        assert message["Subject"] and "Please update the cover" in message.get_content()

    ok(
        client.put(
            "/api/v1/me/subscriptions", headers=alice, json={"kind": "opportunities", "category_id": category}
        ),
        204,
    )
    opportunity = ok(
        client.post(
            "/api/v1/editor/opportunities",
            headers=admin,
            json={
                "fields": {"title": TEXT},
                "eligibility": TEXT,
                "category_id": category,
            },
        ),
        201,
    )
    ok(client.post(f"/api/v1/editor/opportunities/{opportunity['id']}/publish", headers=admin), 204)
    asyncio.run(notify_subscriptions({}))
    asyncio.run(notify_subscriptions({}))
    inbox = ok(client.get("/api/v1/me/notifications", headers=alice))["items"]
    notices = [item for item in inbox if item["template_id"] == "subscription.opportunities"]
    assert len(notices) == 1 and notices[0]["context"]["entity_id"] == opportunity["id"]
    assert (
        client.put("/api/v1/me/notifications/" + notices[0]["id"] + "/read", headers=bob).status_code == 404
    )
    ok(client.put("/api/v1/me/notifications/" + notices[0]["id"] + "/read", headers=alice), 204)
    unread = ok(client.get("/api/v1/me/notifications", headers=alice, params={"unread": True}))["items"]
    assert notices[0]["id"] not in {n["id"] for n in unread}
    assert all(not n["template_id"].startswith("email:") for n in inbox)


def test_browserless_report_contract(client, org_context, monkeypatch):
    _, _, _, analyst, _, _, _, _, _, district, _, _ = org_context
    calls = []

    class Renderer:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            import httpx

            calls.append((url, kwargs))
            content = b"%PDF-1.7\n" if url.endswith("/pdf") else b"\x89PNG\r\n\x1a\n"
            return httpx.Response(200, content=content, request=httpx.Request("POST", url))

    monkeypatch.setattr(settings, "browserless_url", "https://renderer.example.com")
    monkeypatch.setattr("app.analytics.httpx.AsyncClient", Renderer)
    params = {
        "date_from": "2026-01-01",
        "date_to": datetime.now(UTC).date().isoformat(),
        "district_id": district,
    }
    for fmt in ("pdf", "png"):
        response = client.get("/api/v1/analytics/export/" + fmt, headers=analyst, params=params)
        assert response.status_code == 200
    assert len(calls) == 2
    assert all(
        "email" not in kwargs["json"]["html"] and "phone" not in kwargs["json"]["html"] for _, kwargs in calls
    )
