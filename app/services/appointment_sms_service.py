import re

import httpx
from datetime import datetime, date as date_cls, timedelta, timezone
from typing import Optional
from urllib.parse import quote

from app.core.config import SUPABASE_URL, SUPABASE_HEADERS, TEST_PHONE_PREFIXES
from app.core.logger import logger
from app.services.supabase_service import supabase_insert_notification_log
from app.services.twilio_service import twilio_send_sms

# Patients call this number (our inbound AI agent) to reschedule / cancel. We do NOT handle SMS replies.
CLINIC_CALL_NUMBER = "+1 (877) 283-2944"

BOOKED = "sms_appointment_booked"
RESCHEDULED = "sms_appointment_rescheduled"
CANCELLED = "sms_appointment_cancelled"
CONFIRMED = "sms_appointment_confirmed"

# A pending (end-of-call) text left this long without an end-of-call webhook is sent by scheduler_leads.py.
PENDING_MAX_AGE_MINUTES = 10

_APPT_SELECT = ("id,lead_id,patient_name,patient_phone,appointment_date,appointment_time,location,service,"
                "status,provider_name,visit_type,rescheduled_from_id,updated_at")


# ─── text ────────────────────────────────────────────────────────────────────

def friendly_date(raw) -> str:
    """'2026-12-02' -> 'Wednesday, December 2, 2026' (unparseable input is returned as is)."""
    s = str(raw or "").strip()
    try:
        d = date_cls.fromisoformat(s[:10])
    except ValueError:
        return s
    return f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}, {d.year}"


def friendly_time(raw) -> str:
    """'07:00:00' / '07:00' / '13:30' -> '7:00 AM' / '1:30 PM' (anything else is returned as is)."""
    s = str(raw or "").strip()
    m_ = re.fullmatch(r"(\d{1,2}):(\d{2})(?::\d{2}(?:\.\d+)?)?", s)
    if not m_:
        return s
    h, m = int(m_.group(1)), int(m_.group(2))
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return s
    ampm = "AM" if h < 12 else "PM"
    return f"{(h % 12) or 12}:{m:02d} {ampm}"


def build_appointment_sms(notification_type: str, appt: dict) -> str:
    """Short patient text (aim: under ~320 chars). Clinician line only when the row has provider_name."""
    first = (str(appt.get("patient_name") or "").split() or [""])[0].strip()
    provider = str(appt.get("provider_name") or "").strip()
    location = str(appt.get("location") or "").strip()
    d = friendly_date(appt.get("appointment_date"))
    t = friendly_time(appt.get("appointment_time"))

    if notification_type == CANCELLED:
        header = "Your appointment has been cancelled."
        footer = f"To book a new time, call us at {CLINIC_CALL_NUMBER}."
    elif notification_type == RESCHEDULED:
        header = "Your appointment has been rescheduled."
        footer = f"To reschedule or cancel, call us at {CLINIC_CALL_NUMBER}."
    elif notification_type == CONFIRMED:
        header = "Your appointment is confirmed."
        footer = f"To reschedule or cancel, call us at {CLINIC_CALL_NUMBER}."
    else:   # booked / legacy confirmation
        header = "Your appointment is booked."
        footer = f"To reschedule or cancel, call us at {CLINIC_CALL_NUMBER}."

    lines = ["Rausch Physical Therapy & Wellness", f"Hi {first}, {header[0].lower()}{header[1:]}" if first else header]
    when = " at ".join(x for x in (d, t) if x)
    if when:
        lines.append(f"When: {when}")
    if provider:
        lines.append(f"With: {provider}")
    if location:
        lines.append(f"Where: {location}")
    lines.append(footer)
    return "\n".join(lines)


# ─── notification_log helpers ────────────────────────────────────────────────

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
    rows = await _fetch_appointments([appointment_id])
    return rows.get(appointment_id)


async def _fetch_appointments(ids: list[str]) -> dict[str, dict]:
    ids = [i for i in dict.fromkeys(ids) if i]
    if not ids:
        return {}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/appointments?id=in.({','.join(ids)})&select={_APPT_SELECT}",
                headers=SUPABASE_HEADERS,
            )
            if r.status_code == 200:
                return {row["id"]: row for row in (r.json() or [])}
            logger.error("[sms] fetch appointments status=%s body=%s", r.status_code, (r.text or "")[:200])
    except Exception as e:
        logger.error("[sms] fetch appointments failed ids=%s err=%s", ids, e)
    return {}


