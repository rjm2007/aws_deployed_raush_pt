"""
v2 multi-provider endpoints for the INBOUND agent copy (mounted at /api/v1/v2/...). The live inbound agent
keeps using the old v1 routes, which are untouched.

  POST /v2/inbound-lookup-appointments — caller's upcoming visits with clinician + clinic + ids
  POST /v2/reschedule-appointment      — two-step confirm; same clinician by default; create new FIRST, then delete old
  POST /v2/inbound-cancel-appointment  — two-step confirm; only the caller's own visit; DeleteAppointment

Booking a new visit for an inbound caller uses /v2/create-appointment (no lead_id: phone from Vapi caller ID)
and options come from /v2/check-availability (no lead_id needed).

Inbound calls have no lead_id. The caller's phone comes from Vapi caller ID, not from tool args (args.phone is
only a fallback for Swagger / tests). A visit "belongs to the caller" when its Tebra patient has the name the
caller gave, or our appointments row has the caller's phone.
Every endpoint takes direct JSON or the Vapi wrapper and answers ONE line of plain English; machine values are
in a trailing "[ids ...]" tag (not to be read aloud).
"""

import asyncio
from datetime import date as date_cls, datetime, timedelta, timezone
from uuid import uuid4

from fastapi import APIRouter, Request

from app.core.config import (
    FRONT_DESK_PHONE,
    SUPABASE_PRACTICE_ID,
    TEBRA_INBOUND_APPOINTMENTS_WINDOW_DAYS,
    TEST_PHONE_PREFIXES,
    VISIT_TYPES,
    normalize_visit_type,
    service_line_for,
)
from app.core.logger import logger
from app.models.requests import (
    InboundCancelAppointmentV2Request,
    InboundLookupAppointmentsV2Request,
    RescheduleAppointmentV2Request,
    inline_schema_refs,
)
from app.api.appointments import (
    _inbound_lookup_date_range_la,
    _is_upcoming_in_la,
    _keep_inbound_confirmation_status,
    _sort_inbound_appointments,
)
from app.api.booking_v2 import (
    _SERVICE_LABEL,
    _PENDING_CONFIRMS,
    _alternatives,
    _clean,
    _date_arg,
    _is_true,
    _parse,
    _take_confirm,
)
from app.services import booking_v2_service as svc
from app.services import calendar_store
from app.services.appointment_sms_service import send_appointment_sms_if_needed
from app.services.supabase_service import supabase_insert_appointment, supabase_update_appointment
from app.services.tebra_service import (
    call_tebra_delete_appointment,
    call_tebra_get_appointment,
    call_tebra_get_appointments_by_patient_id,
    call_tebra_get_practice_appointments,
    create_appointment_in_tebra,
    find_patients_by_name,
)
from app.services.twilio_service import _format_phone_e164
from app.utils.booking_v2_logic import clinic_label, ids_tag, options_sentence, resolve_clinic, spoken_day
from app.utils.parser import build_vapi_response, extract_vapi_caller_number_from_body
from app.utils.time_utils import CLINIC_TZ, format_12hr, la_to_utc, now_la, parse_time_to_24hr

router = APIRouter(prefix="/v2", tags=["Booking v2 (multi-provider)"])

# One in-flight change per Tebra appointment (a Vapi retry waits, then hits the idempotency checks).
_APPT_LOCKS: dict[str, asyncio.Lock] = {}
# old tebra id -> what the reschedule made (retry after "new created, old not yet deleted" is safe)
_RESCHEDULED: dict[str, dict] = {}
_MAX_PATIENTS = 3
# tebra ids this process cancelled (repeat replies even when we have no appointments row)
_CANCELLED: set[str] = set()


# ─── shared helpers ──────────────────────────────────────────────────────────

def _digits10(raw) -> str:
    d = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return d[-10:] if len(d) >= 10 else d


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _caller_e164(raw_body, args: dict, tool_call_id=None) -> str | None:
    """Vapi caller ID. args.phone only for direct JSON (Swagger / tests): on a Vapi tool call a spoken phone
    must never count, or a caller with a hidden number could claim someone else's visit."""
    raw = extract_vapi_caller_number_from_body(raw_body)
    if not raw and tool_call_id is None:
        raw = _clean(args.get("phone"))
    return _format_phone_e164(raw) if raw else None


def _is_upcoming(start_utc: datetime | None) -> bool:
    """Only visits that have not started yet may be moved or cancelled (never touch past / billed visits)."""
    return bool(start_utc) and start_utc > _now_utc()


def _split_name(raw) -> tuple[str, str] | None:
    parts = str(raw or "").split()
    return (parts[0], " ".join(parts[1:])) if len(parts) >= 2 else None


def _visit_type_from(sb_row: dict | None, duration: int | None, arg) -> str:
    """Explicit arg -> our appointments row -> length (60+ min = evaluation, else follow-up)."""
    if _clean(arg):
        return normalize_visit_type(arg)
    if sb_row and sb_row.get("visit_type") in VISIT_TYPES:
        return sb_row["visit_type"]
    return "evaluation" if (duration or 0) >= 60 else "follow_up"


def _la_parts(dt: datetime | None) -> tuple[str, int] | None:
    """UTC datetime -> (clinic date, minutes after clinic midnight)."""
    if not dt:
        return None
    la = dt.astimezone(CLINIC_TZ)
    return la.date().isoformat(), la.hour * 60 + la.minute


