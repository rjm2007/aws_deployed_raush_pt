from __future__ import annotations

import re
import time

import httpx

from app.core.config import SUPABASE_HEADERS, SUPABASE_URL
from app.core.logger import logger

# FAQ rows live in Supabase table `clinic_faq` (see sql/clinic_faq.sql). Cached in memory —
# the whole FAQ is ~80 rows, so ranking in Python is faster than a DB round trip per question.
_CACHE: dict = {"rows": [], "at": 0.0}
_TTL_SECONDS = 300

_STOP = set("""a an the is are am do does did i me my we you your our us it its to of for in on at and or
can could would will should be with about what whats how when where which who there this that have has
any please tell know want like just get okay ok hi hey so if from""".split())

# Spoken words → FAQ vocabulary
_SYNONYMS = {
    "insurance": "insurance plan plans ppo hmo medicare coverage accepted",
    "cost": "price prices cash pay self-pay rates", "price": "price prices cash pay self-pay rates",
    "much": "price prices cash rates", "pay": "price cash payment self-pay",
    "open": "hours open saturday", "close": "hours", "hours": "hours open",
    "saturday": "saturday saturdays hours", "sunday": "sunday sundays",
    "address": "address located location directions", "where": "located address location",
    "directions": "directions address", "parking": "parking",
    "therapist": "therapist therapists team provider physical", "doctor": "therapist team provider dr",
    "dizzy": "dizziness balance vestibular", "dizziness": "dizziness balance vestibular",
    "pelvic": "pelvic floor health", "surfer": "surfers athletes", "runner": "runners athletes",
    "choose": "request specific therapist", "pick": "request specific therapist", "specific": "request specific",
    "bring": "bring first visit", "wear": "wear", "cancel": "cancel reschedule cancellation fee",
    "referral": "referral doctor", "kids": "kids children", "spanish": "spanish language",
}


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9']+", (text or "").lower())
    return [w.rstrip("s") if len(w) > 4 else w for w in words if w not in _STOP]


async def _load_rows() -> list[dict]:
    if _CACHE["rows"] and time.time() - _CACHE["at"] < _TTL_SECONDS:
        return _CACHE["rows"]
    params = {"select": "id,category,question,answer,follow_up,keywords", "active": "eq.true", "limit": "500"}
    async with httpx.AsyncClient(timeout=8.0) as client:
        r = await client.get(f"{SUPABASE_URL}/rest/v1/clinic_faq", headers=SUPABASE_HEADERS, params=params)
    if r.status_code != 200:
        logger.error("[faq] load failed status=%s body=%s", r.status_code, (r.text or "")[:200])
        return _CACHE["rows"]  # keep serving the last good copy
    rows = r.json() or []
    for row in rows:
        row["_q"] = set(_tokens(f"{row.get('question')} {row.get('keywords') or ''}"))
        row["_c"] = set(_tokens(row.get("category") or ""))
        row["_a"] = set(_tokens(row.get("answer") or ""))
    _CACHE.update(rows=rows, at=time.time())
    logger.info("[faq] loaded %d rows", len(rows))
    return rows


async def search_faq(question: str, limit: int = 3) -> list[dict]:
    """Return up to `limit` FAQ rows ranked by keyword overlap, each with a 0–1 `score`."""
    rows = await _load_rows()
    q = _tokens(question)
    expanded = set(q)
    for w in q:
        expanded.update(_tokens(_SYNONYMS.get(w, "")))
    if not expanded:
        return []
    scored = []
    for row in rows:
        s = 3 * len(expanded & row["_q"]) + 1 * len(expanded & row["_c"]) + 1 * len(expanded & row["_a"])
        if s:
            scored.append((s, row))
    scored.sort(key=lambda x: -x[0])
    top = scored[0][0] if scored else 1
    return [{**{k: v for k, v in r.items() if not k.startswith("_")}, "score": round(s / top, 2)}
            for s, r in scored[:limit]]
