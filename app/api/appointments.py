import re
import asyncio
import httpx
from datetime import datetime, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo
from fastapi import APIRouter, Request

from app.models.requests import (
    CreateAppointmentRequest,
    UpdateAppointmentStatusRequest,
    RescheduleAppointmentRequest,
    ConfirmAppointmentRequest,
    CancelAppointmentRequest,
    InboundLookupAppointmentsRequest,
    inline_schema_refs,
)

from app.core.config import (
    resolve_location,
    resolve_appointment_reason_id,
    SUPABASE_URL,
    SUPABASE_HEADERS,
    SUPABASE_PRACTICE_ID,
    LOCATION_MAP,
    TEBRA_VALID_NAMES,
    TEBRA_INBOUND_APPOINTMENTS_WINDOW_DAYS,
)
from app.core.logger import logger
from app.services.tebra_service import (
    call_tebra_get_appointments,
    call_tebra_get_appointments_by_patient_id,
    get_patient_by_name,
    create_patient,
    create_appointment_in_tebra,
    call_tebra_get_appointment,
    call_tebra_update_appointment,
)
from app.services.supabase_service import (
    supabase_insert_appointment,
    supabase_update_appointment,
    supabase_update_lead,
)
from app.services.appointment_sms_service import send_appointment_sms_if_needed
from app.utils.time_utils import (
    parse_time_to_24hr,
    format_12hr,
    parse_booked_slots,
    get_available_slots,
    get_nearest_available_slots,
    is_valid_clinic_slot,
    format_location_hours,
)
from app.utils.parser import build_vapi_response, coerce_vapi_tool_arguments

router = APIRouter(tags=["Appointments"])