def _start_utc_of(tebra_appt: dict | None) -> datetime | None:
    raw = (tebra_appt or {}).get("StartTime")
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _line_for(ref: dict, pid: int, location_id: int, service: str | None) -> str | None:
    """Service line for this visit: from the service arg, else what the clinician does at that clinic (PT first)."""
    if _clean(service):
        return service_line_for(service)[0]
    lines = {s["service_line"] for s in ref["services"]
             if int(s["tebra_provider_id"]) == int(pid) and int(s["tebra_location_id"]) == int(location_id)}
    return "physical_therapy" if "physical_therapy" in lines or not lines else sorted(lines)[0]


async def _owns(patient_id, sb_row: dict | None, caller: str | None, name: str | None, rid: str) -> bool:
    """The visit is the caller's when our row has the caller's phone, or the Tebra patient has the given name."""
    if sb_row and caller and _digits10(sb_row.get("patient_phone")) and \
            _digits10(sb_row.get("patient_phone")) == _digits10(caller):
        return True
    split = _split_name(name)
    if not split or not patient_id:
        return False
    found = await find_patients_by_name(split[0], split[1], rid)
    ok = found["ok"] and str(patient_id) in {str(p["id"]) for p in found["patients"]}
    logger.info("[%s] ownership check by name patient=%s name_hits=%s ok=%s",
                rid, patient_id, len(found.get("patients") or []), ok)
    return ok


def _who(ref: dict | None, pid, sb_row: dict | None = None) -> str:
    c = svc.clinician_by_id(ref, pid) if ref and pid else None
    return (c or {}).get("full_name") or (sb_row or {}).get("provider_name") or "the clinician on file"


def _where(ref: dict | None, location_id, fallback: str | None = None) -> str:
    clinic = resolve_clinic(str(location_id), ref["clinics"]) if ref and location_id else None
    return clinic_label(clinic) if clinic else (fallback or "our clinic")


# ─────────────────────────────────────────────────────────────────────────────
# POST /v2/inbound-lookup-appointments
# ─────────────────────────────────────────────────────────────────────────────

def _lookup_line(i: int, a: dict) -> str:
    when = f"{spoken_day(a['date'])} at {format_12hr(a['start_min'] // 60, a['start_min'] % 60)}" \
        if a.get("start_min") is not None else f"{a['date']} at {a.get('time_12') or '?'}"
    return f"({i}) {when} with {a['clinician']} at our {a['clinic']} clinic ({VISIT_TYPES[a['visit_type']]['label']})"


def _lookup_ids(i: int, a: dict) -> str:
    parts = [f"({i}) tebra_appointment_id={a['tebra_appointment_id']}"]
    if a.get("clinician_id"):
        parts.append(f"clinician_id={a['clinician_id']}")
    parts += [f"clinician={a['clinician']}", f"location={a['clinic']}", f"visit_type={a['visit_type']}",
              f"date={a['date']}"]
    if a.get("start_min") is not None:
        parts.append(f"time={a['start_min'] // 60:02d}:{a['start_min'] % 60:02d}")
    if a.get("appointment_id"):
        parts.append(f"appointment_id={a['appointment_id']}")
    return " ".join(parts)


def format_lookup_reply(items: list[dict], window_days: int) -> str:
    """Pure: the spoken list + the ids tag (tested)."""
    if not items:
        return (f"NO APPOINTMENTS: I don't see any upcoming appointments under that name in the next {window_days} days. "
                "Ask them to spell their first and last name to double-check, or offer to book a new one.")
    head = (f"Found 1 upcoming appointment: {_lookup_line(1, items[0])}." if len(items) == 1 else
            f"Found {len(items)} upcoming appointments: " + "; ".join(_lookup_line(i, a) for i, a in enumerate(items, 1)) + ".")
    how = (" Read the day, time, clinician and clinic (not the ids) and ask which one they mean. "
           "To reschedule: keep the same clinician unless they ask for someone else or anyone; call check_availability_v2 "
           "with that clinician_name, location and visit_type, then reschedule_appointment_v2 with the tebra_appointment_id. "
           "To cancel: cancel_appointment_v2 with the tebra_appointment_id.")
    return head + how + " [ids (do not read aloud): " + " | ".join(_lookup_ids(i, a) for i, a in enumerate(items, 1)) + "]"