async def _patch_log(row_id: str, data: dict, only_status: str | None = None) -> list[dict]:
    """PATCH one notification_log row; with only_status the update only applies if the row still has it."""
    q = f"id=eq.{row_id}" + (f"&status=eq.{only_status}" if only_status else "")
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.patch(f"{SUPABASE_URL}/rest/v1/notification_log?{q}", headers=SUPABASE_HEADERS, json=data)
            if r.status_code in (200, 204):
                return r.json() if r.status_code == 200 and r.content else []
            logger.error("[sms] notification_log patch failed status=%s body=%s", r.status_code, (r.text or "")[:200])
    except Exception as e:
        logger.error("[sms] notification_log patch exception row=%s err=%s", row_id, e)
    return []


async def _claim_pending(call_id: str) -> list[dict]:
    """Atomically move this call's pending rows to 'processing' (so webhook + scheduler never both send)."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.patch(
                f"{SUPABASE_URL}/rest/v1/notification_log?vapi_call_id=eq.{quote(call_id, safe='')}"
                f"&status=eq.pending&channel=eq.sms",
                headers=SUPABASE_HEADERS, json={"status": "processing"},
            )
            if r.status_code == 200:
                return sorted(r.json() or [], key=lambda x: x.get("created_at") or "")
            logger.error("[sms] claim pending failed call=%s status=%s body=%s", call_id, r.status_code, (r.text or "")[:200])
    except Exception as e:
        logger.error("[sms] claim pending exception call=%s err=%s", call_id, e)
    return []


# ─── send now ────────────────────────────────────────────────────────────────

async def send_appointment_sms_if_needed(
    *,
    rid: str,
    appointment_id: str,
    notification_type: str,
    appt: dict,
    lead_id: Optional[str] = None,
    vapi_call_id: Optional[str] = None,
    ended_reason: Optional[str] = None,
    log_row_id: Optional[str] = None,
) -> bool:
    """
    Idempotent SMS sender (deduped by notification_log appointment_id+type+channel, status sent).
    log_row_id: update that (pending) notification_log row instead of inserting a new one.
    Returns True if SMS was sent, False otherwise (including deduped).
    """
    if not appointment_id or not notification_type:
        return False

    if await _already_sent(appointment_id, notification_type):
        logger.info("[%s] sms dedupe hit type=%s appt_id=%s", rid, notification_type, appointment_id)
        if log_row_id:
            await _patch_log(log_row_id, {"status": "skipped", "appointment_id": appointment_id,
                                          "notification_type": notification_type, "sent_at": None,
                                          "payload": {"skip_reason": "already sent"}})
        return False

    appt_data = appt or {}
    if (not appt_data.get("patient_phone") or not appt_data.get("appointment_date")
            or not appt_data.get("appointment_time") or "provider_name" not in appt_data):
        fetched = await _fetch_appointment(appointment_id)
        if fetched:
            appt_data = {**fetched, **{k: v for k, v in appt_data.items() if v not in (None, "")}}  # caller overrides

    to_phone = appt_data.get("patient_phone")
    if to_phone and str(to_phone).startswith(TEST_PHONE_PREFIXES):
        logger.info("[%s] sms skipped for test number type=%s appt_id=%s", rid, notification_type, appointment_id)
        if log_row_id:
            await _patch_log(log_row_id, {"status": "skipped", "sent_at": None, "payload": {"skip_reason": "test number"}})
        return False
    body = build_appointment_sms(notification_type, appt_data)
    ok, sid, err = await twilio_send_sms(to_phone, body)

    row = {
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
    }
    if log_row_id:
        await _patch_log(log_row_id, {k: v for k, v in row.items() if k not in ("lead_id", "vapi_call_id") or v})
    else:
        await supabase_insert_notification_log(row)

    logger.info("[%s] sms sent attempt type=%s appt_id=%s ok=%s err=%s", rid, notification_type, appointment_id, ok, err)
    return ok


# ─── send now, or hold until the Vapi call ends ──────────────────────────────

async def notify_appointment_sms(
    *,
    rid: str,
    appointment_id: str,
    notification_type: str,
    appt: dict,
    lead_id: Optional[str] = None,
    vapi_call_id: Optional[str] = None,
) -> bool:
    """
    Direct request (no Vapi call id): send now (as before).
    During a Vapi call: only record a 'pending' notification_log row for the call id; the text is sent
    from the end-of-call webhook (flush_pending_for_call) using the FINAL state of the appointment,
    or by the scheduler safety net after PENDING_MAX_AGE_MINUTES.
    Returns True when a text was sent or queued.
    """
    if not appointment_id or not notification_type:
        return False
    if not vapi_call_id:
        return await send_appointment_sms_if_needed(rid=rid, appointment_id=appointment_id,
                                                    notification_type=notification_type, appt=appt, lead_id=lead_id)
    try:   # Vapi retries tool calls: one pending row per call + appointment + type
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/notification_log?vapi_call_id=eq.{quote(vapi_call_id, safe='')}"
                f"&appointment_id=eq.{appointment_id}&notification_type=eq.{notification_type}&channel=eq.sms"
                f"&select=id&limit=1",
                headers=SUPABASE_HEADERS,
            )
            if r.status_code == 200 and (r.json() or []):
                logger.info("[%s] sms already queued type=%s appt_id=%s call=%s", rid, notification_type, appointment_id, vapi_call_id)
                return True
    except Exception as e:
        logger.error("[%s] sms queue check failed: %s", rid, e)
    row = await supabase_insert_notification_log({
        "lead_id": lead_id,
        "appointment_id": appointment_id,
        "notification_type": notification_type,
        "channel": "sms",
        "status": "pending",
        "vapi_call_id": vapi_call_id,
        "payload": {"to": (appt or {}).get("patient_phone"), "appt": appt or {}},
        "sent_at": None,
    })
    if row:
        logger.info("[%s] sms QUEUED until end of call type=%s appt_id=%s call=%s", rid, notification_type, appointment_id, vapi_call_id)
        return True
    logger.error("[%s] sms queue insert failed — sending now instead type=%s appt_id=%s", rid, notification_type, appointment_id)
    return await send_appointment_sms_if_needed(rid=rid, appointment_id=appointment_id, notification_type=notification_type,
                                                appt=appt, lead_id=lead_id, vapi_call_id=vapi_call_id)


# ─── end of call: decide what to send from the FINAL state ───────────────────

_LIVE = ("scheduled", "confirmed")


def plan_call_notifications(pending: list[dict], appts: dict[str, dict]) -> list[dict]:
    """
    pending: this call's pending notification_log rows (appointment_id, notification_type).
    appts: appointments rows by id (the pending ones + their rescheduled_from_id parents), re-fetched now.
    -> [{"appointment_id", "notification_type"}] to send, at most one per appointment:
       - booked/moved in this call and still scheduled → one text for the final visit
         ('booked' if the chain started with a booking in this call, else 'rescheduled')
       - booked in this call and then cancelled in this call → nothing
       - an existing visit moved in this call and then cancelled → 'cancelled' for the original visit
       - an existing visit cancelled → 'cancelled'
    """
    created: dict[str, str] = {}
    cancelled: set[str] = set()
    for p in pending:
        aid, typ = p.get("appointment_id"), p.get("notification_type")
        if not aid:
            continue
        if typ == CANCELLED:
            cancelled.add(aid)
        elif typ in (BOOKED, RESCHEDULED, CONFIRMED):
            if created.get(aid) != BOOKED:
                created[aid] = BOOKED if typ in (BOOKED, CONFIRMED) else RESCHEDULED

    def origin(aid: str) -> str:
        seen = {aid}
        cur = aid
        while created.get(cur) == RESCHEDULED:
            parent = (appts.get(cur) or {}).get("rescheduled_from_id")
            if not parent or parent not in created or parent in seen:
                break
            seen.add(parent)
            cur = parent
        return cur

    out: list[dict] = []
    done: set[tuple] = set()

    def add(aid, typ):
        if aid and (aid, typ) not in done:
            done.add((aid, typ))
            out.append({"appointment_id": aid, "notification_type": typ})

    for aid in dict.fromkeys(p.get("appointment_id") for p in pending):
        row = appts.get(aid)
        if not aid or not row:
            continue
        status = str(row.get("status") or "").lower()
        if aid in created:
            o = origin(aid)
            if status in _LIVE:
                add(aid, created[o])
            elif status == "cancelled" and created[o] == RESCHEDULED:
                add((appts.get(o) or {}).get("rescheduled_from_id") or aid, CANCELLED)
            # booked here then cancelled here / moved again (child handles it) → nothing
        elif aid in cancelled and status == "cancelled":
            add(aid, CANCELLED)
    return out


async def flush_pending_for_call(call_id: str, rid: str, ended_reason: Optional[str] = None) -> dict:
    """Send this call's held texts (final state only). Safe to call twice: rows are claimed atomically."""
    summary = {"pending": 0, "sent": 0, "skipped": 0}
    if not call_id:
        return summary
    rows = await _claim_pending(call_id)
    summary["pending"] = len(rows)
    if not rows:
        return summary
    ids = [r.get("appointment_id") for r in rows]
    appts = await _fetch_appointments(ids)
    parents = [a.get("rescheduled_from_id") for a in appts.values() if a.get("rescheduled_from_id") not in appts]
    if parents:
        appts.update(await _fetch_appointments(parents))
    plan = plan_call_notifications(rows, appts)
    logger.info("[%s] end-of-call sms call=%s pending=%s plan=%s", rid, call_id, len(rows), plan)

    unused = list(rows)
    for item in plan:
        aid, typ = item["appointment_id"], item["notification_type"]
        carrier = next((r for r in unused if r.get("appointment_id") == aid), None) or (unused[0] if unused else None)
        if carrier:
            unused.remove(carrier)
        appt = appts.get(aid) or {}
        held = ((carrier or {}).get("payload") or {}).get("appt") or {}
        data = {**appt}
        if not data.get("patient_phone"):
            data["patient_phone"] = held.get("patient_phone") or ((carrier or {}).get("payload") or {}).get("to")
        if not data.get("location"):
            data["location"] = held.get("location")
        ok = await send_appointment_sms_if_needed(
            rid=rid, appointment_id=aid, notification_type=typ, appt=data,
            lead_id=appt.get("lead_id") or (carrier or {}).get("lead_id"), vapi_call_id=call_id,
            ended_reason=ended_reason, log_row_id=(carrier or {}).get("id"))
        summary["sent"] += 1 if ok else 0
    for r in unused:
        await _patch_log(r["id"], {"status": "skipped", "sent_at": None,
                                   "payload": {**(r.get("payload") or {}), "skip_reason": "final state after call"}})
        summary["skipped"] += 1
    logger.info("[%s] end-of-call sms call=%s done %s", rid, call_id, summary)
    return summary


