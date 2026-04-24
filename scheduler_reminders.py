# ─────────────────────────────────────────────────────────────────────────────
# Rausch PT — Appointment Reminder Scheduler (Docker service)
#
# Runs forever as its own container.
# - 24hr reminders for appointments tomorrow (LA), only during office hours
# - 2hr reminders for appointments today (LA) in a rolling time window
# - Never SMS if appointment starts in < 60 minutes
# - Sends Twilio SMS (not VAPI calls) so patient can reply directly to the
#   same number handled by the n8n SMS agent.
# - Writes logs to /code/logs/scheduler_reminders.log
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import base64
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

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")

# Office hours in LA time
OFFICE_START_HOUR = int(os.getenv("REMINDER_OFFICE_START_HOUR", "8"))   # inclusive
OFFICE_END_HOUR = int(os.getenv("REMINDER_OFFICE_END_HOUR", "17"))      # exclusive

# Never call if appointment begins too soon
MIN_LEAD_TIME_MINUTES = int(os.getenv("REMINDER_MIN_LEAD_TIME_MINUTES", "60"))

# Caps to avoid Twilio rate spikes
MAX_CALLS_PER_RUN_24HR = int(os.getenv("REMINDER_24HR_MAX_CALLS_PER_RUN", "10"))
MAX_CALLS_PER_RUN_2HR = int(os.getenv("REMINDER_2HR_MAX_CALLS_PER_RUN", "10"))

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


def _first_name(full_name: str | None) -> str:
    if not full_name or not str(full_name).strip():
        return "there"
    return str(full_name).strip().split()[0]


def _format_time_12hr(raw_time: str | None) -> str:
    hm = _parse_hm(raw_time or "")
    if not hm:
        return str(raw_time or "")
    hh, mm = hm
    suffix = "AM" if hh < 12 else "PM"
    h12 = hh % 12
    if h12 == 0:
        h12 = 12
    if mm == 0:
        return f"{h12}{suffix}"
    return f"{h12}:{mm:02d}{suffix}"


def _format_date_human(appointment_date: str | None) -> str:
    if not appointment_date:
        return ""
    try:
        y, m, d = (int(x) for x in str(appointment_date).split("-"))
        return datetime(y, m, d).strftime("%a, %b %d")
    except Exception:
        return str(appointment_date)


def build_reminder_sms(appt: dict, reminder_type: str) -> str:
    first = _first_name(appt.get("patient_name"))
    service = (appt.get("service") or "appointment").strip() or "appointment"
    location = (appt.get("location") or "").strip()
    date_str = _format_date_human(appt.get("appointment_date"))
    time_str = _format_time_12hr(appt.get("appointment_time"))

    where = f" at our {location} location" if location else ""

    if reminder_type == "2hr":
        lead = (
            f"Hello {first}, this is a courtesy reminder from Rausch Physical Therapy & Wellness "
            f"regarding your {service} scheduled today at {time_str}{where}."
        )
    else:
        when = f"{date_str} at {time_str}" if date_str and time_str else (date_str or time_str)
        lead = (
            f"Hello {first}, this is a courtesy reminder from Rausch Physical Therapy & Wellness "
            f"regarding your {service} scheduled on {when}{where}."
        )

    tail = (
        "Kindly reply:\n"
        "CONFIRM - to confirm your visit\n"
        "RESCHEDULE - to choose a different time\n"
        "CANCEL - if you are unable to attend\n"
        "\n"
        "Thank you."
    )
    return (lead + "\n\n" + tail).strip()


async def twilio_send_sms(to_phone: str, body: str) -> tuple[bool, str | None, str | None]:
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_FROM_NUMBER:
        return False, None, "Twilio not configured"

    e164 = format_phone(to_phone)
    if not e164:
        return False, None, f"Invalid destination phone: {to_phone!r}"

    from_e164 = format_phone(TWILIO_FROM_NUMBER) or TWILIO_FROM_NUMBER
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    auth = base64.b64encode(f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode("utf-8")).decode("utf-8")
    headers = {"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"}
    data = {"To": e164, "From": from_e164, "Body": body}

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(url, headers=headers, data=data)
            if r.status_code in (200, 201):
                sid = (r.json() or {}).get("sid")
                logger.info("reminder_sms SUCCESS sid=%s phone=%s", sid, e164)
                return True, sid, None
            err = f"Twilio status={r.status_code} body={(r.text or '')[:300]}"
            logger.warning("reminder_sms FAILED %s phone=%s", err, e164)
            return False, None, err
    except Exception as e:
        logger.exception("reminder_sms exception phone=%s: %s", e164, e)
        return False, None, f"Twilio exception: {e}"


async def supabase_insert(path: str, row: dict) -> bool:
    url = f"{SUPABASE_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.post(url, headers=SUPABASE_HEADERS, json=row)
            if r.status_code in (200, 201):
                return True
            logger.warning("supabase_insert status=%s body=%s", r.status_code, (r.text or "")[:300])
            return False
    except Exception as e:
        logger.exception("supabase_insert exception: %s", e)
        return False


async def send_reminder_sms(appt: dict, reminder_type: str) -> dict | None:
    """
    Send the reminder SMS via Twilio, persist it to sms_conversations, and log
    a notification_log row. Returns a truthy dict on success, None on failure.
    """
    phone = appt.get("patient_phone") or ""
    e164 = format_phone(phone)
    if not e164:
        logger.info("send_reminder_sms SKIP invalid phone=%r", phone)
        return None

    body = build_reminder_sms(appt, reminder_type)
    ok, sid, err = await twilio_send_sms(e164, body)

    await supabase_insert(
        "/rest/v1/notification_log",
        {
            "lead_id": appt.get("lead_id"),
            "appointment_id": appt.get("id"),
            "notification_type": f"reminder_{reminder_type}_sms",
            "channel": "sms",
            "status": "sent" if ok else "failed",
            "vapi_call_id": None,
            "payload": {"to": e164, "twilio_sid": sid, "error": err, "body": body},
            "sent_at": now_iso() if ok else None,
        },
    )

    if ok:
        await supabase_insert(
            "/rest/v1/sms_conversations",
            {
                "phone_number": e164,
                "lead_id": appt.get("lead_id"),
                "appointment_id": appt.get("id"),
                "practice_id": None,
                "role": "assistant",
                "message": body,
                "direction": "outbound",
                "intent": f"reminder_{reminder_type}",
                "twilio_sid": sid,
            },
        )
        return {"sid": sid}
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

        result = await send_reminder_sms(appt, "24hr")

        if result is None:
            # revert so we can try later
            await supabase_patch(f"/rest/v1/appointments?id=eq.{appt_id}", {"reminder_sent_24hr": False, "updated_at": now_iso()})
        else:
            calls_started += 1

        await asyncio.sleep(1)

    logger.info("── job_reminder_24hr DONE sent_sms=%s ──", calls_started)


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

        result = await send_reminder_sms(appt, "2hr")

        if result is None:
            await supabase_patch(f"/rest/v1/appointments?id=eq.{appt_id}", {"reminder_sent_2hr": False, "updated_at": now_iso()})
        else:
            calls_started += 1

        await asyncio.sleep(1)

    logger.info("── job_reminder_2hr DONE sent_sms=%s ──", calls_started)


async def main():
    missing = [
        k
        for k in (
            "SUPABASE_URL",
            "SUPABASE_API_KEY",
            "TWILIO_ACCOUNT_SID",
            "TWILIO_AUTH_TOKEN",
            "TWILIO_FROM_NUMBER",
        )
        if not os.getenv(k)
    ]
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

