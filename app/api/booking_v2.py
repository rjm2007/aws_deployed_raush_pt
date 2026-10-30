"""
v2 multi-provider booking endpoints (mounted at /api/v1/v2/...). Used by the NEW booking-only outbound
test agent; the old /check-availability and /create-appointment are untouched.

  POST /v2/check-availability   — reads ONLY Supabase (calendar copy + clinician tables) -> 2-3 options
  POST /v2/create-appointment   — validate -> LIVE Tebra check -> patient -> CreateAppointment -> Supabase
  POST /v2/cancel-appointment   — DeleteAppointment + cache row + appointments status (tests / cleanup)
  POST /v2/update-lead          — do-not-call / not interested / front-desk hand-off on the lead

Every endpoint takes direct JSON or the Vapi wrapper and answers ONE line of plain English.
Machine values the model must pass back are in a trailing "[ids ...]" tag (not to be read aloud).
"""

import asyncio
import re
from datetime import date as date_cls, datetime, timedelta, timezone
from uuid import UUID, uuid4

from fastapi import APIRouter, Request

from app.core.config import (
    AVAILABILITY_WINDOW_DAYS,
    FRONT_DESK_PHONE,
    NEAREST_CLINICS,
    SUPABASE_PRACTICE_ID,
    TEST_PHONE_PREFIXES,
    V2_IDEMPOTENCY_MINUTES,
    VISIT_TYPES,
    normalize_visit_type,
    service_line_for,
)
from app.core.logger import logger
from app.models.requests import (
    CancelAppointmentV2Request,
    CheckAvailabilityV2Request,
    CreateAppointmentV2Request,
    UpdateLeadV2Request,
    inline_schema_refs,
)
from app.services import booking_v2_service as svc
from app.services import calendar_store
from app.services.appointment_sms_service import send_appointment_sms_if_needed
from app.services.supabase_service import supabase_insert_appointment, supabase_update_appointment
from app.services.tebra_service import (
    call_tebra_delete_appointment,
    call_tebra_get_appointment,
    create_appointment_in_tebra,
)
from app.services.twilio_service import _format_phone_e164
from app.utils.availability_engine import Slot
from app.utils.booking_v2_logic import (
    clinic_label,
    ids_tag,
    in_part_of_day,
    match_clinician,
    normalize_gender,
    normalize_part_of_day,
    options_sentence,
    pick_spread,
    resolve_clinic,
    spoken_day,
)
from app.utils.parser import build_vapi_response, coerce_vapi_tool_arguments
from app.utils.time_utils import (
    closed_day_reason, format_12hr, get_location_hours, la_to_utc, now_la, parse_time_to_24hr,
)

router = APIRouter(prefix="/v2", tags=["Booking v2 (multi-provider)"])

