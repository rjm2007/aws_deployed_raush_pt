# ─────────────────────────────────────────────────────────────────────────────
# Rausch PT — New Lead Intro SMS Scheduler (Docker service)
#
# PURPOSE
# - Poll Supabase for new leads
# - Send a one-time intro SMS ("Hi <name>...") for testing / warm outreach
# - Does NOT affect the outbound calling scheduler (scheduler_leads.py)
#
# RUN (Docker):
#   Add a compose service (see docker-compose.yml) or run in the container:
#     python scheduler_leads_sms.py
#
# DEDUPE
# - Uses notification_log (lead_id + notification_type='sms_lead_intro' + channel='sms' + status='sent')
#   to avoid sending the same intro SMS multiple times across restarts.
#
# ENV
# - SUPABASE_URL, SUPABASE_API_KEY
# - TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER
# - Optional: LEADS_SMS_POLL_SECONDS (default 10; clamped 5–300)
# - Optional: LEADS_SMS_BATCH_SIZE (default 25)
# - Optional: LEADS_SMS_DRY_RUN=1 (log only, no Twilio send, no notification_log insert)
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import base64
import logging
import os
import re
from datetime import datetime, timezone

import httpx
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_API_KEY = os.getenv("SUPABASE_API_KEY")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")


def _int_env(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


POLL_SECONDS = max(5, min(300, _int_env("LEADS_SMS_POLL_SECONDS", 10)))
BATCH_SIZE = max(1, min(200, _int_env("LEADS_SMS_BATCH_SIZE", 25)))
DRY_RUN = (os.getenv("LEADS_SMS_DRY_RUN") or "").strip().lower() in ("1", "true", "yes")

NOTIFICATION_TYPE = "sms_lead_intro"

SUPABASE_HEADERS = {
    "apikey": SUPABASE_API_KEY,
    "Authorization": f"Bearer {SUPABASE_API_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}


def _setup_logger() -> logging.Logger:
    os.makedirs("logs", exist_ok=True)
    logger = logging.getLogger("scheduler_leads_sms")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger
    fmt = logging.Formatter("%(asctime)s | %(levelname)s | %(message)s")
    fh = logging.FileHandler("logs/scheduler_leads_sms.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(fh)
    logger.addHandler(sh)
    return logger


logger = _setup_logger()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_phone_e164(phone: str | None) -> str | None:
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


def _first_name(full_name: str | None) -> str:
    if not full_name or not str(full_name).strip():
        return "there"
    return str(full_name).strip().split()[0]


def build_intro_sms(lead: dict) -> str:
    first = _first_name(lead.get("full_name"))
    service = (lead.get("service_of_interest") or "physical therapy").strip()
    location = (lead.get("preferred_location") or "").strip()

    msg = f"Hi {first}, this is Rausch Physical Therapy & Wellness."
    msg += f" We noticed you were interested in {service}"
    if location:
        msg += f" at our {location} location"
    msg += ". I can help you set up an appointment via text — what date would you like to come in?"
    return msg.strip()


async def supabase_get(path: str) -> list:
    url = f"{SUPABASE_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, headers=SUPABASE_HEADERS)
            if r.status_code == 200:
                data = r.json()
                return data if isinstance(data, list) else []
            logger.warning("supabase_get status=%s body=%s", r.status_code, (r.text or "")[:300])
            return []
    except (httpx.TimeoutException, httpx.TransportError) as e:
        logger.warning("supabase_get transient error: %s", e)
        return []
    except Exception as e:
        logger.exception("supabase_get exception: %s", e)
        return []


async def supabase_insert_notification_log(row: dict) -> bool:
    url = f"{SUPABASE_URL}/rest/v1/notification_log"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, headers=SUPABASE_HEADERS, json=row)
            if r.status_code in (200, 201):
                return True
            logger.warning("insert_notification_log status=%s body=%s", r.status_code, (r.text or "")[:300])
            return False
    except (httpx.TimeoutException, httpx.TransportError) as e:
        logger.warning("insert_notification_log transient error: %s", e)
        return False
    except Exception as e:
        logger.exception("insert_notification_log exception: %s", e)
        return False


async def supabase_insert_sms_conversation(row: dict) -> bool:
    """
    Insert a row into `public.sms_conversations`.
    Kept intentionally minimal: we only set fields we actually know.
    """
    url = f"{SUPABASE_URL}/rest/v1/sms_conversations"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(url, headers=SUPABASE_HEADERS, json=row)
            if r.status_code in (200, 201):
                return True
            logger.warning("insert_sms_conversation status=%s body=%s", r.status_code, (r.text or "")[:300])
            return False
    except (httpx.TimeoutException, httpx.TransportError) as e:
        logger.warning("insert_sms_conversation transient error: %s", e)
        return False
    except Exception as e:
        logger.exception("insert_sms_conversation exception: %s", e)
        return False


async def supabase_patch(path: str, data: dict) -> bool:
    url = f"{SUPABASE_URL}{path}"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.patch(url, headers=SUPABASE_HEADERS, json=data)
            if r.status_code in (200, 204):
                return True
            logger.warning("supabase_patch status=%s body=%s", r.status_code, (r.text or "")[:300])
            return False
    except (httpx.TimeoutException, httpx.TransportError) as e:
        logger.warning("supabase_patch transient error: %s", e)
        return False
    except Exception as e:
        logger.exception("supabase_patch exception: %s", e)
        return False


async def already_sent_for_lead(lead_id: str) -> bool:
    try:
        rows = await supabase_get(
            "/rest/v1/notification_log"
            f"?lead_id=eq.{lead_id}"
            f"&notification_type=eq.{NOTIFICATION_TYPE}"
            "&channel=eq.sms"
            "&status=eq.sent"
            "&select=id&limit=1"
        )
        return bool(rows)
    except Exception:
        return False


async def twilio_send_sms(to_phone: str, body: str) -> tuple[bool, str | None, str | None]:
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN or not TWILIO_FROM_NUMBER:
        return False, None, "Twilio not configured"

    to_e164 = _format_phone_e164(to_phone)
    if not to_e164:
        return False, None, f"Invalid destination phone: {to_phone!r}"

    from_e164 = _format_phone_e164(TWILIO_FROM_NUMBER) or TWILIO_FROM_NUMBER
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    auth = base64.b64encode(f"{TWILIO_ACCOUNT_SID}:{TWILIO_AUTH_TOKEN}".encode("utf-8")).decode("utf-8")
    headers = {"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"}
    data = {"To": to_e164, "From": from_e164, "Body": body}

    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.post(url, headers=headers, data=data)
            if r.status_code in (200, 201):
                sid = (r.json() or {}).get("sid")
                return True, sid, None
            return False, None, f"Twilio status={r.status_code} body={(r.text or '')[:200]}"
    except Exception as e:
        return False, None, f"Twilio exception: {e}"


async def send_intro_for_lead(lead: dict) -> None:
    lead_id = lead.get("id")
    phone = lead.get("phone") or ""
    if not lead_id or not phone:
        return

    phone_e164 = _format_phone_e164(phone)
    if not phone_e164:
        return

    if await already_sent_for_lead(lead_id):
        return

    body = build_intro_sms(lead)
    logger.info(
        "lead_intro candidate lead_id=%s phone=%s e164=%s dry_run=%s",
        lead_id,
        phone,
        phone_e164,
        DRY_RUN,
    )

    if DRY_RUN:
        logger.info("DRY_RUN sms body=%r", body)
        return

    ok, sid, err = await twilio_send_sms(phone_e164, body)
    await supabase_insert_notification_log(
        {
            "lead_id": lead_id,
            "appointment_id": None,
            "notification_type": NOTIFICATION_TYPE,
            "channel": "sms",
            "status": "sent" if ok else "failed",
            "vapi_call_id": None,
            "payload": {"to": phone, "twilio_sid": sid, "error": err},
            "sent_at": datetime.utcnow().isoformat() if ok else None,
        }
    )
    logger.info("lead_intro sms ok=%s sid=%s err=%s lead_id=%s", ok, sid, err, lead_id)

    # Save what we sent into the SMS conversation table (future multi-turn context).
    if ok:
        await supabase_insert_sms_conversation(
            {
                "phone_number": phone_e164,
                "lead_id": lead_id,
                "appointment_id": None,
                "practice_id": None,
                "role": "assistant",
                "message": body,
                "direction": "outbound",
                "intent": None,
                "twilio_sid": sid,
            }
        )
        # Move lead to message_sent so the call scheduler knows to call after the delay.
        await supabase_patch(
            f"/rest/v1/leads?id=eq.{lead_id}",
            {
                "queue_status": "message_sent",
                "sms_sent_at": now_iso(),
                "updated_at": now_iso(),
            },
        )
        logger.info("lead_intro moved to message_sent lead_id=%s", lead_id)
    else:
        # Try only once: move the lead out of `new` so it won't be retried on the next poll.
        await supabase_patch(
            f"/rest/v1/leads?id=eq.{lead_id}",
            {
                "queue_status": "manual_follow_up",
                "notes": f"[sms_lead_intro_failed] {err}",
                "updated_at": now_iso(),
            },
        )


async def poll_once() -> None:
    leads = await supabase_get(
        "/rest/v1/leads"
        "?queue_status=eq.new"
        "&select=id,full_name,phone,service_of_interest,preferred_location,created_at"
        "&order=created_at.asc"
        f"&limit={BATCH_SIZE}"
    )
    if not leads:
        return
    await asyncio.gather(*[send_intro_for_lead(l) for l in leads])


async def main() -> None:
    missing = []
    for k in ("SUPABASE_URL", "SUPABASE_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"):
        if not os.getenv(k):
            missing.append(k)
    if missing:
        logger.error("STARTUP ERROR missing env vars: %s", missing)
        return

    logger.info(
        "[scheduler_leads_sms] starting poll_seconds=%s batch=%s dry_run=%s",
        POLL_SECONDS,
        BATCH_SIZE,
        DRY_RUN,
    )

    while True:
        try:
            await poll_once()
        except Exception as e:
            logger.exception("poll loop exception: %s", e)
        await asyncio.sleep(POLL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())

