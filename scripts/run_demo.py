"""Run the API and ARQ worker against a pre-seeded demo database."""

import os
import signal
import socket
import subprocess
import sys
import time
from secrets import token_hex

from dotenv import dotenv_values


def lan_address():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as connection:
        try:
            connection.connect(("192.0.2.1", 80))
            return connection.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def main():
    values = {
        **{key: value for key, value in dotenv_values(".env").items() if value is not None},
        **os.environ,
    }
    values.setdefault("DATABASE_URL", "postgresql://ts-checkin@127.0.0.1:55432/shygys_demo")
    values.setdefault("REDIS_URL", "redis://127.0.0.1:56379/11")
    address = lan_address()
    # Fresh JWT key per demo launch unless the operator explicitly configures one.
    if not values.get("JWT_SECRET"):
        values["JWT_SECRET"] = token_hex(32)
    values.setdefault(
        "CORS_ORIGINS", '["http://localhost:3000","http://127.0.0.1:3000","http://' + address + ':3000"]'
    )
    values.setdefault("FRONTEND_URL", f"http://{address}:3000")
    port = values.get("PORT", "8000")
    processes = []

    def stop(signum=None, frame=None):
        for process in processes:
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        processes.append(
            subprocess.Popen(
                [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", port],
                env=values,
            )
        )
        processes.append(
            subprocess.Popen([sys.executable, "-m", "arq", "app.worker.WorkerSettings"], env=values)
        )
        print(f"Demo API: http://{address}:{port}/docs", flush=True)
        while all(process.poll() is None for process in processes):
            time.sleep(0.5)
    finally:
        stop()
        for process in processes:
            try:
                process.wait(timeout=8)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    return max((process.returncode or 0 for process in processes), default=0)


if __name__ == "__main__":
    raise SystemExit(main())
