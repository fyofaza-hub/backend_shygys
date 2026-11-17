import asyncio
import hashlib
import io
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException, UploadFile
from PIL import Image
from redis import Redis
from redis.exceptions import ConnectionError
from starlette.datastructures import Headers
from starlette.requests import Request

from app import media
from app.auth import throttle
from app.config import settings
from tests.conftest import account


def test_production_docs_and_errors():
    code = """
from app.main import app
from fastapi.testclient import TestClient
@app.get('/test-error')
def fail():
    raise RuntimeError('private-database-password')
client = TestClient(app, raise_server_exceptions=False)
assert app.debug is False
for path in ('/docs', '/redoc', '/openapi.json', '/docs/oauth2-redirect'):
    assert client.get(path).status_code == 404
response = client.get('/test-error')
assert response.status_code == 500
assert response.json() == {'detail': 'internal server error'}
"""
    subprocess.run(
        [sys.executable, "-c", code], env={**os.environ, "ENV": "production"}, check=True, capture_output=True
    )


def test_redis_failure_blocks_request():
    request = Request(
        {
            "type": "http",
            "client": ("test", 80),
            "app": SimpleNamespace(
                state=SimpleNamespace(redis=SimpleNamespace(eval=AsyncMock(side_effect=ConnectionError)))
            ),
        }
    )
    with pytest.raises(HTTPException) as error:
        asyncio.run(throttle(request, "login", 30, 900))
    assert error.value.status_code == 503


@pytest.mark.integration
def test_real_redis_limits_all_sensitive_routes(client):
    user_id, headers = account(client, "content_editor")
    cases = [
        (
            "/auth/login",
            "login",
            30,
            900,
            client._transport.client[0],
            {"email": "nobody@example.com", "password": "temporary-test-password"},
        ),
        (
            "/auth/register",
            "register",
            8,
            3600,
            client._transport.client[0],
            {
                "email": "nobody@example.com",
                "password": "temporary-test-password",
                "locale": "ru",
                "policy_version": "test",
            },
        ),
        (
            "/auth/reset-password",
            "reset",
            10,
            900,
            client._transport.client[0],
            {"token": "x" * 40, "password": "temporary-test-password"},
        ),
        (
            "/translations/generate",
            "translation",
            20,
            3600,
            user_id,
            {
                "entity_type": "article",
                "entity_id": str(uuid4()),
                "source_locale": "ru",
                "external_processing_approved": True,
            },
        ),
    ]
    with Redis.from_url(settings.redis_url) as redis:
        for route, name, limit, seconds, subject, payload in cases:
            key = f"shygys:limit:{name}:{hashlib.sha256(subject.encode()).hexdigest()}"
            redis.set(key, limit, ex=seconds)
            try:
                for prefix in ("", "/api/v1"):
                    response = client.post(prefix + route, json=payload, headers=headers)
                    assert response.status_code == 429, response.text
                    assert response.headers["retry-after"] == str(seconds)
                assert 0 < redis.ttl(key) <= seconds
            finally:
                redis.delete(key)


@pytest.mark.parametrize(
    "name,mime,body,status",
    [
        ("payload.php", "image/png", None, 422),
        ("payload.sh", "image/png", None, 422),
        ("payload.EXE", "image/png", None, 422),
        ("payload.html", "image/png", None, 422),
        ("payload.svg", "image/svg+xml", b'<svg onload="alert(1)"/>', 422),
        ("payload.php.jpg", "image/png", None, 422),
        ("fake.png", "image/png", b"<?php system('id');", 422),
        ("fake.jpg", "image/jpeg", None, 422),
        ("large.png", "image/png", b"x" * 1025, 413),
    ],
)
def test_upload_rejects_before_storage(monkeypatch, name, mime, body, status):
    monkeypatch.setattr(media, "throttle", AsyncMock())
    monkeypatch.setattr(settings, "media_max_bytes", 1024)
    if body is None:
        stream = io.BytesIO()
        Image.new("RGB", (8, 8)).save(stream, "PNG")
        body = stream.getvalue()
    file = UploadFile(io.BytesIO(body), filename=name, headers=Headers({"content-type": mime}))
    with pytest.raises(HTTPException) as error:
        asyncio.run(media.store_image(file, None, SimpleNamespace(id=uuid4()), None, uuid4(), None))
    assert error.value.status_code == status


@pytest.mark.parametrize("fmt,mime", list(media.FORMAT_MIME.items()))
def test_valid_image_signatures(fmt, mime):
    stream = io.BytesIO()
    Image.new("RGB", (16, 12)).save(stream, fmt)
    assert media.check_image(stream.getvalue(), mime) == (16, 12)
