"""
Data + outside-system work for the v2 (multi-provider) booking endpoints.

- Reference data (clinics, clinicians, services, hours) from calendar_store, cached for 60 s per process.
- open_slots_for(): loads hours / time off / the Tebra copy and runs the pure availability engine.
- live_conflicts(): LIVE Tebra GetAppointments right before a booking (the copy can be ~10 min old).
- resolve_patient(): reuse a Tebra patient only on first + last name AND phone match, else create one.
- Supabase helpers for leads / appointments used by booking_v2 (REST, anon key, like the rest of the app).

Never imports from app/api.
"""

import asyncio
import time
from datetime import date as date_cls, datetime, timedelta, timezone

import httpx

from app.core.config import SUPABASE_URL, SUPABASE_HEADERS, V2_EARLIEST_BOOKING_DATE
from app.core.logger import logger
from app.services import calendar_store
from app.services.tebra_service import (
    call_tebra_get_practice_appointments,
    create_patient_v2,
    find_patients_by_name,
)
from app.services.tebra_sync import map_row
from app.utils.availability_engine import NON_BLOCKING_STATUSES, Slot, open_slots
from app.utils.time_utils import CLINIC_TZ, now_la

_REF_TTL_SECONDS = 60
_ref_cache: dict = {"at": 0.0, "data": None}
_ref_lock = asyncio.Lock()


# ─── Reference data ──────────────────────────────────────────────────────────

async def load_reference(rid: str = "?") -> dict:
    """{"clinics", "clinicians" (all, incl. not bookable), "services", "hours"} — cached 60 s."""
    async with _ref_lock:
        if _ref_cache["data"] is not None and time.monotonic() - _ref_cache["at"] < _REF_TTL_SECONDS:
            return _ref_cache["data"]
        clinics, clinicians, services, hours = await asyncio.gather(
            calendar_store.get_clinics(active_only=True, rid=rid),
            calendar_store.get_clinicians(bookable_only=False, rid=rid),
            calendar_store.get_clinician_services(rid=rid),
            calendar_store.get_clinician_hours(rid=rid),
        )
        data = {"clinics": clinics, "clinicians": clinicians, "services": services, "hours": hours}
        _ref_cache.update(at=time.monotonic(), data=data)
        return data


def clear_reference_cache() -> None:
    _ref_cache.update(at=0.0, data=None)


def providers_for(ref: dict, location_id: int, service_line: str | None) -> set[int]:
    """Bookable clinicians offering that service line at that clinic (service_line None = any line)."""
    bookable = {int(c["tebra_provider_id"]) for c in ref["clinicians"]
                if c.get("bookable") is not False and c.get("active") is not False}
    return {
        int(s["tebra_provider_id"]) for s in ref["services"]
        if int(s["tebra_location_id"]) == int(location_id)
        and (service_line is None or s["service_line"] == service_line)
        and int(s["tebra_provider_id"]) in bookable
    }


def clinics_of(ref: dict, provider_id: int) -> list[int]:
    """Clinic IDs where this clinician has any service row (sorted)."""
    return sorted({int(s["tebra_location_id"]) for s in ref["services"] if int(s["tebra_provider_id"]) == int(provider_id)})


def clinician_by_id(ref: dict, provider_id) -> dict | None:
    try:
        pid = int(provider_id)
    except (TypeError, ValueError):
        return None
    return next((c for c in ref["clinicians"] if int(c["tebra_provider_id"]) == pid), None)


# ─── Slots ───────────────────────────────────────────────────────────────────

def booking_floor(today: date_cls) -> date_cls:
    """First date v2 may offer / book: today, or V2_EARLIEST_BOOKING_DATE when set (test safety)."""
    if V2_EARLIEST_BOOKING_DATE:
        try:
            return max(today, date_cls.fromisoformat(V2_EARLIEST_BOOKING_DATE))
        except ValueError:
            logger.warning("V2_EARLIEST_BOOKING_DATE=%r is not YYYY-MM-DD — ignored", V2_EARLIEST_BOOKING_DATE)
    return today