@router.post(
    "/inbound-lookup-appointments",
    summary="Inbound: caller's upcoming visits with clinician, clinic and the ids for reschedule / cancel",
    openapi_extra={"requestBody": {"content": {"application/json": {
        "schema": inline_schema_refs(InboundLookupAppointmentsV2Request.model_json_schema())}}, "required": True}},
)
async def inbound_lookup_appointments_v2(request: Request):
    rid = str(uuid4())[:8]
    tool_call_id = None
    try:
        args, tool_call_id, raw_body = await _parse(request)
        logger.info("[%s] ======== v2/inbound-lookup-appointments START ======== arg_keys=%s", rid, sorted(args))
        name = _clean(args.get("patient_full_name")) or _clean(args.get("name"))
        split = _split_name(name)
        if not split:
            return build_vapi_response(tool_call_id, "I need the caller's first and last name. Ask them to spell both, then call again.")
        caller = _caller_e164(raw_body, args, tool_call_id)

        found = await find_patients_by_name(split[0], split[1], rid)
        if not found["ok"]:
            logger.error("[%s] v2/lookup GetPatients failed: %s", rid, found["error"])
            return build_vapi_response(tool_call_id, "I can't look up appointments right now. Tell the caller the front desk will call them back, and call update_inbound_status with crm_status manual_follow_up.")
        patients = found["patients"]
        same_phone = [p for p in patients if caller and _digits10(caller) in p["phones"]]
        use = (same_phone or patients)[:_MAX_PATIENTS]
        logger.info("[%s] v2/lookup name_hits=%s phone_hits=%s using=%s", rid, len(patients), len(same_phone), len(use))
        if not use:
            return build_vapi_response(tool_call_id, "NO PATIENT: I can't find a patient with that exact first and last name. Ask them to spell both names again.")

        start_d, end_d = _inbound_lookup_date_range_la()
        now = now_la()
        raw: list[dict] = []
        for p in use:
            res = await call_tebra_get_appointments_by_patient_id(str(p["id"]), start_d, end_d, rid=rid)
            if not res.get("ok"):
                logger.error("[%s] v2/lookup GetAppointments failed patient=%s: %s", rid, p["id"], res.get("error_message"))
                continue
            raw += [a for a in res.get("appointments") or []
                    if _keep_inbound_confirmation_status(a.get("confirmation_status")) and _is_upcoming_in_la(a, now)]
        appts = _sort_inbound_appointments(raw)

        try:
            ref = await svc.load_reference(rid)
        except calendar_store.CalendarStoreError as e:
            logger.warning("[%s] v2/lookup reference load failed: %s", rid, e)
            ref = None

        selected = _clean(args.get("selected_tebra_appointment_id"))
        if selected:
            appts = [a for a in appts if str(a.get("tebra_appointment_id")) == selected]

        live_cache: dict = {}
        items: list[dict] = []
        for a in appts:
            tid = str(a["tebra_appointment_id"])
            det = await svc.appointment_details(tid, a.get("appointment_date"), rid, live_cache=live_cache)
            sb = await svc.fetch_appointment_by_tebra(tid, rid)
            parts = _la_parts((det or {}).get("start_utc"))
            t24 = a.get("appointment_time_24hr") or ""
            start_min = parts[1] if parts else (int(t24[:2]) * 60 + int(t24[3:5]) if len(t24) >= 5 and t24[2] == ":" else None)
            pid = (det or {}).get("provider_id") or (sb or {}).get("tebra_provider_id")
            items.append({
                "tebra_appointment_id": tid,
                "appointment_id": (sb or {}).get("id"),
                "date": parts[0] if parts else a.get("appointment_date"),
                "start_min": start_min,
                "time_12": a.get("appointment_time_12hr"),
                "clinician_id": int(pid) if pid else None,
                "clinician": _who(ref, pid, sb),
                "clinic": _where(ref, (det or {}).get("location_id"), (sb or {}).get("location") or a.get("service_location_name")),
                "visit_type": _visit_type_from(sb, (det or {}).get("duration"), None),
            })
        logger.info("[%s] v2/lookup appts=%s ids=%s", rid, len(items), [i["tebra_appointment_id"] for i in items])
        if selected and not items:
            return build_vapi_response(tool_call_id, "That tebra_appointment_id isn't one of this caller's upcoming appointments. Call again without selected_tebra_appointment_id.")
        return build_vapi_response(tool_call_id, format_lookup_reply(items, TEBRA_INBOUND_APPOINTMENTS_WINDOW_DAYS))
    except Exception as e:
        logger.exception("[%s] Error in /v2/inbound-lookup-appointments: %s", rid, e)
        return build_vapi_response(tool_call_id, "Sorry, I had trouble looking up appointments. Please try again.")


# ─────────────────────────────────────────────────────────────────────────────
# POST /v2/reschedule-appointment
# ─────────────────────────────────────────────────────────────────────────────

def _rescheduled_msg(d: dict, idempotent: bool = False) -> str:
    when = f"{spoken_day(d['date'])} at {format_12hr(d['start_min'] // 60, d['start_min'] % 60)}"
    old_bit = "" if d.get("old_removed", True) else (
        " Their old time could not be removed automatically; tell them the front desk will clear it.")
    return (f"RESCHEDULED: {d['visit_label']} moved to {d['provider']} on {when} at our {d['clinic']} clinic."
            f"{old_bit} Confirm the new day, time and clinician to the caller. "
            f"[ids (do not read aloud): appointment_id:{d.get('sb_id')} tebra_id:{d['tebra_id']} "
            f"old_tebra_id:{d['old_tebra_id']}{' repeat' if idempotent else ''}]")


async def _delete_in_tebra(tebra_id: str, rid: str, la_date: str | None = None) -> bool:
    """DeleteAppointment; when it reports failure, check whether it is gone anyway.
    GetAppointment returns None both for 'gone' and for a Tebra error, so a None is only trusted after the
    practice day list (la_date) confirms the id is absent. Unknown = NOT gone (never claim a false cancel)."""
    res = await call_tebra_delete_appointment(str(tebra_id), rid)
    if res["success"]:
        return True
    still = await call_tebra_get_appointment(str(tebra_id), rid)
    gone = False
    if not (still and still.get("StartTime")) and la_date:
        d = date_cls.fromisoformat(la_date)
        day = await call_tebra_get_practice_appointments((d - timedelta(days=1)).isoformat(),
                                                         (d + timedelta(days=1)).isoformat(), rid=rid)
        if day.get("ok"):
            key = calendar_store.appointment_key(tebra_id)
            gone = all(calendar_store.appointment_key(r.get("id")) != key for r in day.get("rows") or [])
    logger.info("[%s] delete %s failed (%s); gone anyway=%s", rid, tebra_id, res.get("error"), gone)
    return gone


