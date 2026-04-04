import re
import asyncio
import httpx
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Request

from app.core.config import (
    resolve_location,
    resolve_appointment_reason_id,
    SUPABASE_URL,
    SUPABASE_HEADERS,
    SUPABASE_PRACTICE_ID,
    LOCATION_MAP,
    TEBRA_VALID_NAMES,
)
from app.core.logger import logger
from app.services.tebra_service import (
    call_tebra_get_appointments,
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
from app.utils.time_utils import (
    parse_time_to_24hr,
    format_12hr,
    parse_booked_slots,
    get_available_slots,
    get_nearest_available_slots,
)
from app.utils.parser import build_vapi_response

router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT: CREATE APPOINTMENT
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/create-appointment")
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

        # ── Step 1: Find or create patient ──
        logger.info("[%s] STEP 1 → GetPatients (looking up %s %s)", rid, first_name, last_name)
        patient_id = await get_patient_by_name(first_name, last_name, rid)
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

        # ── Step 1c: Server-side slot guard ──
        logger.info("[%s] STEP 1c → Slot pre-check date=%s time=%s location=%s", rid, date, time_str, tebra_name)
        pre_xml = await call_tebra_get_appointments(date, tebra_name)
        if "<IsError>true</IsError>" not in pre_xml:
            booked_now = parse_booked_slots(pre_xml, date)
            if (parsed_time[0], parsed_time[1]) in booked_now:
                available_now = get_available_slots(booked_now)
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

@router.post("/update-appointment-status")
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

        valid_statuses = {
            "Confirmed", "Cancelled", "NoShow", "Rescheduled",
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

@router.post("/reschedule-appointment")
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
        lead_id        = args.get("lead_id")

        # ── Validate required fields ──
        missing = [f for f, v in {
            "tebra_appointment_id": tebra_appt_id,
            "appointment_id":       appointment_id,
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

        # ── Validate time format ──
        parsed_time = parse_time_to_24hr(new_time)
        if not parsed_time:
            return build_vapi_response(
                tool_call_id,
                f"I need the time in HH:MM format or like '9 AM'. You said '{new_time}'."
            )

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
                    available_new = get_available_slots(booked_new)
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
        time_normalized = f"{parsed_time[0]:02d}:{parsed_time[1]:02d}"

        # ── Step 4: Update Supabase — mark old appointment as rescheduled ──
        await supabase_update_appointment(appointment_id, {
            "status":     "rescheduled",
            "updated_at": datetime.utcnow().isoformat(),
        })

        # ── Step 5: Insert new appointment into Supabase ──
        old_sb_appt = None
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

        new_appt_data = {
            "tebra_appointment_id":      new_tebra_id,
            "tebra_patient_id":          patient_id,
            "tebra_service_location_id": location_id,
            "tebra_reason_id":           reason_id,
            "appointment_date":          new_date,
            "appointment_time":          time_normalized,
            "status":                    "scheduled",
            "practice_id":               SUPABASE_PRACTICE_ID,
            "rescheduled_from_id":       appointment_id,
            "service":                   new_service or (old_sb_appt.get("service") if old_sb_appt else None),
            "location":                  new_location or (old_sb_appt.get("location") if old_sb_appt else None),
            "patient_name":              old_sb_appt.get("patient_name") if old_sb_appt else None,
            "patient_phone":             old_sb_appt.get("patient_phone") if old_sb_appt else None,
        }
        # Guard against unresolved VAPI template variables like '{{lead_id}}'
        if lead_id and not lead_id.startswith("{{"):
            new_appt_data["lead_id"] = lead_id
        elif old_sb_appt and old_sb_appt.get("lead_id"):
            new_appt_data["lead_id"] = old_sb_appt["lead_id"]

        # Remove None values
        new_appt_data = {k: v for k, v in new_appt_data.items() if v is not None}

        new_sb_appt = await supabase_insert_appointment(new_appt_data)
        new_sb_id   = new_sb_appt.get("id") if new_sb_appt else None

        logger.info("[%s] reschedule complete old_tebra=%s → new_tebra=%s new_supabase=%s",
                    rid, tebra_appt_id, new_tebra_id, new_sb_id)

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

@router.post("/confirm-appointment")
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
        lead_id        = args.get("lead_id")
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

        return build_vapi_response(tool_call_id, msg)

    except Exception as e:
        logger.exception("Error in /confirm-appointment: %s", e)
        return build_vapi_response(
            locals().get("tool_call_id"),
            "Sorry, there was an error confirming the appointment. Please try again."
        )
