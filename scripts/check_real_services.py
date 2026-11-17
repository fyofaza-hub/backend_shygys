"""Exercise demo API against actual S3, Mailpit and optional external Browserless."""

import argparse
import hashlib
import io
import json
import re
import time
from datetime import date
from pathlib import Path
from secrets import token_urlsafe
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
from PIL import Image

from app.config import settings
from app.media import s3


def poll(fn, timeout=90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = fn()
        if result:
            return result
        time.sleep(1)
    raise RuntimeError("service operation did not finish within the timeout")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--mailpit", default="http://127.0.0.1:8025")
    parser.add_argument("--report", default="/tmp/shygys-real-services.json")
    parser.add_argument("--require-browserless", action="store_true")
    args = parser.parse_args()
    report = {
        "s3": "pending",
        "smtp": "pending",
        "browserless": "unconfigured",
        "clamav_enabled": settings.enable_clamav,
    }
    credentials = Path("DEMO_ACCESS.md").read_text()
    password = re.search(r"пароль `([^`]+)`", credentials)[1]
    accounts = dict(re.findall(r"\| (\w+) \| ([^ ]+@[^ ]+) \|", credentials))
    with (
        httpx.Client(base_url=args.api, timeout=90, trust_env=False) as api,
        httpx.Client(base_url=args.mailpit, timeout=10, trust_env=False) as mailbox,
    ):

        def request(method, path, **kwargs):
            response = api.request(method, path, **kwargs)
            response.raise_for_status()
            return response.json() if response.content else None

        def login(email, account_password):
            token = request("POST", "/api/v1/auth/login", json={"email": email, "password": account_password})
            return {"Authorization": "Bearer " + token["access_token"]}

        def find_message(email, subject=None):
            response = mailbox.get("/api/v1/messages")
            response.raise_for_status()
            for message in response.json()["messages"]:
                if any(person["Address"] == email for person in message["To"]) and (
                    subject is None or message["Subject"] == subject
                ):
                    result = mailbox.get("/api/v1/message/" + message["ID"])
                    result.raise_for_status()
                    return result.json()
            return None

        request("GET", "/health/ready")
        email = "real-service-" + uuid4().hex + "@example.com"
        account_password = token_urlsafe(24)
        response = api.post(
            "/api/v1/auth/register",
            json={
                "email": email,
                "password": account_password,
                "locale": "ru",
                "policy_version": "demo-services-v1",
            },
        )
        assert response.status_code == 202, response.status_code
        message = poll(lambda: find_message(email))
        link = re.search(r"https?://\S+", message["Text"])[0]
        token = parse_qs(urlsplit(link).query)["token"][0]
        response = api.post("/api/v1/auth/verify-email", json={"token": token})
        assert response.status_code == 204, response.status_code
        creator = login(email, account_password)
        owner_id = request("GET", "/api/v1/me", headers=creator)["id"]
        blob = io.BytesIO()
        Image.new("RGB", (1400, 1000), (37, 110, 167)).save(blob, "JPEG", quality=95)
        original = blob.getvalue()
        response = api.post(
            "/api/v1/media/upload",
            headers=creator,
            files={"file": ("service-test.jpg", original, "image/jpeg")},
            data={"rights": "Demo service verification"},
        )
        assert response.status_code == 202, response.status_code
        asset = response.json()

        def ready_asset():
            status = request("GET", "/api/v1/me/media/" + asset["id"], headers=creator)
            if status["status"] == "failed":
                raise RuntimeError("media worker failed")
            return status if status["status"] == "ready" else None

        asset = poll(ready_asset)
        dimensions = {}
        for variant, url in asset["variants"].items():
            result = api.get(url, headers=creator)
            result.raise_for_status()
            if variant == "original":
                assert result.content == original
            else:
                with Image.open(io.BytesIO(result.content)) as image:
                    assert image.format == "WEBP"
                    assert max(image.size) <= (320 if variant == "thumbnail" else 960)
                    dimensions[variant] = list(image.size)
        assert api.get(asset["variants"]["original"]).status_code == 401
        assert api.get(f"/api/v1/media/{asset['id']}/320").status_code == 404
        bad = api.post(
            "/api/v1/media/upload",
            headers=creator,
            files={"file": ("false.jpg", b"not an image", "image/jpeg")},
        )
        assert bad.status_code == 422
        objects = s3().list_objects_v2(Bucket=settings.s3_bucket, Prefix="previews/" + asset["id"] + "/")
        assert objects["KeyCount"] == 2
        original_key = f"originals/{owner_id}/{asset['id']}"
        body = s3().get_object(Bucket=settings.s3_bucket, Key=original_key)["Body"]
        try:
            assert body.read() == original
        finally:
            body.close()
        public_object = httpx.get(
            settings.s3_endpoint.rstrip("/") + "/" + settings.s3_bucket + "/" + original_key,
            timeout=10,
            trust_env=False,
        )
        assert public_object.status_code in (401, 403)
        report["s3"] = {
            "status": "passed",
            "asset_id": asset["id"],
            "previews": dimensions,
            "original_sha256": hashlib.sha256(original).hexdigest(),
            "private": True,
        }
        request(
            "POST",
            "/api/v1/me/creator",
            headers=creator,
            json={"slug": "service-check-" + uuid4().hex, "public_name": "Service check"},
        )
        request(
            "POST",
            "/api/v1/me/consents",
            headers=creator,
            json={"type": "portfolio", "policy_version": "demo-services-v1"},
        )
        work = request(
            "POST",
            "/api/v1/me/portfolio",
            headers=creator,
            json={
                "title": {"kk": "Сынақ", "ru": "Проверка", "en": "Check"},
                "media_ids": [asset["id"]],
                "cover_id": asset["id"],
            },
        )
        request("POST", f"/api/v1/me/portfolio/{work['portfolio_item_id']}/submit", headers=creator)
        moderator = login(accounts["moderator"], password)
        cases = request(
            "GET",
            "/api/v1/moderation/cases",
            headers=moderator,
            params={"creator_id": work["creator_id"], "decision": "pending_review"},
        )["items"]
        request(
            "POST",
            "/api/v1/moderation/cases/" + cases[0]["id"] + "/decision",
            headers=moderator,
            json={"decision": "changes_requested", "reason": "SMTP integration check"},
        )
        decision = poll(lambda: find_message(email, "Статус заявки изменён"))
        assert "SMTP integration check" in decision["Text"]
        report["smtp"] = {
            "status": "passed",
            "verification": True,
            "moderation_decision": True,
            "delivery": "Mailpit SMTP container",
        }
        if settings.browserless_url:
            analyst = login(accounts["analyst"], password)
            signatures = {"pdf": b"%PDF-", "png": b"\x89PNG\r\n\x1a\n"}
            exports = {}
            for fmt, signature in signatures.items():
                result = api.get(
                    "/api/v1/analytics/export/" + fmt,
                    headers=analyst,
                    params={"date_from": "2026-01-01", "date_to": date.today().isoformat()},
                )
                result.raise_for_status()
                assert result.content.startswith(signature)
                target = Path(args.report).with_suffix("." + fmt)
                target.write_bytes(result.content)
                if fmt == "png":
                    with Image.open(io.BytesIO(result.content)) as image:
                        image.verify()
                exports[fmt] = {"bytes": len(result.content), "path": str(target)}
            report["browserless"] = {"status": "passed", "exports": exports}
    Path(args.report).write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    if args.require_browserless and report["browserless"] == "unconfigured":
        raise SystemExit("External BROWSERLESS_URL is required for PDF/PNG verification.")


if __name__ == "__main__":
    main()
