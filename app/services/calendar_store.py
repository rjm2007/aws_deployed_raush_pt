"""
Supabase REST access for the multi-provider calendar tables.

This is the ONLY module that reads/writes these tables:
  clinics, clinicians, clinician_services, clinician_hours, clinician_time_off,
  visit_types, tebra_appointments, tebra_sync_log
(schema: migrations/001 + 003). Uses the anon REST key like the rest of the app (RLS is off).

Errors: every function raises CalendarStoreError on a non-2xx reply or network error, so callers
can say "I can't check the calendar right now" instead of silently treating it as empty.

Times: tebra_appointments.start_at / end_at are timestamptz (UTC). clinician_hours / time_off
times are clinic wall-clock (America/Los_Angeles), weekday 0=Mon..6=Sun.
"""

import hashlib
from datetime import datetime, timedelta, timezone

import httpx

from app.core.config import SUPABASE_URL, SUPABASE_API_KEY
from app.core.logger import logger

_REST = f"{SUPABASE_URL}/rest/v1"
_PAGE = 1000          # Supabase caps a REST select at 1000 rows
_WRITE_CHUNK = 500
_TIMEOUT = 20.0


class CalendarStoreError(Exception):
    pass


def _headers(prefer: str | None = None) -> dict:
    h = {
        "apikey":        SUPABASE_API_KEY or "",
        "Authorization": f"Bearer {SUPABASE_API_KEY or ''}",
        "Content-Type":  "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def _iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def _in_list(values) -> str:
    return "in.(" + ",".join(str(int(v)) for v in values) + ")"


def appointment_key(raw_id) -> int:
    """
    Tebra appointment ID -> bigint primary key for tebra_appointments.
    Numeric IDs are kept as-is. Recurring-series occurrences come back with non-numeric IDs
    (e.g. '5LGN'): those get a stable NEGATIVE 60-bit hash, so they can never clash with a real ID.
    The raw ID is always stored in tebra_appointment_ref.
    """
    s = str(raw_id).strip()
    if s.isdigit():
        return int(s)
    h = int(hashlib.sha1(s.encode("utf-8")).hexdigest()[:15], 16)
    return -(h or 1)


async def _select_all(table: str, params: list[tuple[str, str]], rid: str = "?") -> list[dict]:
    """GET every row matching params, paging past the 1000-row REST cap."""
    out: list[dict] = []
    offset = 0
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            while True:
                page_params = params + [("limit", str(_PAGE)), ("offset", str(offset))]
                r = await client.get(f"{_REST}/{table}", headers=_headers(), params=page_params)
                if r.status_code not in (200, 206):
                    raise CalendarStoreError(f"select {table} status={r.status_code} body={r.text[:300]}")
                rows = r.json()
                out.extend(rows)
                if len(rows) < _PAGE:
                    break
                offset += _PAGE
    except httpx.HTTPError as e:
        raise CalendarStoreError(f"select {table} network error: {e}") from e
    logger.info("[%s] calendar_store select %s rows=%s", rid, table, len(out))
    return out


async def _write(method: str, table: str, params: list[tuple[str, str]] | None, json_body, prefer: str,
                 rid: str = "?") -> httpx.Response:
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            r = await client.request(method, f"{_REST}/{table}", headers=_headers(prefer),
                                     params=params or [], json=json_body)
    except httpx.HTTPError as e:
        raise CalendarStoreError(f"{method} {table} network error: {e}") from e
    if r.status_code not in (200, 201, 204):
        raise CalendarStoreError(f"{method} {table} status={r.status_code} body={r.text[:300]}")
    return r


# ─── Reference data ──────────────────────────────────────────────────────────

async def get_clinics(active_only: bool = True, rid: str = "?") -> list[dict]:
    params = [("select", "*"), ("order", "tebra_location_id")]
    if active_only:
        params.append(("active", "is.true"))
    return await _select_all("clinics", params, rid)


async def get_clinicians(bookable_only: bool = False, rid: str = "?") -> list[dict]:
    """bookable_only=True -> only active clinicians the AI may book."""
    params = [("select", "*"), ("order", "tebra_provider_id")]
    if bookable_only:
        params += [("bookable", "is.true"), ("active", "is.true")]
    return await _select_all("clinicians", params, rid)


async def get_visit_types(rid: str = "?") -> list[dict]:
    return await _select_all("visit_types", [("select", "*")], rid)


async def get_clinician_services(location_id: int | None = None, service_line: str | None = None,
                                 rid: str = "?") -> list[dict]:
    params = [("select", "*")]
    if location_id is not None:
        params.append(("tebra_location_id", f"eq.{int(location_id)}"))
    if service_line:
        params.append(("service_line", f"eq.{service_line}"))
    return await _select_all("clinician_services", params, rid)


async def get_clinician_hours(provider_ids=None, location_id: int | None = None, rid: str = "?") -> list[dict]:
    params = [("select", "*"), ("order", "tebra_provider_id,weekday,start_time")]
    if provider_ids:
        params.append(("tebra_provider_id", _in_list(provider_ids)))
    if location_id is not None:
        params.append(("tebra_location_id", f"eq.{int(location_id)}"))
    return await _select_all("clinician_hours", params, rid)


async def get_clinician_time_off(start_date: str, end_date: str, provider_ids=None, rid: str = "?") -> list[dict]:
    """Exceptions with date in [start_date, end_date] (YYYY-MM-DD, inclusive)."""
    params = [("select", "*"), ("date", f"gte.{start_date}"), ("date", f"lte.{end_date}")]
    if provider_ids:
        params.append(("tebra_provider_id", _in_list(provider_ids)))
    return await _select_all("clinician_time_off", params, rid)


# ─── Calendar copy (tebra_appointments) ──────────────────────────────────────

async def get_tebra_appointments(start_utc: datetime, end_utc: datetime, provider_ids=None,
                                 rid: str = "?") -> list[dict]:
    """Rows that OVERLAP [start_utc, end_utc). Includes cancelled rows — the engine skips them."""
    params = [
        ("select", "*"),
        ("start_at", f"lt.{_iso(end_utc)}"),
        ("end_at", f"gt.{_iso(start_utc)}"),
        ("order", "start_at"),
    ]
    if provider_ids:
        params.append(("tebra_provider_id", _in_list(provider_ids)))
    return await _select_all("tebra_appointments", params, rid)


async def upsert_tebra_appointments(rows: list[dict], rid: str = "?") -> int:
    """Insert or update by tebra_appointment_id. All rows must have the same keys."""
    n = 0
    for i in range(0, len(rows), _WRITE_CHUNK):
        chunk = rows[i:i + _WRITE_CHUNK]
        await _write("POST", "tebra_appointments", [("on_conflict", "tebra_appointment_id")], chunk,
                     "resolution=merge-duplicates,return=minimal", rid)
        n += len(chunk)
    return n


async def replace_tebra_window(start_utc: datetime, end_utc: datetime, rows: list[dict],
                               run_started_at: datetime, keep_ai_minutes: int = 15, rid: str = "?") -> dict:
    """
    Make the copy for appointments STARTING in [start_utc, end_utc) equal to `rows` (fresh from Tebra).
    1) upsert the fresh rows (synced_at = run_started_at, source = 'sync')
    2) delete rows in the window that this run did not see (synced_at < run_started_at),
       EXCEPT 'ai_booking' rows younger than keep_ai_minutes (booked by us, Tebra may not show them yet).
    Upsert-then-delete means the window is never empty while the sync runs.
    """
    upserted = await upsert_tebra_appointments(rows, rid) if rows else 0
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=keep_ai_minutes)
    params = [
        ("start_at", f"gte.{_iso(start_utc)}"),
        ("start_at", f"lt.{_iso(end_utc)}"),
        ("synced_at", f"lt.{_iso(run_started_at)}"),
        ("or", f"(source.eq.sync,synced_at.lt.{_iso(cutoff)})"),
        ("select", "tebra_appointment_id"),
    ]
    r = await _write("DELETE", "tebra_appointments", params, None, "return=representation", rid)
    deleted = len(r.json() or [])
    logger.info("[%s] calendar_store replace_window %s..%s upserted=%s deleted=%s",
                rid, _iso(start_utc), _iso(end_utc), upserted, deleted)
    return {"upserted": upserted, "deleted": deleted}