async def _finish_old(old_tid: str, sb_old: dict | None, rid: str, la_date: str | None = None) -> bool:
    """Remove the old visit (Tebra, calendar copy, appointments row -> rescheduled). True when it is gone.
    Never raises: the new visit already exists, so the caller must still hear RESCHEDULED."""
    try:
        return await _finish_old_inner(old_tid, sb_old, rid, la_date)
    except Exception as e:
        logger.exception("[%s] RESCHEDULE removing old visit %s raised: %s — front desk must clear it", rid, old_tid, e)
        return False


async def _finish_old_inner(old_tid: str, sb_old: dict | None, rid: str, la_date: str | None) -> bool:
    removed = await _delete_in_tebra(old_tid, rid, la_date)
    if not removed:
        logger.error("[%s] RESCHEDULE old visit %s NOT removed from Tebra — front desk must clear it", rid, old_tid)
        return False
    try:
        await calendar_store.delete_tebra_appointment(old_tid, rid)
    except Exception as e:
        logger.error("[%s] cache delete of old %s failed: %s", rid, old_tid, e)
    try:
        if sb_old and sb_old.get("status") not in ("rescheduled", "cancelled"):
            await supabase_update_appointment(sb_old["id"], {"status": "rescheduled",
                                                             "updated_at": datetime.now(timezone.utc).isoformat()})
    except Exception as e:
        logger.error("[%s] appointments row %s -> rescheduled failed: %s", rid, (sb_old or {}).get("id"), e)
    return True


def _already_moved_msg(d: dict) -> str:
    """The old visit was already moved by an earlier reschedule, to a DIFFERENT time than asked now."""
    when = f"{spoken_day(d['date'])} at {format_12hr(d['start_min'] // 60, d['start_min'] % 60)}"
    old_bit = "" if d.get("old_removed", True) else " Their old time could not be removed automatically; the front desk will clear it."
    return (f"ALREADY MOVED: that appointment was already moved to {d['provider']} on {when} at our {d['clinic']} clinic, "
            f"so nothing new was booked.{old_bit} Tell the caller. To change it again, call inbound_lookup_appointments_v2 "
            f"and reschedule the new appointment. [ids (do not read aloud): tebra_id:{d['tebra_id']} old_tebra_id:{d['old_tebra_id']}]")