_SERVICE_LABEL = {"physical_therapy": "physical therapy", "pelvic_health": "pelvic health"}
_NO_PREF = {"", "any", "anyone", "no", "none", "no preference", "earliest", "whoever", "doesn't matter",
            "does not matter", "first available", "anybody", "either", "null", "string"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# One in-flight booking per (lead/phone, clinician, date): a Vapi retry waits, then hits idempotency.
_BOOKING_LOCKS: dict[tuple, asyncio.Lock] = {}


# ─── shared parsing ──────────────────────────────────────────────────────────

async def _parse(request: Request) -> tuple[dict, str | None, dict]:
    body = await request.json()
    if isinstance(body, dict) and isinstance(body.get("message"), dict) and body["message"].get("toolCalls"):
        tc = body["message"]["toolCalls"][0]
        return coerce_vapi_tool_arguments((tc.get("function") or {}).get("arguments")), tc.get("id"), body
    return (body if isinstance(body, dict) else {}), None, body


def _clean(v) -> str | None:
    """Drop empty values and unresolved Vapi placeholders ('{{lead_id}}', 'string', 'null')."""
    if v is None:
        return None
    s = str(v).strip()
    if not s or s.startswith("{{") or s.lower() in ("string", "null", "none", "undefined", "n/a"):
        return None
    return s


def _lead_id(raw) -> str | None:
    s = _clean(raw)
    if not s:
        return None
    try:
        UUID(s)
        return s
    except ValueError:
        return None


def _date_arg(raw) -> tuple[str | None, bool]:
    """-> (YYYY-MM-DD or None, bad_format)."""
    s = _clean(raw)
    if not s:
        return None, False
    s = s[:10]
    if not _DATE_RE.match(s):
        return None, True
    try:
        date_cls.fromisoformat(s)
    except ValueError:
        return None, True
    return s, False


def _transfer_msg(when: str) -> str:
    return (f"TRANSFER: {when} is more than {AVAILABILITY_WINDOW_DAYS} days out, which I can't book. "
            f"Tell the caller you'll connect them with the front desk and use transfer_to_front_desk. "
            f"If the transfer isn't possible, call update_lead with outcome front_desk and tell them the front desk "
            f"will call them back (front desk number {FRONT_DESK_PHONE}).")


def _ordered_near(slots: list[Slot], date_s: str, start_min: int) -> list[Slot]:
    """Same date closest to the wanted time first, then later dates earliest first."""
    same = sorted((s for s in slots if s.date == date_s), key=lambda s: (abs(s.start_min - start_min), s.start_min))
    later = [s for s in slots if s.date > date_s]
    return same + later


def _earliest_per_date(slots: list[Slot], n_dates: int = 3) -> list[Slot]:
    """One option per open date (first n dates), rotating clinicians when possible."""
    by_date: dict[str, list[Slot]] = {}
    for s in slots:
        by_date.setdefault(s.date, []).append(s)
    out: list[Slot] = []
    for d in sorted(by_date)[:n_dates]:
        day = sorted(by_date[d], key=lambda s: (s.start_min, s.provider_name))
        used = {c.provider_id for c in out}
        out.append(next((s for s in day if s.provider_id not in used), day[0]))
    return out


def _names(ref: dict, pids) -> str:
    names = sorted(c["full_name"] for c in ref["clinicians"] if int(c["tebra_provider_id"]) in set(pids))
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _day_closed_reason(date_s: str, clinic: dict, today: date_cls, floor: date_cls) -> str:
    label = clinic_label(clinic)
    why = closed_day_reason(date_s, today=today)
    d = date_cls.fromisoformat(date_s)
    if why == "past":
        return f"{spoken_day(date_s)} has already passed."
    if why:
        return f"The clinics are closed on {spoken_day(date_s)} for {why}."
    if d < floor:
        return f"I can only book from {spoken_day(floor.isoformat())} onward."
    if get_location_hours(date_s, label, today=today) is None:
        return f"Our {label} clinic is closed on {d.strftime('%A')}s."
    return f"There are no openings on {spoken_day(date_s)}."


# ─────────────────────────────────────────────────────────────────────────────
# POST /v2/check-availability
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/check-availability",
    summary="Multi-provider availability (Supabase copy): 2-3 options with clinician, day and time",
    openapi_extra={"requestBody": {"content": {"application/json": {
        "schema": inline_schema_refs(CheckAvailabilityV2Request.model_json_schema())}}, "required": True}},
)
async def check_availability_v2(request: Request):
    rid = str(uuid4())[:8]
    tool_call_id = None
    try:
        args, tool_call_id, _ = await _parse(request)
        logger.info("[%s] ======== v2/check-availability START ======== args=%s", rid, args)

        lead_id = _lead_id(args.get("lead_id"))
        location, service = _clean(args.get("location")), _clean(args.get("service"))
        lead = None
        if lead_id and (not location or not service):
            lead = await svc.fetch_lead(lead_id, rid)
            if lead:
                location = location or lead.get("preferred_location")
                service = service or lead.get("service_of_interest")

        ref = await svc.load_reference(rid)
        clinic = resolve_clinic(location, ref["clinics"])
        if not clinic:
            names = ", ".join(clinic_label(c) for c in ref["clinics"])
            return build_vapi_response(tool_call_id, f"I couldn't tell which clinic '{location or ''}' is. Our clinics are {names}. Ask which one they prefer and call again.")
        label = clinic_label(clinic)

        service_line, ai_bookable = service_line_for(service)
        if not ai_bookable:
            if lead_id:
                await svc.update_lead(lead_id, {"queue_status": "manual_follow_up", "lead_outcome": "manual"},
                                      {"handoff_reason": f"service not AI-bookable: {service}",
                                       "handoff_at": datetime.now(timezone.utc).isoformat()}, rid)
            logger.info("[%s] v2/check-availability service not AI-bookable service=%s lead=%s", rid, service, lead_id)
            return build_vapi_response(tool_call_id, (
                f"HANDOFF: {service} appointments are scheduled by our front desk team, not by me. Do not offer times. "
                f"Tell the caller the front desk will call them shortly to set it up (or they can call {FRONT_DESK_PHONE}). "
                "The lead is already marked for front-desk follow-up."))

        visit_type = normalize_visit_type(args.get("visit_type"))
        duration = VISIT_TYPES[visit_type]["minutes"]
        gender = normalize_gender(args.get("gender"))
        part = normalize_part_of_day(args.get("part_of_day"))
        raw_name = _clean(args.get("clinician_name"))
        if raw_name and raw_name.strip().lower() in _NO_PREF:
            raw_name = None

        date_s, bad1 = _date_arg(args.get("date"))
        date_from, bad2 = _date_arg(args.get("date_from"))
        date_to, bad3 = _date_arg(args.get("date_to"))
        if bad1 or bad2 or bad3:
            return build_vapi_response(tool_call_id, "Dates must be YYYY-MM-DD (for example 2026-11-03). Work out the exact date and call again.")
        time_raw = _clean(args.get("time"))
        want_t = parse_time_to_24hr(time_raw) if time_raw else None
        if time_raw and not want_t:
            return build_vapi_response(tool_call_id, "Time must be HH:MM in 24-hour clinic time (for example 14:30). Call again with that.")
        want_min = want_t[0] * 60 + want_t[1] if want_t else None

        # a one-day "range" (date_from = date_to, e.g. "this Sunday") is just a day: use the date path,
        # which says why a day is closed and offers the next openings
        if date_from and not date_s and date_to == date_from:
            date_s, date_from, date_to = date_from, None, None

        today = now_la().date()
        last_day = today + timedelta(days=AVAILABILITY_WINDOW_DAYS)
        floor = svc.booking_floor(today)
        if date_s and date_cls.fromisoformat(date_s) > last_day:
            return build_vapi_response(tool_call_id, _transfer_msg(spoken_day(date_s)))
        if date_from and date_cls.fromisoformat(date_from) > last_day:
            return build_vapi_response(tool_call_id, _transfer_msg(f"The range starting {spoken_day(date_from)}"))
        if date_from and date_to and date_to < date_from:
            date_from, date_to = date_to, date_from

        # ── who ──
        notes: list[str] = []
        target = clinic
        spread = True
        clinic_pids = svc.providers_for(ref, clinic["tebra_location_id"], service_line)
        provider_ids = set(clinic_pids)
        svc_label = _SERVICE_LABEL.get(service_line, "physical therapy")

        if raw_name:
            m = match_clinician(raw_name, ref["clinicians"],
                                providers_at_clinic=svc.providers_for(ref, clinic["tebra_location_id"], None))
            if m["status"] == "ambiguous":
                who = " or ".join(
                    f"{c['full_name']} ({' / '.join(clinic_label(next((k for k in ref['clinics'] if int(k['tebra_location_id']) == l), None)) for l in svc.clinics_of(ref, c['tebra_provider_id'])) or 'not bookable'})"
                    for c in m["candidates"])
                return build_vapi_response(tool_call_id, f"ASK: '{raw_name}' could be {who}. Ask which one they mean, then call again with the full name.")
            if m["status"] == "none":
                notes.append(f"We don't have a clinician named {raw_name} on file. At {label}, {svc_label} is with {_names(ref, clinic_pids) or 'nobody'}.")
                raw_name = None
            else:
                c = m["clinician"]
                pid = int(c["tebra_provider_id"])
                if c.get("bookable") is False or c.get("active") is False:
                    notes.append(f"{c['full_name']} isn't booked through me; the front desk can help with that. Here's who is open at {label} instead.")
                elif pid in clinic_pids:
                    provider_ids, spread = {pid}, False
                else:
                    other = [l for l in svc.clinics_of(ref, pid) if pid in svc.providers_for(ref, l, service_line)]
                    if other:
                        near = NEAREST_CLINICS.get(int(clinic["tebra_location_id"]), [])
                        other.sort(key=lambda l: near.index(l) if l in near else 99)
                        target = next(k for k in ref["clinics"] if int(k["tebra_location_id"]) == other[0])
                        provider_ids, spread = {pid}, False
                        notes.append(f"{c['full_name']} works at our {clinic_label(target)} clinic, not {label}. "
                                     f"These are openings at {clinic_label(target)}; if they'd rather stay at {label}, call again without a clinician name.")
                    else:
                        notes.append(f"{c['full_name']} doesn't do {svc_label}. At {label}, {svc_label} is with {_names(ref, clinic_pids) or 'nobody'}.")

        # pelvic health (or any service) with nobody at this clinic -> nearest clinic that has it
        if spread and not provider_ids:
            for l in NEAREST_CLINICS.get(int(clinic["tebra_location_id"]), []):
                pids = svc.providers_for(ref, l, service_line)
                if pids:
                    target = next(k for k in ref["clinics"] if int(k["tebra_location_id"]) == l)
                    provider_ids = pids
                    notes.append(f"No one at {label} does {svc_label}. The nearest is our {clinic_label(target)} clinic with "
                                 f"{_names(ref, pids)}. If {clinic_label(target)} doesn't work for them, the front desk can help at {FRONT_DESK_PHONE}.")
                    break
        if not provider_ids:
            return build_vapi_response(tool_call_id, f"HANDOFF: nobody I can book does {svc_label} near {label}. Tell the caller the front desk will call them, and call update_lead with outcome front_desk.")

        # gender filter (named clinician wins over gender)
        use_gender = None
        if gender and spread:
            g_pids = {p for p in provider_ids
                      if (svc.clinician_by_id(ref, p) or {}).get("gender") == gender}
            if g_pids:
                use_gender = gender
            else:
                notes.append(f"We don't have a {gender} clinician for {svc_label} at {clinic_label(target)} on file, so these are with anyone.")

        # ── when ──
        if date_from:
            d0 = date_from
            d1 = min(date_to or last_day.isoformat(), last_day.isoformat())
        else:
            d0, d1 = today.isoformat(), last_day.isoformat()
        slots = await svc.open_slots_for(ref, target, provider_ids, duration, d0, d1, gender=use_gender, rid=rid)
        if use_gender and not slots:
            notes.append(f"There are no openings with a {gender} clinician in that range, so these are with anyone.")
            use_gender = None
            slots = await svc.open_slots_for(ref, target, provider_ids, duration, d0, d1, rid=rid)
        # nothing in the asked range (closed days, holiday week, fully booked) -> next openings after it
        range_empty = False
        if not slots and date_from and d1 < last_day.isoformat():
            nxt = (date_cls.fromisoformat(d1) + timedelta(days=1)).isoformat()
            slots = await svc.open_slots_for(ref, target, provider_ids, duration, nxt, last_day.isoformat(),
                                             gender=use_gender, rid=rid)
            range_empty = bool(slots)
        if part:
            in_part = [s for s in slots if in_part_of_day(s, part)]
            if in_part:
                slots = in_part
            else:
                notes.append(f"There are no {part} openings, so here are other times.")

        if not slots:
            if date_from and date_to and date_cls.fromisoformat(date_to) > last_day:
                return build_vapi_response(tool_call_id, _transfer_msg("The rest of that range"))
            who = _names(ref, provider_ids) if not spread else f"{svc_label} at {clinic_label(target)}"
            rng = f"between {spoken_day(d0)} and {spoken_day(d1)}" if date_from else f"in the next {AVAILABILITY_WINDOW_DAYS} days"
            msg = " ".join(notes + [f"NO OPENINGS: there are no openings with {who} {rng}."])
            msg += (" Offer to look at another clinician, or tell them the front desk will call (call update_lead with outcome front_desk)."
                    if not spread else " Tell them the front desk will call to find a time (call update_lead with outcome front_desk).")
            return build_vapi_response(tool_call_id, msg)

        lead_txt = ""
        if date_s:
            day_slots = [s for s in slots if s.date == date_s]
            if want_min is not None:
                exact = [s for s in day_slots if s.start_min == want_min]
                if exact:
                    chosen = pick_spread(exact, 3, spread)
                    lead_txt = f"Yes, {format_12hr(*want_t)} on {spoken_day(date_s, today)} is open with"
                else:
                    chosen = pick_spread(_ordered_near(slots, date_s, want_min), 3, spread, far=False)
                    reason = "" if day_slots else " " + _day_closed_reason(date_s, target, today, floor)
                    lead_txt = f"{format_12hr(*want_t)} on {spoken_day(date_s, today)} isn't open.{reason} Closest options:"
            elif day_slots:
                chosen = pick_spread(day_slots, 3, spread)
                lead_txt = f"Openings on {spoken_day(date_s, today)}:"
            else:
                chosen = pick_spread([s for s in slots if s.date > date_s] or slots, 3, spread)
                lead_txt = _day_closed_reason(date_s, target, today, floor) + " Next openings:"
        elif range_empty:
            chosen = pick_spread(slots, 3, spread)
            why = (_day_closed_reason(d0, target, today, floor) if d0 == d1
                   else f"There are no openings between {spoken_day(d0)} and {spoken_day(d1)}.")
            lead_txt = why + " Next openings after that:"
        elif date_from:
            chosen = _earliest_per_date(slots, 3) if spread else pick_spread(
                [next(s for s in slots if s.date == d) for d in sorted({s.date for s in slots})[:3]], 3, False)
            lead_txt = "First open days in that range:"
        elif want_min is not None:
            exact = [s for s in slots if s.start_min == want_min]
            if exact:
                chosen = pick_spread(exact, 3, spread)
                lead_txt = f"Earliest days with {format_12hr(*want_t)} open:"
            else:
                chosen = pick_spread(sorted(slots, key=lambda s: (s.date, abs(s.start_min - want_min))), 3, spread, far=False)
                lead_txt = f"{format_12hr(*want_t)} isn't open soon. Closest options:"
        else:
            chosen = pick_spread(slots, 3, spread)
            lead_txt = "Earliest openings:"

        tgt_label = clinic_label(target)
        where = "" if target is clinic else f" at {tgt_label}"
        if date_from and date_to and date_cls.fromisoformat(date_to) > last_day:
            notes.append(f"I can only see up to {spoken_day(last_day.isoformat())}; for later dates use transfer_to_front_desk.")
        if lead_txt.endswith(" with"):   # same day + time for everyone -> just list the clinicians
            names = [s.provider_name for s in chosen]
            spoken = names[0] if len(names) == 1 else ", ".join(names[:-1]) + ", or " + names[-1]
        else:
            spoken = options_sentence(chosen, today)
        msg = (" ".join(notes) + " " if notes else "") + \
              f"{lead_txt} {spoken}{where}. " \
              f"Offer these ({VISIT_TYPES[visit_type]['label'].lower()}, {duration} min) and book only after a clear yes. " + \
              ids_tag(chosen, tgt_label, visit_type)
        logger.info("[%s] v2/check-availability clinic=%s target=%s service=%s visit=%s gender=%s named=%s slots=%s chosen=%s",
                    rid, label, tgt_label, service_line, visit_type, use_gender, not spread, len(slots),
                    [(s.provider_id, s.date, s.time_24) for s in chosen])
        return build_vapi_response(tool_call_id, msg)

    except calendar_store.CalendarStoreError as e:
        logger.error("[%s] v2/check-availability calendar error: %s", rid, e)
        return build_vapi_response(tool_call_id, "I can't check the calendar right now. Tell the caller the front desk will call them to book, and call update_lead with outcome front_desk.")
    except Exception as e:
        logger.exception("[%s] Error in /v2/check-availability: %s", rid, e)
        return build_vapi_response(tool_call_id, "Sorry, I had trouble checking availability. Please try again.")


