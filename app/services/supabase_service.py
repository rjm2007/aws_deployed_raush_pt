import httpx
from datetime import datetime

from app.core.config import SUPABASE_URL, SUPABASE_HEADERS, SUPABASE_PRACTICE_ID
from app.core.logger import logger


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


def _normalize_person_name_key(raw: str | None) -> str:
    """Strict name normalization only (no fuzzy)."""
    if not raw:
        return ""
    return " ".join(str(raw).strip().lower().split())


def _sb_row_time_hm(raw) -> tuple[int, int] | None:
    if raw is None:
        return None
    ts = str(raw).strip()
    if not ts:
        return None
    seg = ts.split(":")
    if len(seg) < 2:
        return None
    try:
        return int(seg[0]), int(seg[1])
    except ValueError:
        return None


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


async def supabase_fetch_appointments_by_patient_date_time(
    patient_name: str,
    appointment_date: str,
    time_hour: int,
    time_minute: int,
) -> list[dict]:
    """
    Fetch appointments on a single calendar day, then keep rows where patient_name matches exactly
    (case-insensitive, whitespace-normalized) AND clock time matches.
    """
    if not patient_name or not appointment_date:
        return []
    name_key = _normalize_person_name_key(patient_name)
    if not name_key:
        return []

    params = [
        ("select", "id,tebra_appointment_id,patient_name,patient_phone,appointment_date,appointment_time,location,service,status"),
        ("appointment_date", f"eq.{appointment_date}"),
        ("tebra_appointment_id", "not.is.null"),
        ("or", f"(practice_id.eq.{SUPABASE_PRACTICE_ID},practice_id.is.null)"),
        ("order", "appointment_time.asc"),
    ]

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/appointments",
                headers=SUPABASE_HEADERS,
                params=params,
            )
            if r.status_code != 200:
                logger.error(
                    "[supabase] fetch_appointments_by_patient_date_time failed status=%s body=%s",
                    r.status_code,
                    (r.text or "")[:500],
                )
                return []
            rows = r.json() or []
            if not isinstance(rows, list):
                return []
    except Exception as e:
        logger.error("[supabase] fetch_appointments_by_patient_date_time exception: %s", e)
        return []

    out: list[dict] = []
    for row in rows:
        row_name_key = _normalize_person_name_key(row.get("patient_name") or "")
        if row_name_key != name_key:
            continue
        hm = _sb_row_time_hm(row.get("appointment_time"))
        if not hm or hm[0] != time_hour or hm[1] != time_minute:
            continue
        out.append(row)
    return out


async def supabase_upsert_inbound_call(data: dict) -> tuple[dict | None, str | None]:
    """
    Upsert inbound_calls by call_id.
    Returns (row_or_none, error_code_or_none).

        error_code values:
            - missing_table: inbound_calls relation is not present
            - schema_mismatch: inbound_calls table exists but missing expected columns/constraints
            - write_failed: any other Supabase write failure
    """
    try:
        payload = dict(data)
        payload.setdefault("practice_id", SUPABASE_PRACTICE_ID)
        payload.setdefault("updated_at", datetime.utcnow().isoformat())

        headers = dict(SUPABASE_HEADERS)
        headers["Prefer"] = "resolution=merge-duplicates,return=representation"

        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(
                f"{SUPABASE_URL}/rest/v1/inbound_calls?on_conflict=call_id",
                headers=headers,
                json=payload,
            )
            if r.status_code in (200, 201):
                rows = r.json()
                return (rows[0] if rows else None), None

            body = r.text or ""
            body_l = body.lower()
            if "inbound_calls" in body_l and (
                "does not exist" in body_l
                or "relation" in body_l
                or "could not find the table" in body_l
            ):
                logger.error("[supabase] inbound_calls table missing. status=%s body=%s",
                             r.status_code, body[:300])
                return None, "missing_table"

            if "inbound_calls" in body_l and (
                "column" in body_l
                or "constraint" in body_l
                or "invalid input syntax" in body_l
            ):
                logger.error("[supabase] inbound_calls schema mismatch. status=%s body=%s",
                             r.status_code, body[:300])
                return None, "schema_mismatch"

            logger.error("[supabase] upsert_inbound_call failed status=%s body=%s",
                         r.status_code, body[:300])
            return None, "write_failed"
    except Exception as e:
        logger.error("[supabase] upsert_inbound_call exception: %s", e)
        return None, "write_failed"


async def supabase_fetch_inbound_call_by_call_id(call_id: str) -> dict | None:
    """Fetch one inbound_calls row by call_id."""
    if not call_id:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/inbound_calls?call_id=eq.{call_id}"
                "&select=id,call_id,crm_status,caller_name,caller_number,appointment_id,location,notes,updated_at",
                headers=SUPABASE_HEADERS,
            )
            if r.status_code == 200:
                rows = r.json()
                return rows[0] if rows else None
            logger.error("[supabase] fetch_inbound_call_by_call_id failed status=%s body=%s",
                         r.status_code, (r.text or "")[:300])
            return None
    except Exception as e:
        logger.error("[supabase] fetch_inbound_call_by_call_id exception: %s", e)
        return None


async def supabase_update_inbound_call_by_id(row_id: str, data: dict) -> bool:
    """PATCH an inbound_calls row by its primary key id. Returns True on success."""
    if not row_id:
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.patch(
                f"{SUPABASE_URL}/rest/v1/inbound_calls?id=eq.{row_id}",
                headers=SUPABASE_HEADERS,
                json=data,
            )
            if r.status_code in (200, 204):
                return True
            logger.error("[supabase] update_inbound_call_by_id failed status=%s body=%s",
                         r.status_code, (r.text or "")[:300])
            return False
    except Exception as e:
        logger.error("[supabase] update_inbound_call_by_id exception: %s", e)
        return False


async def supabase_fetch_latest_inbound_by_caller_number(caller_number: str) -> dict | None:
    """
    Return the most recent complete inbound_calls row for a given caller_number.
    Used to detect a returning caller who wants to reschedule a previously booked appointment.
    """
    caller_number = _normalize_phone_digits(caller_number) or caller_number
    if not caller_number:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/inbound_calls",
                headers=SUPABASE_HEADERS,
                params=[
                    ("select", "id,call_id,crm_status,caller_name,caller_number,appointment_id,route,location,notes,created_at"),
                    ("caller_number", f"eq.{caller_number}"),
                    ("crm_status", "eq.complete"),
                    ("order", "created_at.desc"),
                    ("limit", "1"),
                ],
            )
            if r.status_code == 200:
                rows = r.json()
                return rows[0] if rows else None
            logger.error("[supabase] fetch_latest_inbound_by_caller_number failed status=%s body=%s",
                         r.status_code, (r.text or "")[:300])
            return None
    except Exception as e:
        logger.error("[supabase] fetch_latest_inbound_by_caller_number exception: %s", e)
        return None


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
