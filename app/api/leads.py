import json
import re
from datetime import datetime
from uuid import uuid4

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.config import (
    VAPI_API_KEY,
    VAPI_INBOUND_ASSISTANT_ID,
    VAPI_LEAD_ASSISTANT_ID,
    VAPI_REMINDER_ASSISTANT_ID,
)
from app.core.logger import logger
from app.models.requests import UpdateLeadStatusRequest, InboundCallEventRequest
from app.services.supabase_service import (
    supabase_update_lead,
    supabase_insert_scheduled_callback,
    supabase_upsert_inbound_call,
    supabase_fetch_inbound_call_by_call_id,
    supabase_fetch_latest_inbound_by_caller_number,
    supabase_update_inbound_call_by_id,
    supabase_fetch_lead_by_id,
    _insert_call_log,
)
from app.utils.parser import build_vapi_response

router = APIRouter(tags=["Leads"])

# Dedupe PATCH /call/{id} per call when many status-update events arrive.
_vapi_inbound_vars_patched: set[str] = set()
_VAPI_PATCH_IDS_CAP = 4000

_INBOUND_ALLOWED_CRM_STATUSES = {
    "manual_follow_up",
    "complete",
}

_OUTBOUND_ASSISTANT_IDS = {
    VAPI_LEAD_ASSISTANT_ID,
    VAPI_REMINDER_ASSISTANT_ID,
}

_DIGIT_WORD_MAP = {
    "zero": "0",
    "oh": "0",
    "o": "0",
    "one": "1",
    "two": "2",
    "to": "2",
    "too": "2",
    "three": "3",
    "four": "4",
    "for": "4",
    "five": "5",
    "six": "6",
    "seven": "7",
    "eight": "8",
    "ate": "8",
    "nine": "9",
}

_BAD_CALL_ID_LITERALS = {
    "current_call",
    "currentcall",
    "call_id",
    "callid",
    "test",
    "test_call",
    "sample",
    "unknown",
}

_BAD_NAME_LITERALS = {
    "if available",
    "unknown",
    "n/a",
    "na",
    "none",
    "null",
    "caller",
    "patient",
}


def _is_unresolved_template(value: str | None) -> bool:
    if value is None:
        return False
    s = str(value).strip()
    if not s:
        return False
    return "{{" in s and "}}" in s


def _is_plausible_call_id(value: str | None) -> bool:
    s = str(value or "").strip()
    if not s:
        return False
    if _is_unresolved_template(s):
        return False
    if s.lower() in _BAD_CALL_ID_LITERALS:
        return False
    # Reject obvious junk/testing ids that create duplicate noise rows.
    if len(s) < 8:
        return False
    if re.fullmatch(r"\d+", s):
        return False
    return True


def _normalize_call_id(primary: str | None, fallback: str | None) -> str:
    p = str(primary or "").strip()
    f = str(fallback or "").strip()
    if _is_plausible_call_id(p):
        return p
    if _is_plausible_call_id(f):
        return f
    return p or f


def _is_placeholder_name(value: str | None) -> bool:
    s = " ".join(str(value or "").strip().lower().split())
    if not s:
        return True
    if s in _BAD_NAME_LITERALS:
        return True
    if s.startswith("if ") and "available" in s:
        return True
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


def _extract_phone_from_text(text: str | None) -> str | None:
    if not text:
        return None

    # Numeric forms, e.g. "(949) 555-1212" or "949 555 1212".
    for match in re.findall(r"(?:\+?1[\s\-\.]*)?(?:\(?\d[\d\s\-\(\)\.]{8,}\d)", str(text)):
        phone = _normalize_phone(match)
        if phone:
            return phone

    # Spoken digit forms, e.g. "eight eight five zero...".
    tokens = re.findall(r"[a-zA-Z]+", str(text).lower())
    run = ""
    for token in tokens:
        digit = _DIGIT_WORD_MAP.get(token)
        if digit is None:
            if len(run) >= 10:
                return _normalize_phone(run)
            run = ""
            continue
        run += digit
        if len(run) >= 10:
            phone = _normalize_phone(run)
            if phone:
                return phone

    if len(run) >= 10:
        return _normalize_phone(run)
    return None


