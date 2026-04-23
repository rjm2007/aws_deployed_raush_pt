import httpx
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.config import (
    SUPABASE_URL,
    SUPABASE_HEADERS,
    SUPABASE_PRACTICE_ID,
    VAPI_REMINDER_ASSISTANT_ID,
    VAPI_INBOUND_ASSISTANT_ID,
)
from app.core.logger import logger
from app.models.requests import (
    InboundCallerLookupRequest,
    UpdateInboundStatusRequest,
    UpdateLeadStatusRequest,
    inline_schema_refs,
)
from app.services.supabase_service import (
    supabase_update_lead,
    supabase_insert_scheduled_callback,
    supabase_insert_notification_log,
    supabase_upsert_inbound_call,
    supabase_fetch_inbound_call_by_call_id,
    _insert_call_log,
)
from app.utils.parser import (
    build_vapi_response,
    coerce_vapi_tool_arguments,
    extract_vapi_caller_number_from_body,
    vapi_response_content,
)
from app.services.twilio_service import twilio_send_sms

router = APIRouter(tags=["Leads"])

# Must match Supabase CHECK constraint on public.inbound_calls.crm_status.
_INBOUND_ALLOWED_CRM_STATUSES = {
    "in_progress",
    "follow_up",
    "manual_follow_up",
    "complete",
}

def _is_inbound_vapi_call(call_obj: dict, variable_values: dict) -> bool:
    """
    Prefer Vapi system fields over custom variables.
    Vapi sets call.type to e.g. "inboundPhoneCall" / "outboundPhoneCall".
    """
    try:
        call_type = (call_obj.get("type") or "").strip()
        if call_type == "inboundPhoneCall":
            return True
        if call_type == "outboundPhoneCall":
            return False
    except Exception:
        pass

    # Fallbacks (less reliable).
    call_direction = (variable_values.get("call_direction") or variable_values.get("direction") or "").strip().lower()
    if call_direction == "inbound":
        return True
    if call_direction == "outbound":
        return False

    # Last resort: inbound assistant id marker (works when assistant is pinned to phone number).
    return False


def _normalize_phone(raw: str | None) -> str | None:
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


def _extract_tool_args(body: dict) -> tuple[str | None, dict]:
    """Return (tool_call_id, args_dict) for either VAPI wrapper or direct JSON payload."""
    tool_call_id = None
    if not isinstance(body, dict):
        return None, {}
    args: dict = dict(body)

    if "message" in body and isinstance(body.get("message"), dict):
        msg = body["message"]
        tool_calls = msg.get("toolCalls")
        if isinstance(tool_calls, list) and tool_calls and isinstance(tool_calls[0], dict):
            tc = tool_calls[0]
            tool_call_id = tc.get("id")
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            args = coerce_vapi_tool_arguments(fn.get("arguments"))

    return tool_call_id, args if isinstance(args, dict) else {}


def _extract_inbound_call_id_from_vapi(body: dict, request: Request | None = None) -> str | None:
    """VAPI phone: message.call.id. VAPI chat widget: body.chat.id or message.chat.id. Optional header X-Chat-Id."""
    if not isinstance(body, dict):
        return None
    chat_top = body.get("chat")
    if isinstance(chat_top, dict):
        cid = chat_top.get("id")
        if isinstance(cid, str) and cid.strip():
            return cid.strip()
    msg = body.get("message") if isinstance(body.get("message"), dict) else {}
    chat_msg = msg.get("chat")
    if isinstance(chat_msg, dict):
        cid = chat_msg.get("id")
        if isinstance(cid, str) and cid.strip():
            return cid.strip()
    call_obj = msg.get("call") if isinstance(msg.get("call"), dict) else {}
    call_id = call_obj.get("id") or msg.get("callId")
    if isinstance(call_id, str) and call_id.strip():
        return call_id.strip()
    if request is not None:
        h = request.headers.get("X-Chat-Id") or request.headers.get("x-chat-id")
        if isinstance(h, str) and h.strip():
            return h.strip()
    return None


def _preview_text(s: str | None, max_len: int = 240) -> str | None:
    if s is None:
        return None
    t = str(s).strip()
    if len(t) <= max_len:
        return t
    return t[: max_len - 1] + "…"