# ─────────────────────────────────────────────────────────────────────────────
# POST /v2/create-appointment
# ─────────────────────────────────────────────────────────────────────────────

def _booked_msg(name_first: str, visit_label: str, provider: str, date_s: str, start_min: int, clinic: str,
                appt_id, tebra_id, patient_id, created: bool | None) -> str:
    when = f"{spoken_day(date_s)} at {format_12hr(start_min // 60, start_min % 60)}"
    created_bit = "" if created is None else (" patient:new" if created else " patient:existing")
    return (f"BOOKED: {visit_label} with {provider} on {when} at our {clinic} clinic"
            f"{' for ' + name_first if name_first else ''}. Confirm these details to the caller. "
            f"[ids (do not read aloud): appointment_id:{appt_id} tebra_id:{tebra_id} tebra_patient_id:{patient_id}{created_bit}]")


async def _alternatives(ref, clinic, pid, service_line, duration, date_s, start_min, extra, rid) -> list[Slot]:
    """2 alternatives: same clinician first (closest to the wanted time), then others at the clinic."""
    end = (date_cls.fromisoformat(date_s) + timedelta(days=14)).isoformat()
    mine = await svc.open_slots_for(ref, clinic, {pid}, duration, date_s, end, extra_bookings=extra, rid=rid)
    picks = _ordered_near(mine, date_s, start_min)[:2]
    if len(picks) < 2:
        others = svc.providers_for(ref, clinic["tebra_location_id"], service_line) - {pid}
        more = await svc.open_slots_for(ref, clinic, others, duration, date_s, end, extra_bookings=extra, rid=rid)
        picks += pick_spread(_ordered_near(more, date_s, start_min), 2 - len(picks), far=False)
    return sorted(picks, key=lambda s: (s.date, s.start_min))


