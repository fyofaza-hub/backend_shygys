import pytest

from app.antivirus import scan_image
from app.config import settings


def test_disabled_scanner_does_not_connect(monkeypatch):
    monkeypatch.setattr(settings, "enable_clamav", False)
    monkeypatch.setattr(settings, "require_media_scan", False)
    monkeypatch.setattr(settings, "clamav_host", "127.0.0.1")

    def unexpected(*args, **kwargs):
        raise AssertionError("disabled antivirus must not connect")

    monkeypatch.setattr("app.antivirus.socket.create_connection", unexpected)
    scan_image(b"image")


def test_enabled_scanner_requires_configuration(monkeypatch):
    monkeypatch.setattr(settings, "enable_clamav", True)
    monkeypatch.setattr(settings, "clamav_host", "")
    with pytest.raises(OSError, match="unconfigured"):
        scan_image(b"image")