async def open_slots_for(ref: dict, clinic: dict, provider_ids: set[int], duration: int,
                         start_date: str, end_date: str, gender: str | None = None,
                         extra_bookings: list[dict] | None = None, rid: str = "?") -> list[Slot]:
    """Open starts for these clinicians at one clinic between two clinic dates (engine clips to the window)."""
    if not provider_ids:
        return []
    today = now_la().date()
    floor = booking_floor(today)
    d0 = max(date_cls.fromisoformat(start_date), floor)
    d1 = date_cls.fromisoformat(end_date)
    if d1 < d0:
        return []
    start_utc = datetime(d0.year, d0.month, d0.day, tzinfo=CLINIC_TZ).astimezone(timezone.utc)
    end_utc = (datetime(d1.year, d1.month, d1.day, tzinfo=CLINIC_TZ) + timedelta(days=1)).astimezone(timezone.utc)
    time_off, bookings = await asyncio.gather(
        calendar_store.get_clinician_time_off(d0.isoformat(), d1.isoformat(), provider_ids=provider_ids, rid=rid),
        calendar_store.get_tebra_appointments(start_utc, end_utc, provider_ids=provider_ids, rid=rid),
    )
    hours = [h for h in ref["hours"] if int(h["tebra_provider_id"]) in provider_ids]
    return open_slots(
        clinicians=ref["clinicians"], hours=hours, time_off=time_off,
        bookings=bookings + list(extra_bookings or []),
        location_id=int(clinic["tebra_location_id"]),
        location_label=clinic.get("lead_location_label") or clinic["name"],
        duration=duration, start_date=d0.isoformat(), end_date=d1.isoformat(),
        provider_ids=provider_ids, gender=gender,
    )


# ─── Live Tebra check ────────────────────────────────────────────────────────

async def live_provider_rows(provider_id: int, date_yyyy_mm_dd: str, rid: str = "?") -> dict:
    """
    LIVE Tebra rows for this clinician touching that clinic date (practice-wide GetAppointments, UTC,
    padded by a day; filtered here because Tebra has no provider filter).
    Returns {"ok", "error", "rows": [tebra_appointments-shaped dicts]}.
    """
    d = date_cls.fromisoformat(date_yyyy_mm_dd)
    res = await call_tebra_get_practice_appointments((d - timedelta(days=1)).isoformat(),
                                                     (d + timedelta(days=1)).isoformat(), rid=rid)
    if not res["ok"]:
        await asyncio.sleep(1.5)
        res = await call_tebra_get_practice_appointments((d - timedelta(days=1)).isoformat(),
                                                         (d + timedelta(days=1)).isoformat(), rid=rid)
    if not res["ok"]:
        return {"ok": False, "error": res.get("error_message"), "rows": []}
    now = datetime.now(timezone.utc)
    rows = [map_row(r, now) for r in res["rows"]]
    mine = [r for r in rows if r.get("tebra_provider_id") == int(provider_id)]
    logger.info("[%s] live check provider=%s date=%s practice_rows=%s provider_rows=%s",
                rid, provider_id, date_yyyy_mm_dd, len(rows), len(mine))
    return {"ok": True, "error": None, "rows": mine}


def overlapping(rows: list[dict], start_utc: datetime, end_utc: datetime) -> list[dict]:
    out = []
    for r in rows:
        if str(r.get("status") or "").lower() in NON_BLOCKING_STATUSES:
            continue
        s = datetime.fromisoformat(str(r["start_at"]).replace("Z", "+00:00"))
        e = datetime.fromisoformat(str(r["end_at"]).replace("Z", "+00:00"))
        if s < end_utc and start_utc < e:
            out.append(r)
    return out


# ─── Patient ─────────────────────────────────────────────────────────────────

def _digits10(raw: str | None) -> str:
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


async def resolve_patient(first: str, last: str, phone: str, gender: str | None, rid: str = "?") -> dict:
    """
    Reuse an existing Tebra patient ONLY when first + last name AND phone match; otherwise create one.
    Returns {"ok", "patient_id", "created", "error"}.
    """
    found = await find_patients_by_name(first, last, rid)
    if not found["ok"]:
        return {"ok": False, "patient_id": None, "created": False, "error": found["error"]}
    want = _digits10(phone)
    same_phone = [p for p in found["patients"] if want and want in p["phones"]]
    logger.info("[%s] patient match name_hits=%s phone_hits=%s", rid, len(found["patients"]), len(same_phone))
    if same_phone:
        return {"ok": True, "patient_id": same_phone[0]["id"], "created": False, "error": None}
    created = await create_patient_v2(first, last, phone, gender, rid)
    if not created["ok"]:
        return {"ok": False, "patient_id": None, "created": False, "error": created["error"]}
    return {"ok": True, "patient_id": created["patient_id"], "created": True, "error": None}