@router.post(
    "/create-appointment",
    summary="Book a clinician in Tebra (live slot check, patient match/create) + Supabase",
    openapi_extra={"requestBody": {"content": {"application/json": {
        "schema": inline_schema_refs(CreateAppointmentV2Request.model_json_schema())}}, "required": True}},
)
async def create_appointment_v2(request: Request):
    rid = str(uuid4())[:8]
    tool_call_id = None
    try:
        args, tool_call_id, _ = await _parse(request)
        logger.info("[%s] ======== v2/create-appointment START ======== args=%s", rid, args)

        lead_id = _lead_id(args.get("lead_id"))
        lead = await svc.fetch_lead(lead_id, rid) if lead_id else None
        name = _clean(args.get("name")) or (lead or {}).get("full_name")
        phone = _clean(args.get("phone")) or (lead or {}).get("phone")
        location = _clean(args.get("location")) or (lead or {}).get("preferred_location")
        service = _clean(args.get("service")) or (lead or {}).get("service_of_interest")
        visit_type = normalize_visit_type(args.get("visit_type"))
        vt = VISIT_TYPES[visit_type]
        gender = normalize_gender(args.get("gender"))
        e164 = _format_phone_e164(phone) if phone else None
        date_s, bad_date = _date_arg(args.get("date"))
        t = parse_time_to_24hr(_clean(args.get("time")) or "")
        try:
            pid = int(str(_clean(args.get("clinician_id")) or "").strip())
        except ValueError:
            pid = None

        missing = [k for k, v in {"name": name, "phone": e164, "location": location, "clinician_id": pid,
                                  "date": date_s, "time": t}.items() if not v]
        if missing or bad_date:
            return build_vapi_response(tool_call_id, f"Missing or invalid: {', '.join(missing) or 'date'}. Use the ids from check_availability (clinician_id, date YYYY-MM-DD, time HH:MM) and the caller's full name.")
        parts = name.split()
        if len(parts) < 2:
            return build_vapi_response(tool_call_id, "I need the patient's first and last name to book. Ask for their last name and call again.")
        first, last = parts[0], " ".join(parts[1:])
        start_min = t[0] * 60 + t[1]
        if start_min % 30:
            return build_vapi_response(tool_call_id, "Appointments start on the hour or half hour. Use a time from check_availability.")

        ref = await svc.load_reference(rid)
        clinic = resolve_clinic(location, ref["clinics"])
        if not clinic:
            return build_vapi_response(tool_call_id, f"I couldn't tell which clinic '{location}' is. Use the location from check_availability.")
        label = clinic_label(clinic)
        service_line, ai_bookable = service_line_for(service)
        if not ai_bookable:
            return build_vapi_response(tool_call_id, f"HANDOFF: {service} is booked by the front desk, not by me. Tell the caller the front desk will call them.")
        c = svc.clinician_by_id(ref, pid)
        if not c or c.get("bookable") is False or c.get("active") is False:
            return build_vapi_response(tool_call_id, "That clinician can't be booked through me. Call check_availability again and offer one of its options.")
        if pid not in svc.providers_for(ref, clinic["tebra_location_id"], service_line):
            return build_vapi_response(tool_call_id, f"{c['full_name']} doesn't see {_SERVICE_LABEL.get(service_line)} patients at {label}. Call check_availability again for the right options.")

        provider = c["full_name"]
        start_utc = la_to_utc(date_s, t[0], t[1])
        end_utc = start_utc + timedelta(minutes=vt["minutes"])
        end_min = start_min + vt["minutes"]

        lock_key = (lead_id or e164, pid, date_s)
        lock = _BOOKING_LOCKS.setdefault(lock_key, asyncio.Lock())
        async with lock:
            # ── 1. Idempotency: same lead (or phone) + clinician + start within 10 min ──
            existing = await svc.find_recent_booking(lead_id, e164, pid, date_s, start_min, V2_IDEMPOTENCY_MINUTES, rid)
            if existing:
                et = str(existing.get("appointment_time") or "00:00")
                logger.info("[%s] v2/create IDEMPOTENT existing appt=%s tebra=%s", rid, existing["id"], existing.get("tebra_appointment_id"))
                return build_vapi_response(tool_call_id, _booked_msg(
                    first, (existing.get("visit_type") or visit_type).replace("_", " ").replace("evaluation", "Evaluation"),
                    existing.get("provider_name") or provider, date_s, int(et[:2]) * 60 + int(et[3:5]), label,
                    existing["id"], existing.get("tebra_appointment_id"), existing.get("tebra_patient_id"), None))

            # ── 2. Slot must be open in the engine (clinic hours, clinician hours, cache, window) ──
            open_now = await svc.open_slots_for(ref, clinic, {pid}, vt["minutes"], date_s, date_s, rid=rid)
            if not any(s.start_min == start_min for s in open_now):
                alts = await _alternatives(ref, clinic, pid, service_line, vt["minutes"], date_s, start_min, None, rid)
                logger.info("[%s] v2/create slot not open in engine pid=%s %s %s", rid, pid, date_s, t)
                if not alts:
                    return build_vapi_response(tool_call_id, f"That time isn't available with {provider} and I don't see close alternatives. Call check_availability again.")
                return build_vapi_response(tool_call_id, (
                    f"NOT AVAILABLE: {format_12hr(*t)} on {spoken_day(date_s)} isn't open with {provider}. "
                    f"Alternatives: {options_sentence(alts)}. Ask which works, then book. " + ids_tag(alts, label, visit_type)))

            # ── 3. LIVE Tebra check of this clinician + slot ──
            live = await svc.live_provider_rows(pid, date_s, rid)
            if not live["ok"]:
                logger.error("[%s] v2/create live check failed: %s — not booking", rid, live["error"])
                if lead_id:
                    await svc.update_lead(lead_id, {"queue_status": "manual_follow_up", "lead_outcome": "manual"},
                                          {"handoff_reason": "live Tebra check failed during booking",
                                           "requested_slot": {"clinician_id": pid, "date": date_s, "time": f"{t[0]:02d}:{t[1]:02d}",
                                                              "visit_type": visit_type, "location": label}}, rid)
                return build_vapi_response(tool_call_id, "I couldn't confirm that time in our system right now. Tell the caller the front desk will call them shortly to lock it in. Do not say it is booked.")
            clash = svc.overlapping(live["rows"], start_utc, end_utc)
            if clash:
                try:   # refresh the copy so the next search is right
                    await calendar_store.upsert_tebra_appointments(live["rows"], rid)
                except calendar_store.CalendarStoreError as e:
                    logger.warning("[%s] cache refresh failed: %s", rid, e)
                alts = await _alternatives(ref, clinic, pid, service_line, vt["minutes"], date_s, start_min, live["rows"], rid)
                logger.info("[%s] v2/create LIVE CONFLICT pid=%s %s %s clashes=%s", rid, pid, date_s, t, [r["tebra_appointment_ref"] for r in clash])
                if not alts:
                    return build_vapi_response(tool_call_id, f"That time was just taken with {provider}. Call check_availability again for new options.")
                return build_vapi_response(tool_call_id, (
                    f"TAKEN: {format_12hr(*t)} on {spoken_day(date_s)} with {provider} was just booked by someone else. "
                    f"Alternatives: {options_sentence(alts)}. Ask which works, then book. " + ids_tag(alts, label, visit_type)))

            # ── 4. Patient: reuse only on name + phone match ──
            pat = await svc.resolve_patient(first, last, e164, gender, rid)
            if not pat["ok"]:
                logger.error("[%s] v2/create patient step failed: %s", rid, pat["error"])
                return build_vapi_response(tool_call_id, "I'm having trouble creating the patient record. Tell the caller the front desk will call to finish booking. Do not say it is booked.")
            patient_id = pat["patient_id"]

            # ── 5. CreateAppointment (one retry only when the request itself failed) ──
            async def _create():
                try:
                    return await create_appointment_in_tebra(
                        patient_id, str(clinic["tebra_location_id"]), date_s, f"{t[0]:02d}:{t[1]:02d}", vt["reason_id"], rid,
                        provider_id=pid, duration_minutes=vt["minutes"], notes="AI booking")
                except Exception as ex:
                    logger.error("[%s] CreateAppointment exception: %s", rid, ex)
                    return {"success": False, "appointment_id": None, "error": f"request failed: {ex}", "transient": True}
            result = await _create()
            if not result["success"] and result.get("transient"):
                await asyncio.sleep(1.5)
                live2 = await svc.live_provider_rows(pid, date_s, rid)
                if live2["ok"] and not svc.overlapping(live2["rows"], start_utc, end_utc):
                    result = await _create()
            if not result["success"]:
                logger.error("[%s] v2/create CreateAppointment failed: %s", rid, result.get("error"))
                if lead_id:
                    await svc.update_lead(lead_id, {"queue_status": "manual_follow_up", "lead_outcome": "manual",
                                                    "tebra_patient_id": patient_id},
                                          {"handoff_reason": f"CreateAppointment failed: {str(result.get('error'))[:200]}"}, rid)
                return build_vapi_response(tool_call_id, "I'm having a little trouble finishing the booking. Tell the caller the front desk will call them right back to confirm. Do not say it is booked.")
            tebra_id = result["appointment_id"]
            logger.info("[%s] v2/create BOOKED tebra=%s patient=%s(created=%s) pid=%s %s %02d:%02d %smin",
                        rid, tebra_id, patient_id, pat["created"], pid, date_s, t[0], t[1], vt["minutes"])

            # ── 6. Supabase: cache row, appointments row, lead ──
            try:
                await calendar_store.insert_tebra_appointment({
                    "tebra_appointment_id": tebra_id, "tebra_provider_id": pid,
                    "tebra_location_id": int(clinic["tebra_location_id"]), "start_at": start_utc, "end_at": end_utc,
                    "duration_minutes": vt["minutes"], "status": "scheduled", "appt_type": "P",
                    "tebra_patient_id": int(patient_id), "source": "ai_booking",
                }, rid)
            except calendar_store.CalendarStoreError as e:
                logger.error("[%s] cache insert failed (sync will catch up): %s", rid, e)

            appt_row = {
                "tebra_appointment_id": tebra_id, "tebra_patient_id": patient_id,
                "tebra_service_location_id": str(clinic["tebra_location_id"]), "tebra_reason_id": vt["reason_id"],
                "service": service or "Physical Therapy", "location": label,
                "appointment_date": date_s, "appointment_time": f"{t[0]:02d}:{t[1]:02d}",
                "patient_name": name, "patient_phone": e164, "status": "scheduled", "practice_id": SUPABASE_PRACTICE_ID,
                "tebra_provider_id": pid, "provider_name": provider, "visit_type": visit_type,
                "duration_minutes": vt["minutes"], "end_time": f"{end_min // 60:02d}:{end_min % 60:02d}",
            }
            if lead_id:
                appt_row["lead_id"] = lead_id
            sb = await supabase_insert_appointment(appt_row)
            sb_id = sb.get("id") if sb else None
            if not sb_id:
                logger.error("[%s] appointments insert FAILED (Tebra booking OK tebra=%s)", rid, tebra_id)
            if lead_id:
                await svc.update_lead(lead_id, {"queue_status": "complete", "lead_outcome": "booked",
                                                "tebra_patient_id": patient_id},
                                      {"booked_v2": {"tebra_appointment_id": tebra_id, "clinician_id": pid,
                                                     "date": date_s, "time": f"{t[0]:02d}:{t[1]:02d}"}}, rid)

            # ── 7. Confirmation SMS (never to +1555 test numbers) ──
            if sb_id:
                if e164.startswith(TEST_PHONE_PREFIXES):
                    logger.info("[%s] SMS skipped for test number %s (appt=%s)", rid, e164, sb_id)
                else:
                    await send_appointment_sms_if_needed(
                        rid=rid, appointment_id=sb_id, notification_type="sms_appointment_booked",
                        appt={"patient_name": name, "patient_phone": e164, "appointment_date": date_s,
                              "appointment_time": f"{t[0]:02d}:{t[1]:02d}", "location": label,
                              "service": f"{vt['label']} with {provider}"},
                        lead_id=lead_id)

        return build_vapi_response(tool_call_id, _booked_msg(first, vt["label"], provider, date_s, start_min, label,
                                                             sb_id, tebra_id, patient_id, pat["created"]))

    except calendar_store.CalendarStoreError as e:
        logger.error("[%s] v2/create-appointment calendar error: %s", rid, e)
        return build_vapi_response(tool_call_id, "I can't reach the calendar right now. Tell the caller the front desk will call them to finish booking. Do not say it is booked.")
    except Exception as e:
        logger.exception("[%s] Error in /v2/create-appointment: %s", rid, e)
        return build_vapi_response(tool_call_id, "Sorry, there was an error booking. Tell the caller the front desk will call them. Do not say it is booked.")


