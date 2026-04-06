import httpx
from datetime import datetime
from uuid import uuid4

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.config import (
    SUPABASE_URL,
    SUPABASE_HEADERS,
    VAPI_REMINDER_ASSISTANT_ID,
)
from app.core.logger import logger
from app.models.requests import UpdateLeadStatusRequest, inline_schema_refs
from app.services.supabase_service import (
    supabase_update_lead,
    supabase_insert_scheduled_callback,
    _insert_call_log,
)
from app.utils.parser import build_vapi_response

router = APIRouter(tags=["Leads"])


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

        logger.info("[%s] vapi-webhook end-of-call-report call_id=%s assistant=%s ended=%s lead_id=%s duration=%s",
                    rid, call_id, assistant_id, ended_reason, lead_id, duration_seconds)

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