def _log_inbound_status_return(rid: str, tool_call_id: str | None, message: str) -> JSONResponse:
    content = vapi_response_content(tool_call_id, message)
    logger.info("[%s] update-inbound-status RESPONSE_JSON=%s", rid, content)
    return JSONResponse(content=content)


async def _upsert_inbound_calls_row(rid: str, payload: dict) -> tuple[dict | None, str | None]:
    """
    Single write path for inbound_calls. Does NOT touch outbound lead logic.
    """
    row, err = await supabase_upsert_inbound_call(payload)
    if err:
        logger.warning("[%s] inbound_calls upsert err=%s payload_keys=%s", rid, err, list(payload.keys()))
    return row, err


@router.post(
    "/update-inbound-status",
    summary="Inbound tool: upsert inbound_calls CRM row",
    openapi_extra={
        "requestBody": {
            "content": {"application/json": {"schema": inline_schema_refs(UpdateInboundStatusRequest.model_json_schema())}},
            "required": True,
        }
    },
)
async def update_inbound_status(request: Request):
    """
    Inbound assistant tool endpoint.
    Writes to public.inbound_calls using call_id as idempotency key.
    Allowed crm_status values:
      - in_progress
      - follow_up
      - manual_follow_up
      - complete
    """
    try:
        rid = str(uuid4())[:8]
        body = await request.json()
        logger.info("[%s] ======== update-inbound-status START ========", rid)
        logger.info("[%s] update-inbound-status body=%s", rid, body)

        tool_call_id, args = _extract_tool_args(body)
        logger.info(
            "[%s] update-inbound-status parsed tool_call_id=%s arg_keys=%s",
            rid,
            tool_call_id,
            list(args.keys()) if isinstance(args, dict) else None,
        )

        crm_status = (args.get("crm_status") or "").strip()
        if crm_status not in _INBOUND_ALLOWED_CRM_STATUSES:
            err_msg = "crm_status must be one of: in_progress, follow_up, manual_follow_up, complete."
            logger.info("[%s] update-inbound-status RESPONSE (validation) tool_call_id=%s message=%r", rid, tool_call_id, err_msg)
            return _log_inbound_status_return(rid, tool_call_id, err_msg)

        call_id = (args.get("call_id") or "").strip() or (
            _extract_inbound_call_id_from_vapi(body, request) or ""
        )
        if not call_id:
            err_msg = "Missing call_id."
            logger.info("[%s] update-inbound-status RESPONSE (validation) tool_call_id=%s message=%r", rid, tool_call_id, err_msg)
            return _log_inbound_status_return(rid, tool_call_id, err_msg)

        caller_number = _normalize_phone(
            args.get("caller_number")
            or args.get("caller_phone")
            or extract_vapi_caller_number_from_body(body)
        )
        caller_name = args.get("caller_name") or args.get("patient_name") or args.get("name")
        appointment_id = args.get("appointment_id")
        location = args.get("location")
        route = args.get("route")
        notes = args.get("notes") or args.get("summary")

        logger.info(
            "[%s] update-inbound-status request_summary call_id=%s crm_status=%s route=%r appointment_id=%r "
            "caller_number_tail=%s caller_name=%r notes_preview=%r",
            rid,
            call_id,
            crm_status,
            route,
            appointment_id,
            (caller_number[-4:] if caller_number and len(caller_number) >= 4 else caller_number),
            caller_name,
            _preview_text(notes),
        )

        now_iso = datetime.utcnow().isoformat()
        payload = {
            "call_id": call_id,
            "crm_status": crm_status,
            "caller_number": caller_number,
            "caller_name": caller_name,
            "appointment_id": appointment_id,
            "location": location,
            "route": route,
            "notes": notes,
            "updated_at": now_iso,
        }
        payload = {k: v for k, v in payload.items() if v is not None and v != ""}

        logger.info("[%s] update-inbound-status supabase_payload=%s", rid, payload)
        row, upsert_err = await _upsert_inbound_calls_row(rid, payload)
        logger.info("[%s] update-inbound-status upsert result row=%s err=%s", rid, row is not None, upsert_err)

        ok_msg = f"Inbound call {call_id} saved with crm_status={crm_status}."
        logger.info("[%s] update-inbound-status RESPONSE tool_call_id=%s message=%r", rid, tool_call_id, ok_msg)
        if not tool_call_id:
            logger.warning("[%s] update-inbound-status no tool_call_id — Vapi may report 'No result returned'.", rid)
        return _log_inbound_status_return(rid, tool_call_id, ok_msg)
    except Exception as e:
        logger.exception("Error in /update-inbound-status: %s", e)
        _tcid = locals().get("tool_call_id")
        _em = "Sorry, there was an error updating inbound status. Please try again."
        logger.info("[%s] update-inbound-status RESPONSE (exception) tool_call_id=%s message=%r", rid, _tcid, _em)
        return _log_inbound_status_return(rid, _tcid, _em)