# ─── Supabase: leads / appointments ──────────────────────────────────────────

async def _sb_get(path: str, params: list[tuple[str, str]], rid: str) -> list[dict] | None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{SUPABASE_URL}/rest/v1/{path}", headers=SUPABASE_HEADERS, params=params)
        if r.status_code == 200:
            return r.json() or []
        logger.error("[%s] supabase GET %s status=%s body=%s", rid, path, r.status_code, r.text[:300])
    except Exception as e:
        logger.error("[%s] supabase GET %s exception=%s", rid, path, e)
    return None


async def fetch_lead(lead_id: str, rid: str = "?") -> dict | None:
    rows = await _sb_get("leads", [
        ("id", f"eq.{lead_id}"),
        ("select", "id,full_name,phone,service_of_interest,preferred_location,queue_status,lead_outcome,"
                   "tebra_patient_id,metadata,notes"),
    ], rid)
    return rows[0] if rows else None


async def update_lead(lead_id: str, data: dict, metadata_patch: dict | None = None, rid: str = "?") -> bool:
    """PATCH a lead; metadata_patch is merged into the existing metadata json (read-modify-write)."""
    body = dict(data)
    if metadata_patch:
        lead = await fetch_lead(lead_id, rid) or {}
        meta = dict(lead.get("metadata") or {})
        meta.update(metadata_patch)
        body["metadata"] = meta
    body.setdefault("updated_at", datetime.now(timezone.utc).isoformat())
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.patch(f"{SUPABASE_URL}/rest/v1/leads?id=eq.{lead_id}",
                                   headers=SUPABASE_HEADERS, json=body)
        if r.status_code in (200, 204):
            logger.info("[%s] lead %s updated keys=%s", rid, lead_id, sorted(body))
            return True
        logger.error("[%s] lead update failed status=%s body=%s", rid, r.status_code, r.text[:300])
    except Exception as e:
        logger.error("[%s] lead update exception=%s", rid, e)
    return False


async def find_recent_booking(lead_id: str | None, phone_e164: str | None, provider_id: int,
                              date_yyyy_mm_dd: str, start_min: int, within_minutes: int,
                              rid: str = "?") -> dict | None:
    """Scheduled v2 booking for the same lead (or phone, when no lead) + clinician starting within N min."""
    params = [
        ("tebra_provider_id", f"eq.{int(provider_id)}"),
        ("appointment_date", f"eq.{date_yyyy_mm_dd}"),
        ("status", "in.(scheduled,confirmed)"),
        ("select", "id,tebra_appointment_id,tebra_patient_id,appointment_date,appointment_time,location,"
                   "provider_name,visit_type,duration_minutes"),
    ]
    if lead_id:
        params.append(("lead_id", f"eq.{lead_id}"))
    elif phone_e164:
        params.append(("patient_phone", f"eq.{phone_e164}"))
    else:
        return None
    rows = await _sb_get("appointments", params, rid) or []
    for row in rows:
        t = str(row.get("appointment_time") or "")
        try:
            m = int(t[:2]) * 60 + int(t[3:5])
        except ValueError:
            continue
        if abs(m - start_min) <= within_minutes:
            return row
    return None


async def fetch_appointment(appointment_id: str | None = None, tebra_appointment_id: str | None = None,
                            rid: str = "?") -> dict | None:
    params = [("select", "id,lead_id,tebra_appointment_id,status,appointment_date,appointment_time,"
                         "tebra_provider_id,location")]
    if appointment_id:
        params.append(("id", f"eq.{appointment_id}"))
    elif tebra_appointment_id:
        params.append(("tebra_appointment_id", f"eq.{tebra_appointment_id}"))
    else:
        return None
    rows = await _sb_get("appointments", params, rid)
    return rows[0] if rows else None
