"""Measure HTTP catalog and map latency with an isolated, deterministic 10,000-profile dataset."""

import argparse
import asyncio
import json
import math
import time

import httpx
import psycopg

from app.config import settings


def seed() -> list[str]:
    with psycopg.connect(settings.database_url) as db:
        if db.info.dbname != "shygys_load" or db.execute("SELECT count(*) FROM creators").fetchone()[0]:
            raise SystemExit("Seeding requires an empty, dedicated database named shygys_load.")
        db.execute("""
            INSERT INTO districts (code,labels,centroid)
            SELECT 'load-' || n,'{"kk":"Сынақ","ru":"Тест","en":"Load"}',
                   ST_SetSRID(ST_MakePoint(82+n*.04,49+n*.03),4326)
            FROM generate_series(0,19) n;
            INSERT INTO users (email,password_hash,verified_at)
            SELECT 'load-' || n || '@example.com','disabled-test-fixture',now()
            FROM generate_series(1,10000) n;
            INSERT INTO consents(user_id,type,policy_version)
            SELECT id,'data_processing','load' FROM users;
            INSERT INTO creators(user_id,slug,public_name,legal_status,moderation_status,publication_status,
                                 district_id,accuracy,public_point,approved,search_text)
            SELECT u.id, 'load-'||n,'Creative workshop '||n,
                   CASE WHEN n % 2=0 THEN 'sole_proprietor'::legal_status ELSE 'individual'::legal_status END,
                   'approved','published',d.id,'district',d.centroid,
                   jsonb_build_object('public_name','Creative workshop '||n,'district_id',d.id,
                   'legal_status',CASE WHEN n % 2=0 THEN 'sole_proprietor' ELSE 'individual' END,
                   'description',jsonb_build_object('kk','Шеберхана','ru','Мастерская','en','Workshop')),
                   'Creative workshop '||n
            FROM generate_series(1,10000) n JOIN users u ON u.email='load-'||n||'@example.com'
            JOIN districts d ON d.code='load-'||(n % 20);
            ANALYZE;
        """)
        return [str(r[0]) for r in db.execute("SELECT id FROM districts ORDER BY code")]


async def run(url: str, districts: list[str], concurrency: int, count: int):
    scenarios = (
        ("catalog", "/api/v1/creators", lambda n: {"page": 1 + n % 10}),
        ("filtered", "/api/v1/creators", lambda n: {"district_id": districts[n % 20]}),
        ("search", "/api/v1/creators", lambda n: {"q": f"workshop {1 + n % 100}"}),
        (
            "map",
            "/api/v1/map/creators",
            lambda n: {"bbox": "81,48,85,51", "zoom": 9, "district_id": districts[n % 20]},
        ),
    )
    timings = {key: [] for key, *_ in scenarios}
    errors = 0
    next_index = iter(range(count))

    async with httpx.AsyncClient(
        base_url=url, timeout=10, limits=httpx.Limits(max_connections=concurrency)
    ) as api:
        ready = await api.get("/health/ready")
        ready.raise_for_status()
        for key, path, params in scenarios:
            response = await api.get(path, params=params(1))
            response.raise_for_status()
            if response.json()["total"] == 0:
                raise SystemExit(f"Empty benchmark fixture for {key}")

        async def client():
            nonlocal errors
            for n in next_index:
                key, path, params = scenarios[n % len(scenarios)]
                started = time.perf_counter()
                try:
                    response = await api.get(path, params=params(n // len(scenarios)))
                    if response.status_code != 200 or response.json()["total"] == 0:
                        errors += 1
                except (httpx.HTTPError, ValueError, KeyError):
                    errors += 1
                timings[key].append(1000 * (time.perf_counter() - started))

        started = time.perf_counter()
        await asyncio.gather(*(client() for _ in range(concurrency)))
        elapsed = time.perf_counter() - started
    report = {
        "profiles": 10000,
        "concurrency": concurrency,
        "requests": count,
        "errors": errors,
        "seconds": round(elapsed, 2),
        "requests_per_second": round(count / elapsed, 2),
        "scenarios": {},
    }
    for key, values in timings.items():
        ordered = sorted(values)
        report["scenarios"][key] = {
            f"p{p}_ms": round(ordered[math.ceil(p / 100 * len(ordered)) - 1], 2) for p in (50, 95, 99)
        }
    print(json.dumps(report, indent=2))
    if errors or any(report["scenarios"][key]["p95_ms"] > 1000 for key in ("catalog", "filtered")):
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:58081")
    parser.add_argument("--concurrency", type=int, default=24)
    parser.add_argument("--requests", type=int, default=2400)
    args = parser.parse_args()
    if args.requests < 4 or args.concurrency < 1:
        parser.error("at least four requests and one client are required")
    asyncio.run(run(args.url, seed(), args.concurrency, args.requests))


if __name__ == "__main__":
    main()
