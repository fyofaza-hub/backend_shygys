"""Run persistent MinIO and a Mailpit container for this Linux demo environment."""

import os
import signal
import subprocess
import time
from pathlib import Path

from app.config import settings


def main():
    runtime = Path.home() / ".local/share/shygys-services"
    minio = runtime / "bin/minio"
    podman = runtime / "root/usr/bin/podman"
    if not minio.exists() or not podman.exists():
        raise SystemExit("Install the local MinIO binary and Podman runtime described in DEVELOPMENT.md.")
    environment = {
        **os.environ,
        "MINIO_ROOT_USER": settings.s3_access_key,
        "MINIO_ROOT_PASSWORD": settings.s3_secret_key.get_secret_value(),
        "LD_LIBRARY_PATH": str(runtime / "root/usr/lib/x86_64-linux-gnu"),
        "CONTAINERS_CONF": str(runtime / "config/containers.conf"),
        "CONTAINERS_STORAGE_CONF": str(runtime / "config/storage.conf"),
    }
    if not environment["MINIO_ROOT_USER"] or not environment["MINIO_ROOT_PASSWORD"]:
        raise SystemExit("Set S3_ACCESS_KEY and S3_SECRET_KEY in .env first.")
    for directory in ("minio", "mailpit", "logs"):
        (runtime / directory).mkdir(parents=True, exist_ok=True)
    commands = [
        (
            "minio",
            [
                str(minio),
                "server",
                str(runtime / "minio"),
                "--address",
                "127.0.0.1:9000",
                "--console-address",
                "127.0.0.1:9001",
            ],
        ),
        (
            "mailpit",
            [
                "unshare",
                "-Urm",
                str(podman),
                "--tmpdir",
                str(runtime / "tmp"),
                "run",
                "--replace",
                "--name",
                "shygys-mailpit",
                "--network",
                "host",
                "--cgroups",
                "disabled",
                "--security-opt",
                "label=disable",
                "--user",
                "0:0",
                "--mount",
                "type=bind,source=/dev/pts,destination=/dev/pts",
                "-e",
                "MP_SMTP_BIND_ADDR=127.0.0.1:1025",
                "-e",
                "MP_UI_BIND_ADDR=127.0.0.1:8025",
                "-e",
                "MP_DATABASE=/data/mailpit.db",
                "-v",
                str(runtime / "mailpit") + ":/data",
                "ghcr.io/axllent/mailpit:v1.27.4",
            ],
        ),
    ]
    children = []
    logs = []

    def stop(signum=None, frame=None):
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        for name, command in commands:
            log = (runtime / "logs" / (name + ".log")).open("ab")
            logs.append(log)
            children.append(
                subprocess.Popen(command, env=environment, stdout=log, stderr=log, start_new_session=True)
            )
        print("MinIO and Mailpit started; see local service logs for readiness.", flush=True)
        while all(child.poll() is None for child in children):
            time.sleep(0.5)
    finally:
        stop()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()
        for log in logs:
            log.close()
    return max((child.returncode or 0 for child in children), default=0)


if __name__ == "__main__":
    raise SystemExit(main())
