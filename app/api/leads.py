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
    supabase_insert_notification_log,
    _insert_call_log,
)
from app.utils.parser import build_vapi_response
from app.services.twilio_service import twilio_send_sms

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

        logger.info("[%s] vapi-webhook end-of-call-report call_id=%s assistant=%s ended=%s lead_id=%s duration=%s",
                    rid, call_id, assistant_id, ended_reason, lead_id, duration_seconds)

        # ── SMS notification (Twilio) for successful appointment outcomes ──
        # This runs on end-of-call reports and is safe to skip if not configured.
        # Only send if we have an appointment_id and the call was actually connected (not no-answer).
        try:
            if (
                appointment_id
                and not (isinstance(appointment_id, str) and appointment_id.startswith("{{"))
                and ended_reason not in ("customer-did-not-answer", "silence-timed-out")
            ):
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
                            notification_type = "sms_appointment_rescheduled" if was_rescheduled else "sms_appointment_confirmation"
                            sms_body = (
                                "RAUSCH PHYSICAL THERAPY\n"
                                f"Your appointment has been {'rescheduled' if was_rescheduled else 'booked'}.\n\n"
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
                                "RAUSCH PHYSICAL THERAPY\n"
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
