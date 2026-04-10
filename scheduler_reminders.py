# ─────────────────────────────────────────────────────────────────────────────
# Rausch PT — Appointment Reminder Scheduler (Docker service)
#
# Runs forever as its own container.
# - 24hr reminders for appointments tomorrow (LA), only during office hours
# - 2hr reminders for appointments today (LA) in a rolling time window
# - Never call if appointment starts in < 60 minutes
# - Writes logs to /code/logs/scheduler_reminders.log
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from dotenv import load_dotenv
from zoneinfo import ZoneInfo

load_dotenv()

LA_TZ = ZoneInfo("America/Los_Angeles")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_API_KEY = os.getenv("SUPABASE_API_KEY")
VAPI_API_KEY = os.getenv("VAPI_API_KEY")
VAPI_REMINDER_ASSISTANT_ID = os.getenv("VAPI_REMINDER_ASSISTANT_ID")
VAPI_PHONE_NUMBER_ID = os.getenv("VAPI_PHONE_NUMBER_ID")

# Office hours in LA time
OFFICE_START_HOUR = int(os.getenv("REMINDER_OFFICE_START_HOUR", "8"))   # inclusive
OFFICE_END_HOUR = int(os.getenv("REMINDER_OFFICE_END_HOUR", "17"))      # exclusive

# Never call if appointment begins too soon
MIN_LEAD_TIME_MINUTES = int(os.getenv("REMINDER_MIN_LEAD_TIME_MINUTES", "60"))

# Caps to avoid hitting Vapi concurrency
MAX_CALLS_PER_RUN_24HR = int(os.getenv("REMINDER_24HR_MAX_CALLS_PER_RUN", "3"))
MAX_CALLS_PER_RUN_2HR = int(os.getenv("REMINDER_2HR_MAX_CALLS_PER_RUN", "3"))

# Fetch size (cap calls separately)
BATCH_SIZE = int(os.getenv("REMINDER_BATCH_SIZE", "30"))


