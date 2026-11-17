"""ClamAV INSTREAM scanning for originals before they enter object storage."""

import socket
import struct

from app.config import settings


class MalwareFound(ValueError):
    pass


def scan_image(data: bytes):
    if not settings.enable_clamav and not settings.require_media_scan:
        return
    if not settings.clamav_host:
        raise OSError("media scanner is unconfigured")
    with socket.create_connection(
        (settings.clamav_host, settings.clamav_port), timeout=settings.clamav_timeout
    ) as scanner:
        scanner.sendall(b"zINSTREAM\0")
        for offset in range(0, len(data), 65536):
            chunk = data[offset : offset + 65536]
            scanner.sendall(struct.pack(">I", len(chunk)) + chunk)
        scanner.sendall(b"\0\0\0\0")
        with scanner.makefile("rb") as replies:
            reply = bytearray()
            while len(reply) < 4096:
                byte = replies.read(1)
                if byte in (b"\0", b""):
                    break
                reply.extend(byte)
    if reply.endswith(b" FOUND"):
        raise MalwareFound("file rejected by media scanner")
    if reply != b"stream: OK":
        raise OSError("media scanner returned an invalid result")
