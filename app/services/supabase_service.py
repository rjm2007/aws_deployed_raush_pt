import httpx
from datetime import datetime

from app.core.config import SUPABASE_URL, SUPABASE_HEADERS, SUPABASE_PRACTICE_ID
from app.core.logger import logger


async def supabase_insert_appointment(data: dict) -> dict | None:
    """Insert a row into the appointments table. Returns inserted row or None."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{SUPABASE_URL}/rest/v1/appointments",
                headers=SUPABASE_HEADERS,
                json=data,
            )
            if r.status_code in (200, 201):
                rows = r.json()
                return rows[0] if rows else None
            logger.error("[supabase] insert_appointment failed status=%s body=%s",
                         r.status_code, r.text)
            return None
    except Exception as e:
        logger.error("[supabase] insert_appointment exception: %s", e)
        return None


async def supabase_update_lead(lead_id: str, data: dict) -> bool:
    """Update a leads row by id. Returns True on success."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.patch(
                f"{SUPABASE_URL}/rest/v1/leads?id=eq.{lead_id}",
                headers=SUPABASE_HEADERS,
                json=data,
            )
            if r.status_code in (200, 204):
                return True
            logger.error("[supabase] update_lead failed status=%s body=%s",
                         r.status_code, r.text)
            return False
    except Exception as e:
        logger.error("[supabase] update_lead exception: %s", e)
        return False


async def supabase_update_appointment(appt_id: str, data: dict) -> bool:
    """Update an appointments row by id. Returns True on success."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.patch(
                f"{SUPABASE_URL}/rest/v1/appointments?id=eq.{appt_id}",
                headers=SUPABASE_HEADERS,
                json=data,
            )
            if r.status_code in (200, 204):
                return True
            logger.error("[supabase] update_appointment failed status=%s body=%s",
                         r.status_code, r.text)
            return False
    except Exception as e:
        logger.error("[supabase] update_appointment exception: %s", e)
        return False


# Valid call_logs outcome values (CHECK constraint in Supabase)
_CALL_LOG_OUTCOME_MAP = {
    "booked":                    "booked",
    "not_interested":            "not_interested",
    "callback_scheduled":        "callback_requested",
    "callback_requested":        "callback_requested",
    "no_answer":                 "no_answer",
    "rescheduled":               "rescheduled",
    "cancelled":                 "cancelled",
    "confirmed":                 "confirmed",
    "voicemail":                 "voicemail",
    "manual":                    "info_given",
    "scraped":                   "info_given",
    "customer-ended-call":       "info_given",
    "customer-did-not-answer":   "no_answer",
    "assistant-ended-call":      "info_given",
    "in_progress":               "info_given",
    "new":                       "info_given",
}


async def _insert_call_log(
    rid: str,
    lead_id: str,
    vapi_call_id: str | None,
    call_status: str | None,
    duration_seconds: int | None,
    call_type: str | None,
    call_direction: str = "outbound",
    outcome: str | None = None,
    notes: str | None = None,
    appointment_id: str | None = None,
) -> bool:
    """Insert a row into call_logs. Returns True on success."""
    try:
        if outcome:
            outcome = _CALL_LOG_OUTCOME_MAP.get(outcome, "info_given")

        data = {
            "lead_id":          lead_id,
            "vapi_call_id":     vapi_call_id,
            "call_status":      call_status,
            "duration_seconds": duration_seconds,
            "call_type":        call_type,
            "call_direction":   call_direction,
            "outcome":          outcome,
            "notes":            notes,
            "practice_id":      SUPABASE_PRACTICE_ID,
        }
        if appointment_id:
            data["appointment_id"] = appointment_id

        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{SUPABASE_URL}/rest/v1/call_logs",
                headers=SUPABASE_HEADERS,
                json=data,
            )
            if r.status_code in (200, 201):
                rows   = r.json()
                log_id = rows[0].get("id") if rows else "?"
                logger.info("[%s] call_log INSERT success id=%s vapi=%s", rid, log_id, vapi_call_id)
                return True
            logger.error("[%s] call_log INSERT failed status=%s body=%s",
                         rid, r.status_code, r.text[:300])
            return False
    except Exception as e:
        logger.error("[%s] call_log INSERT exception: %s", rid, e)
        return False


async def supabase_insert_scheduled_callback(data: dict) -> dict | None:
    """Insert a row into scheduled_callbacks. Returns inserted row or None."""
    try:
        data.setdefault("practice_id", SUPABASE_PRACTICE_ID)
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{SUPABASE_URL}/rest/v1/scheduled_callbacks",
                headers=SUPABASE_HEADERS,
                json=data,
            )
            if r.status_code in (200, 201):
                rows = r.json()
                return rows[0] if rows else None
            logger.error("[supabase] insert_scheduled_callback failed status=%s body=%s",
                         r.status_code, r.text)
            return None
    except Exception as e:
        logger.error("[supabase] insert_scheduled_callback exception: %s", e)
        return None


async def supabase_insert_notification_log(data: dict) -> dict | None:
    """Insert a row into notification_log. Returns inserted row or None."""
    try:
        data.setdefault("practice_id", SUPABASE_PRACTICE_ID)
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{SUPABASE_URL}/rest/v1/notification_log",
                headers=SUPABASE_HEADERS,
                json=data,
            )
            if r.status_code in (200, 201):
                rows = r.json()
                return rows[0] if rows else None
            logger.error("[supabase] insert_notification_log failed status=%s body=%s",
                         r.status_code, r.text)
            return None
    except Exception as e:
        logger.error("[supabase] insert_notification_log exception: %s", e)
        return None
