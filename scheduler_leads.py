# ─────────────────────────────────────────────────────────────────────────────
# Rausch PT — New Lead Outbound Caller Scheduler (Docker service)
#
# Runs forever as its own container.
# - Pulls leads from Supabase queues
# - Only places calls during clinic office hours in America/Los_Angeles (configurable)
# - Claims (marks in_progress) before calling to avoid double-calls
# - Triggers Vapi outbound calls
# - Writes logs to /code/logs/scheduler_leads.log
#
# TEMP client / demo — near-instant outbound after a lead is inserted:
#   LEADS_TEST_POLL_SECONDS=20
#   (poll every N seconds; ignores LA office-hours gate while set; remove both for production.)
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
VAPI_OUTBOUND_LEAD_ASSISTANT_ID = os.getenv("VAPI_OUTBOUND_LEAD_ASSISTANT_ID") or os.getenv("VAPI_LEAD_ASSISTANT_ID")
VAPI_PHONE_NUMBER_ID = os.getenv("VAPI_PHONE_NUMBER_ID")

# How often to try calling new leads (production)
LEADS_CRON_MINUTES = os.getenv("LEADS_CRON_MINUTES", "0,20,40")  # default: ~every 20 min at :00,:20,:40

# TEMP: set e.g. 20 to poll every 20s instead of cron (unset = normal cron)
_raw_test_poll = os.getenv("LEADS_TEST_POLL_SECONDS", "").strip()
if _raw_test_poll:
    try:
        LEADS_TEST_POLL_SECONDS = max(10, min(300, int(_raw_test_poll)))
    except ValueError:
        LEADS_TEST_POLL_SECONDS = None
else:
    LEADS_TEST_POLL_SECONDS = None

# Limits to stay under Vapi concurrency (global limit ~= 10)
MAX_CALLS_PER_RUN = int(os.getenv("LEADS_MAX_CALLS_PER_RUN", "4"))

# Retry protection: only retry in_progress leads if they haven't been touched recently
MIN_RETRY_AGE_MINUTES = int(os.getenv("LEADS_MIN_RETRY_AGE_MINUTES", "30"))

# Batch fetch sizes (we still hard-cap actual calls per run)
BATCH_SIZE = int(os.getenv("LEADS_BATCH_SIZE", "20"))

# Same idea as reminder scheduler: no outbound dials outside LA office window
LEADS_OFFICE_START_HOUR = int(os.getenv("LEADS_OFFICE_START_HOUR", "8"))  # inclusive
LEADS_OFFICE_END_HOUR = int(os.getenv("LEADS_OFFICE_END_HOUR", "17"))  # exclusive


def _setup_logger() -> logging.Logger:
    os.makedirs("logs", exist_ok=True)
    logger = logging.getLogger("scheduler_leads")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    fh = logging.FileHandler("logs/scheduler_leads.log", encoding="utf-8")
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


def in_leads_office_hours(dt_local: datetime) -> bool:
    h = dt_local.hour
    return LEADS_OFFICE_START_HOUR <= h < LEADS_OFFICE_END_HOUR


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
        # must be international length (US is 11 digits including country code 1)
        if len(digits_only) < 11:
            return None
        return cleaned

    # No leading '+': assume US if 10 digits
    if len(digits_only) == 10:
        return "+1" + digits_only
    if len(digits_only) == 11 and digits_only.startswith("1"):
        return "+" + digits_only
    return "+" + digits_only


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


