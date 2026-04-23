import httpx
from datetime import datetime
from typing import Optional

from app.core.config import SUPABASE_URL, SUPABASE_HEADERS
from app.core.logger import logger
from app.services.supabase_service import supabase_insert_notification_log
from app.services.twilio_service import twilio_send_sms


def build_appointment_sms(notification_type: str, appt: dict) -> str:
    patient_name = appt.get("patient_name") or ""
    patient_phone = appt.get("patient_phone") or ""
    appt_date = appt.get("appointment_date") or ""
    appt_time = appt.get("appointment_time") or ""
    appt_location = appt.get("location") or ""
    appt_service = appt.get("service") or ""

    if notification_type == "sms_appointment_cancelled":
        header = "Your appointment has been cancelled."
    elif notification_type == "sms_appointment_rescheduled":
        header = "Your appointment has been rescheduled."
    elif notification_type == "sms_appointment_confirmed":
        header = "Your appointment has been confirmed."
    else:
        # sms_appointment_booked / sms_appointment_confirmation
        header = "Your appointment has been booked."

    return (
        "RAUSCH PHYSICAL THERAPY & WELLNESS\n"
        f"{header}\n\n"
        f"Name: {patient_name}\n"
        f"Location: {appt_location}\n"
        f"Date: {appt_date}\n"
        f"Time: {appt_time}\n"
        f"Phone: {patient_phone}\n"
        f"Service: {appt_service}"
    ).strip()


async def _already_sent(appointment_id: str, notification_type: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/notification_log"
                f"?appointment_id=eq.{appointment_id}"
                f"&notification_type=eq.{notification_type}"
                f"&channel=eq.sms"
                f"&status=eq.sent"
                f"&select=id&limit=1",
                headers=SUPABASE_HEADERS,
            )
            return r.status_code == 200 and bool(r.json() or [])
    except Exception as e:
        logger.error("[sms] dedupe check failed appt_id=%s type=%s err=%s", appointment_id, notification_type, e)
        return False


async def _fetch_appointment(appointment_id: str) -> dict | None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/appointments?id=eq.{appointment_id}"
                f"&select=id,patient_name,patient_phone,appointment_date,appointment_time,location,service,status,updated_at",
                headers=SUPABASE_HEADERS,
            )
            if r.status_code == 200 and (r.json() or []):
                return (r.json() or [None])[0]
    except Exception as e:
        logger.error("[sms] fetch appointment failed appt_id=%s err=%s", appointment_id, e)
    return None


async def send_appointment_sms_if_needed(
    *,
    rid: str,
    appointment_id: str,
    notification_type: str,
    appt: dict,
    lead_id: Optional[str] = None,
    vapi_call_id: Optional[str] = None,
    ended_reason: Optional[str] = None,
) -> bool:
    """
    Idempotent SMS sender (deduped by notification_log appointment_id+type+channel).
    Returns True if SMS was sent, False otherwise (including deduped).
    """
    if not appointment_id or not notification_type:
        return False

    if await _already_sent(appointment_id, notification_type):
        logger.info("[%s] sms dedupe hit type=%s appt_id=%s", rid, notification_type, appointment_id)
        return False

    appt_data = appt or {}
    if not appt_data.get("patient_phone") or not appt_data.get("appointment_date") or not appt_data.get("appointment_time"):
        fetched = await _fetch_appointment(appointment_id)
        if fetched:
            appt_data = {**fetched, **appt_data}  # allow caller overrides

    to_phone = appt_data.get("patient_phone")
    body = build_appointment_sms(notification_type, appt_data)
    ok, sid, err = await twilio_send_sms(to_phone, body)

    await supabase_insert_notification_log({
        "lead_id": lead_id,
        "appointment_id": appointment_id,
        "notification_type": notification_type,
        "channel": "sms",
        "status": "sent" if ok else "failed",
        "vapi_call_id": vapi_call_id,
        "payload": {
            "to": to_phone,
            "twilio_sid": sid,
            "error": err,
            "ended_reason": ended_reason,
        },
        "sent_at": datetime.utcnow().isoformat() if ok else None,
    })

    logger.info("[%s] sms sent attempt type=%s appt_id=%s ok=%s err=%s", rid, notification_type, appointment_id, ok, err)
    return ok