# ─────────────────────────────────────────────────────────────────────────────
# POST /v2/cancel-appointment
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/cancel-appointment",
    summary="Delete a booking in Tebra + remove it from the calendar copy + mark appointments cancelled",
    openapi_extra={"requestBody": {"content": {"application/json": {
        "schema": inline_schema_refs(CancelAppointmentV2Request.model_json_schema())}}, "required": True}},
)
async def cancel_appointment_v2(request: Request):
    rid = str(uuid4())[:8]
    tool_call_id = None
    try:
        args, tool_call_id, _ = await _parse(request)
        logger.info("[%s] ======== v2/cancel-appointment START ======== args=%s", rid, args)
        tebra_id = _clean(args.get("tebra_appointment_id"))
        appt_id = _lead_id(args.get("appointment_id"))
        row = await svc.fetch_appointment(appt_id, None, rid) if appt_id else (
            await svc.fetch_appointment(None, tebra_id, rid) if tebra_id else None)
        tebra_id = tebra_id or (row or {}).get("tebra_appointment_id")
        if not tebra_id:
            return build_vapi_response(tool_call_id, "I need a tebra_appointment_id or appointment_id to cancel.")

        result = await call_tebra_delete_appointment(str(tebra_id), rid)
        gone = result["success"]
        if not gone:
            still = await call_tebra_get_appointment(str(tebra_id), rid)
            gone = still is None or not still.get("StartTime")
            logger.info("[%s] delete failed (%s); still in Tebra=%s", rid, result.get("error"), not gone)
        if not gone:
            return build_vapi_response(tool_call_id, f"I couldn't cancel appointment {tebra_id} in Tebra right now: {result.get('error')}.")

        removed = 0
        try:
            removed = await calendar_store.delete_tebra_appointment(tebra_id, rid)
        except calendar_store.CalendarStoreError as e:
            logger.error("[%s] cache delete failed: %s", rid, e)
        if row and row.get("status") != "cancelled":
            await supabase_update_appointment(row["id"], {"status": "cancelled",
                                                          "cancelled_at": datetime.now(timezone.utc).isoformat(),
                                                          "updated_at": datetime.now(timezone.utc).isoformat()})
        logger.info("[%s] v2/cancel done tebra=%s cache_removed=%s sb=%s", rid, tebra_id, removed, (row or {}).get("id"))
        return build_vapi_response(tool_call_id, f"CANCELLED: appointment {tebra_id} is cancelled and the time is free again.")
    except Exception as e:
        logger.exception("[%s] Error in /v2/cancel-appointment: %s", rid, e)
        return build_vapi_response(tool_call_id, "Sorry, there was an error cancelling. Please try again.")