async def job_call_new_leads():
    if not (SUPABASE_URL and SUPABASE_API_KEY and VAPI_API_KEY and VAPI_OUTBOUND_LEAD_ASSISTANT_ID and VAPI_PHONE_NUMBER_ID):
        logger.error("Missing required env vars for scheduler_leads; skipping run.")
        return

    now_la = datetime.now(LA_TZ)
    # Production: respect LA office window. TEMP demo: LEADS_TEST_POLL_SECONDS skips this so clients can test anytime.
    if LEADS_TEST_POLL_SECONDS is None:
        if not in_leads_office_hours(now_la):
            logger.info(
                "job_call_new_leads skip (outside office hours %02d-%02d LA)",
                LEADS_OFFICE_START_HOUR,
                LEADS_OFFICE_END_HOUR,
            )
            return
    else:
        logger.info(
            "TEST MODE job_call_new_leads (LEADS_TEST_POLL_SECONDS=%ss) — office hours check skipped for demo",
            LEADS_TEST_POLL_SECONDS,
        )

    logger.info("── job_call_new_leads START ──")

    # New leads
    new_leads = await supabase_get(
        "/rest/v1/leads"
        "?queue_status=eq.new"
        "&call_attempts=lt.3"
        "&select=id,full_name,phone,service_of_interest,preferred_location,call_attempts,created_at,updated_at"
        "&order=created_at.asc"
        f"&limit={BATCH_SIZE}"
    )

    # Retry leads: only if stale enough
    # Note: Supabase REST filtering on updated_at is possible but depends on column type; do it in Python reliably.
    retry_candidates = await supabase_get(
        "/rest/v1/leads"
        "?queue_status=eq.in_progress"
        "&call_attempts=lt.3"
        "&select=id,full_name,phone,service_of_interest,preferred_location,call_attempts,created_at,updated_at"
        "&order=updated_at.asc"
        f"&limit={BATCH_SIZE}"
    )

    callback_leads = []
    now_z = now_utc().strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"
    callback_leads = await supabase_get(
        "/rest/v1/leads"
        "?queue_status=eq.follow_up"
        f"&callback_requested_at=lte.{now_z}"
        "&select=id,full_name,phone,service_of_interest,preferred_location,call_attempts,created_at,updated_at"
        "&order=callback_requested_at.asc"
        f"&limit={BATCH_SIZE}"
    )

    leads: list[dict] = []
    leads.extend(new_leads or [])

    # stale retry filter
    cutoff = now_utc() - timedelta(minutes=MIN_RETRY_AGE_MINUTES)
    for l in (retry_candidates or []):
        ts = l.get("updated_at") or ""
        try:
            # ISO parse best-effort; if it fails, treat as eligible (better to call than starve)
            updated = datetime.fromisoformat(ts.replace("Z", "+00:00")) if ts else None
        except Exception:
            updated = None
        if updated is None or updated <= cutoff:
            leads.append(l)

    leads.extend(callback_leads or [])

    if not leads:
        logger.info("No leads to call; done.")
        return

    calls_started = 0
    for lead in leads:
        if calls_started >= MAX_CALLS_PER_RUN:
            break

        lead_id = lead.get("id")
        full_name = lead.get("full_name") or ""
        phone = lead.get("phone") or ""
        service = lead.get("service_of_interest") or "evaluation"
        location = lead.get("preferred_location") or ""

        if not lead_id or not phone:
            continue

        e164 = format_phone(phone)
        if not e164:
            logger.info("Invalid phone for lead_id=%s phone=%r -> manual_follow_up", lead_id, phone)
            await supabase_patch(
                f"/rest/v1/leads?id=eq.{lead_id}",
                {"queue_status": "manual_follow_up", "notes": f"Invalid phone number: {phone}", "updated_at": now_iso()},
            )
            continue

        # Claim lead
        marked = await supabase_patch(
            f"/rest/v1/leads?id=eq.{lead_id}",
            {"queue_status": "in_progress", "updated_at": now_iso()},
        )
        if not marked:
            continue

        logger.info("Calling lead_id=%s name=%r phone=%s", lead_id, full_name, e164)
        call_obj = await trigger_vapi_call(
            assistant_id=VAPI_OUTBOUND_LEAD_ASSISTANT_ID,
            phone=phone,
            variable_values={
                "lead_id": lead_id,
                "patient_name": full_name,
                "patient_phone": phone,
                "service": service,
                "location": location,
                "today_date": datetime.now(LA_TZ).strftime("%Y-%m-%d"),
            },
        )
        if call_obj is None:
            # revert claim so it can be retried later
            await supabase_patch(f"/rest/v1/leads?id=eq.{lead_id}", {"queue_status": "new", "updated_at": now_iso()})
        else:
            calls_started += 1

        await asyncio.sleep(1)

    logger.info("── job_call_new_leads DONE started_calls=%s ──", calls_started)


async def main():
    missing = [k for k in ("SUPABASE_URL", "SUPABASE_API_KEY", "VAPI_API_KEY", "VAPI_PHONE_NUMBER_ID") if not os.getenv(k)]
    if missing:
        logger.error("STARTUP ERROR missing env vars: %s", missing)
        return
    if not VAPI_OUTBOUND_LEAD_ASSISTANT_ID:
        logger.error("STARTUP ERROR missing VAPI_OUTBOUND_LEAD_ASSISTANT_ID (or VAPI_LEAD_ASSISTANT_ID)")
        return

    if LEADS_TEST_POLL_SECONDS is not None:
        logger.info(
            "[scheduler_leads] TEST MODE poll every %ss (remove LEADS_TEST_POLL_SECONDS for production cron)",
            LEADS_TEST_POLL_SECONDS,
        )
    else:
        logger.info(
            "[scheduler_leads] starting cron minutes=%s max_calls_per_run=%s office_hours=%02d-%02d LA",
            LEADS_CRON_MINUTES,
            MAX_CALLS_PER_RUN,
            LEADS_OFFICE_START_HOUR,
            LEADS_OFFICE_END_HOUR,
        )

    scheduler = AsyncIOScheduler(timezone="UTC")
    if LEADS_TEST_POLL_SECONDS is not None:
        scheduler.add_job(
            job_call_new_leads,
            "interval",
            seconds=LEADS_TEST_POLL_SECONDS,
            id="leads_test_fast",
            next_run_time=datetime.now(timezone.utc),
        )
    else:
        scheduler.add_job(job_call_new_leads, "cron", minute=LEADS_CRON_MINUTES, id="leads_cron")
    scheduler.start()

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit, asyncio.CancelledError):
        scheduler.shutdown()
        logger.info("[scheduler_leads] stopped")


if __name__ == "__main__":
    asyncio.run(main())