@router.post("/inbound-call-event", summary="Inbound tool alias: update-inbound-status")
async def inbound_call_event(request: Request):
    # Backward-compatible alias (some VAPI tools may still point here).
    return await update_inbound_status(request)


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT: INBOUND CALLER LOOKUP (Supabase-only)
# ─────────────────────────────────────────────────────────────────────────────

def _phone_digits_variants(phone: str) -> list[str]:
    """Return digits-only variants of a phone to match DB rows stored in different formats."""
    digits = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if not digits:
        return []
    variants: set[str] = set()
    variants.add(digits)
    if len(digits) == 11 and digits.startswith("1"):
        variants.add(digits[1:])
    if len(digits) == 10:
        variants.add("1" + digits)
    out: set[str] = set()
    for d in variants:
        out.add(d)
        out.add("+" + d)
    return list(out)


async def _fetch_lead_by_phone(phone_variants: list[str]) -> dict | None:
    if not phone_variants:
        return None
    in_list = ",".join(f'"{v}"' for v in phone_variants)
    params = [
        ("select", "id,full_name,phone,service_of_interest,preferred_location,queue_status,tebra_patient_id,created_at,updated_at"),
        ("phone", f"in.({in_list})"),
        ("order", "updated_at.desc"),
        ("limit", "1"),
    ]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{SUPABASE_URL}/rest/v1/leads", headers=SUPABASE_HEADERS, params=params)
            if r.status_code == 200:
                rows = r.json() or []
                return rows[0] if rows else None
            logger.warning("caller_lookup leads status=%s body=%s", r.status_code, (r.text or "")[:200])
    except Exception as e:
        logger.warning("caller_lookup leads exception: %s", e)
    return None


async def _fetch_upcoming_appointments_by_phone(phone_variants: list[str]) -> list[dict]:
    if not phone_variants:
        return []
    la = ZoneInfo("America/Los_Angeles")
    today = datetime.now(la).date().isoformat()
    in_list = ",".join(f'"{v}"' for v in phone_variants)
    params = [
        ("select", "id,tebra_appointment_id,patient_name,patient_phone,appointment_date,appointment_time,location,service,status,lead_id"),
        ("patient_phone", f"in.({in_list})"),
        ("appointment_date", f"gte.{today}"),
        ("status", "in.(scheduled,confirmed)"),
        ("or", f"(practice_id.eq.{SUPABASE_PRACTICE_ID},practice_id.is.null)"),
        ("order", "appointment_date.asc,appointment_time.asc"),
        ("limit", "10"),
    ]
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(f"{SUPABASE_URL}/rest/v1/appointments", headers=SUPABASE_HEADERS, params=params)
            if r.status_code == 200:
                rows = r.json() or []
                return rows if isinstance(rows, list) else []
            logger.warning("caller_lookup appts status=%s body=%s", r.status_code, (r.text or "")[:200])
    except Exception as e:
        logger.warning("caller_lookup appts exception: %s", e)
    return []


def _format_time_hm(raw) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    parts = s.split(":")
    if len(parts) < 2:
        return None
    try:
        return f"{int(parts[0]):02d}:{int(parts[1]):02d}"
    except ValueError:
        return None


