"""
Pydantic request models for all API endpoints.

These are used ONLY for Swagger / OpenAPI documentation and "Try it out".
Every endpoint still accepts the raw VAPI tool-call wrapper format too —
the dual-format parsing logic in each endpoint is unchanged.
"""

from __future__ import annotations

import copy
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


def inline_schema_refs(schema: dict) -> dict:
    """Resolve $defs/$ref so Swagger UI can render schemas in openapi_extra."""
    schema = copy.deepcopy(schema)
    defs = schema.pop("$defs", {})

    def _resolve(obj):
        if isinstance(obj, dict):
            if "$ref" in obj and obj["$ref"].startswith("#/$defs/"):
                name = obj["$ref"][len("#/$defs/"):]
                if name in defs:
                    return _resolve(copy.deepcopy(defs[name]))
            return {k: _resolve(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_resolve(item) for item in obj]
        return obj

    return _resolve(schema)


# ─────────────────────────────────────────────────────────────────────────────
# ENUMS
# ─────────────────────────────────────────────────────────────────────────────

class AppointmentStatus(str, Enum):
    Confirmed       = "Confirmed"
    NoShow          = "NoShow"
    Rescheduled     = "Rescheduled"
    Scheduled       = "Scheduled"
    CheckedIn       = "CheckedIn"
    CheckedOut      = "CheckedOut"
    NeedsReschedule = "NeedsReschedule"


class ConfirmOutcome(str, Enum):
    confirmed = "confirmed"
    cancelled = "cancelled"


class ReminderType(str, Enum):
    hr24 = "24hr"
    hr2  = "2hr"


class QueueStatus(str, Enum):
    new              = "new"
    in_progress      = "in_progress"
    complete         = "complete"
    not_interested   = "not_interested"
    follow_up        = "follow_up"
    manual_follow_up = "manual_follow_up"


class LeadOutcome(str, Enum):
    booked          = "booked"
    not_interested  = "not_interested"
    no_answer       = "no_answer"
    callback        = "callback"
    manual          = "manual"

class InboundCrmStatus(str, Enum):
    in_progress      = "in_progress"
    follow_up        = "follow_up"
    manual_follow_up = "manual_follow_up"
    complete         = "complete"


# ─────────────────────────────────────────────────────────────────────────────
# AVAILABILITY
# ─────────────────────────────────────────────────────────────────────────────

class CheckAvailabilityRequest(BaseModel):
    date: str = Field(..., example="2026-04-15", description="Date in YYYY-MM-DD format")
    location: str = Field(..., example="Dana Point", description="Clinic location name")
    service: Optional[str] = Field(None, example="evaluation", description="Service name")
    time: Optional[str] = Field(None, example="09:00", description="Time in HH:MM 24-hour format (optional — omit to get all slots)")


# ─────────────────────────────────────────────────────────────────────────────
# APPOINTMENTS
# ─────────────────────────────────────────────────────────────────────────────

class CreateAppointmentRequest(BaseModel):
    date: str = Field(..., example="2026-04-15", description="Date in YYYY-MM-DD format")
    time: str = Field(..., example="09:00", description="Time in HH:MM 24-hour or natural format like '9 AM'")
    name: str = Field(..., example="John Smith", description="Patient full name")
    phone: Optional[str] = Field(
        None,
        example="+19491234567",
        description="Patient phone. Required for outbound (lead) bookings. For inbound (no lead_id), omit — API uses Vapi caller ID.",
    )
    location: str = Field(..., example="Dana Point", description="Clinic location name")
    service: Optional[str] = Field(None, example="evaluation", description="Service name")
    lead_id: Optional[str] = Field(None, description="Supabase lead UUID")


class UpdateAppointmentStatusRequest(BaseModel):
    tebra_appointment_id: str = Field(..., example="33463", description="Tebra appointment ID (integer as string)")
    new_status: AppointmentStatus = Field(..., description="New appointment status in Tebra")
    appointment_id: Optional[str] = Field(None, description="Supabase appointment UUID (also updates local DB if provided)")


class RescheduleAppointmentRequest(BaseModel):
    tebra_appointment_id: str = Field(..., example="33463", description="Old Tebra appointment ID")
    appointment_id: Optional[str] = Field(
        None,
        description="Supabase UUID of the old appointment when we have it (form/outbound bookings). "
        "Omit when the visit exists only in Tebra (e.g. staff booked at front desk); Tebra reschedule still runs and a new Supabase row can be created.",
    )
    new_date: str = Field(..., example="2026-04-16", description="New date in YYYY-MM-DD format")
    new_time: str = Field(..., example="10:00", description="New time in HH:MM 24-hour format")
    location: Optional[str] = Field(None, example="Dana Point", description="New clinic location (defaults to same)")
    service: Optional[str] = Field(None, example="evaluation", description="New service name (defaults to same)")
    lead_id: Optional[str] = Field(None, description="Supabase lead UUID")


class ConfirmAppointmentRequest(BaseModel):
    tebra_appointment_id: str = Field(..., example="33463", description="Tebra appointment ID")
    appointment_id: str = Field(..., description="Supabase appointment UUID")
    outcome: ConfirmOutcome = Field(..., description="Must be 'confirmed' or 'cancelled'")
    lead_id: Optional[str] = Field(None, description="Supabase lead UUID")
    notes: Optional[str] = Field(None, example="Patient confirmed appointment.", description="Call summary")
    reminder_type: ReminderType = Field(ReminderType.hr24, description="'24hr' or '2hr'")


class CancelAppointmentRequest(BaseModel):
    tebra_appointment_id: str = Field(..., example="33463", description="Tebra appointment ID")
    appointment_id: str = Field(..., description="Supabase appointment UUID")
    lead_id: Optional[str] = Field(None, description="Supabase lead UUID (updates lead to not_interested)")
    notes: Optional[str] = Field(None, example="Patient requested cancellation.", description="Cancellation reason / notes")


# ─────────────────────────────────────────────────────────────────────────────
# LEADS
# ─────────────────────────────────────────────────────────────────────────────

class UpdateLeadStatusRequest(BaseModel):
    lead_id: str = Field(..., description="Supabase lead UUID")
    queue_status: Optional[QueueStatus] = Field(None, description="New queue status")
    lead_outcome: Optional[LeadOutcome] = Field(None, description="Call outcome")
    callback_requested_at: Optional[str] = Field(None, example="2026-04-15T14:00:00Z", description="ISO datetime for callback")
    callback_notes: Optional[str] = Field(None, description="Callback notes")
    tebra_patient_id: Optional[str] = Field(None, description="Tebra patient ID")
    notes: Optional[str] = Field(None, description="Free-text notes")


# ─────────────────────────────────────────────────────────────────────────────
# INBOUND
# ─────────────────────────────────────────────────────────────────────────────

class UpdateInboundStatusRequest(BaseModel):
    crm_status: InboundCrmStatus = Field(..., description="Inbound CRM status for this call")
    summary: Optional[str] = Field(None, description="2–3 sentence recap of the call")
    notes: Optional[str] = Field(None, description="Alias of summary (either is ok)")
    route: Optional[str] = Field(None, example="reschedule", description="Inbound route label (e.g. new_appointment, reschedule)")
    caller_name: Optional[str] = Field(None, example="John Smith", description="Caller full name if known")
    appointment_id: Optional[str] = Field(None, description="Supabase appointment UUID when resolved")
    call_id: Optional[str] = Field(None, description="Optional. VAPI call id; backend can also infer it from wrapper payload")
    caller_number: Optional[str] = Field(None, example="9495551212", description="Optional. Caller phone digits (backend can infer it from wrapper payload)")


class InboundLookupAppointmentsRequest(BaseModel):
    patient_full_name: str = Field(
        ...,
        example="Jane Doe",
        description="Patient first and last name (spell both). Used with Tebra GetPatients, then GetAppointments by PatientID.",
    )
    selected_tebra_appointment_id: Optional[str] = Field(
        None,
        example="33735",
        description="Optional. If the list response was ambiguous, call again with the chosen tebra_appointment_id; otherwise use IDs from the list and go straight to reschedule_appointment.",
    )
    date: Optional[str] = Field(
        None,
        example="2026-04-15",
        description="Deprecated; ignored. Date range is server-side (today → +90 days Pacific).",
    )
    time: Optional[str] = Field(
        None,
        example="10:00",
        description="Deprecated; ignored.",
    )
    timezone_offset_from_gmt: Optional[int] = Field(
        None,
        description="Optional override for Tebra Filter TimeZoneOffsetFromGMT (default from server env).",
    )
    caller_number: Optional[str] = Field(
        None,
        example="9495551212",
        description="Ignored.",
    )


class InboundCallerLookupRequest(BaseModel):
    """Look up a caller in Supabase by phone number (inbound-agent first tool call)."""
    phone: Optional[str] = Field(
        None,
        example="+19495551212",
        description=(
            "Optional. Caller phone number. If omitted, the server reads caller ID from the Vapi webhook "
            "(message.call.customer.number). Use this field only for Swagger / manual testing."
        ),
    )