def _extract_name_from_text(text: str | None) -> str | None:
    if not text:
        return None
    t = " ".join(str(text).strip().split())
    if not t:
        return None

    patterns = [
        r"(?:my\s+name\s+is|i\s+am|this\s+is)\s+([A-Za-z][A-Za-z\-']{1,30}(?:\s+[A-Za-z][A-Za-z\-']{1,30}){0,3})",
        r"name\s*[:,-]?\s*([A-Za-z][A-Za-z\-']{1,30}(?:\s+[A-Za-z][A-Za-z\-']{1,30}){0,3})",
    ]
    for pat in patterns:
        m = re.search(pat, t, flags=re.IGNORECASE)
        if not m:
            continue
        candidate = " ".join(m.group(1).split())
        candidate = re.sub(r"\s+(and\s+my|and|my)$", "", candidate, flags=re.IGNORECASE).strip()
        candidate = re.sub(r"\s+(mobile|phone|number)$", "", candidate, flags=re.IGNORECASE).strip()
        lowered = candidate.lower()
        if lowered in ("unknown", "n a", "na", "none"):
            continue
        if len(candidate) < 2:
            continue
        return candidate
    return None


def _collect_text_fragments(value, limit: int = 80) -> list[str]:
    out: list[str] = []

    def _walk(v):
        if len(out) >= limit:
            return
        if isinstance(v, str):
            s = v.strip()
            if s:
                out.append(s)
            return
        if isinstance(v, dict):
            for item in v.values():
                _walk(item)
                if len(out) >= limit:
                    return
            return
        if isinstance(v, list):
            for item in v:
                _walk(item)
                if len(out) >= limit:
                    return

    _walk(value)
    return out


def _extract_phone_from_payload(payload: dict | None) -> str | None:
    if not isinstance(payload, dict):
        return None

    direct_priority_keys = (
        "caller_number",
        "caller_phone",
        "patient_phone",
        "mobile_number",
        "phone_number",
        "customer_number",
    )
    for key in direct_priority_keys:
        value = payload.get(key)
        phone = _normalize_phone(str(value)) if value is not None else None
        if phone:
            return phone

    for text in _collect_text_fragments(payload):
        phone = _extract_phone_from_text(text)
        if phone:
            return phone
    return None