async def flush_stale_pending(rid: str, max_age_minutes: int = PENDING_MAX_AGE_MINUTES) -> int:
    """Safety net (scheduler): flush calls whose newest pending row is older than max_age_minutes
    (the end-of-call webhook never came). Returns the number of calls flushed."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/notification_log?status=eq.pending&channel=eq.sms"
                f"&select=vapi_call_id,created_at&order=created_at.asc&limit=500",
                headers=SUPABASE_HEADERS,
            )
            if r.status_code != 200:
                logger.error("[%s] stale pending fetch status=%s", rid, r.status_code)
                return 0
            rows = r.json() or []
    except Exception as e:
        logger.error("[%s] stale pending fetch failed: %s", rid, e)
        return 0
    newest: dict[str, str] = {}
    for row in rows:
        cid = row.get("vapi_call_id")
        if cid:
            newest[cid] = max(newest.get(cid, ""), row.get("created_at") or "")
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=max_age_minutes)
    flushed = 0
    for cid, ts in newest.items():
        try:
            created = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            continue
        if created.tzinfo is None:
            created = created.replace(tzinfo=timezone.utc)
        if created <= cutoff:
            logger.info("[%s] stale pending sms call=%s (no end-of-call webhook) → flushing", rid, cid)
            await flush_pending_for_call(cid, rid, ended_reason="no-webhook-timeout")
            flushed += 1
    return flushed