@router.post(
    "/inbound-caller-lookup",
    summary="Inbound tool: look up caller by phone (Supabase leads + upcoming appointments)",
    openapi_extra={
        "requestBody": {
            "content": {"application/json": {"schema": inline_schema_refs(InboundCallerLookupRequest.model_json_schema())}},
            "required": False,
        }
    },
)
async def inbound_caller_lookup(request: Request):
    """
    Inbound-agent first tool. Looks the caller up by phone number in Supabase only
    (no Tebra call). Returns lead details + upcoming appointments.

    Phone source priority:
      1. `phone` field in request body (for Swagger / manual testing)
      2. Vapi wrapper caller ID (message.call.customer.number)
    """
    rid = uuid4().hex[:8]
    tool_call_id: str | None = None
    try:
        body = await request.json()
    except Exception:
        body = {}

    tool_call_id, args = _extract_tool_args(body if isinstance(body, dict) else {})
    arg_phone = (args.get("phone") if isinstance(args, dict) else None) or None
    vapi_phone = extract_vapi_caller_number_from_body(body if isinstance(body, dict) else None)
    phone_raw = (arg_phone or vapi_phone or "").strip()

    logger.info("[%s] inbound-caller-lookup phone_arg=%r vapi_phone=%r", rid, arg_phone, vapi_phone)

    if not phone_raw:
        msg = "No caller phone available. Please ask the caller for their name and continue normally."
        return build_vapi_response(tool_call_id, msg)

    variants = _phone_digits_variants(phone_raw)
    lead = await _fetch_lead_by_phone(variants)
    appts = await _fetch_upcoming_appointments_by_phone(variants)

    upcoming = []
    for a in appts:
        upcoming.append({
            "appointment_id": a.get("id"),
            "tebra_appointment_id": a.get("tebra_appointment_id"),
            "date": a.get("appointment_date"),
            "time": _format_time_hm(a.get("appointment_time")),
            "location": a.get("location"),
            "service": a.get("service"),
            "status": a.get("status"),
            "patient_name": a.get("patient_name"),
        })

    found = bool(lead) or bool(upcoming)
    full_name = (lead or {}).get("full_name") or (upcoming[0]["patient_name"] if upcoming else None)
    first_name = (full_name or "").strip().split()[0] if full_name else None

    result = {
        "found": found,
        "phone": phone_raw,
        "lead_id": (lead or {}).get("id"),
        "full_name": full_name,
        "first_name": first_name,
        "service_of_interest": (lead or {}).get("service_of_interest"),
        "preferred_location": (lead or {}).get("preferred_location"),
        "tebra_patient_id": (lead or {}).get("tebra_patient_id"),
        "has_upcoming_appointment": bool(upcoming),
        "upcoming": upcoming,
    }

    logger.info(
        "[%s] inbound-caller-lookup found=%s lead_id=%s upcoming=%d name=%r",
        rid, found, result["lead_id"], len(upcoming), full_name,
    )

    # Vapi tool returns must be a string; serialize JSON so the assistant can parse it.
    import json as _json
    return build_vapi_response(tool_call_id, _json.dumps(result, separators=(",", ":")))


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT: UPDATE LEAD STATUS (Agent 1)
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/update-lead-status",
    summary="Update lead record with call outcome",
    openapi_extra={
        "requestBody": {
            "content": {"application/json": {"schema": inline_schema_refs(UpdateLeadStatusRequest.model_json_schema())}},
            "required": True,
        }
    },
)
async def update_lead_status(request: Request):
    """
    Called by VAPI at the end of an outbound new lead call.
    Updates the lead record in Supabase with the call outcome.
    """
    try:
        rid  = str(uuid4())[:8]
        body = await request.json()
        logger.info("[%s] ======== update-lead-status START ========", rid)
        logger.info("[%s] update-lead-status body=%s", rid, body)

        tool_call_id = None
        if "message" in body and "toolCalls" in body["message"]:
            tc           = body["message"]["toolCalls"][0]
            tool_call_id = tc.get("id")
            args         = tc["function"]["arguments"]
        else:
            args = body

        lead_id               = args.get("lead_id")
        queue_status          = args.get("queue_status")
        lead_outcome          = args.get("lead_outcome")
        callback_requested_at = args.get("callback_requested_at")
        callback_notes        = args.get("callback_notes")
        tebra_patient_id      = args.get("tebra_patient_id")
        notes                 = args.get("notes")

        # Guard: VAPI may pass unresolved template literal
        if lead_id and isinstance(lead_id, str) and lead_id.startswith("{{"):
            logger.warning("[%s] update-lead-status lead_id is unresolved template: %s", rid, lead_id)
            lead_id = None

        if not lead_id:
            msg = "Missing lead_id. Cannot update lead."
            logger.warning("[%s] update-lead-status — no lead_id in args: %s", rid, args)
            return build_vapi_response(tool_call_id, msg)

        # ── Verify lead exists and check current state for idempotency ──
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/leads?id=eq.{lead_id}"
                f"&select=id,queue_status,lead_outcome,call_attempts,last_called_at",
                headers=SUPABASE_HEADERS,
            )
            if r.status_code != 200 or not r.json():
                return build_vapi_response(
                    tool_call_id,
                    f"Lead with id {lead_id} not found. Cannot update."
                )
            current_lead = r.json()[0]

        # ── Idempotency: skip duplicate update if already in terminal state ──
        current_qs = current_lead.get("queue_status")
        if current_qs in ("complete", "not_interested", "follow_up", "manual_follow_up") and current_qs == queue_status:
            logger.info("[%s] update-lead-status SKIP duplicate — lead already %s", rid, current_qs)
            return build_vapi_response(
                tool_call_id,
                f"Lead already updated. Status: {current_qs}, Outcome: {current_lead.get('lead_outcome', 'unchanged')}."
            )

        # ── Build update payload (only include non-null fields) ──
        update_data: dict = {"updated_at": datetime.utcnow().isoformat()}

        if queue_status:
            update_data["queue_status"]   = queue_status
        if lead_outcome:
            update_data["lead_outcome"]   = lead_outcome
            # Auto-set queue_status if VAPI didn't specify one
            if not queue_status:
                if lead_outcome == "not_interested":
                    update_data["queue_status"] = "not_interested"
                elif lead_outcome == "no_answer":
                    _new_att = (current_lead.get("call_attempts") or 0) + 1
                    update_data["queue_status"] = "in_progress" if _new_att < 3 else "manual_follow_up"
        if callback_requested_at:
            update_data["callback_requested_at"] = callback_requested_at
            # Callback → manual follow-up only; do NOT auto-schedule a robocall
            update_data["queue_status"] = "manual_follow_up"
        if callback_notes:
            update_data["callback_notes"] = callback_notes
        if tebra_patient_id:
            update_data["tebra_patient_id"] = tebra_patient_id
        if notes:
            update_data["notes"]          = notes

        # ── Scheduled callback insert removed — callbacks are manual follow-up only ──

        # ── Increment call_attempts ──
        try:
            old_attempts = current_lead.get("call_attempts") or 0
            update_data["call_attempts"]  = old_attempts + 1
            update_data["last_called_at"] = datetime.utcnow().isoformat()
            logger.info("[%s] update-lead-status call_attempts %s → %s",
                        rid, old_attempts, old_attempts + 1)
        except Exception as e:
            logger.error("[%s] update-lead-status fetch_attempts error: %s", rid, e)

        success = await supabase_update_lead(lead_id, update_data)

        if success:
            msg = f"Lead updated successfully. Status: {queue_status or 'unchanged'}, Outcome: {lead_outcome or 'unchanged'}."
            logger.info("[%s] update-lead-status success lead_id=%s", rid, lead_id)
        else:
            msg = "Failed to update lead record. Please check logs."
            logger.error("[%s] update-lead-status failed lead_id=%s", rid, lead_id)

        return build_vapi_response(tool_call_id, msg)

    except Exception as e:
        logger.exception("Error in /update-lead-status: %s", e)
        return build_vapi_response(
            locals().get("tool_call_id"),
            "Sorry, there was an error updating the lead. Please try again."
        )


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT: VAPI WEBHOOK (end-of-call fallback)
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/vapi-webhook", summary="VAPI end-of-call webhook (fallback lead updater)")
async def vapi_webhook(request: Request):
    """
    Receives VAPI server events (end-of-call-report, etc.).
    If the call ended and update_lead_status was never called,
    this fallback updates the lead based on the call summary.
    """
    try:
        body     = await request.json()
        msg_type = body.get("message", {}).get("type", "")
        rid      = str(uuid4())[:8]

        if msg_type != "end-of-call-report":
            return JSONResponse(content={"ok": True})

        report       = body.get("message", {})
        ended_reason = report.get("endedReason", "")
        call_obj     = report.get("call", {})
        assistant_id = call_obj.get("assistantId", "")
        call_id      = call_obj.get("id", "")
        started_at   = report.get("startedAt") or call_obj.get("startedAt", "")
        ended_at     = report.get("endedAt") or call_obj.get("endedAt", "")
        summary      = report.get("summary", "")

        # Calculate duration in seconds
        duration_seconds = None
        if started_at and ended_at:
            try:
                s = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
                e = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
                duration_seconds = int((e - s).total_seconds())
            except Exception:
                pass

        # Extract lead_id from the call's variable values
        overrides       = call_obj.get("assistantOverrides", {})
        variable_values = overrides.get("variableValues", {})
        lead_id         = variable_values.get("lead_id")
        appointment_id  = variable_values.get("appointment_id")
        is_inbound = _is_inbound_vapi_call(call_obj, variable_values)

        logger.info("[%s] vapi-webhook end-of-call-report call_id=%s assistant=%s ended=%s lead_id=%s duration=%s",
                    rid, call_id, assistant_id, ended_reason, lead_id, duration_seconds)

        # ── Inbound fallback persistence (does not touch outbound lead updates) ──
        # Prefer Vapi call.type == inboundPhoneCall, fallback to inbound assistant id marker.
        if is_inbound or (VAPI_INBOUND_ASSISTANT_ID and str(assistant_id).lower() == str(VAPI_INBOUND_ASSISTANT_ID).lower()):
            try:
                existing = await supabase_fetch_inbound_call_by_call_id(call_id) if call_id else None
                # Keep inbound simple for now: unresolved -> follow_up; else keep existing crm_status if present.
                crm_status = "follow_up"
                if existing and existing.get("crm_status") in _INBOUND_ALLOWED_CRM_STATUSES:
                    crm_status = existing["crm_status"]

                inbound_payload = {
                    "call_id": call_id,
                    "crm_status": crm_status,
                    "appointment_id": appointment_id if appointment_id and not str(appointment_id).startswith("{{") else None,
                    "caller_name": variable_values.get("caller_name") or variable_values.get("patient_name") or variable_values.get("name"),
                    "caller_number": _normalize_phone(variable_values.get("caller_number") or variable_values.get("patient_phone") or variable_values.get("from_number") or call_obj.get("from") or call_obj.get("fromNumber")),
                    "started_at": started_at or None,
                    "ended_at": ended_at or None,
                    "duration_seconds": duration_seconds,
                    "route": variable_values.get("route") or variable_values.get("intent"),
                    "location": variable_values.get("location"),
                    "notes": (summary or None),
                    "updated_at": datetime.utcnow().isoformat(),
                }
                inbound_payload = {k: v for k, v in inbound_payload.items() if v is not None and v != ""}
                await _upsert_inbound_calls_row(rid, inbound_payload)
            except Exception as _ie:
                logger.warning("[%s] inbound fallback persist error: %s", rid, _ie)
            return JSONResponse(content={"ok": True, "direction": "inbound"})

        # ── SMS notification (Twilio) for successful appointment outcomes ──
        # This runs on end-of-call reports and is safe to skip if not configured.
        # Only send if we have an appointment_id and the call was actually connected (not no-answer).
        #
        # IMPORTANT: Reminder calls must NOT trigger "booked" SMS on end-of-call.
        # Reminders already have dedicated flows (confirm/cancel/reschedule) and
        # reschedule endpoints send their own SMS. Sending "booked" here causes
        # confusing duplicate messages after reschedule.
        try:
            if (
                appointment_id
                and not (isinstance(appointment_id, str) and appointment_id.startswith("{{"))
                and ended_reason not in ("customer-did-not-answer", "silence-timed-out")
            ):
                reminder_type = (variable_values.get("reminder_type") or "").strip().lower()
                is_reminder_call = (assistant_id == VAPI_REMINDER_ASSISTANT_ID) or (reminder_type in ("24hr", "2hr"))
                if is_reminder_call:
                    logger.info("[%s] sms notification skipped — reminder call (assistant=%s reminder_type=%s appt_id=%s)",
                                rid, assistant_id, reminder_type or None, appointment_id)
                else:
                    async with httpx.AsyncClient(timeout=10.0) as client:
                        appt_rows = []
                        r_appt = await client.get(
                            f"{SUPABASE_URL}/rest/v1/appointments?id=eq.{appointment_id}"
                            f"&select=id,patient_name,patient_phone,appointment_date,appointment_time,location,service,status,tebra_appointment_id,updated_at",
                            headers=SUPABASE_HEADERS,
                        )
                        if r_appt.status_code == 200:
                            appt_rows = r_appt.json() or []

                        appt = appt_rows[0] if appt_rows else None

                        # If appointment was rescheduled, find the new appointment row
                        was_rescheduled = False
                        if appt and (appt.get("status") == "rescheduled"):
                            was_rescheduled = True
                            r_new = await client.get(
                                f"{SUPABASE_URL}/rest/v1/appointments?rescheduled_from_id=eq.{appointment_id}"
                                f"&select=id,patient_name,patient_phone,appointment_date,appointment_time,location,service,status,tebra_appointment_id,updated_at"
                                f"&order=updated_at.desc&limit=1",
                                headers=SUPABASE_HEADERS,
                            )
                            if r_new.status_code == 200 and (r_new.json() or []):
                                appt = (r_new.json() or [None])[0]

                        if appt:
                            status = (appt.get("status") or "").lower()
                            patient_name = appt.get("patient_name") or variable_values.get("patient_name") or ""
                            patient_phone = appt.get("patient_phone") or variable_values.get("patient_phone")
                            appt_date = appt.get("appointment_date") or variable_values.get("appointment_date")
                            appt_time = appt.get("appointment_time") or variable_values.get("appointment_time")
                            appt_location = appt.get("location") or variable_values.get("location")
                            appt_service = appt.get("service") or variable_values.get("service")
                            appt_id_for_log = appt.get("id") or appointment_id

                            notification_type = None
                            sms_body = None

                            if status in ("scheduled", "confirmed"):
                                if was_rescheduled:
                                    notification_type = "sms_appointment_rescheduled"
                                else:
                                    notification_type = "sms_appointment_confirmed" if status == "confirmed" else "sms_appointment_booked"
                                sms_body = (
                                    "RAUSCH PHYSICAL THERAPY & WELLNESS\n"
                                    f"Your appointment has been {'rescheduled' if was_rescheduled else ('confirmed' if status == 'confirmed' else 'booked')}.\n\n"
                                    f"Name: {patient_name}\n"
                                    f"Location: {appt_location}\n"
                                    f"Date: {appt_date}\n"
                                    f"Time: {appt_time}\n"
                                    f"Phone: {patient_phone}\n"
                                    f"Service: {appt_service}"
                                ).strip()
                            elif status == "cancelled":
                                notification_type = "sms_appointment_cancelled"
                                sms_body = (
                                    "RAUSCH PHYSICAL THERAPY & WELLNESS\n"
                                    "Your appointment has been cancelled.\n\n"
                                    f"Name: {patient_name}\n"
                                    f"Location: {appt_location}\n"
                                    f"Date: {appt_date}\n"
                                    f"Time: {appt_time}\n"
                                    f"Phone: {patient_phone}\n"
                                    f"Service: {appt_service}"
                                ).strip()
                            elif status == "rescheduled":
                                # If we couldn't find a new row, we avoid sending a confusing message.
                                notification_type = None
                                sms_body = None
                            else:
                                notification_type = None
                                sms_body = None

                            if notification_type and sms_body and patient_phone:
                                # Dedupe: if we already sent this notification for this appointment_id, skip.
                                r_existing = await client.get(
                                    f"{SUPABASE_URL}/rest/v1/notification_log"
                                    f"?appointment_id=eq.{appt_id_for_log}"
                                    f"&notification_type=eq.{notification_type}"
                                    f"&channel=eq.sms"
                                    f"&status=eq.sent"
                                    f"&select=id&limit=1",
                                    headers=SUPABASE_HEADERS,
                                )
                                already_sent = (r_existing.status_code == 200 and (r_existing.json() or []))

                                if not already_sent:
                                    ok, sid, err = await twilio_send_sms(patient_phone, sms_body)
                                    await supabase_insert_notification_log({
                                        "lead_id": lead_id if (lead_id and not str(lead_id).startswith("{{")) else None,
                                        "appointment_id": appt_id_for_log,
                                        "notification_type": notification_type,
                                        "channel": "sms",
                                        "status": "sent" if ok else "failed",
                                        "vapi_call_id": call_id,
                                        "payload": {
                                            "to": patient_phone,
                                            "twilio_sid": sid,
                                            "error": err,
                                            "ended_reason": ended_reason,
                                            "appointment_status": status,
                                        },
                                        "sent_at": datetime.utcnow().isoformat() if ok else None,
                                    })
                                    logger.info("[%s] sms notification type=%s appt_id=%s ok=%s err=%s",
                                                rid, notification_type, appt_id_for_log, ok, err)
                                else:
                                    logger.info("[%s] sms notification already sent type=%s appt_id=%s",
                                                rid, notification_type, appt_id_for_log)
        except Exception as _sms_e:
            logger.error("[%s] sms notification error: %s", rid, _sms_e)

        if not lead_id or (isinstance(lead_id, str) and lead_id.startswith("{{")):
            logger.info("[%s] vapi-webhook — no valid lead_id, skipping", rid)
            return JSONResponse(content={"ok": True})

        # ── Check if lead was already updated by the tool call ──
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{SUPABASE_URL}/rest/v1/leads?id=eq.{lead_id}"
                f"&select=id,queue_status,lead_outcome,call_attempts",
                headers=SUPABASE_HEADERS,
            )
            if r.status_code != 200 or not r.json():
                logger.warning("[%s] vapi-webhook — lead %s not found", rid, lead_id)
                return JSONResponse(content={"ok": True})
            lead = r.json()[0]

        current_qs      = lead.get("queue_status")
        current_outcome = lead.get("lead_outcome")

        # Determine call_type based on which assistant handled the call
        call_type_log = "outbound_reminder" if assistant_id == VAPI_REMINDER_ASSISTANT_ID else "outbound_new_lead"

        # If already in a terminal state, just log the call
        if current_qs in ("complete", "not_interested", "follow_up", "manual_follow_up"):
            logger.info("[%s] vapi-webhook — lead already %s, inserting call_log only", rid, current_qs)
            await _insert_call_log(
                rid=rid,
                lead_id=lead_id,
                vapi_call_id=call_id,
                call_status=ended_reason,
                duration_seconds=duration_seconds,
                call_type=call_type_log,
                call_direction="outbound",
                outcome=current_outcome or ended_reason,
                notes=summary if summary else None,
            )
            return JSONResponse(content={"ok": True})

        # Lead is still in_progress or new — the tool call never ran
        old_attempts = lead.get("call_attempts") or 0
        new_attempts = old_attempts + 1

        if ended_reason in ("customer-did-not-answer", "silence-timed-out"):
            queue_status = "in_progress" if new_attempts < 3 else "manual_follow_up"
            lead_outcome = "no_answer"
        elif ended_reason in ("customer-ended-call", "assistant-ended-call"):
            queue_status = "manual_follow_up"
            lead_outcome = "manual"
        else:
            queue_status = "in_progress" if new_attempts < 3 else "manual_follow_up"
            lead_outcome = "no_answer"

        update_data = {
            "queue_status":   queue_status,
            "lead_outcome":   lead_outcome,
            "call_attempts":  new_attempts,
            "last_called_at": datetime.utcnow().isoformat(),
            "updated_at":     datetime.utcnow().isoformat(),
            "notes": f"[auto-fallback] {ended_reason}. {summary}" if summary else f"[auto-fallback] {ended_reason}",
        }

        success = await supabase_update_lead(lead_id, update_data)
        logger.info("[%s] vapi-webhook fallback update lead_id=%s status=%s outcome=%s attempts=%s success=%s",
                    rid, lead_id, queue_status, lead_outcome, new_attempts, success)

        await _insert_call_log(
            rid=rid,
            lead_id=lead_id,
            vapi_call_id=call_id,
            call_status=ended_reason,
            duration_seconds=duration_seconds,
            call_type=call_type_log,
            call_direction="outbound",
            outcome=lead_outcome,
            notes=summary if summary else f"[auto-fallback] {ended_reason}",
        )

        return JSONResponse(content={"ok": True})

    except Exception as e:
        logger.exception("Error in /vapi-webhook: %s", e)
        return JSONResponse(content={"ok": True})