# ─────────────────────────────────────────────────────────────────────────────
# POST /v2/update-lead
# ─────────────────────────────────────────────────────────────────────────────

_OUTCOMES = {
    # outcome: (queue_status, lead_outcome, metadata)
    "do_not_call":    ("not_interested", "not_interested", {"do_not_call": True}),
    "not_interested": ("not_interested", "not_interested", {}),
    "front_desk":     ("manual_follow_up", "manual", {}),
}
_OUTCOME_ALIASES = {"dnc": "do_not_call", "do_not_call_again": "do_not_call", "manual_follow_up": "front_desk",
                    "handoff": "front_desk", "transfer_failed": "front_desk", "callback": "front_desk",
                    "manual": "front_desk"}


@router.post(
    "/update-lead",
    summary="Outbound v2 lead outcome: do_not_call / not_interested / front_desk hand-off",
    openapi_extra={"requestBody": {"content": {"application/json": {
        "schema": inline_schema_refs(UpdateLeadV2Request.model_json_schema())}}, "required": True}},
)
async def update_lead_v2(request: Request):
    rid = str(uuid4())[:8]
    tool_call_id = None
    try:
        args, tool_call_id, _ = await _parse(request)
        logger.info("[%s] ======== v2/update-lead START ======== args=%s", rid, args)
        lead_id = _lead_id(args.get("lead_id"))
        outcome = (_clean(args.get("outcome")) or "").lower().replace("-", "_").replace(" ", "_")
        outcome = _OUTCOME_ALIASES.get(outcome, outcome)
        if not lead_id:
            return build_vapi_response(tool_call_id, "Missing lead_id; nothing updated. Continue the call normally.")
        if outcome not in _OUTCOMES:
            return build_vapi_response(tool_call_id, "outcome must be do_not_call, not_interested or front_desk.")
        lead = await svc.fetch_lead(lead_id, rid)
        if not lead:
            return build_vapi_response(tool_call_id, "Lead not found; nothing updated. Continue the call normally.")
        qs, lo, meta = _OUTCOMES[outcome]
        meta = dict(meta)
        now_iso = datetime.now(timezone.utc).isoformat()
        reason = _clean(args.get("reason"))
        meta[f"{outcome}_at"] = now_iso
        if reason:
            meta[f"{outcome}_reason"] = reason[:500]
        data = {"queue_status": qs, "lead_outcome": lo}
        if lead.get("lead_outcome") == "booked":
            data = {}      # never undo a booking; only record the flag / reason
        notes = _clean(args.get("notes"))
        if notes:
            data["notes"] = ((lead.get("notes") or "") + f"\n[v2 {now_iso[:16]}] {notes}").strip()[:4000]
        ok = await svc.update_lead(lead_id, data, meta, rid)
        if not ok:
            return build_vapi_response(tool_call_id, "I couldn't save that right now, but continue and close the call politely.")
        say = {"do_not_call": "Saved: they won't be called again. Apologize briefly and end the call politely.",
               "not_interested": "Saved as not interested. Thank them and end the call politely.",
               "front_desk": "Saved: the front desk will call them back. Tell them that and end the call politely."}[outcome]
        return build_vapi_response(tool_call_id, say)
    except Exception as e:
        logger.exception("[%s] Error in /v2/update-lead: %s", rid, e)
        return build_vapi_response(tool_call_id, "I couldn't save that right now, but continue and close the call politely.")