@router.post(
    "/reschedule-appointment",
    summary="Inbound: move a visit (same clinician by default) — confirm first, create new then delete old",
    openapi_extra={"requestBody": {"content": {"application/json": {
        "schema": inline_schema_refs(RescheduleAppointmentV2Request.model_json_schema())}}, "required": True}},
)
async def reschedule_appointment_v2(request: Request):
    rid = str(uuid4())[:8]
    tool_call_id = None
    try:
        args, tool_call_id, raw_body = await _parse(request)
        logger.info("[%s] ======== v2/reschedule-appointment START ======== args=%s", rid,
                    {k: v for k, v in args.items() if k not in ("patient_full_name", "name")})
        old_tid = _clean(args.get("tebra_appointment_id"))
        name = _clean(args.get("patient_full_name")) or _clean(args.get("name"))
        caller = _caller_e164(raw_body, args, tool_call_id)
        date_s, bad_date = _date_arg(args.get("new_date") or args.get("date"))
        t = parse_time_to_24hr(_clean(args.get("new_time") or args.get("time")) or "")
        raw_pid = _clean(args.get("clinician_id"))
        try:
            arg_pid = int(raw_pid) if raw_pid and raw_pid.lower() not in ("same", "0") else None
        except ValueError:
            arg_pid = None

        missing = [k for k, v in {"tebra_appointment_id": old_tid, "new_date": date_s, "new_time": t}.items() if not v]
        if missing or bad_date:
            return build_vapi_response(tool_call_id, f"Missing or invalid: {', '.join(missing) or 'new_date'}. Use the tebra_appointment_id from inbound_lookup_appointments_v2 and a date YYYY-MM-DD + time HH:MM from check_availability_v2.")
        start_min = t[0] * 60 + t[1]
        if start_min % 30:
            return build_vapi_response(tool_call_id, "Appointments start on the hour or half hour. Use a time from check_availability_v2.")

        lock = _APPT_LOCKS.setdefault(old_tid, asyncio.Lock())
        async with lock:
            # ── 1. Retry of a reschedule this process already made (new visit exists) ──
            # Any earlier move of this old visit (same or other slot) means: never book a second new visit.
            done = _RESCHEDULED.get(old_tid)
            if done:
                if not done.get("old_removed"):
                    done["old_removed"] = await _finish_old(old_tid, await svc.fetch_appointment_by_tebra(old_tid, rid),
                                                            rid, done.get("old_date"))
                if (done["date"], done["start_min"]) == (date_s, start_min) and (not arg_pid or arg_pid == done["pid"]):
                    logger.info("[%s] v2/reschedule IDEMPOTENT (memory) old=%s new=%s", rid, old_tid, done["tebra_id"])
                    return build_vapi_response(tool_call_id, _rescheduled_msg(done, idempotent=True))
                logger.info("[%s] v2/reschedule ALREADY MOVED old=%s new=%s", rid, old_tid, done["tebra_id"])
                return build_vapi_response(tool_call_id, _already_moved_msg(done))

            # ── 2. The old visit: must still exist and belong to the caller ──
            sb_old = await svc.fetch_appointment_by_tebra(old_tid, rid)
            got = await call_tebra_get_appointment(old_tid, rid)
            if not got or not got.get("StartTime"):
                child = await svc.fetch_rescheduled_child(sb_old["id"], rid) if sb_old else None
                if child and child.get("appointment_date") == date_s and str(child.get("appointment_time") or "")[:5] == f"{t[0]:02d}:{t[1]:02d}":
                    logger.info("[%s] v2/reschedule IDEMPOTENT (supabase) old=%s new=%s", rid, old_tid, child.get("tebra_appointment_id"))
                    return build_vapi_response(tool_call_id, _rescheduled_msg({
                        "visit_label": VISIT_TYPES.get(child.get("visit_type") or "", {}).get("label", "Appointment"),
                        "provider": child.get("provider_name") or "the clinician", "date": date_s, "start_min": start_min,
                        "clinic": child.get("location") or "our", "sb_id": child["id"],
                        "tebra_id": child.get("tebra_appointment_id"), "old_tebra_id": old_tid}, idempotent=True))
                return build_vapi_response(tool_call_id, "NOT FOUND: I can't find that appointment any more. Call inbound_lookup_appointments_v2 again and use an id from it.")
            patient_id = got.get("PatientId")
            if not await _owns(patient_id, sb_old, caller, name, rid):
                logger.info("[%s] v2/reschedule ownership FAILED old=%s", rid, old_tid)
                return build_vapi_response(tool_call_id, "NOT FOUND: that appointment isn't under the name and phone on this call. Ask for their first and last name, call inbound_lookup_appointments_v2, and use an id from it.")

            old_start = _start_utc_of(got)
            old_parts = _la_parts(old_start)
            if not _is_upcoming(old_start):
                logger.info("[%s] v2/reschedule old=%s is not upcoming", rid, old_tid)
                return build_vapi_response(tool_call_id, "NOT FOUND: that appointment isn't an upcoming one (it has already started or passed), so I can't move it. Call inbound_lookup_appointments_v2 and use an upcoming one, or offer to book a new visit.")

            # An earlier reschedule (e.g. before a restart) already made the new visit but the old one is still in
            # Tebra: finish removing it, never book a second new visit.
            child = await svc.fetch_rescheduled_child(sb_old["id"], rid) if sb_old else None
            if child:
                ct = str(child.get("appointment_time") or "00:00")
                prev = {"pid": int(child["tebra_provider_id"]) if child.get("tebra_provider_id") else None,
                        "date": child.get("appointment_date"), "start_min": int(ct[:2]) * 60 + int(ct[3:5]),
                        "provider": child.get("provider_name") or "the clinician", "clinic": child.get("location") or "the",
                        "visit_label": VISIT_TYPES.get(child.get("visit_type") or "", {}).get("label", "Appointment"),
                        "tebra_id": child.get("tebra_appointment_id"), "old_tebra_id": old_tid, "sb_id": child.get("id"),
                        "old_date": old_parts[0] if old_parts else None}
                prev["old_removed"] = await _finish_old(old_tid, sb_old, rid, prev["old_date"])
                _RESCHEDULED[old_tid] = prev
                if (prev["date"], prev["start_min"]) == (date_s, start_min) and (not arg_pid or arg_pid == prev["pid"]):
                    return build_vapi_response(tool_call_id, _rescheduled_msg(prev, idempotent=True))
                return build_vapi_response(tool_call_id, _already_moved_msg(prev))
            det = await svc.appointment_details(old_tid, old_parts[0] if old_parts else None, rid)
            old_pid = (det or {}).get("provider_id") or (sb_old or {}).get("tebra_provider_id")
            old_loc = (det or {}).get("location_id") or got.get("ServiceLocationId")

            ref = await svc.load_reference(rid)
            clinic = resolve_clinic(_clean(args.get("location")) or (str(old_loc) if old_loc else None), ref["clinics"])
            if not clinic:
                return build_vapi_response(tool_call_id, "I couldn't tell which clinic to use. Call again with location from check_availability_v2.")
            label = clinic_label(clinic)
            pid = arg_pid or (int(old_pid) if old_pid else None)
            if not pid:
                return build_vapi_response(tool_call_id, "I can't tell who their current clinician is. Ask who they'd like to see (or anyone), call check_availability_v2, then call again with clinician_id from its ids.")
            c = svc.clinician_by_id(ref, pid)
            if not c or c.get("bookable") is False or c.get("active") is False:
                return build_vapi_response(tool_call_id, f"{_who(ref, pid, sb_old)} can't be booked through me. Offer another clinician (call check_availability_v2 without clinician_name), or {FRONT_DESK_PHONE} for the front desk.")
            service = _clean(args.get("service")) or (sb_old or {}).get("service")
            if _clean(service) and not service_line_for(service)[1]:
                return build_vapi_response(tool_call_id, f"HANDOFF: {service} is scheduled by the front desk, not by me. Tell the caller the front desk will call them, and call update_inbound_status with crm_status manual_follow_up.")
            service_line = _line_for(ref, pid, clinic["tebra_location_id"], service)
            if pid not in svc.providers_for(ref, clinic["tebra_location_id"], service_line):
                return build_vapi_response(tool_call_id, f"{c['full_name']} doesn't see {_SERVICE_LABEL.get(service_line, 'these')} patients at {label}. Call check_availability_v2 for the right options.")

            provider = c["full_name"]
            visit_type = _visit_type_from(sb_old, (det or {}).get("duration"), args.get("visit_type"))
            vt = VISIT_TYPES[visit_type]
            start_utc = la_to_utc(date_s, t[0], t[1])
            end_utc = start_utc + timedelta(minutes=vt["minutes"])
            end_min = start_min + vt["minutes"]
            if old_parts and (old_parts[0], old_parts[1]) == (date_s, start_min) and old_pid and int(old_pid) == pid \
                    and int(clinic["tebra_location_id"]) == int(old_loc or 0):
                return build_vapi_response(tool_call_id, "That's already their appointment time. Ask what day or time they'd like instead.")

            # ── 3. New slot must be open (their own old visit doesn't block it) ──
            open_now = await svc.open_slots_for(ref, clinic, {pid}, vt["minutes"], date_s, date_s, rid=rid,
                                                exclude_tebra_ids=[old_tid])
            if not any(s.start_min == start_min for s in open_now):
                alts = await _alternatives(ref, clinic, pid, service_line, vt["minutes"], date_s, start_min, None, rid)
                logger.info("[%s] v2/reschedule slot not open pid=%s %s %s", rid, pid, date_s, t)
                if not alts:
                    return build_vapi_response(tool_call_id, f"That time isn't available with {provider} and I don't see close alternatives. Call check_availability_v2 again.")
                return build_vapi_response(tool_call_id, (
                    f"NOT AVAILABLE: {format_12hr(*t)} on {spoken_day(date_s)} isn't open with {provider}. "
                    f"Alternatives: {options_sentence(alts)}. Ask which works; their current appointment stays until they confirm. "
                    + ids_tag(alts, label, visit_type)))

            # ── 4. Confirm gate (keyed by the old visit + new clinician + slot) ──
            confirm_key = ("reschedule", old_tid, pid, int(clinic["tebra_location_id"]), visit_type, date_s, start_min)
            if not _take_confirm(confirm_key, _is_true(args.get("confirmed"))):
                old_when = (f"{spoken_day(old_parts[0])} at {format_12hr(old_parts[1] // 60, old_parts[1] % 60)}"
                            if old_parts else "current")
                when = f"{spoken_day(date_s)} at {format_12hr(*t)}"
                logger.info("[%s] v2/reschedule CONFIRM FIRST old=%s pid=%s %s %02d:%02d", rid, old_tid, pid, date_s, t[0], t[1])
                return build_vapi_response(tool_call_id, (
                    f"CONFIRM FIRST: not changed yet. Read it back and ask: \"I'll move your {old_when} appointment with "
                    f"{_who(ref, old_pid, sb_old)} to {provider} on {when} at our {label} clinic. Shall I make that change?\" "
                    "Then stop and wait. Only after the caller's clear yes, call reschedule_appointment_v2 again with the same "
                    "arguments plus confirmed=true. If they say no, keep their current appointment."))

            # ── 5. LIVE Tebra check (ignoring their own old visit) ──
            live = await svc.live_provider_rows(pid, date_s, rid)
            if not live["ok"]:
                logger.error("[%s] v2/reschedule live check failed: %s", rid, live["error"])
                return build_vapi_response(tool_call_id, "I couldn't confirm that time in our system right now. Their current appointment is unchanged. Tell them the front desk will call to move it, and call update_inbound_status with crm_status manual_follow_up.")
            rows = [r for r in live["rows"] if r.get("tebra_appointment_id") != calendar_store.appointment_key(old_tid)]
            clash = svc.overlapping(rows, start_utc, end_utc)
            if clash:
                alts = await _alternatives(ref, clinic, pid, service_line, vt["minutes"], date_s, start_min, rows, rid)
                logger.info("[%s] v2/reschedule LIVE CONFLICT pid=%s %s %s", rid, pid, date_s, t)
                if not alts:
                    return build_vapi_response(tool_call_id, f"That time was just taken with {provider}. Their current appointment is unchanged. Call check_availability_v2 again.")
                return build_vapi_response(tool_call_id, (
                    f"TAKEN: {format_12hr(*t)} on {spoken_day(date_s)} with {provider} was just booked by someone else. "
                    f"Their current appointment is unchanged. Alternatives: {options_sentence(alts)}. " + ids_tag(alts, label, visit_type)))

            # ── 6. Create the NEW visit first (same Tebra patient) ──
            async def _create():
                try:
                    return await create_appointment_in_tebra(
                        str(patient_id), str(clinic["tebra_location_id"]), date_s, f"{t[0]:02d}:{t[1]:02d}", vt["reason_id"], rid,
                        provider_id=pid, duration_minutes=vt["minutes"], notes=f"AI reschedule from {old_tid}")
                except Exception as ex:
                    logger.error("[%s] CreateAppointment exception: %s", rid, ex)
                    return {"success": False, "appointment_id": None, "error": f"request failed: {ex}", "transient": True}
            result = await _create()
            if not result["success"] and result.get("transient"):
                await asyncio.sleep(1.5)
                live2 = await svc.live_provider_rows(pid, date_s, rid)
                if live2["ok"] and not svc.overlapping(
                        [r for r in live2["rows"] if r.get("tebra_appointment_id") != calendar_store.appointment_key(old_tid)],
                        start_utc, end_utc):
                    result = await _create()
            if not result["success"]:
                logger.error("[%s] v2/reschedule CreateAppointment failed: %s", rid, result.get("error"))
                return build_vapi_response(tool_call_id, "I couldn't move it right now; their current appointment is still booked. Tell them the front desk will call to move it, and call update_inbound_status with crm_status manual_follow_up.")
            tebra_id = result["appointment_id"]
            _PENDING_CONFIRMS.pop(confirm_key, None)
            done = {"pid": pid, "date": date_s, "start_min": start_min, "provider": provider, "clinic": label,
                    "visit_label": vt["label"], "tebra_id": tebra_id, "old_tebra_id": old_tid, "sb_id": None,
                    "old_removed": False, "old_date": old_parts[0] if old_parts else None}
            _RESCHEDULED[old_tid] = done
            logger.info("[%s] v2/reschedule NEW BOOKED tebra=%s patient=%s pid=%s %s %02d:%02d %smin (old=%s)",
                        rid, tebra_id, patient_id, pid, date_s, t[0], t[1], vt["minutes"], old_tid)

            # ── 7. Then remove the OLD visit (never raises) ──
            done["old_removed"] = await _finish_old(old_tid, sb_old, rid, done["old_date"])
            try:   # the move itself worked; bookkeeping errors must not hide that from the caller
                # ── 8. Supabase: calendar copy + appointments row for the new visit ──
                try:
                    await calendar_store.insert_tebra_appointment({
                        "tebra_appointment_id": tebra_id, "tebra_provider_id": pid,
                        "tebra_location_id": int(clinic["tebra_location_id"]), "start_at": start_utc, "end_at": end_utc,
                        "duration_minutes": vt["minutes"], "status": "scheduled", "appt_type": "P",
                        "tebra_patient_id": int(patient_id) if str(patient_id).isdigit() else None, "source": "ai_booking",
                    }, rid)
                except calendar_store.CalendarStoreError as e:
                    logger.error("[%s] cache insert failed (sync will catch up): %s", rid, e)

                phone = (sb_old or {}).get("patient_phone") or caller
                patient_name = name or (sb_old or {}).get("patient_name") or got.get("AppointmentName")
                row = {
                    "tebra_appointment_id": tebra_id, "tebra_patient_id": patient_id,
                    "tebra_service_location_id": str(clinic["tebra_location_id"]), "tebra_reason_id": vt["reason_id"],
                    "service": service or "Physical Therapy", "location": label,
                    "appointment_date": date_s, "appointment_time": f"{t[0]:02d}:{t[1]:02d}",
                    "patient_name": patient_name, "patient_phone": phone, "status": "scheduled",
                    "practice_id": SUPABASE_PRACTICE_ID, "tebra_provider_id": pid, "provider_name": provider,
                    "visit_type": visit_type, "duration_minutes": vt["minutes"],
                    "end_time": f"{end_min // 60:02d}:{end_min % 60:02d}",
                    "rescheduled_from_id": (sb_old or {}).get("id"), "lead_id": (sb_old or {}).get("lead_id"),
                }
                sb = await supabase_insert_appointment({k: v for k, v in row.items() if v is not None})
                done["sb_id"] = sb.get("id") if sb else None
                if not done["sb_id"]:
                    logger.error("[%s] appointments insert FAILED (Tebra reschedule OK tebra=%s)", rid, tebra_id)

                # ── 9. SMS (never to +1555 test numbers) ──
                if done["sb_id"] and phone:
                    if str(phone).startswith(TEST_PHONE_PREFIXES):
                        logger.info("[%s] SMS skipped for test number (appt=%s)", rid, done["sb_id"])
                    else:
                        await send_appointment_sms_if_needed(
                            rid=rid, appointment_id=done["sb_id"], notification_type="sms_appointment_rescheduled",
                            appt={"patient_name": patient_name or "", "patient_phone": phone, "appointment_date": date_s,
                                  "appointment_time": f"{t[0]:02d}:{t[1]:02d}", "location": label,
                                  "service": f"{vt['label']} with {provider}"},
                            lead_id=row.get("lead_id"))
            except Exception as e:
                logger.exception("[%s] v2/reschedule bookkeeping after the move failed: %s", rid, e)

        return build_vapi_response(tool_call_id, _rescheduled_msg(done))

    except calendar_store.CalendarStoreError as e:
        logger.error("[%s] v2/reschedule-appointment calendar error: %s", rid, e)
        return build_vapi_response(tool_call_id, "I can't reach the calendar right now; their current appointment is unchanged. Tell them the front desk will call to move it.")
    except Exception as e:
        logger.exception("[%s] Error in /v2/reschedule-appointment: %s", rid, e)
        return build_vapi_response(tool_call_id, "Sorry, there was an error moving the appointment. Tell them the front desk will call to confirm. Do not say it was moved.")


