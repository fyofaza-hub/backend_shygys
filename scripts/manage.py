"""Bootstrap the first administrator and import the approved source dictionary."""

import argparse
import getpass
import re

import psycopg
from openpyxl import load_workbook
from psycopg.types.json import Jsonb
from pydantic import EmailStr, TypeAdapter

from app.auth import passwords
from app.config import settings
from app.schemas import Localized


def bootstrap(email: str):
    email = TypeAdapter(EmailStr).validate_python(email).lower()
    password = getpass.getpass("Initial superadmin password: ")
    if not 12 <= len(password) <= 128:
        raise SystemExit("Password must contain 12 to 128 characters.")
    with psycopg.connect(settings.database_url) as db:
        db.execute("LOCK TABLE user_roles IN SHARE ROW EXCLUSIVE MODE")
        if db.execute("SELECT 1 FROM user_roles WHERE role='superadmin'").fetchone():
            raise SystemExit("Initial superadmin already exists.")
        user_id = db.execute(
            "INSERT INTO users(email,password_hash,verified_at) VALUES (%s,%s,now()) RETURNING id",
            (email, passwords.hash(password)),
        ).fetchone()[0]
        db.execute("INSERT INTO user_roles(user_id,role) VALUES (%s,'superadmin')", (user_id,))
        db.execute(
            "INSERT INTO audit_events(actor_id,action,entity_id) VALUES (%s,'superadmin.bootstrapped',%s)",
            (user_id, user_id),
        )


def import_oked(path: str, version: str):
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", version):
        raise SystemExit("Invalid dictionary version.")
    book = load_workbook(path, read_only=True, data_only=True)
    entries = {}
    try:
        rows = book.active.values
        if tuple(next(rows)) != ("code", "kk", "ru", "en"):
            raise SystemExit("Expected four columns: code, kk, ru, en.")
        for code, kk, ru, en in rows:
            if all(v is None for v in (code, kk, ru, en)):
                continue
            labels = Localized(kk=kk, ru=ru, en=en)
            if (
                not isinstance(code, str)
                or not re.fullmatch(r"\d{2}(?:\.?\d){0,8}", code)
                or not labels.complete()
            ):
                raise SystemExit("Provide OKED codes as text and fill all three labels.")
            if code.replace(".", "") in {key.replace(".", "") for key in entries}:
                raise SystemExit(f"Duplicate OKED code: {code}")
            entries[code] = labels.model_dump()
    finally:
        book.close()
    if len(entries) != 43:
        raise SystemExit(f"Expected 43 distinct entries, got {len(entries)}.")
    with psycopg.connect(settings.database_url) as db:
        db.execute("LOCK TABLE oked IN SHARE ROW EXCLUSIVE MODE")
        existing = dict(db.execute("SELECT code,labels FROM oked WHERE version=%s", (version,)).fetchall())
        if existing == entries:
            return
        if existing:
            raise SystemExit("Dictionary version already exists with different data.")
        with db.cursor() as cursor:
            cursor.executemany(
                "INSERT INTO oked(code,labels,version) VALUES (%s,%s,%s)",
                [(k, Jsonb(v), version) for k, v in entries.items()],
            )


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    init = sub.add_parser("bootstrap-admin")
    init.add_argument("email")
    oked = sub.add_parser("import-oked")
    oked.add_argument("path")
    oked.add_argument("--version", required=True)
    args = parser.parse_args()
    if args.action == "bootstrap-admin":
        bootstrap(args.email)
    else:
        import_oked(args.path, args.version)


if __name__ == "__main__":
    main()
