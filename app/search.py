import math
import re
from typing import Annotated, Literal
from uuid import UUID

import httpx
from fastapi import APIRouter, Query, Request
from pydantic import Field

from app.auth import throttle
from app.config import settings
from app.db import DB, many
from app.registry import PUBLIC
from app.schemas import Locale, Model, Page, PageQuery
from app.translation import ai_headers

router = APIRouter(tags=["search"])
SearchKind = Literal["creator", "opportunity"]
CONTACT_PATTERN = re.compile(r"\S+@\S+\.\S+|(?:\d[\s().-]*){10,}")


class SearchQuery(PageQuery):
    q: str = Field(min_length=2, max_length=200)
    locale: Locale = "kk"
    kind: SearchKind | None = None
    semantic: bool = False


class SearchHit(Model):
    id: UUID
    kind: SearchKind
    slug: str | None
    title: str


async def ranked(client: httpx.AsyncClient, phrase: str, rows: list[dict]) -> list[dict]:
    base = settings.alemai_api_root
    response = await client.post(
        f"{base}/embeddings",
        headers=ai_headers(),
        json={
            "model": settings.alemai_embedding_model,
            "input": [phrase, *[r["ranking_text"] for r in rows]],
        },
    )
    response.raise_for_status()
    vectors = sorted(response.json()["data"], key=lambda item: item["index"])
    if [v["index"] for v in vectors] != list(range(len(rows) + 1)):
        raise ValueError("invalid embedding response")
    reference = vectors[0]["embedding"]
    reference_norm = math.hypot(*reference)
    scores = []
    for row, vector in zip(rows, vectors[1:], strict=True):
        values = vector["embedding"]
        similarity = math.fsum(a * b for a, b in zip(reference, values, strict=True)) / (
            reference_norm * math.hypot(*values)
        )
        if not math.isfinite(similarity):
            raise ValueError("invalid cosine similarity")
        scores.append((similarity, row))
    selected = [row for _, row in sorted(scores, key=lambda item: item[0], reverse=True)]
    if not settings.alemai_reranker_model or not settings.alemai_reranker_url:
        return selected
    response = await client.post(
        settings.alemai_reranker_url,
        headers=ai_headers(),
        json={
            "model": settings.alemai_reranker_model,
            "query": phrase,
            "documents": [r["ranking_text"] for r in selected],
        },
    )
    response.raise_for_status()
    results = response.json()["results"]
    if len(results) != len(selected) or {r["index"] for r in results} != set(range(len(selected))):
        raise ValueError("invalid reranker response")
    return [selected[r["index"]] for r in sorted(results, key=lambda r: r["relevance_score"], reverse=True)]


@router.get("/search")
async def search(f: Annotated[SearchQuery, Query()], request: Request, db: DB) -> Page[SearchHit]:
    await throttle(request, "search", 60, 60)
    # A bounded lexical shortlist always exists even when the external ranking provider is disabled.
    rows = await many(
        db,
        f"""
        WITH candidates AS (
            SELECT c.id,'creator' AS kind,c.slug,c.public_name AS title,
                   greatest(similarity(c.public_name,%s),word_similarity(%s,c.search_text)) AS score,
                   EXISTS(SELECT 1 FROM consents s WHERE s.user_id=c.user_id
                          AND s.type='ai_search' AND s.withdrawn_at IS NULL) AS external_allowed,
                   left(c.approved->'description'->>%s::text,500) AS ranking_text
            FROM creators c WHERE {PUBLIC} AND %s::text IS DISTINCT FROM 'opportunity'
            UNION ALL
            SELECT id,'opportunity',NULL,fields->'title'->>%s::text,
                   word_similarity(%s,fields::text || eligibility::text),external_ai_search,left(fields->'title'->>%s::text,500)
            FROM opportunities WHERE status='published' AND (publish_at IS NULL OR publish_at<=now()) AND %s::text IS DISTINCT FROM 'creator'
        )
        SELECT * FROM candidates WHERE score>=0.1 ORDER BY score DESC,id LIMIT 60
    """,
        (f.q, f.q, f.locale, f.kind, f.locale, f.q, f.locale, f.kind),
    )
    if (
        f.semantic
        and settings.alemai_enabled
        and settings.alemai_base_url
        and settings.alemai_embedding_model
        and (settings.alemai_api_key.get_secret_value())
        and not CONTACT_PATTERN.search(f.q)
    ):
        eligible = [
            r
            for r in rows
            if r["external_allowed"] and r["ranking_text"] and not CONTACT_PATTERN.search(r["ranking_text"])
        ]
        if eligible:
            try:
                async with httpx.AsyncClient(timeout=4) as client:
                    updated = iter(await ranked(client, f.q, eligible))
                    eligible_ids = {r["id"] for r in eligible}
                    rows = [next(updated) if r["id"] in eligible_ids else r for r in rows]
            except (httpx.HTTPError, ValueError, KeyError, TypeError, ArithmeticError):
                pass
    offset = (f.page - 1) * f.limit
    return Page[SearchHit](
        items=[
            SearchHit(**{k: r[k] for k in SearchHit.model_fields}) for r in rows[offset : offset + f.limit]
        ],
        total=len(rows),
        page=f.page,
        limit=f.limit,
    )