def _setup_logger() -> logging.Logger:
    os.makedirs("logs", exist_ok=True)
    logger = logging.getLogger("scheduler_reminders")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    fh = logging.FileHandler("logs/scheduler_reminders.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


logger = _setup_logger()


SUPABASE_HEADERS = {
    "apikey": SUPABASE_API_KEY,
    "Authorization": f"Bearer {SUPABASE_API_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

VAPI_HEADERS = {"Authorization": f"Bearer {VAPI_API_KEY}", "Content-Type": "application/json"}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now_utc().isoformat()


def now_local() -> datetime:
    return datetime.now(LA_TZ)


def in_office_hours(dt_local: datetime) -> bool:
    h = dt_local.hour
    return OFFICE_START_HOUR <= h < OFFICE_END_HOUR


def today_date() -> str:
    return now_local().strftime("%Y-%m-%d")


def tomorrow_date() -> str:
    return (now_local() + timedelta(days=1)).strftime("%Y-%m-%d")


def format_phone(phone: str) -> str | None:
    """
    Normalize to E.164. Handles inputs like '+1 (949) 123-4567' -> '+19491234567'
    """
    if not phone:
        return None
    cleaned = re.sub(r"[^\d+]", "", str(phone).strip())
    digits_only = cleaned.replace("+", "")
    if len(digits_only) < 10 or len(digits_only) > 15:
        return None
    if cleaned.startswith("+"):
        if len(digits_only) < 11:
            return None
        return cleaned
    if len(digits_only) == 10:
        return "+1" + digits_only
    if len(digits_only) == 11 and digits_only.startswith("1"):
        return "+" + digits_only
    return "+" + digits_only


def _parse_hm(raw_time: str) -> tuple[int, int] | None:
    if not raw_time:
        return None
    s = str(raw_time).strip()
    if not s:
        return None
    parts = s.split(":")
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), int(parts[1])
    except Exception:
        return None


def _appt_dt_local(appointment_date: str, appointment_time: str) -> datetime | None:
    """
    appointment_date: YYYY-MM-DD in LA local
    appointment_time: HH:MM in LA local
    """
    hm = _parse_hm(appointment_time)
    if not hm or not appointment_date:
        return None
    try:
        y, m, d = (int(x) for x in str(appointment_date).split("-"))
        hh, mm = hm
        return datetime(y, m, d, hh, mm, 0, tzinfo=LA_TZ)
    except Exception:
        return None


async def supabase_get(path: str) -> list:
    url = f"{SUPABASE_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(url, headers=SUPABASE_HEADERS)
            if r.status_code == 200:
                data = r.json()
                return data if isinstance(data, list) else []
            logger.warning("supabase_get status=%s body=%s", r.status_code, (r.text or "")[:300])
            return []
    except Exception as e:
        logger.exception("supabase_get exception: %s", e)
        return []


async def supabase_patch(path: str, data: dict) -> bool:
    url = f"{SUPABASE_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.patch(url, headers=SUPABASE_HEADERS, json=data)
            if r.status_code in (200, 204):
                return True
            logger.warning("supabase_patch status=%s body=%s", r.status_code, (r.text or "")[:300])
            return False
    except Exception as e:
        logger.exception("supabase_patch exception: %s", e)
        return False


async def trigger_vapi_call(assistant_id: str, phone: str, variable_values: dict) -> dict | None:
    e164 = format_phone(phone)
    if not e164:
        logger.info("trigger_vapi_call SKIP invalid phone=%r", phone)
        return None

    payload = {
        "assistantId": assistant_id,
        "phoneNumberId": VAPI_PHONE_NUMBER_ID,
        "customer": {"number": e164},
        "assistantOverrides": {"variableValues": variable_values},
    }

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post("https://api.vapi.ai/call/phone", headers=VAPI_HEADERS, json=payload)
            if r.status_code == 201:
                call_obj = r.json()
                logger.info("trigger_vapi_call SUCCESS vapi_call_id=%s phone=%s", call_obj.get("id"), e164)
                return call_obj
            logger.warning("trigger_vapi_call FAILED status=%s phone=%s body=%s", r.status_code, e164, (r.text or "")[:400])
            return None
    except Exception as e:
        logger.exception("trigger_vapi_call exception phone=%s: %s", e164, e)
        return None


async def job_reminder_24hr():
    if not in_office_hours(now_local()):
        logger.info("job_reminder_24hr skip (outside office hours %02d-%02d LA)", OFFICE_START_HOUR, OFFICE_END_HOUR)
        return

    logger.info("── job_reminder_24hr START tomorrow=%s ──", tomorrow_date())

    appointments = await supabase_get(
        "/rest/v1/appointments"
        "?reminder_sent_24hr=eq.false"
        "&status=eq.scheduled"
        f"&appointment_date=eq.{tomorrow_date()}"
        "&select=id,lead_id,tebra_appointment_id,tebra_patient_id,patient_name,patient_phone,"
        "appointment_date,appointment_time,location,service,reminder_sent_2hr"
        f"&limit={BATCH_SIZE}"
    )

    if not appointments:
        logger.info("No 24hr reminders to send.")
        return

    calls_started = 0
    now_l = now_local()
    for appt in appointments:
        if calls_started >= MAX_CALLS_PER_RUN_24HR:
            break

        appt_id = appt.get("id")
        phone = appt.get("patient_phone") or ""
        if not appt_id or not phone:
            continue

        appt_dt = _appt_dt_local(appt.get("appointment_date"), appt.get("appointment_time"))
        if not appt_dt:
            continue

        minutes_until = int((appt_dt - now_l).total_seconds() // 60)
        if minutes_until < MIN_LEAD_TIME_MINUTES:
            logger.info("24hr SKIP appt_id=%s starts_in=%sm (<%sm)", appt_id, minutes_until, MIN_LEAD_TIME_MINUTES)
            continue

        # Extra safety: if a 2hr reminder already went out, don't send 24hr too.
        if appt.get("reminder_sent_2hr") is True:
            logger.info("24hr SKIP appt_id=%s (2hr already sent)", appt_id)
            continue

        marked = await supabase_patch(f"/rest/v1/appointments?id=eq.{appt_id}", {"reminder_sent_24hr": True, "updated_at": now_iso()})
        if not marked:
            continue

        call_obj = await trigger_vapi_call(
            assistant_id=VAPI_REMINDER_ASSISTANT_ID,
            phone=phone,
            variable_values={
                "lead_id": appt.get("lead_id"),
                "appointment_id": appt_id,
                "tebra_appointment_id": appt.get("tebra_appointment_id"),
                "tebra_patient_id": appt.get("tebra_patient_id"),
                "patient_name": appt.get("patient_name") or "",
                "patient_phone": phone,
                "appointment_date": appt.get("appointment_date"),
                "appointment_time": appt.get("appointment_time"),
                "location": appt.get("location"),
                "service": appt.get("service"),
                "reminder_type": "24hr",
            },
        )

        if call_obj is None:
            # revert so we can try later
            await supabase_patch(f"/rest/v1/appointments?id=eq.{appt_id}", {"reminder_sent_24hr": False, "updated_at": now_iso()})
        else:
            calls_started += 1

        await asyncio.sleep(1)

    logger.info("── job_reminder_24hr DONE started_calls=%s ──", calls_started)


async def job_reminder_2hr():
    if not in_office_hours(now_local()):
        logger.info("job_reminder_2hr skip (outside office hours %02d-%02d LA)", OFFICE_START_HOUR, OFFICE_END_HOUR)
        return

    logger.info("── job_reminder_2hr START today=%s ──", today_date())

    appointments = await supabase_get(
        "/rest/v1/appointments"
        "?reminder_sent_2hr=eq.false"
        "&status=in.(scheduled,confirmed)"
        f"&appointment_date=eq.{today_date()}"
        "&select=id,lead_id,tebra_appointment_id,tebra_patient_id,patient_name,patient_phone,"
        "appointment_date,appointment_time,location,service,reminder_sent_24hr"
        f"&limit={BATCH_SIZE}"
    )

    if not appointments:
        logger.info("No 2hr candidates fetched.")
        return

    now_l = now_local()
    window_start = now_l + timedelta(hours=1, minutes=30)
    window_end = now_l + timedelta(hours=2, minutes=30)

    # Eligible appointments in the 2hr-ish window
    candidates: list[dict] = []
    for appt in appointments:
        appt_dt = _appt_dt_local(appt.get("appointment_date"), appt.get("appointment_time"))
        if not appt_dt:
            continue
        if not (window_start <= appt_dt <= window_end):
            continue
        minutes_until = int((appt_dt - now_l).total_seconds() // 60)
        if minutes_until < MIN_LEAD_TIME_MINUTES:
            continue
        # Extra safety: if 24hr already sent, you can still send 2hr; if you want only one, skip here.
        candidates.append(appt)

    if not candidates:
        logger.info("No appointments in 2hr window.")
        return

    calls_started = 0
    for appt in candidates:
        if calls_started >= MAX_CALLS_PER_RUN_2HR:
            break

        appt_id = appt.get("id")
        phone = appt.get("patient_phone") or ""
        if not appt_id or not phone:
            continue

        marked = await supabase_patch(f"/rest/v1/appointments?id=eq.{appt_id}", {"reminder_sent_2hr": True, "updated_at": now_iso()})
        if not marked:
            continue

        call_obj = await trigger_vapi_call(
            assistant_id=VAPI_REMINDER_ASSISTANT_ID,
            phone=phone,
            variable_values={
                "lead_id": appt.get("lead_id"),
                "appointment_id": appt_id,
                "tebra_appointment_id": appt.get("tebra_appointment_id"),
                "tebra_patient_id": appt.get("tebra_patient_id"),
                "patient_name": appt.get("patient_name") or "",
                "patient_phone": phone,
                "appointment_date": appt.get("appointment_date"),
                "appointment_time": appt.get("appointment_time"),
                "location": appt.get("location"),
                "service": appt.get("service"),
                "reminder_type": "2hr",
            },
        )

        if call_obj is None:
            await supabase_patch(f"/rest/v1/appointments?id=eq.{appt_id}", {"reminder_sent_2hr": False, "updated_at": now_iso()})
        else:
            calls_started += 1

        await asyncio.sleep(1)

    logger.info("── job_reminder_2hr DONE started_calls=%s ──", calls_started)


async def main():
    missing = [k for k in ("SUPABASE_URL", "SUPABASE_API_KEY", "VAPI_API_KEY", "VAPI_REMINDER_ASSISTANT_ID", "VAPI_PHONE_NUMBER_ID") if not os.getenv(k)]
    if missing:
        logger.error("STARTUP ERROR missing env vars: %s", missing)
        return

    logger.info(
        "[scheduler_reminders] starting office_hours=%02d-%02d LA min_lead=%sm",
        OFFICE_START_HOUR,
        OFFICE_END_HOUR,
        MIN_LEAD_TIME_MINUTES,
    )

    scheduler = AsyncIOScheduler(timezone="UTC")
    # Staggered cron schedules (UTC):
    # - 24hr: minute 5 every hour
    # - 2hr : minute 35 every hour and half-hour (i.e. 05/35 pattern; here we choose 35 only; window is wide enough)
    scheduler.add_job(job_reminder_24hr, "cron", minute="5", id="reminder_24hr")
    scheduler.add_job(job_reminder_2hr, "cron", minute="35", id="reminder_2hr")
    scheduler.start()

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        scheduler.shutdown()
        logger.info("[scheduler_reminders] stopped")


if __name__ == "__main__":
    asyncio.run(main())