# ─────────────────────────────────────────────────────────────────────────────
# POST /v2/inbound-cancel-appointment
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/inbound-cancel-appointment",
    summary="Inbound: cancel the caller's own visit — confirm first, then DeleteAppointment + Supabase",
    openapi_extra={"requestBody": {"content": {"application/json": {
        "schema": inline_schema_refs(InboundCancelAppointmentV2Request.model_json_schema())}}, "required": True}},
)
async def inbound_cancel_appointment_v2(request: Request):
    rid = str(uuid4())[:8]
    tool_call_id = None
    try:
        args, tool_call_id, raw_body = await _parse(request)
        logger.info("[%s] ======== v2/inbound-cancel-appointment START ======== tebra=%s confirmed=%s", rid,
                    args.get("tebra_appointment_id"), args.get("confirmed"))
        tid = _clean(args.get("tebra_appointment_id"))
        name = _clean(args.get("patient_full_name")) or _clean(args.get("name"))
        caller = _caller_e164(raw_body, args, tool_call_id)
        if not tid:
            return build_vapi_response(tool_call_id, "Missing tebra_appointment_id. Call inbound_lookup_appointments_v2 first and use an id from it.")

        lock = _APPT_LOCKS.setdefault(tid, asyncio.Lock())
        async with lock:
            sb = await svc.fetch_appointment_by_tebra(tid, rid)
            got = await call_tebra_get_appointment(tid, rid)
            if not got or not got.get("StartTime"):
                if tid in _CANCELLED or (sb and sb.get("status") == "cancelled"):
                    return build_vapi_response(tool_call_id, f"CANCELLED: that appointment is already cancelled. [ids (do not read aloud): tebra_id:{tid} repeat]")
                return build_vapi_response(tool_call_id, "NOT FOUND: I can't find that appointment any more. Call inbound_lookup_appointments_v2 again.")
            if not await _owns(got.get("PatientId"), sb, caller, name, rid):
                logger.info("[%s] v2/inbound-cancel ownership FAILED tebra=%s", rid, tid)
                return build_vapi_response(tool_call_id, "NOT FOUND: that appointment isn't under the name and phone on this call. Ask for their first and last name, call inbound_lookup_appointments_v2, and use an id from it.")

            start = _start_utc_of(got)
            parts = _la_parts(start)
            if not _is_upcoming(start):
                logger.info("[%s] v2/inbound-cancel tebra=%s is not upcoming", rid, tid)
                return build_vapi_response(tool_call_id, "NOT FOUND: that appointment isn't an upcoming one (it has already started or passed), so I can't cancel it. Call inbound_lookup_appointments_v2 and use an upcoming one.")
            try:
                ref = await svc.load_reference(rid)
            except calendar_store.CalendarStoreError:
                ref = None
            det = await svc.appointment_details(tid, parts[0] if parts else None, rid)
            who = _who(ref, (det or {}).get("provider_id") or (sb or {}).get("tebra_provider_id"), sb)
            where = _where(ref, (det or {}).get("location_id") or got.get("ServiceLocationId"), (sb or {}).get("location"))
            when = f"{spoken_day(parts[0])} at {format_12hr(parts[1] // 60, parts[1] % 60)}" if parts else "upcoming"

            confirm_key = ("cancel", tid)
            if not _take_confirm(confirm_key, _is_true(args.get("confirmed"))):
                logger.info("[%s] v2/inbound-cancel CONFIRM FIRST tebra=%s", rid, tid)
                return build_vapi_response(tool_call_id, (
                    f"CONFIRM FIRST: not cancelled yet. Ask: \"Just to confirm, you'd like to cancel your {when} appointment "
                    f"with {who} at our {where} clinic?\" Then stop and wait. Only after a clear yes, call "
                    "cancel_appointment_v2 again with the same arguments plus confirmed=true. If they'd rather move it, "
                    "use the reschedule steps instead."))

            if not await _delete_in_tebra(tid, rid, parts[0] if parts else None):
                return build_vapi_response(tool_call_id, "I couldn't cancel it in our system right now. Tell them the front desk will call to confirm the cancellation, and call update_inbound_status with crm_status manual_follow_up. Do not say it is cancelled.")
            _PENDING_CONFIRMS.pop(confirm_key, None)
            _CANCELLED.add(tid)
            try:   # it IS cancelled in Tebra now; bookkeeping errors must not hide that from the caller
                try:
                    await calendar_store.delete_tebra_appointment(tid, rid)
                except Exception as e:
                    logger.error("[%s] cache delete failed: %s", rid, e)
                now_iso = datetime.now(timezone.utc).isoformat()
                if sb and sb.get("status") != "cancelled":
                    await supabase_update_appointment(sb["id"], {"status": "cancelled", "cancelled_at": now_iso, "updated_at": now_iso})
                    phone = sb.get("patient_phone") or caller
                    if phone and not str(phone).startswith(TEST_PHONE_PREFIXES):
                        await send_appointment_sms_if_needed(
                            rid=rid, appointment_id=sb["id"], notification_type="sms_appointment_cancelled",
                            appt={"patient_name": sb.get("patient_name") or name or "", "patient_phone": phone,
                                  "appointment_date": sb.get("appointment_date") or (parts[0] if parts else ""),
                                  "appointment_time": str(sb.get("appointment_time") or "")[:5], "location": where,
                                  "service": sb.get("service") or ""},
                            lead_id=sb.get("lead_id"))
            except Exception as e:
                logger.exception("[%s] v2/inbound-cancel bookkeeping after the delete failed: %s", rid, e)
            logger.info("[%s] v2/inbound-cancel done tebra=%s sb=%s", rid, tid, (sb or {}).get("id"))
            return build_vapi_response(tool_call_id, (
                f"CANCELLED: their {when} appointment with {who} at our {where} clinic is cancelled. Tell them, and ask "
                f"if they'd like to book a new time. [ids (do not read aloud): tebra_id:{tid}]"))
    except Exception as e:
        logger.exception("[%s] Error in /v2/inbound-cancel-appointment: %s", rid, e)
        return build_vapi_response(tool_call_id, "Sorry, there was an error cancelling. Do not say it is cancelled; tell them the front desk will call to confirm.")