def _sanitize_supabase_lead_id(raw) -> str | None:
    """
    Supabase `lead_id` must be a UUID. Vapi/OpenAPI often sends placeholders like the literal
    string \"string\" or \"{{lead_id}}\" — drop those so inserts/patches do not 400.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or s.startswith("{{"):
        return None
    low = s.lower()
    if low in ("string", "null", "none", "undefined", "nan", "n/a", "na", "lead_id"):
        return None
    try:
        UUID(s)
        return s
    except ValueError:
        return None


def _normalize_phone_digits(raw: str | None) -> str | None:
    if not raw:
        return None
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) < 10:
        return None
    if len(digits) > 15:
        return None
    return digits

def _split_patient_first_last(raw: str | None) -> tuple[str, str] | None:
    parts = (str(raw or "").strip()).split()
    if len(parts) < 2:
        return None
    return parts[0], " ".join(parts[1:])


def _inbound_lookup_date_range_la() -> tuple[str, str]:
    la = ZoneInfo("America/Los_Angeles")
    today = datetime.now(la).date()
    end = today + timedelta(days=TEBRA_INBOUND_APPOINTMENTS_WINDOW_DAYS)
    return today.isoformat(), end.isoformat()


def _keep_inbound_confirmation_status(status: str | None) -> bool:
    if not status or not str(status).strip():
        return True
    u = str(status).strip().lower()
    if "cancel" in u:
        return False
    if u == "rescheduled":
        return False
    if "no show" in u or "noshow" in u.replace(" ", ""):
        return False
    return True


def _is_upcoming_in_la(a: dict, now_la: datetime) -> bool:
    d = a.get("appointment_date")
    if not d or not isinstance(d, str):
        return False
    today_s = now_la.date().isoformat()
    if d > today_s:
        return True
    if d < today_s:
        return False
    t24 = a.get("appointment_time_24hr") or ""
    if not (isinstance(t24, str) and len(t24) >= 5 and t24[2] == ":"):
        return True
    try:
        hh, mm = int(t24[:2]), int(t24[3:5])
    except ValueError:
        return True
    return (hh, mm) >= (now_la.hour, now_la.minute)


def _sort_inbound_appointments(appts: list[dict]) -> list[dict]:
    def key(a: dict) -> tuple:
        d = a.get("appointment_date") or ""
        t = a.get("appointment_time_24hr") or "99:99"
        return (d, t)

    return sorted(appts, key=key)


async def _supabase_appointment_id_for_tebra(tebra_id: str, rid: str) -> str | None:
    if not tebra_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/appointments?tebra_appointment_id=eq.{tebra_id}"
                "&select=id&limit=1",
                headers=SUPABASE_HEADERS,
            )
            if r.status_code == 200 and (r.json() or []):
                return (r.json() or [None])[0].get("id")
    except Exception as e:
        logger.warning("[%s] tebra→supabase id lookup error: %s", rid, e)
    return None


async def _attach_supabase_ids(appts: list[dict], rid: str) -> None:
    for a in appts:
        tid = a.get("tebra_appointment_id")
        if not tid:
            continue
        sb = await _supabase_appointment_id_for_tebra(str(tid), rid)
        if sb:
            a["supabase_appointment_id"] = sb


def _format_inbound_locked_row(a: dict) -> str:
    loc = a.get("service_location_name") or "unknown location"
    d = a.get("appointment_date") or "?"
    t = a.get("appointment_time_12hr") or a.get("appointment_time_24hr") or "?"
    tid = a.get("tebra_appointment_id")
    sb = a.get("supabase_appointment_id")
    reason = a.get("appointment_reason")
    reason_bit = f" — {reason}" if reason else ""
    sb_bit = f" appointment_id:{sb}" if sb else ""
    return (
        f"Locked for reschedule (Pacific): {d} at {t} — {loc}{reason_bit}. "
        f"Use tebra_appointment_id {tid}{sb_bit} with reschedule_appointment. "
        "Do not read the numeric IDs aloud unless you are double-checking with the caller."
    )


def _format_inbound_appointment_list(appts: list[dict], window_days: int) -> str:
    if not appts:
        return (
            f"No upcoming appointments in Tebra for that patient in the next {window_days} days "
            "(Pacific calendar), for the configured practice."
        )
    lines = [
        f"Found {len(appts)} upcoming appointment(s) (Pacific time, next {window_days} days). "
        "Read date, time, and location to the caller. Each line below has tebra_appointment_id and "
        "appointment_id (Supabase) when known — after they pick an option, match their answer to one line "
        "and pass those IDs to reschedule_appointment (no second inbound_lookup needed unless ambiguous). "
        "Do not read those IDs aloud.",
        "",
    ]
    for i, a in enumerate(appts, start=1):
        loc = a.get("service_location_name") or "unknown location"
        d = a.get("appointment_date") or "?"
        t = a.get("appointment_time_12hr") or a.get("appointment_time_24hr") or "?"
        tid = a.get("tebra_appointment_id")
        reason = a.get("appointment_reason") or ""
        rbit = f" — {reason}" if reason else ""
        sb = a.get("supabase_appointment_id")
        sb_bit = f" appointment_id:{sb}" if sb else ""
        lines.append(
            f"({i}) {d} at {t} — {loc}{rbit} — tebra_appointment_id {tid}{sb_bit}"
        )
    return "\n".join(lines)


def _inbound_lookup_vapi_response(rid: str, tool_call_id: str | None, message: str):
    """Log Vapi tool reply; warn if toolCallId missing (Vapi shows 'No result returned')."""
    preview = message if len(message) <= 700 else message[:700] + "…"
    logger.info(
        "[%s] inbound-lookup-appointments RESPONSE tool_call_id=%s result_chars=%s preview=%r",
        rid,
        tool_call_id,
        len(message),
        preview,
    )
    if not tool_call_id:
        logger.warning(
            "[%s] inbound-lookup-appointments missing tool_call_id — Vapi will likely show 'No result returned'.",
            rid,
        )
    return build_vapi_response(tool_call_id, message)


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT: CREATE APPOINTMENT
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/create-appointment",
    summary="Create a new appointment in Tebra + Supabase",
    openapi_extra={
        "requestBody": {
            "content": {"application/json": {"schema": inline_schema_refs(CreateAppointmentRequest.model_json_schema())}},
            "required": True,
        }
    },
)
async def create_appointment(request: Request):
    try:
        rid  = str(uuid4())[:8]
        body = await request.json()
        logger.info("[%s] ======== create-appointment START ========", rid)
        logger.info("[%s] create-appointment body=%s", rid, body)

        tool_call_id = None
        if "message" in body and "toolCalls" in body["message"]:
            tc           = body["message"]["toolCalls"][0]
            tool_call_id = tc.get("id")
            args         = tc["function"]["arguments"]
            date         = args.get("date")
            time_str     = args.get("time")
            name         = args.get("name")
            phone        = args.get("phone")
            service      = args.get("service")
            location     = args.get("location")
            lead_id      = args.get("lead_id")
        else:
            date     = body.get("date")
            time_str = body.get("time")
            name     = body.get("name")
            phone    = body.get("phone")
            service  = body.get("service")
            location = body.get("location")
            lead_id  = body.get("lead_id")

        lead_id = _sanitize_supabase_lead_id(lead_id)

        # ── Validate required fields ──
        missing = [f for f, v in {
            "name": name, "phone": phone,
            "date": date, "time": time_str, "location": location
        }.items() if not v]
        if missing:
            return build_vapi_response(
                tool_call_id,
                f"Missing required fields: {', '.join(missing)}. Please provide them to book."
            )

        # ── Validate date format ──
        if not date or not re.match(r'^\d{4}-\d{2}-\d{2}$', str(date)):
            return build_vapi_response(
                tool_call_id,
                f"I need the date in YYYY-MM-DD format, for example 2026-04-02. "
                f"You said '{date}' — could you give me the exact date?"
            )

        # ── Block Sundays — clinic is closed ──
        requested_date = datetime.strptime(date, "%Y-%m-%d")
        if requested_date.weekday() == 6:  # Sunday
            logger.info("[%s] create-appointment BLOCKED — %s is a Sunday", rid, date)
            return build_vapi_response(
                tool_call_id,
                f"Sorry, {date} is a Sunday and the clinic is closed. "
                f"We are open Monday through Saturday. Please choose a different date."
            )

        # ── Validate time format ──
        parsed_time = parse_time_to_24hr(time_str)
        if not parsed_time:
            return build_vapi_response(
                tool_call_id,
                f"I need the time in HH:MM format or like '9 AM' or '1:30 PM'. "
                f"You said '{time_str}' — could you repeat the time?"
            )
        time_normalized = f"{parsed_time[0]:02d}:{parsed_time[1]:02d}"

        # ── Split name ──
        parts      = name.strip().split(" ", 1)
        first_name = parts[0]
        last_name  = parts[1] if len(parts) > 1 else "."

        # ── Resolve location ──
        loc         = resolve_location(location)
        location_id = loc["id"]
        tebra_name  = loc["name"]
        if not location_id:
            return build_vapi_response(
                tool_call_id,
                f"Could not resolve location '{location}'. Please provide a valid clinic location."
            )

        # ── Validate clinic working hours for this location/date ──
        if not is_valid_clinic_slot(date, location, parsed_time[0], parsed_time[1]):
            return build_vapi_response(
                tool_call_id,
                f"Sorry, {format_12hr(parsed_time[0], parsed_time[1])} is outside clinic hours for {location} on {date}. "
                f"We're open {format_location_hours(date, location)}."
            )

        reason_id = resolve_appointment_reason_id(service)
        logger.info("[%s] ── INPUT SUMMARY ──────────────────────────────────────", rid)
        logger.info("[%s]   tool_call_id : %s", rid, tool_call_id)
        logger.info("[%s]   name         : %s  (first=%s last=%s)", rid, name, first_name, last_name)
        logger.info("[%s]   phone        : %s", rid, phone)
        logger.info("[%s]   date         : %s", rid, date)
        logger.info("[%s]   time         : %s", rid, time_str)
        logger.info("[%s]   service      : %s → reasonId=%s", rid, service, reason_id)
        logger.info("[%s]   location     : %s → locationId=%s", rid, location, location_id)
        logger.info("[%s]   lead_id      : %s", rid, lead_id)
        logger.info("[%s] ────────────────────────────────────────────────────────", rid)

        # ── Steps 1 + 1c in parallel: GetPatients + GetAppointments simultaneously ──
        logger.info("[%s] STEP 1+1c → GetPatients + GetAppointments in parallel", rid)
        patient_id, pre_xml = await asyncio.gather(
            get_patient_by_name(first_name, last_name, rid),
            call_tebra_get_appointments(date, tebra_name),
        )

        if patient_id:
            logger.info("[%s] STEP 1 RESULT → Patient EXISTS in Tebra | patientId=%s", rid, patient_id)
        else:
            logger.info("[%s] STEP 1 RESULT → Patient NOT FOUND — will create new", rid)
            logger.info("[%s] STEP 1b → CreatePatient (name=%s %s phone=%s)", rid, first_name, last_name, phone)
            patient_id = await create_patient(first_name, last_name, phone, rid)
            if not patient_id:
                logger.error("[%s] STEP 1b RESULT → CreatePatient FAILED — returning error to VAPI", rid)
                return build_vapi_response(
                    tool_call_id,
                    "I was unable to create a patient record. "
                    "Could you please confirm your full name and phone number?"
                )
            logger.info("[%s] STEP 1b RESULT → CreatePatient SUCCESS | patientId=%s", rid, patient_id)

        # ── Step 1c: Server-side slot guard (uses pre_xml already fetched above) ──
        logger.info("[%s] STEP 1c → Slot pre-check date=%s time=%s location=%s", rid, date, time_str, tebra_name)
        if "<IsError>true</IsError>" not in pre_xml:
            booked_now = parse_booked_slots(pre_xml, date)
            if (parsed_time[0], parsed_time[1]) in booked_now:
                # ── Idempotency: VAPI retry after ECONNRESET may hit this path even though
                #    the appointment was already booked by a previous request. Check Supabase
                #    for an existing scheduled appointment for this lead at the exact same slot.
                if lead_id:
                    try:
                        async with httpx.AsyncClient(timeout=8.0) as _client:
                            _r = await _client.get(
                                f"{SUPABASE_URL}/rest/v1/appointments"
                                f"?lead_id=eq.{lead_id}"
                                f"&appointment_date=eq.{date}"
                                f"&appointment_time=eq.{time_normalized}"
                                f"&status=eq.scheduled"
                                f"&select=id,tebra_appointment_id",
                                headers=SUPABASE_HEADERS,
                            )
                            if _r.status_code == 200 and _r.json():
                                existing       = _r.json()[0]
                                existing_sb_id = existing["id"]
                                existing_tebra = existing["tebra_appointment_id"]
                                logger.info(
                                    "[%s] STEP 1c IDEMPOTENT: slot conflict but lead already has "
                                    "scheduled appt sb=%s tebra=%s — returning success",
                                    rid, existing_sb_id, existing_tebra,
                                )
                                return build_vapi_response(
                                    tool_call_id,
                                    f"Appointment booked successfully! "
                                    f"{name} is scheduled for {service} at {location} "
                                    f"on {date} at {format_12hr(parsed_time[0], parsed_time[1])}. "
                                    f"appointment_id:{existing_sb_id} tebra_id:{existing_tebra}",
                                )
                    except Exception as _e:
                        logger.error("[%s] STEP 1c idempotency check error: %s", rid, _e)

                # ── Inbound idempotency (no lead_id): phone + exact slot (+ location) ──
                if not lead_id and phone:
                    try:
                        phone_digits = _normalize_phone_digits(phone)
                        if phone_digits:
                            async with httpx.AsyncClient(timeout=8.0) as _client:
                                _r2 = await _client.get(
                                    f"{SUPABASE_URL}/rest/v1/appointments"
                                    f"?patient_phone=eq.{phone_digits}"
                                    f"&appointment_date=eq.{date}"
                                    f"&appointment_time=eq.{time_normalized}"
                                    f"&status=eq.scheduled"
                                    f"&location=eq.{location}"
                                    f"&select=id,tebra_appointment_id,patient_name",
                                    headers=SUPABASE_HEADERS,
                                )
                                if _r2.status_code == 200 and _r2.json():
                                    existing = _r2.json()[0]
                                    existing_sb_id = existing.get("id")
                                    existing_tebra = existing.get("tebra_appointment_id")
                                    logger.info(
                                        "[%s] STEP 1c IDEMPOTENT(inbound): slot conflict but phone already has "
                                        "scheduled appt sb=%s tebra=%s — returning success",
                                        rid, existing_sb_id, existing_tebra,
                                    )
                                    return build_vapi_response(
                                        tool_call_id,
                                        f"Appointment booked successfully! "
                                        f"{name} is scheduled for {service} at {location} "
                                        f"on {date} at {format_12hr(parsed_time[0], parsed_time[1])}. "
                                        f"appointment_id:{existing_sb_id} tebra_id:{existing_tebra}",
                                    )
                    except Exception as _e2:
                        logger.error("[%s] STEP 1c inbound idempotency check error: %s", rid, _e2)

                available_now = get_available_slots(booked_now, date, location)
                nearest       = get_nearest_available_slots(parsed_time[0], parsed_time[1], available_now)
                slots_str     = ", ".join(nearest) if nearest else "no other slots today"
                logger.warning("[%s] STEP 1c SLOT CONFLICT: %s is already booked at %s. Nearest: %s",
                               rid, time_str, location, slots_str)
                return build_vapi_response(
                    tool_call_id,
                    f"Sorry, {format_12hr(parsed_time[0], parsed_time[1])} is already taken at {location}. "
                    f"Please ask the patient which of these works: {slots_str}."
                )
        logger.info("[%s] STEP 1c RESULT → Slot is free, proceeding to book", rid)

        # ── Step 2: Create appointment in Tebra (with one retry) ──
        logger.info("[%s] STEP 2 → CreateAppointment (patientId=%s date=%s time=%s location=%s)",
                    rid, patient_id, date, time_str, location)
        result = await create_appointment_in_tebra(
            patient_id, location_id, date, time_str, reason_id, rid
        )
        if not result["success"]:
            logger.warning("[%s] STEP 2 first attempt failed (%s) — retrying once...",
                           rid, result.get("error"))
            await asyncio.sleep(1.5)
            result = await create_appointment_in_tebra(
                patient_id, location_id, date, time_str, reason_id, rid
            )
            logger.info("[%s] STEP 2 retry result → success=%s error=%s",
                        rid, result["success"], result.get("error"))
        logger.info("[%s] STEP 2 RESULT → success=%s appointmentId=%s error=%s",
                    rid, result["success"], result.get("appointment_id"), result.get("error"))

        if result["success"]:
            tebra_appt_id = result["appointment_id"]

            # ── Step 3: Write to Supabase ──
            appt_data = {
                "tebra_appointment_id":      tebra_appt_id,
                "tebra_patient_id":          patient_id,
                "tebra_service_location_id": location_id,
                "tebra_reason_id":           reason_id,
                "service":                   service,
                "location":                  location,
                "appointment_date":          date,
                "appointment_time":          time_normalized,
                "patient_name":              name,
                "patient_phone":             phone,
                "status":                    "scheduled",
                "practice_id":               SUPABASE_PRACTICE_ID,
            }
            if lead_id:
                appt_data["lead_id"] = lead_id

            logger.info("[%s] STEP 3 → Supabase INSERT appointments | data=%s", rid, appt_data)
            supabase_appt = await supabase_insert_appointment(appt_data)

            if supabase_appt:
                logger.info("[%s] STEP 3 RESULT → Supabase INSERT SUCCESS | supabase_id=%s",
                            rid, supabase_appt.get("id"))
            else:
                logger.error("[%s] STEP 3 RESULT → Supabase INSERT FAILED (Tebra booking was OK — apptId=%s)",
                             rid, tebra_appt_id)

            # ── Step 4: Update lead with tebra_patient_id ──
            if lead_id:
                await supabase_update_lead(lead_id, {
                    "tebra_patient_id": patient_id,
                    "updated_at":       datetime.utcnow().isoformat(),
                })
                logger.info("[%s] lead updated tebra_patient_id=%s", rid, patient_id)

            supabase_appt_id = supabase_appt.get("id") if supabase_appt else None
            logger.info("[%s] RESPONSE → supabase_appt_id=%s tebra_appt_id=%s",
                        rid, supabase_appt_id, tebra_appt_id)

            # ── SMS (booked) — send only after Supabase insert success ──
            if supabase_appt_id:
                await send_appointment_sms_if_needed(
                    rid=rid,
                    appointment_id=supabase_appt_id,
                    notification_type="sms_appointment_booked",
                    appt={
                        "patient_name": name,
                        "patient_phone": phone,
                        "appointment_date": date,
                        "appointment_time": time_normalized,
                        "location": location,
                        "service": service,
                    },
                    lead_id=lead_id,
                )
            msg = (
                f"Appointment booked successfully! "
                f"{name} is scheduled for {service} at {location} "
                f"on {date} at {format_12hr(parsed_time[0], parsed_time[1])}. "
                f"appointment_id:{supabase_appt_id} tebra_id:{tebra_appt_id}"
            )
        else:
            msg = (
                "Booking failed after retry. Please tell the patient: "
                "'I'm having a little trouble completing this on my end right now. "
                "Let me have someone call you right back to confirm your appointment.' "
                f"Error detail: {result['error']}"
            )

        logger.info("[%s] create-appointment done success=%s apptId=%s",
                    rid, result["success"], result.get("appointment_id"))
        return build_vapi_response(tool_call_id, msg)

    except Exception as e:
        logger.exception("Error in /create-appointment: %s", e)
        return build_vapi_response(
            locals().get("tool_call_id"),
            "Sorry, there was an error processing your booking. Please try again."
        )


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT: UPDATE APPOINTMENT STATUS IN TEBRA (Agent 2)
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/update-appointment-status",
    summary="Update appointment status in Tebra (Confirmed / Cancelled / etc.)",
    openapi_extra={
        "requestBody": {
            "content": {"application/json": {"schema": inline_schema_refs(UpdateAppointmentStatusRequest.model_json_schema())}},
            "required": True,
        }
    },
)
async def update_appointment_status(request: Request):
    try:
        rid  = str(uuid4())[:8]
        body = await request.json()
        logger.info("[%s] ======== update-appointment-status START ========", rid)
        logger.info("[%s] update-appointment-status body=%s", rid, body)

        tool_call_id = None
        if "message" in body and "toolCalls" in body["message"]:
            tc           = body["message"]["toolCalls"][0]
            tool_call_id = tc.get("id")
            args         = tc["function"]["arguments"]
        else:
            args = body

        tebra_appt_id  = args.get("tebra_appointment_id")
        new_status     = args.get("new_status")
        appointment_id = args.get("appointment_id")

        if not tebra_appt_id:
            return build_vapi_response(tool_call_id, "Missing tebra_appointment_id.")

        if new_status == "Cancelled":
            return build_vapi_response(
                tool_call_id,
                "Use the cancel_appointment tool to cancel appointments — it handles all required updates correctly."
            )

        valid_statuses = {
            "Confirmed", "NoShow", "Rescheduled",
            "Scheduled", "CheckedIn", "CheckedOut", "NeedsReschedule",
        }
        if not new_status or new_status not in valid_statuses:
            return build_vapi_response(
                tool_call_id,
                f"Invalid status '{new_status}'. Must be one of: {', '.join(sorted(valid_statuses))}."
            )

        # ── Step 1: Get current appointment from Tebra ──
        logger.info("[%s] STEP 1 → GetAppointment tebra_id=%s", rid, tebra_appt_id)
        appt_data = await call_tebra_get_appointment(tebra_appt_id, rid)
        if not appt_data:
            return build_vapi_response(
                tool_call_id,
                f"Could not fetch appointment {tebra_appt_id} from Tebra. It may not exist."
            )

        # ── Step 2: Update status and send back ──
        appt_data["AppointmentStatus"] = new_status
        logger.info("[%s] STEP 2 → UpdateAppointment tebra_id=%s → %s", rid, tebra_appt_id, new_status)
        result = await call_tebra_update_appointment(appt_data, rid)

        if result["success"]:
            msg = f"Appointment {tebra_appt_id} status updated to {new_status} in Tebra."
            logger.info("[%s] update-appointment-status SUCCESS", rid)

            # ── Step 3: Also update Supabase if appointment_id provided ──
            if appointment_id:
                sb_map = {
                    "Confirmed":       "confirmed",
                    "Cancelled":       "cancelled",
                    "NoShow":          "no_show",
                    "Rescheduled":     "rescheduled",
                    "Scheduled":       "scheduled",
                    "CheckedIn":       "checked_in",
                    "CheckedOut":      "checked_out",
                    "NeedsReschedule": "needs_reschedule",
                }
                await supabase_update_appointment(appointment_id, {
                    "status":     sb_map.get(new_status, new_status.lower()),
                    "updated_at": datetime.utcnow().isoformat(),
                })
                logger.info("[%s] Supabase appointment also updated appt_id=%s", rid, appointment_id)
        else:
            msg = f"Failed to update appointment in Tebra: {result['error']}"
            logger.error("[%s] update-appointment-status FAILED error=%s", rid, result["error"])

        return build_vapi_response(tool_call_id, msg)

    except Exception as e:
        logger.exception("Error in /update-appointment-status: %s", e)
        return build_vapi_response(
            locals().get("tool_call_id"),
            "Sorry, there was an error updating the appointment status. Please try again."
        )


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT: RESCHEDULE APPOINTMENT (Agent 2)
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/reschedule-appointment",
    summary="Reschedule an existing appointment (mark old as Rescheduled, create new)",
    openapi_extra={
        "requestBody": {
            "content": {"application/json": {"schema": inline_schema_refs(RescheduleAppointmentRequest.model_json_schema())}},
            "required": True,
        }
    },
)
async def reschedule_appointment(request: Request):
    try:
        rid  = str(uuid4())[:8]
        body = await request.json()
        logger.info("[%s] ======== reschedule-appointment START ========", rid)
        logger.info("[%s] reschedule-appointment body=%s", rid, body)

        tool_call_id = None
        if "message" in body and "toolCalls" in body["message"]:
            tc           = body["message"]["toolCalls"][0]
            tool_call_id = tc.get("id")
            args         = tc["function"]["arguments"]
        else:
            args = body

        tebra_appt_id  = args.get("tebra_appointment_id")
        appointment_id = args.get("appointment_id")
        new_date       = args.get("new_date")
        new_time       = args.get("new_time")
        new_location   = args.get("location")
        new_service    = args.get("service")
        lead_id        = _sanitize_supabase_lead_id(args.get("lead_id"))
        appointment_id = str(appointment_id).strip() if appointment_id else None

        # ── Validate required fields ──
        # appointment_id is optional: set when inbound_lookup attached a Supabase row (Case A);
        # staff-only Tebra visits (Case B) reschedule with tebra_appointment_id only.
        missing = [f for f, v in {
            "tebra_appointment_id": tebra_appt_id,
            "new_date":             new_date,
            "new_time":             new_time,
        }.items() if not v]
        if missing:
            return build_vapi_response(
                tool_call_id,
                f"Missing required fields: {', '.join(missing)}. Cannot reschedule."
            )

        # ── Validate date format ──
        if not re.match(r'^\d{4}-\d{2}-\d{2}$', str(new_date)):
            return build_vapi_response(
                tool_call_id,
                f"I need the date in YYYY-MM-DD format. You said '{new_date}'."
            )

        # ── Block Sundays — clinic is closed ──
        requested_date = datetime.strptime(new_date, "%Y-%m-%d")
        if requested_date.weekday() == 6:  # Sunday
            logger.info("[%s] reschedule-appointment BLOCKED — %s is a Sunday", rid, new_date)
            return build_vapi_response(
                tool_call_id,
                f"Sorry, {new_date} is a Sunday and the clinic is closed. "
                f"We are open Monday through Saturday. Please choose a different date."
            )

        # ── Validate time format ──
        parsed_time = parse_time_to_24hr(new_time)
        if not parsed_time:
            return build_vapi_response(
                tool_call_id,
                f"I need the time in HH:MM format or like '9 AM'. You said '{new_time}'."
            )

        time_normalized = f"{parsed_time[0]:02d}:{parsed_time[1]:02d}"

        # ── Idempotency guard (Supabase-first, only when we have old appointment_id) ──
        if appointment_id:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    r = await client.get(
                        f"{SUPABASE_URL}/rest/v1/appointments"
                        f"?rescheduled_from_id=eq.{appointment_id}"
                        f"&appointment_date=eq.{new_date}"
                        f"&appointment_time=eq.{time_normalized}"
                        f"&select=id,tebra_appointment_id",
                        headers=SUPABASE_HEADERS,
                    )
                    if r.status_code == 200 and r.json():
                        existing = r.json()[0]
                        existing_sb_id = existing.get("id")
                        existing_tebra = existing.get("tebra_appointment_id")
                        logger.info(
                            "[%s] reschedule IDEMPOTENT hit old_appt_id=%s new_appt_id=%s new_tebra_id=%s",
                            rid, appointment_id, existing_sb_id, existing_tebra
                        )
                        msg = (
                            f"Appointment already rescheduled! "
                            f"New appointment on {new_date} at {format_12hr(parsed_time[0], parsed_time[1])}. "
                            f"appointment_id:{existing_sb_id} tebra_id:{existing_tebra}"
                        )
                        return build_vapi_response(tool_call_id, msg)
            except Exception as e:
                logger.error("[%s] reschedule idempotency check error: %s", rid, e)

        # ── Step 1: Get current appointment from Tebra ──
        logger.info("[%s] STEP 1 → GetAppointment tebra_id=%s", rid, tebra_appt_id)
        old_appt = await call_tebra_get_appointment(tebra_appt_id, rid)
        if not old_appt:
            return build_vapi_response(
                tool_call_id,
                f"Could not fetch appointment {tebra_appt_id} from Tebra. It may not exist."
            )

        # ── Step 2: Mark old appointment as Rescheduled ──
        old_appt["AppointmentStatus"] = "Rescheduled"
        logger.info("[%s] STEP 2 → UpdateAppointment (old) tebra_id=%s → Rescheduled", rid, tebra_appt_id)
        update_result = await call_tebra_update_appointment(old_appt, rid)
        if not update_result["success"]:
            return build_vapi_response(
                tool_call_id,
                f"Failed to mark old appointment as Rescheduled: {update_result['error']}"
            )

        # Rate limit guard — Tebra requires ½ second between calls
        await asyncio.sleep(0.6)

        # ── Resolve location_id and reason_id for new appointment ──
        patient_id  = old_appt["PatientId"]
        location_id = old_appt["ServiceLocationId"]
        reason_id   = old_appt.get("AppointmentReasonId", "0")

        if new_location:
            loc = resolve_location(new_location)
            if loc["id"]:
                location_id = loc["id"]
        if new_service:
            reason_id = resolve_appointment_reason_id(new_service)

        # ── Slot guard: verify new timeslot is free ──
        tebra_loc_name = next(
            (v["name"] for v in LOCATION_MAP.values() if v["id"] == location_id),
            next((n for n, lid in TEBRA_VALID_NAMES.items() if lid == location_id), "")
        )
        if tebra_loc_name:
            pre_xml = await call_tebra_get_appointments(new_date, tebra_loc_name)
            if "<IsError>true</IsError>" not in pre_xml:
                booked_new    = parse_booked_slots(pre_xml, new_date)
                if (parsed_time[0], parsed_time[1]) in booked_new:
                    available_new = get_available_slots(booked_new, new_date, new_location or "")
                    nearest_new   = get_nearest_available_slots(parsed_time[0], parsed_time[1], available_new)
                    slots_str     = ", ".join(nearest_new) if nearest_new else "no open slots that day"
                    logger.warning("[%s] Reschedule slot conflict: %s is booked. Nearest: %s",
                                   rid, new_time, slots_str)
                    # Rollback: un-mark old as Rescheduled
                    old_appt["AppointmentStatus"] = "Scheduled"
                    await asyncio.sleep(0.6)
                    await call_tebra_update_appointment(old_appt, rid)
                    return build_vapi_response(
                        tool_call_id,
                        f"Sorry, {format_12hr(parsed_time[0], parsed_time[1])} is already taken. "
                        f"Ask the patient which of these works: {slots_str}."
                    )

        # ── Step 3: Create new appointment ──
        logger.info("[%s] STEP 3 → CreateAppointment (new) patient=%s date=%s time=%s loc=%s reason=%s",
                    rid, patient_id, new_date, new_time, location_id, reason_id)
        create_result = await create_appointment_in_tebra(
            patient_id, location_id, new_date, new_time, reason_id, rid
        )

        if not create_result["success"]:
            # Rollback: try to un-reschedule old appointment
            logger.error("[%s] CreateAppointment (new) FAILED — attempting rollback of old", rid)
            old_appt["AppointmentStatus"] = "Scheduled"
            await asyncio.sleep(0.6)
            await call_tebra_update_appointment(old_appt, rid)
            return build_vapi_response(
                tool_call_id,
                f"Could not create the new appointment: {create_result['error']}. "
                f"The original appointment has been kept as Scheduled."
            )

        new_tebra_id    = create_result["appointment_id"]

        # ── Step 4: Update Supabase — mark old appointment as rescheduled (Case A only) ──
        if appointment_id:
            await supabase_update_appointment(appointment_id, {
                "status":     "rescheduled",
                "updated_at": datetime.utcnow().isoformat(),
            })

        # ── Step 5: Insert new appointment into Supabase ──
        old_sb_appt = None
        if appointment_id:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    r = await client.get(
                        f"{SUPABASE_URL}/rest/v1/appointments?id=eq.{appointment_id}"
                        f"&select=patient_name,patient_phone,service,location,lead_id",
                        headers=SUPABASE_HEADERS,
                    )
                    if r.status_code == 200 and r.json():
                        old_sb_appt = r.json()[0]
            except Exception as e:
                logger.error("[%s] Failed to fetch old Supabase appointment: %s", rid, e)

        tebra_appt_name = (old_appt.get("AppointmentName") or "").strip() or None

        new_appt_data = {
            "tebra_appointment_id":      new_tebra_id,
            "tebra_patient_id":          patient_id,
            "tebra_service_location_id": location_id,
            "tebra_reason_id":           reason_id,
            "appointment_date":          new_date,
            "appointment_time":          time_normalized,
            "status":                    "scheduled",
            "practice_id":               SUPABASE_PRACTICE_ID,
            "service":                   new_service or (old_sb_appt.get("service") if old_sb_appt else None),
            "location":                  new_location or (old_sb_appt.get("location") if old_sb_appt else None),
            "patient_name":              (old_sb_appt.get("patient_name") if old_sb_appt else None) or tebra_appt_name,
            "patient_phone":             old_sb_appt.get("patient_phone") if old_sb_appt else None,
        }
        if appointment_id:
            new_appt_data["rescheduled_from_id"] = appointment_id
        if not new_appt_data.get("location") and location_id:
            for slug, meta in LOCATION_MAP.items():
                if str(meta.get("id")) == str(location_id):
                    new_appt_data["location"] = slug
                    break

        if lead_id:
            new_appt_data["lead_id"] = lead_id
        elif old_sb_appt and old_sb_appt.get("lead_id"):
            sb_lid = _sanitize_supabase_lead_id(old_sb_appt.get("lead_id"))
            if sb_lid:
                new_appt_data["lead_id"] = sb_lid

        # Remove None values
        new_appt_data = {k: v for k, v in new_appt_data.items() if v is not None}

        new_sb_appt = await supabase_insert_appointment(new_appt_data)
        new_sb_id   = new_sb_appt.get("id") if new_sb_appt else None

        logger.info("[%s] reschedule complete old_tebra=%s → new_tebra=%s new_supabase=%s",
                    rid, tebra_appt_id, new_tebra_id, new_sb_id)

        # ── SMS (rescheduled) — send only after new Supabase appointment insert success ──
        if new_sb_id:
            await send_appointment_sms_if_needed(
                rid=rid,
                appointment_id=new_sb_id,
                notification_type="sms_appointment_rescheduled",
                appt={
                    "patient_name": new_appt_data.get("patient_name") or "",
                    "patient_phone": new_appt_data.get("patient_phone") or "",
                    "appointment_date": new_date,
                    "appointment_time": time_normalized,
                    "location": new_appt_data.get("location") or new_location or "",
                    "service": new_appt_data.get("service") or new_service or "",
                },
                lead_id=new_appt_data.get("lead_id"),
            )

        msg = (
            f"Appointment rescheduled successfully! "
            f"New appointment on {new_date} at {format_12hr(parsed_time[0], parsed_time[1])}. "
            f"appointment_id:{new_sb_id} tebra_id:{new_tebra_id}"
        )
        return build_vapi_response(tool_call_id, msg)

    except Exception as e:
        logger.exception("Error in /reschedule-appointment: %s", e)
        return build_vapi_response(
            locals().get("tool_call_id"),
            "Sorry, there was an error rescheduling the appointment. Please try again."
        )


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT: CONFIRM APPOINTMENT (merged — replaces update_appointment_status
#           + update_reminder_outcome for confirm/cancel flows)
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/confirm-appointment",
    summary="Confirm or cancel appointment (merged Tebra + Supabase in parallel)",
    openapi_extra={
        "requestBody": {
            "content": {"application/json": {"schema": inline_schema_refs(ConfirmAppointmentRequest.model_json_schema())}},
            "required": True,
        }
    },
)
async def confirm_appointment(request: Request):
    """
    Merged endpoint for the Reminder Agent confirm / cancel flow.
    Runs Tebra status update AND Supabase appointment+lead update in parallel,
    saving one full tool-call round-trip and ~2-3 seconds vs calling
    update_appointment_status + update_reminder_outcome sequentially.

    Fields:
      tebra_appointment_id  (required) — Tebra integer appointment ID
      appointment_id        (required) — Supabase appointment UUID
      outcome               (required) — "confirmed" or "cancelled"
      lead_id               (optional) — Supabase lead UUID
      notes                 (optional) — call summary
      reminder_type         (optional) — "24hr" or "2hr" (default "24hr")
    """
    try:
        rid  = str(uuid4())[:8]
        body = await request.json()
        logger.info("[%s] ======== confirm-appointment START ========", rid)
        logger.info("[%s] confirm-appointment body=%s", rid, body)

        tool_call_id = None
        if "message" in body and "toolCalls" in body["message"]:
            tc           = body["message"]["toolCalls"][0]
            tool_call_id = tc.get("id")
            args         = tc["function"]["arguments"]
        else:
            args = body

        tebra_appt_id  = args.get("tebra_appointment_id")
        appointment_id = args.get("appointment_id")
        outcome        = (args.get("outcome") or "").lower()
        lead_id        = _sanitize_supabase_lead_id(args.get("lead_id"))
        notes          = args.get("notes")
        reminder_type  = args.get("reminder_type", "24hr")

        if not tebra_appt_id:
            return build_vapi_response(tool_call_id, "Missing tebra_appointment_id.")
        if not appointment_id:
            return build_vapi_response(tool_call_id, "Missing appointment_id.")
        if outcome not in ("confirmed", "cancelled"):
            return build_vapi_response(tool_call_id, "outcome must be 'confirmed' or 'cancelled'.")

        tebra_new_status = "Confirmed" if outcome == "confirmed" else "Cancelled"
        outcome_col      = "reminder_24hr_outcome" if reminder_type == "24hr" else "reminder_2hr_outcome"

        # ── Build Supabase payload ──
        appt_payload: dict = {
            outcome_col:  outcome,
            "status":     outcome,
            "updated_at": datetime.utcnow().isoformat(),
        }
        if notes:
            appt_payload["reminder_notes"] = notes
        if outcome == "cancelled":
            appt_payload["cancelled_at"] = datetime.utcnow().isoformat()

        # ── Helper: Tebra update (Get → Update) ──
        async def _tebra_update() -> str:
            appt_data = await call_tebra_get_appointment(tebra_appt_id, rid)
            if not appt_data:
                return f"Could not fetch appointment {tebra_appt_id} from Tebra."
            appt_data["AppointmentStatus"] = tebra_new_status
            result = await call_tebra_update_appointment(appt_data, rid)
            if result["success"]:
                logger.info("[%s] Tebra updated → %s", rid, tebra_new_status)
                return "ok"
            logger.error("[%s] Tebra update FAILED: %s", rid, result["error"])
            return f"Tebra update failed: {result['error']}"

        # ── Run Tebra + Supabase in parallel ──
        tebra_result, sb_ok = await asyncio.gather(
            _tebra_update(),
            supabase_update_appointment(appointment_id, appt_payload),
        )

        tebra_ok = tebra_result == "ok"
        if tebra_ok and sb_ok:
            msg = f"Appointment {outcome}. Tebra and records updated."
        elif tebra_ok:
            msg = f"Tebra updated to {tebra_new_status} but Supabase update failed — check logs."
        elif sb_ok:
            msg = f"Supabase updated but Tebra update failed: {tebra_result}"
        else:
            msg = f"Both updates failed. Tebra: {tebra_result}"

        logger.info("[%s] confirm-appointment DONE tebra_ok=%s sb_ok=%s outcome=%s",
                    rid, tebra_ok, sb_ok, outcome)

        # ── Optionally update lead ──
        if lead_id and sb_ok:
            lead_payload: dict = {"updated_at": datetime.utcnow().isoformat()}
            if outcome == "cancelled":
                lead_payload["lead_outcome"] = "not_interested"
                lead_payload["queue_status"] = "not_interested"
            elif outcome == "confirmed":
                lead_payload["lead_outcome"] = "booked"
            await supabase_update_lead(lead_id, lead_payload)
            logger.info("[%s] lead updated lead_id=%s", rid, lead_id)

        # ── SMS (confirm/cancel) — send only after Supabase update success ──
        if sb_ok:
            notif_type = "sms_appointment_cancelled" if outcome == "cancelled" else "sms_appointment_confirmed"
            await send_appointment_sms_if_needed(
                rid=rid,
                appointment_id=appointment_id,
                notification_type=notif_type,
                appt={
                    "patient_phone": None,  # will be resolved by Twilio formatter if present; here we rely on Supabase values in webhook, but tool SMS needs phone
                    "patient_name": "",
                    "appointment_date": None,
                    "appointment_time": None,
                    "location": None,
                    "service": None,
                },
                lead_id=lead_id,
            )

        return build_vapi_response(tool_call_id, msg)

    except Exception as e:
        logger.exception("Error in /confirm-appointment: %s", e)
        return build_vapi_response(
            locals().get("tool_call_id"),
            "Sorry, there was an error confirming the appointment. Please try again."
        )


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT: CANCEL APPOINTMENT
# (Tebra → Cancelled, Supabase appointment → cancelled, Lead → not_interested)
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/cancel-appointment",
    summary="Cancel an appointment (Tebra + Supabase + Lead update in parallel)",
    openapi_extra={
        "requestBody": {
            "content": {"application/json": {"schema": inline_schema_refs(CancelAppointmentRequest.model_json_schema())}},
            "required": True,
        }
    },
)
async def cancel_appointment(request: Request):
    """
    Cancels an appointment end-to-end:
      1. Tebra — marks appointment as Cancelled via GetAppointment → UpdateAppointment
      2. Supabase appointments — status='cancelled', cancelled_at=now()
      3. Supabase leads — lead_outcome='not_interested', queue_status='not_interested'

    All three updates run in parallel.
    """
    try:
        rid  = str(uuid4())[:8]
        body = await request.json()
        logger.info("[%s] ======== cancel-appointment START ========", rid)
        logger.info("[%s] cancel-appointment body=%s", rid, body)

        tool_call_id = None
        if "message" in body and "toolCalls" in body["message"]:
            tc           = body["message"]["toolCalls"][0]
            tool_call_id = tc.get("id")
            args         = tc["function"]["arguments"]
        else:
            args = body

        tebra_appt_id  = args.get("tebra_appointment_id")
        appointment_id = args.get("appointment_id")
        lead_id        = _sanitize_supabase_lead_id(args.get("lead_id"))
        notes          = args.get("notes")

        if not tebra_appt_id:
            return build_vapi_response(tool_call_id, "Missing tebra_appointment_id.")
        if not appointment_id:
            return build_vapi_response(tool_call_id, "Missing appointment_id.")

        now_iso = datetime.utcnow().isoformat()

        # ── Supabase appointment payload ──
        appt_payload: dict = {
            "status":       "cancelled",
            "cancelled_at": now_iso,
            "updated_at":   now_iso,
        }
        if notes:
            appt_payload["reminder_notes"] = notes

        # ── Helper: Tebra cancel (Get → Update) ──
        async def _tebra_cancel() -> str:
            appt_data = await call_tebra_get_appointment(tebra_appt_id, rid)
            if not appt_data:
                return f"Could not fetch appointment {tebra_appt_id} from Tebra."
            appt_data["AppointmentStatus"] = "Cancelled"
            result = await call_tebra_update_appointment(appt_data, rid)
            if result["success"]:
                logger.info("[%s] Tebra cancelled appt %s", rid, tebra_appt_id)
                return "ok"
            logger.error("[%s] Tebra cancel FAILED: %s", rid, result["error"])
            return f"Tebra cancel failed: {result['error']}"

        # ── Helper: Lead update ──
        async def _lead_update() -> bool:
            if not lead_id:
                return True
            return await supabase_update_lead(lead_id, {
                "lead_outcome":  "not_interested",
                "queue_status":  "not_interested",
                "updated_at":    now_iso,
            })

        # ── Run Tebra + Supabase appointment + Lead update in parallel ──
        tebra_result, sb_ok, lead_ok = await asyncio.gather(
            _tebra_cancel(),
            supabase_update_appointment(appointment_id, appt_payload),
            _lead_update(),
        )

        tebra_ok = tebra_result == "ok"

        # ── Build response message ──
        parts = []
        if tebra_ok:
            parts.append("Tebra: cancelled")
        else:
            parts.append(f"Tebra: {tebra_result}")
        parts.append(f"Supabase appointment: {'updated' if sb_ok else 'FAILED'}")
        if lead_id:
            parts.append(f"Lead: {'updated to not_interested' if lead_ok else 'FAILED'}")

        if tebra_ok and sb_ok and lead_ok:
            msg = "Appointment cancelled successfully. All records updated."
        else:
            msg = "Appointment cancellation partial. " + " | ".join(parts)

        logger.info("[%s] cancel-appointment DONE tebra=%s sb=%s lead=%s",
                    rid, tebra_ok, sb_ok, lead_ok)

        # ── SMS (cancelled) — send only after Supabase update success ──
        if sb_ok:
            await send_appointment_sms_if_needed(
                rid=rid,
                appointment_id=appointment_id,
                notification_type="sms_appointment_cancelled",
                appt={},
                lead_id=lead_id,
            )

        return build_vapi_response(tool_call_id, msg)

    except Exception as e:
        logger.exception("Error in /cancel-appointment: %s", e)
        return build_vapi_response(
            locals().get("tool_call_id"),
            "Sorry, there was an error cancelling the appointment. Please try again."
        )


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT: INBOUND LOOKUP APPOINTMENTS (Tebra GetPatients → GetAppointments by PatientID)
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/inbound-lookup-appointments",
    summary="List upcoming appointments (Tebra first, Supabase IDs merged) or lock one row via selected_tebra_appointment_id",
    openapi_extra={
        "requestBody": {
            "content": {"application/json": {"schema": inline_schema_refs(InboundLookupAppointmentsRequest.model_json_schema())}},
            "required": True,
        }
    },
)
async def inbound_lookup_appointments(request: Request):
    """
    Inbound: Tebra first — GetPatients (first + last) → GetAppointments by PatientID for the Pacific
    date window. Supabase second — each Tebra row is looked up by tebra_appointment_id to attach
    appointment_id when the booking exists in our DB (form/outbound path).
    """
    try:
        rid = str(uuid4())[:8]
        body = await request.json()
        logger.info("[%s] ======== inbound-lookup-appointments START ========", rid)
        logger.info("[%s] inbound-lookup-appointments body=%s", rid, body)

        tool_call_id = None
        if "message" in body and "toolCalls" in body["message"]:
            tc = body["message"]["toolCalls"][0]
            tool_call_id = tc.get("id")
            args = coerce_vapi_tool_arguments((tc.get("function") or {}).get("arguments"))
        else:
            args = body if isinstance(body, dict) else {}

        logger.info(
            "[%s] inbound-lookup-appointments parsed tool_call_id=%s arg_keys=%s",
            rid,
            tool_call_id,
            list(args.keys()) if isinstance(args, dict) else None,
        )

        name = args.get("patient_full_name") or args.get("name") or args.get("patient_name")
        selected_raw = args.get("selected_tebra_appointment_id")

        if not name or not str(name).strip():
            return _inbound_lookup_vapi_response(rid, tool_call_id, "Missing required field: patient_full_name.")

        name_clean = str(name).strip()
        split = _split_patient_first_last(name_clean)
        if not split:
            return _inbound_lookup_vapi_response(
                rid,
                tool_call_id,
                "I need both first and last name (e.g. Jane Doe). Ask the caller to spell both.",
            )
        first_name, last_name = split

        tz_override = args.get("timezone_offset_from_gmt")
        tz_int = None
        if tz_override is not None and str(tz_override).strip() != "":
            try:
                tz_int = int(tz_override)
            except (TypeError, ValueError):
                return _inbound_lookup_vapi_response(
                    rid,
                    tool_call_id,
                    f"timezone_offset_from_gmt must be an integer. You said '{tz_override}'.",
                )

        start_d, end_d = _inbound_lookup_date_range_la()
        now_la = datetime.now(ZoneInfo("America/Los_Angeles"))
        logger.info("[%s] inbound-lookup window_la=%s..%s name=%r", rid, start_d, end_d, name_clean)

        patient_id = await get_patient_by_name(first_name, last_name, rid=rid)
        if not patient_id:
            return _inbound_lookup_vapi_response(
                rid,
                tool_call_id,
                "No patient found in Tebra with that exact first and last name. "
                "Ask the caller to spell both names again.",
            )
        logger.info("[%s] inbound-lookup GetPatients ok patient_id=%s", rid, patient_id)

        tebra_result = await call_tebra_get_appointments_by_patient_id(
            patient_id=patient_id,
            start_date=start_d,
            end_date=end_d,
            timezone_offset_from_gmt=tz_int,
            rid=rid,
        )
        if not tebra_result.get("ok"):
            err = tebra_result.get("error_message") or "Tebra lookup failed."
            return _inbound_lookup_vapi_response(rid, tool_call_id, f"Could not look up appointments in Tebra: {err}")

        raw_appts = list(tebra_result.get("appointments") or [])
        filtered: list[dict] = []
        for a in raw_appts:
            if not _keep_inbound_confirmation_status(a.get("confirmation_status")):
                continue
            if not _is_upcoming_in_la(a, now_la):
                continue
            filtered.append(a)

        logger.info(
            "[%s] inbound-lookup tebra_rows raw=%s after_status_time_filter=%s",
            rid,
            len(raw_appts),
            len(filtered),
        )

        appts = _sort_inbound_appointments(filtered)
        await _attach_supabase_ids(appts, rid)
        logger.info(
            "[%s] inbound-lookup final_appts=%s ids=%s",
            rid,
            len(appts),
            [a.get("tebra_appointment_id") for a in appts],
        )

        selected = str(selected_raw).strip() if selected_raw is not None and str(selected_raw).strip() else None
        if selected:
            chosen = None
            for a in appts:
                if str(a.get("tebra_appointment_id") or "") == selected:
                    chosen = a
                    break
            if not chosen:
                return _inbound_lookup_vapi_response(
                    rid,
                    tool_call_id,
                    "That tebra_appointment_id is not in this patient's upcoming list. "
                    "Re-run listing without selected_tebra_appointment_id or pick a valid option.",
                )
            return _inbound_lookup_vapi_response(rid, tool_call_id, _format_inbound_locked_row(chosen))

        if len(appts) == 1:
            return _inbound_lookup_vapi_response(rid, tool_call_id, _format_inbound_locked_row(appts[0]))

        msg = _format_inbound_appointment_list(appts, TEBRA_INBOUND_APPOINTMENTS_WINDOW_DAYS)
        return _inbound_lookup_vapi_response(rid, tool_call_id, msg)
    except Exception as e:
        logger.exception("Error in /inbound-lookup-appointments: %s", e)
        return _inbound_lookup_vapi_response(
            rid,
            locals().get("tool_call_id"),
            "Sorry, there was an error looking up appointments. Please try again.",
        )