async def insert_tebra_appointment(row: dict, rid: str = "?") -> None:
    """
    Add ONE appointment right after an AI booking so availability sees it before the next sync.
    Defaults source='ai_booking'. Idempotent (upsert on tebra_appointment_id).
    Expected keys: tebra_appointment_id, tebra_provider_id, tebra_location_id, start_at, end_at,
    duration_minutes, status, appt_type, tebra_patient_id.
    """
    body = dict(row)
    body["tebra_appointment_ref"] = str(body.get("tebra_appointment_ref") or body["tebra_appointment_id"])
    body["tebra_appointment_id"] = appointment_key(body["tebra_appointment_id"])
    body.setdefault("source", "ai_booking")
    body.setdefault("appt_type", "P")
    body.setdefault("status", "scheduled")
    body["synced_at"] = _iso(datetime.now(timezone.utc))
    for k in ("start_at", "end_at"):
        if isinstance(body.get(k), datetime):
            body[k] = _iso(body[k])
    await _write("POST", "tebra_appointments", [("on_conflict", "tebra_appointment_id")], [body],
                 "resolution=merge-duplicates,return=minimal", rid)
    logger.info("[%s] calendar_store inserted appt=%s provider=%s start=%s",
                rid, body["tebra_appointment_ref"], body.get("tebra_provider_id"), body.get("start_at"))


