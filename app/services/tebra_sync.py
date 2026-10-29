"""
Tebra -> Supabase calendar sync (multi-provider availability reads the Supabase copy).

- Pulls GetAppointments for the whole practice in chunks of <= 7 days (1 call/second, see tebra_service).
- Tebra is asked for UTC (TimeZoneOffsetFromGMT=0), so every row is stored as real timestamptz,
  correct on both sides of a DST change.
- Each chunk REPLACES its window in tebra_appointments (rows deleted in Tebra disappear), keeping
  fresh 'ai_booking' rows Tebra does not show yet. A failed chunk leaves its window untouched.
- Rows that ended before today (clinic time) are deleted.
- One tebra_sync_log row per run.

Run by scheduler_tebra_sync.py: days 0-14 every 10 min, days 15-60 every 60 min.
"""

from collections import Counter
from datetime import datetime, timedelta, timezone

from app.core.config import AVAILABILITY_WINDOW_DAYS
from app.core.logger import logger
from app.services import calendar_store
from app.services.tebra_service import call_tebra_get_practice_appointments
from app.utils.time_utils import CLINIC_TZ, now_la

SYNC_CHUNK_DAYS = 7
KEEP_AI_BOOKING_MINUTES = 15


def _la_midnight_utc(d) -> datetime:
    return datetime(d.year, d.month, d.day, tzinfo=CLINIC_TZ).astimezone(timezone.utc)


def _status(raw: str | None) -> str:
    s = (raw or "").strip().lower()
    if "cancel" in s:
        return "cancelled"
    return s or "scheduled"


def _appt_type(raw: str | None) -> str:
    """Tebra returns 'Patient' / 'Other' (docs say P / O). Stored as 'P' or 'O'."""
    s = (raw or "").strip().upper()
    return "O" if s.startswith("O") else "P"


def provider_from_resources(resources: list[tuple[int, int | None]]) -> int | None:
    """Clinician = the first resource with ResourceTypeID 1 (Doctor). Type 2 = practice resource (ignored)."""
    for res_id, res_type in resources or []:
        if res_type == 1:
            return res_id
    return None


def map_row(r: dict, synced_at: datetime) -> dict:
    """tebra_service practice row -> tebra_appointments row (all rows have the same keys)."""
    loc = r.get("location_id")
    pid = r.get("patient_id")
    return {
        "tebra_appointment_id":  calendar_store.appointment_key(r["id"]),
        "tebra_appointment_ref": str(r["id"]),
        "tebra_provider_id":     provider_from_resources(r.get("resources")),
        "tebra_location_id":     int(loc) if loc and str(loc).isdigit() else None,
        "start_at":              r["start_utc"].isoformat(),
        "end_at":                r["end_utc"].isoformat(),
        "duration_minutes":      r.get("duration"),
        "status":                _status(r.get("status")),
        "appt_type":             _appt_type(r.get("type")),
        "tebra_patient_id":      int(pid) if pid and str(pid).isdigit() else None,
        "source":                "sync",
        "synced_at":             synced_at.isoformat(),
    }


async def sync_window(day_from: int, day_to: int, rid: str = "sync") -> dict:
    """
    Sync clinic dates today+day_from .. today+day_to (inclusive, America/Los_Angeles).
    Returns {ok, rows, chunks_ok, chunks_failed, errors, per_provider: Counter, window}.
    """
    today = now_la().date()
    d_first = today + timedelta(days=day_from)
    d_last = today + timedelta(days=day_to)
    window = (d_first.isoformat(), d_last.isoformat())
    run_started = datetime.now(timezone.utc)
    log_id = None
    try:
        log_id = await calendar_store.start_sync_log(*window, rid=rid)
    except Exception as e:
        logger.error("[%s] tebra_sync could not write sync log start: %s", rid, e)

    total, chunks_ok, chunks_failed, errors = 0, 0, 0, []
    per_provider: Counter = Counter()
    c0 = d_first
    while c0 <= d_last:
        c1 = min(c0 + timedelta(days=SYNC_CHUNK_DAYS - 1), d_last)
        ws, we = _la_midnight_utc(c0), _la_midnight_utc(c1 + timedelta(days=1))
        # Tebra filters by UTC day here: ask for every UTC date the LA window touches, then trim.
        res = await call_tebra_get_practice_appointments(
            ws.date().isoformat(), (we - timedelta(seconds=1)).date().isoformat(), rid=rid)
        if not res["ok"]:
            chunks_failed += 1
            errors.append(f"{c0}..{c1}: {res['error_message']}")
            logger.error("[%s] tebra_sync chunk %s..%s FAILED (window kept): %s", rid, c0, c1, res["error_message"])
            c0 = c1 + timedelta(days=1)
            continue
        rows = [map_row(r, run_started) for r in res["rows"] if ws <= r["start_utc"] < we]
        try:
            await calendar_store.replace_tebra_window(ws, we, rows, run_started,
                                                      keep_ai_minutes=KEEP_AI_BOOKING_MINUTES, rid=rid)
            chunks_ok += 1
            total += len(rows)
            per_provider.update(r["tebra_provider_id"] for r in rows)
            logger.info("[%s] tebra_sync chunk %s..%s rows=%s", rid, c0, c1, len(rows))
        except Exception as e:
            chunks_failed += 1
            errors.append(f"{c0}..{c1}: supabase {e}")
            logger.error("[%s] tebra_sync chunk %s..%s supabase write FAILED: %s", rid, c0, c1, e)
        c0 = c1 + timedelta(days=1)

    ok = chunks_failed == 0
    try:
        await calendar_store.finish_sync_log(log_id, total, ok, "; ".join(errors) or None, rid=rid)
    except Exception as e:
        logger.error("[%s] tebra_sync could not write sync log finish: %s", rid, e)
    logger.info("[%s] tebra_sync DONE window=%s..%s rows=%s ok=%s chunks_ok=%s failed=%s",
                rid, window[0], window[1], total, ok, chunks_ok, chunks_failed)
    return {"ok": ok, "rows": total, "chunks_ok": chunks_ok, "chunks_failed": chunks_failed,
            "errors": errors, "per_provider": per_provider, "window": window}


async def cleanup_past(rid: str = "sync") -> int:
    """Delete copies of appointments that ended before today (clinic time)."""
    n = await calendar_store.delete_tebra_appointments_ended_before(_la_midnight_utc(now_la().date()), rid=rid)
    logger.info("[%s] tebra_sync cleanup removed=%s", rid, n)
    return n


async def sync_fast(rid: str = "sync-fast") -> dict:
    return await sync_window(0, 14, rid)


async def sync_slow(rid: str = "sync-slow") -> dict:
    return await sync_window(15, AVAILABILITY_WINDOW_DAYS, rid)