def _extract_name_from_payload(payload: dict | None) -> str | None:
    if not isinstance(payload, dict):
        return None

    for key in ("caller_name", "patient_name", "name", "customer_name"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip() and not _is_unresolved_template(value):
            normalized = " ".join(value.strip().split())
            if not _is_placeholder_name(normalized):
                return normalized

    for text in _collect_text_fragments(payload):
        found = _extract_name_from_text(text)
        if found:
            return found
    return None


def _derive_transcript_summary(report: dict, ended_reason: str) -> str:
    def _is_low_value_text(text: str) -> bool:
        t = (text or "").strip().lower()
        if not t:
            return True
        # Vapi call IDs look like "call_XYgIufoXpME3L9rcEci0yaoY" — filter them out.
        if re.match(r"^call_[a-zA-Z0-9]{10,}$", (text or "").strip()):
            return True
        boilerplate_markers = (
            "thanks for calling rausch physical therapy",
            "which one would you like to do today",
            "i can help you reschedule",
            "how can i help you",
        )
        return any(marker in t for marker in boilerplate_markers)

    summary = (report.get("summary") or "").strip()
    if summary.lower().startswith("http://") or summary.lower().startswith("https://"):
        summary = ""
    if summary and not _is_low_value_text(summary):
        return summary

    analysis = report.get("analysis") if isinstance(report.get("analysis"), dict) else {}
    analysis_summary = str(analysis.get("summary") or "").strip()
    if analysis_summary and not _is_low_value_text(analysis_summary):
        return analysis_summary

    for text in _collect_text_fragments(report, limit=120):
        if len(text) < 20:
            continue
        if _is_low_value_text(text):
            continue
        lowered = text.lower()
        if lowered in ("book a new appointment", "reschedule appointment", "reschedule", "new appointment"):
            continue
        if any(skip in lowered for skip in ("tool call", "request started", "request ended", "assistant id")):
            continue
        compact = " ".join(text.split())
        if len(compact) > 300:
            compact = compact[:297] + "..."
        return compact

    return f"Inbound call ended with reason: {ended_reason or 'unknown'}"


def _extract_tool_args(body: dict) -> tuple[str | None, dict]:
    """Return (tool_call_id, args_dict) for either VAPI wrapper or direct JSON payload."""
    tool_call_id = (
        body.get("toolCallId")
        or body.get("tool_call_id")
        or (body.get("message", {}).get("toolCallId") if isinstance(body.get("message"), dict) else None)
        or (body.get("message", {}).get("tool_call_id") if isinstance(body.get("message"), dict) else None)
    )
    args: dict = body if isinstance(body, dict) else {}

    def _parse_args(raw_args) -> dict:
        if isinstance(raw_args, str):
            try:
                parsed = json.loads(raw_args)
                return parsed if isinstance(parsed, dict) else {}
            except Exception:
                return {}
        if isinstance(raw_args, dict):
            return raw_args
        return {}

    def _read_tool_call_from_list(tool_calls_value) -> tuple[str | None, dict] | None:
        if not (isinstance(tool_calls_value, list) and tool_calls_value):
            return None
        tc = tool_calls_value[0] if isinstance(tool_calls_value[0], dict) else {}
        tc_id = (
            tc.get("id")
            or tc.get("toolCallId")
            or tc.get("tool_call_id")
            or (tc.get("call", {}).get("id") if isinstance(tc.get("call"), dict) else None)
        )
        fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
        tc_args = _parse_args(fn.get("arguments", tc.get("arguments", {})))
        return tc_id, tc_args

    # Top-level Vapi shapes: {toolCalls:[...]}, {toolCallList:[...]}, or {toolCall:{...}}
    top_level_tool_calls = body.get("toolCalls") or body.get("toolCallList")
    parsed_top = _read_tool_call_from_list(top_level_tool_calls)
    if parsed_top:
        tc_id, tc_args = parsed_top
        return tool_call_id or tc_id, tc_args

    top_level_tool_call = body.get("toolCall") if isinstance(body.get("toolCall"), dict) else None
    if top_level_tool_call:
        tc_id = top_level_tool_call.get("id") or top_level_tool_call.get("toolCallId")
        fn = top_level_tool_call.get("function") if isinstance(top_level_tool_call.get("function"), dict) else {}
        tc_args = _parse_args(fn.get("arguments", top_level_tool_call.get("arguments", {})))
        return tool_call_id or tc_id, tc_args

    msg = body.get("message") if isinstance(body, dict) else None
    if isinstance(msg, dict):
        # Vapi server messages use `toolCalls`; SDK/types also expose `toolCallList`.
        tool_calls = (
            msg.get("toolCalls")
            or msg.get("toolCallList")
            or body.get("toolCalls")
            or body.get("toolCallList")
        )
        parsed_msg = _read_tool_call_from_list(tool_calls)
        if parsed_msg:
            tc_id, tc_args = parsed_msg
            return tool_call_id or tc_id, tc_args

        # Alternate Vapi shape used by some runners.
        tool_call = msg.get("toolCall") if isinstance(msg.get("toolCall"), dict) else None
        if tool_call:
            tool_call_id = tool_call_id or tool_call.get("id") or tool_call.get("toolCallId")
            fn = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
            args = _parse_args(fn.get("arguments", tool_call.get("arguments", {})))
            return tool_call_id, args

    # Top-level arguments fallback shape.
    if isinstance(body, dict) and "arguments" in body:
        args = _parse_args(body.get("arguments"))

    if not tool_call_id and isinstance(body, dict):
        # Some runtimes place id under `call.id` for tool events.
        call_obj = body.get("call") if isinstance(body.get("call"), dict) else {}
        tool_call_id = call_obj.get("id") or call_obj.get("toolCallId")

    return tool_call_id, args


def _augment_inbound_args_from_vapi_body(args: dict, body: dict) -> dict:
    """Backfill inbound args from Vapi wrapper payload when function args are incomplete."""
    out = dict(args or {})
    msg = body.get("message") if isinstance(body, dict) else {}
    if not isinstance(msg, dict):
        msg = {}

    call_obj = msg.get("call") if isinstance(msg.get("call"), dict) else {}
    top_call_obj = body.get("call") if isinstance(body.get("call"), dict) else {}
    report = msg if msg.get("type") == "end-of-call-report" else {}

    overrides = call_obj.get("assistantOverrides") if isinstance(call_obj.get("assistantOverrides"), dict) else {}
    variable_values = overrides.get("variableValues") if isinstance(overrides.get("variableValues"), dict) else {}

    # Critical idempotency key for inbound_calls upsert.
    out.setdefault(
        "call_id",
        call_obj.get("id")
        or top_call_obj.get("id")
        or msg.get("callId")
        or report.get("call", {}).get("id"),
    )
    if _is_unresolved_template(out.get("call_id")):
        out["call_id"] = (
            call_obj.get("id")
            or top_call_obj.get("id")
            or msg.get("callId")
            or report.get("call", {}).get("id")
        )

    out["call_id"] = _normalize_call_id(out.get("call_id"), None)

    # caller_number: always read from inbound caller ID — never ask the caller for their number.
    cust = msg.get("customer") if isinstance(msg.get("customer"), dict) else {}
    out.setdefault(
        "caller_number",
        cust.get("number")
        or cust.get("phoneNumber")
        or call_obj.get("from")
        or call_obj.get("fromNumber")
        or call_obj.get("customer", {}).get("number")
        or msg.get("from")
        or msg.get("fromNumber")
        or variable_values.get("from_number"),
    )
    if not out.get("caller_number"):
        out["caller_number"] = _extract_phone_from_payload(msg) or _extract_phone_from_payload(body)
    out["caller_number"] = _normalize_phone(out.get("caller_number")) or out.get("caller_number")

    out.setdefault(
        "caller_name",
        variable_values.get("caller_name")
        or variable_values.get("patient_name")
        or variable_values.get("name")
        or msg.get("customer", {}).get("name")
        or call_obj.get("customer", {}).get("name"),
    )
    if _is_unresolved_template(out.get("caller_name")) or not out.get("caller_name"):
        out["caller_name"] = (
            _extract_name_from_payload(variable_values)
            or _extract_name_from_payload(call_obj.get("customer") if isinstance(call_obj.get("customer"), dict) else None)
            or _extract_name_from_payload(msg)
            or _extract_name_from_payload(body)
        )

    out.setdefault("appointment_id", variable_values.get("appointment_id"))
    out.setdefault("route", variable_values.get("route") or variable_values.get("intent"))
    out.setdefault("location", variable_values.get("location"))
    out.setdefault("started_at", msg.get("startedAt") or call_obj.get("startedAt"))
    out.setdefault("ended_at", msg.get("endedAt") or call_obj.get("endedAt"))

    if not out.get("notes"):
        ended_reason = str(msg.get("endedReason") or "")
        out["notes"] = _derive_transcript_summary(msg, ended_reason)

    return out


def _resolve_call_direction(assistant_id: str, variable_values: dict, call_obj: dict) -> str:
    """Best-effort direction resolver for webhook reports."""
    for key in ("call_direction", "direction", "callDirection"):
        val = variable_values.get(key)
        if isinstance(val, str):
            v = val.strip().lower()
            if v in ("inbound", "outbound"):
                return v

    monitor = call_obj.get("monitor") if isinstance(call_obj.get("monitor"), dict) else {}
    for source in (call_obj, monitor):
        val = source.get("direction") if isinstance(source, dict) else None
        if isinstance(val, str):
            v = val.strip().lower()
            if v in ("inbound", "outbound"):
                return v

    return "outbound" if assistant_id in _OUTBOUND_ASSISTANT_IDS else "inbound"


def _derive_inbound_crm_status(ended_reason: str) -> str:
    """For inbound webhook fallback, keep unresolved/sudden cut calls in manual follow-up."""
    r = (ended_reason or "").strip().lower()
    if r in ("assistant-ended-call", "customer-ended-call", "completed"):
        return "complete"
    if r in ("silence-timed-out", "error", "failed", "hangup"):
        return "manual_follow_up"
    if r == "customer-did-not-answer":
        return "manual_follow_up"
    return "manual_follow_up"


def _context_indicates_completed_inbound(report: dict, variable_values: dict) -> bool:
    # Strong signal: appointment_id already captured in call variables.
    if variable_values.get("appointment_id"):
        return True

    combined = " ".join(_collect_text_fragments(report, limit=140)).lower()
    completion_markers = (
        "appointment has been booked",
        "your appointment has been booked",
        "you are all set",
        "booked at",
        "rescheduled",
        "booking confirmed",
    )
    return any(marker in combined for marker in completion_markers)


def _pick_best_notes(existing: str | None, derived: str | None, fallback: str) -> str:
    """
    Choose the best notes value when the webhook fires after the tool call has already run.
    Prefers model-written summary over webhook-derived text; falls back to derived or fallback.
    """
    def _is_auto_generated(text: str | None) -> bool:
        if not text:
            return True
        t = text.strip()
        if re.match(r"^call_[a-zA-Z0-9]{10,}$", t):
            return True
        if t.lower().startswith("inbound call ended with reason"):
            return True
        if t.lower().startswith("[auto-") or t.lower().startswith("[auto "):
            return True
        return False

    # If existing notes are meaningful (written by model), keep them.
    if existing and not _is_auto_generated(existing):
        return existing
    # Fall back to derived if it looks meaningful.
    if derived and not _is_auto_generated(derived):
        return derived
    return fallback


async def _process_inbound_event(args: dict, tool_call_id: str | None, rid: str, source: str) -> JSONResponse:
    """Shared inbound persistence path for tool route + webhook fallback."""
    call_id = str(args.get("call_id") or "").strip()
    if _is_unresolved_template(call_id):
        fallback_call_id = str(args.get("vapi_call_id") or "").strip()
        call_id = "" if _is_unresolved_template(fallback_call_id) else fallback_call_id
    call_id = _normalize_call_id(call_id, args.get("vapi_call_id"))

    # call_id is no longer required from the model — the backend fills it from the Vapi body.
    # If still empty after augmentation (direct/test call with no body), generate a UUID fallback.
    if not _is_plausible_call_id(call_id):
        call_id = str(uuid4())
        logger.warning("[%s] call_id missing/implausible — using generated id=%s (real calls always have it from Vapi body)", rid, call_id)

    crm_status = str(args.get("crm_status") or "").strip()
    appointment_id = args.get("appointment_id")

    # Webhook path derives its own crm_status (always complete/manual_follow_up); skip validation for it.
    if source != "webhook" and crm_status not in _INBOUND_ALLOWED_CRM_STATUSES:
        return build_vapi_response(
            tool_call_id,
            "crm_status must be 'complete' (appointment confirmed) or 'manual_follow_up' (unresolved). "
            "Call this tool exactly once at the very end of the conversation.",
        )

    started_at = args.get("started_at")
    ended_at = args.get("ended_at")
    duration_seconds = args.get("duration_seconds")
    if duration_seconds is None and started_at and ended_at:
        try:
            s = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
            e = datetime.fromisoformat(str(ended_at).replace("Z", "+00:00"))
            duration_seconds = int((e - s).total_seconds())
        except Exception:
            duration_seconds = None

    now_iso = datetime.utcnow().isoformat()
    # Prefer model-generated summary (new field), fall back to legacy notes/manual_summary.
    notes_value = args.get("summary") or args.get("notes") or args.get("manual_summary")
    if isinstance(notes_value, str):
        normalized_notes = " ".join(notes_value.strip().split())
        if normalized_notes.lower() in (
            "book a new appointment",
            "new appointment",
            "reschedule appointment",
            "reschedule",
        ):
            notes_value = None
        else:
            notes_value = normalized_notes
    existing_inbound = await supabase_fetch_inbound_call_by_call_id(call_id)

    caller_name_value = args.get("caller_name") or _extract_name_from_payload(args)
    if _is_placeholder_name(caller_name_value):
        caller_name_value = None
    if not caller_name_value and existing_inbound:
        existing_name = existing_inbound.get("caller_name")
        caller_name_value = None if _is_placeholder_name(existing_name) else existing_name
    inbound_payload = {
        "call_id": call_id,
        "crm_status": crm_status,
        "caller_name": caller_name_value,
        "caller_number": _normalize_phone(args.get("caller_number") or args.get("caller_phone"))
        or args.get("caller_number")
        or args.get("caller_phone"),
        "appointment_id": appointment_id,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": duration_seconds,
        "route": args.get("route"),
        "location": args.get("location"),
        "notes": notes_value,
        "updated_at": now_iso,
    }
    inbound_payload = {k: v for k, v in inbound_payload.items() if v is not None}

    # ── Reschedule dedup: patch the caller's prior booking row instead of creating a new one ──
    # When route=reschedule and we have a caller_number, find the most recent complete row
    # for this number and update it in place so the CRM shows one card (not two).
    if inbound_payload.get("route") == "reschedule" and inbound_payload.get("caller_number"):
        prior_row = await supabase_fetch_latest_inbound_by_caller_number(inbound_payload["caller_number"])
        if prior_row:
            patch_data = {
                "route": "reschedule",
                "updated_at": now_iso,
            }
            for field in ("appointment_id", "location", "notes", "caller_name"):
                if inbound_payload.get(field):
                    patch_data[field] = inbound_payload[field]
            patched = await supabase_update_inbound_call_by_id(prior_row["id"], patch_data)
            if patched:
                logger.info(
                    "[%s] reschedule_merged | row_id=%s caller=%s from_route=%s -> reschedule | notes=%s",
                    rid, prior_row["id"], inbound_payload["caller_number"],
                    prior_row.get("route"), notes_value,
                )
                msg = f"Inbound call {call_id} saved with crm_status={crm_status}."
                if tool_call_id:
                    return build_vapi_response(tool_call_id, msg)
                return JSONResponse(
                    content={
                        "message": msg,
                        "inbound_call_id": prior_row["id"],
                        "call_id": call_id,
                        "crm_status": crm_status,
                    }
                )
            logger.warning("[%s] reschedule — patch failed, falling through to normal upsert", rid)

    inbound_row, inbound_err = await supabase_upsert_inbound_call(inbound_payload)
    if inbound_err == "missing_table":
        return build_vapi_response(
            tool_call_id,
            "inbound_calls table is missing in Supabase. Run the SQL migration first and retry.",
        )
    if inbound_err == "schema_mismatch":
        return build_vapi_response(
            tool_call_id,
            "inbound_calls schema does not match expected columns. Apply sql/2026-04-07_create_inbound_calls.sql and retry.",
        )
    if inbound_err:
        return build_vapi_response(
            tool_call_id,
            "Failed to persist inbound call event. Please check logs.",
        )

    msg = f"Inbound call {call_id} saved with crm_status={crm_status}."

    if source == "webhook":
        logger.info("[%s] inbound webhook persisted call_id=%s crm_status=%s", rid, call_id, crm_status)

    logger.info(
        "[%s] call_completed | call_id=%s crm_status=%s route=%s location=%s caller=%s notes=%s",
        rid, call_id, crm_status,
        inbound_payload.get("route"), inbound_payload.get("location"),
        inbound_payload.get("caller_name"), notes_value,
    )

    if tool_call_id:
        return build_vapi_response(tool_call_id, msg)

    return JSONResponse(
        content={
            "message": msg,
            "inbound_call_id": inbound_row.get("id") if inbound_row else None,
            "call_id": call_id,
            "crm_status": crm_status,
        }
    )


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT: UPDATE LEAD STATUS (Agent 1)
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/update-lead-status",
    summary="Update lead record with call outcome",
    openapi_extra={
        "requestBody": {
            "content": {"application/json": {"schema": UpdateLeadStatusRequest.model_json_schema()}},
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

        tool_call_id, args = _extract_tool_args(body)

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
        current_lead = await supabase_fetch_lead_by_id(lead_id)
        if not current_lead:
            return build_vapi_response(
                tool_call_id,
                f"Lead with id {lead_id} not found. Cannot update."
            )

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
        if callback_notes:
            update_data["callback_notes"] = callback_notes
        if tebra_patient_id:
            update_data["tebra_patient_id"] = tebra_patient_id
        if notes:
            update_data["notes"]          = notes

        # ── Insert scheduled callback if requested ──
        if callback_requested_at and lead_id:
            await supabase_insert_scheduled_callback({
                "lead_id":        lead_id,
                "scheduled_for":  callback_requested_at,
                "assistant_type": "lead",
                "notes":          callback_notes,
            })

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


@router.post(
    "/inbound-call-event",
    summary="Upsert inbound call and mirror CRM status to lead",
    openapi_extra={
        "requestBody": {
            "content": {"application/json": {"schema": InboundCallEventRequest.model_json_schema()}},
            "required": True,
        }
    },
)
async def inbound_call_event(request: Request):
    """
    Receives inbound call progress events and persists them into inbound_calls.
    Optionally mirrors CRM status into leads and writes inbound call_logs rows.
    """
    try:
        rid = str(uuid4())[:8]
        body = await request.json()
        logger.info("[%s] ======== inbound-call-event START ========", rid)
        logger.info("[%s] inbound-call-event body=%s", rid, body)

        tool_call_id, args = _extract_tool_args(body)
        logger.info("[%s] inbound-call-event tool_call_id=%s", rid, tool_call_id)
        args = _augment_inbound_args_from_vapi_body(args, body)
        return await _process_inbound_event(args, tool_call_id, rid, source="tool")

    except Exception as e:
        logger.exception("Error in /inbound-call-event: %s", e)
        return build_vapi_response(
            locals().get("tool_call_id"),
            "Sorry, there was an error handling the inbound call event. Please try again.",
        )


@router.post(
    "/update-inbound-status",
    summary="Tool endpoint to update inbound call status in CRM",
    openapi_extra={
        "requestBody": {
            "content": {"application/json": {"schema": InboundCallEventRequest.model_json_schema()}},
            "required": True,
        }
    },
)
async def update_inbound_status(request: Request):
    """Alias tool endpoint for inbound assistant status updates."""
    try:
        rid = str(uuid4())[:8]
        body = await request.json()
        logger.info("[%s] ======== update-inbound-status START ========", rid)
        logger.info("[%s] update-inbound-status body=%s", rid, body)

        tool_call_id, args = _extract_tool_args(body)
        logger.info("[%s] update-inbound-status tool_call_id=%s", rid, tool_call_id)
        args = _augment_inbound_args_from_vapi_body(args, body)
        return await _process_inbound_event(args, tool_call_id, rid, source="tool")
    except Exception as e:
        logger.exception("Error in /update-inbound-status: %s", e)
        return build_vapi_response(
            locals().get("tool_call_id"),
            "Sorry, there was an error updating inbound status. Please try again.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# VAPI: inject {{call_id}} when assistant is pinned on the number (no assistant-request)
# ─────────────────────────────────────────────────────────────────────────────


async def _patch_inbound_call_dynamic_variables(rid: str, body: dict) -> None:
    """
    If the inbound assistant is selected in the Vapi UI, assistant-request does not run.
    On status-update, PATCH the live call so prompts see {{call_id}} and {{call_direction}}.
    Requires VAPI_API_KEY and VAPI_INBOUND_ASSISTANT_ID (same UUID as the pinned assistant).
    """
    if not VAPI_API_KEY or not VAPI_INBOUND_ASSISTANT_ID:
        return
    msg = body.get("message") if isinstance(body.get("message"), dict) else {}
    if msg.get("type") != "status-update":
        return
    if msg.get("status") not in ("in-progress", "ringing"):
        return
    call_obj = msg.get("call") if isinstance(msg.get("call"), dict) else {}
    call_id = str(call_obj.get("id") or "").strip()
    assistant_raw = str(call_obj.get("assistantId") or "").strip()
    if not call_id or not _is_plausible_call_id(call_id):
        return
    if assistant_raw.lower() != VAPI_INBOUND_ASSISTANT_ID.lower():
        return

    global _vapi_inbound_vars_patched
    if call_id in _vapi_inbound_vars_patched:
        return
    _vapi_inbound_vars_patched.add(call_id)
    if len(_vapi_inbound_vars_patched) > _VAPI_PATCH_IDS_CAP:
        _vapi_inbound_vars_patched.clear()
        _vapi_inbound_vars_patched.add(call_id)

    url = f"https://api.vapi.ai/call/{call_id}"
    payload = {
        "assistantOverrides": {
            "variableValues": {
                "call_id": call_id,
                "call_direction": "inbound",
            }
        }
    }
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            r = await client.patch(
                url,
                headers={
                    "Authorization": f"Bearer {VAPI_API_KEY}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if r.status_code >= 400:
            logger.warning(
                "[%s] inbound PATCH variableValues failed status=%s body=%s",
                rid,
                r.status_code,
                (r.text or "")[:500],
            )
        else:
            logger.info("[%s] inbound PATCH variableValues ok call_id=%s", rid, call_id)
    except Exception as e:
        logger.warning("[%s] inbound PATCH variableValues error: %s", rid, e)


# ─────────────────────────────────────────────────────────────────────────────
# ENDPOINT: VAPI WEBHOOK (end-of-call fallback)
# ─────────────────────────────────────────────────────────────────────────────

@router.post(
    "/vapi-webhook",
    summary="VAPI server URL: inbound vars PATCH + assistant-request + end-of-call-report",
)
async def vapi_webhook(request: Request):
    """
    VAPI Server URL entrypoint.
    - status-update (inbound assistant): PATCH call variableValues for {{call_id}} when assistant is UI-pinned.
    - assistant-request: returns inbound assistant id + variableValues (when number has no fixed assistant).
    - end-of-call-report: inbound_calls upsert + outbound lead fallback.
    """
    try:
        body     = await request.json()
        msg_type = body.get("message", {}).get("type", "")
        rid      = str(uuid4())[:8]

        await _patch_inbound_call_dynamic_variables(rid, body)

        if msg_type == "assistant-request":
            msg      = body.get("message") if isinstance(body.get("message"), dict) else {}
            call_obj = msg.get("call") if isinstance(msg.get("call"), dict) else {}
            call_id  = str(call_obj.get("id") or "").strip()
            if not VAPI_INBOUND_ASSISTANT_ID:
                logger.error("[%s] assistant-request — VAPI_INBOUND_ASSISTANT_ID unset", rid)
                return JSONResponse(
                    content={
                        "error": "Server configuration error: inbound assistant id is not set.",
                    }
                )
            if not call_id:
                logger.warning("[%s] assistant-request — missing message.call.id", rid)
                return JSONResponse(
                    content={"error": "Could not read call id from telephony; please try again."}
                )
            payload = {
                "assistantId": VAPI_INBOUND_ASSISTANT_ID,
                "assistantOverrides": {
                    "variableValues": {
                        "call_id": call_id,
                        "call_direction": "inbound",
                    }
                },
            }
            logger.info(
                "[%s] assistant-request — inbound assistant=%s call_id=%s",
                rid,
                VAPI_INBOUND_ASSISTANT_ID,
                call_id,
            )
            return JSONResponse(content=payload)

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
        call_direction  = _resolve_call_direction(assistant_id, variable_values, call_obj)

        logger.info("[%s] vapi-webhook end-of-call-report call_id=%s assistant=%s direction=%s ended=%s lead_id=%s duration=%s",
                    rid, call_id, assistant_id, call_direction, ended_reason, lead_id, duration_seconds)

        # Inbound fallback persistence: ensure sudden-cut calls still land in inbound_calls.
        if call_direction == "inbound":
            inbound_crm_status = (
                "complete"
                if _context_indicates_completed_inbound(report, variable_values)
                else _derive_inbound_crm_status(ended_reason)
            )
            derived_notes = _derive_transcript_summary(report, ended_reason)

            existing_inbound = await supabase_fetch_inbound_call_by_call_id(call_id)
            if existing_inbound and existing_inbound.get("crm_status") == "complete" and inbound_crm_status != "complete":
                # Never downgrade a completed inbound call from fallback webhook.
                inbound_crm_status = "complete"

            inbound_args = {
                "call_id": call_id,
                "crm_status": inbound_crm_status,
                "appointment_id": variable_values.get("appointment_id")
                or (existing_inbound.get("appointment_id") if existing_inbound else None),
                "caller_name": variable_values.get("caller_name")
                or variable_values.get("patient_name")
                or variable_values.get("name")
                or call_obj.get("customer", {}).get("name")
                or (existing_inbound.get("caller_name") if existing_inbound else None),
                "caller_number": variable_values.get("caller_number")
                or variable_values.get("patient_phone")
                or variable_values.get("mobile_number")
                or variable_values.get("phone")
                or variable_values.get("from_number")
                or call_obj.get("from")
                or call_obj.get("fromNumber")
                or call_obj.get("customer", {}).get("number")
                or (existing_inbound.get("caller_number") if existing_inbound else None),
                "started_at": started_at,
                "ended_at": ended_at,
                "duration_seconds": duration_seconds,
                "route": variable_values.get("route") or variable_values.get("intent"),
                "location": variable_values.get("location")
                or (existing_inbound.get("location") if existing_inbound else None),
                "notes": _pick_best_notes(
                    existing=(existing_inbound.get("notes") if existing_inbound else None),
                    derived=derived_notes[:500] if derived_notes else None,
                    fallback=f"Inbound call ended with reason: {ended_reason}",
                ),
            }
            await _process_inbound_event(inbound_args, None, rid, source="webhook")
            return JSONResponse(content={"ok": True, "direction": "inbound", "crm_status": inbound_crm_status})

        if not lead_id or (isinstance(lead_id, str) and lead_id.startswith("{{")):
            logger.info("[%s] vapi-webhook — no valid lead_id, skipping", rid)
            return JSONResponse(content={"ok": True})

        # ── Check if lead was already updated by the tool call ──
        lead = await supabase_fetch_lead_by_id(lead_id)
        if not lead:
            logger.warning("[%s] vapi-webhook — lead %s not found", rid, lead_id)
            return JSONResponse(content={"ok": True})

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
                notes=summary[:500] if summary else None,
            )
            return JSONResponse(content={"ok": True})

        # Lead is still in_progress or new — the tool call never ran
        old_attempts = lead.get("call_attempts") or 0
        new_attempts = old_attempts + 1

        if ended_reason == "customer-did-not-answer":
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
            "notes": f"[auto-fallback] {ended_reason}. {summary[:200]}" if summary else f"[auto-fallback] {ended_reason}",
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
            notes=summary[:500] if summary else f"[auto-fallback] {ended_reason}",
        )

        return JSONResponse(content={"ok": True})

    except Exception as e:
        logger.exception("Error in /vapi-webhook: %s", e)
        return JSONResponse(content={"ok": True})