async def get_tebra_appointment(raw_id, rid: str = "?") -> dict | None:
    """One row of the calendar copy by Tebra appointment ID (or None)."""
    rows = await _select_all("tebra_appointments",
                             [("select", "*"), ("tebra_appointment_id", f"eq.{appointment_key(raw_id)}")], rid)
    return rows[0] if rows else None


async def delete_tebra_appointment(raw_id, rid: str = "?") -> int:
    """Remove one appointment from the copy (after a cancel / delete in Tebra). Returns rows removed."""
    r = await _write("DELETE", "tebra_appointments",
                     [("tebra_appointment_id", f"eq.{appointment_key(raw_id)}"), ("select", "tebra_appointment_id")],
                     None, "return=representation", rid)
    return len(r.json() or [])


async def delete_tebra_appointments_ended_before(utc_dt: datetime, rid: str = "?") -> int:
    r = await _write("DELETE", "tebra_appointments",
                     [("end_at", f"lt.{_iso(utc_dt)}"), ("select", "tebra_appointment_id")],
                     None, "return=representation", rid)
    return len(r.json() or [])


# ─── Sync log ────────────────────────────────────────────────────────────────

async def start_sync_log(window_start: str, window_end: str, rid: str = "?") -> int | None:
    r = await _write("POST", "tebra_sync_log", None,
                     {"window_start": window_start, "window_end": window_end},
                     "return=representation", rid)
    rows = r.json() or []
    return rows[0]["id"] if rows else None


async def finish_sync_log(log_id: int | None, rows: int, ok: bool, error: str | None = None, rid: str = "?") -> None:
    if log_id is None:
        return
    await _write("PATCH", "tebra_sync_log", [("id", f"eq.{int(log_id)}")],
                 {"finished_at": _iso(datetime.now(timezone.utc)), "rows": rows, "ok": ok,
                  "error": (error or None) and str(error)[:2000]},
                 "return=minimal", rid)
