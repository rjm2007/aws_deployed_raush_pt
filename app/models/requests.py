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


# ─────────────────────────────────────────────────────────────────────────────
# v2 — MULTI-PROVIDER BOOKING (/api/v1/v2/...)
# ─────────────────────────────────────────────────────────────────────────────

class VisitType(str, Enum):
    evaluation = "evaluation"
    follow_up  = "follow_up"


class GenderPreference(str, Enum):
    any    = "any"
    male   = "male"
    female = "female"


class CheckAvailabilityV2Request(BaseModel):
    location: Optional[str] = Field(None, example="Laguna Niguel", description="Lead clinic label (Laguna Niguel, Dana Point, Mission Viejo, Fort Fitness - Laguna Hills). Falls back to the lead's preferred_location.")
    service: Optional[str] = Field(None, example="Physical Therapy", description="Lead service_of_interest. Rx Massage / StretchFix / Chiropractic -> front-desk hand-off.")
    visit_type: Optional[VisitType] = Field(VisitType.evaluation, description="evaluation (60 min, default) or follow_up (45 min)")
    clinician_name: Optional[str] = Field(None, example="Sarah Young", description="Named clinician (fuzzy match). Leave empty for anyone / earliest.")
    gender: Optional[GenderPreference] = Field(None, description="Clinician gender preference (from the clinician table, never guessed)")
    date: Optional[str] = Field(None, example="2026-11-03", description="Specific day YYYY-MM-DD")
    time: Optional[str] = Field(None, example="09:00", description="Wanted start HH:MM (24h, clinic time)")
    date_from: Optional[str] = Field(None, example="2026-12-01", description="Range start YYYY-MM-DD (e.g. 'next month') -> first 2-3 open days")
    date_to: Optional[str] = Field(None, example="2026-12-31", description="Range end YYYY-MM-DD")
    part_of_day: Optional[str] = Field(None, example="morning", description="morning / afternoon / evening")
    lead_id: Optional[str] = Field(None, description="Supabase lead UUID (fills location/service; non-bookable service marks it for front desk)")


class CreateAppointmentV2Request(BaseModel):
    lead_id: Optional[str] = Field(None, description="Supabase lead UUID (outbound)")
    name: Optional[str] = Field(None, example="MPTEST Jane Doe", description="Patient first + last name (falls back to the lead)")
    phone: Optional[str] = Field(None, example="+15555550123", description="Patient phone (falls back to the lead)")
    location: Optional[str] = Field(None, example="Laguna Niguel", description="Clinic from the check_availability ids tag")
    service: Optional[str] = Field(None, example="Physical Therapy", description="Lead service_of_interest")
    visit_type: Optional[VisitType] = Field(VisitType.evaluation, description="evaluation (60) / follow_up (45)")
    clinician_id: int = Field(..., example=3708, description="Tebra provider ID from the ids tag")
    date: str = Field(..., example="2026-11-03", description="YYYY-MM-DD from the ids tag")
    time: str = Field(..., example="09:00", description="HH:MM from the ids tag")
    gender: Optional[str] = Field(None, description="Patient gender if they said it (male/female) — only used when creating a new patient")
    confirmed: Optional[bool] = Field(False, description="Two-step booking: the first call (without it) returns CONFIRM FIRST; "
                                                         "call again with true only after the caller's clear yes to the read-back")


class CancelAppointmentV2Request(BaseModel):
    tebra_appointment_id: Optional[str] = Field(None, example="37018", description="Tebra appointment ID")
    appointment_id: Optional[str] = Field(None, description="Supabase appointments UUID (alternative)")


class UpdateLeadV2Request(BaseModel):
    lead_id: str = Field(..., description="Supabase lead UUID")
    outcome: str = Field(..., example="do_not_call", description="do_not_call | not_interested | front_desk")
    reason: Optional[str] = Field(None, example="asked not to be called again", description="Short reason (stored in leads.metadata)")
    notes: Optional[str] = Field(None, description="Free-text note appended to leads.notes")


# ─────────────────────────────────────────────────────────────────────────────
# v2 — INBOUND MULTI-PROVIDER (/api/v1/v2/...). Caller phone = Vapi caller ID (phone arg only for Swagger).
# ─────────────────────────────────────────────────────────────────────────────

class InboundLookupAppointmentsV2Request(BaseModel):
    patient_full_name: str = Field(..., example="Test Inboundmp", description="Caller's first + last name (spelled). Tebra GetPatients; patients with the caller's phone first")
    selected_tebra_appointment_id: Optional[str] = Field(None, example="37018", description="Optional: return only this one appointment")
    phone: Optional[str] = Field(None, example="+15555550123", description="Only for Swagger / tests; on calls the Vapi caller ID is used")


class RescheduleAppointmentV2Request(BaseModel):
    tebra_appointment_id: str = Field(..., example="37018", description="Old visit, from the inbound_lookup_appointments_v2 ids tag")
    new_date: str = Field(..., example="2026-11-10", description="YYYY-MM-DD from the check_availability_v2 ids tag")
    new_time: str = Field(..., example="09:00", description="HH:MM (24h clinic time) from the check_availability_v2 ids tag")
    patient_full_name: Optional[str] = Field(None, example="Test Inboundmp", description="Caller's first + last name (ownership check)")
    clinician_id: Optional[int] = Field(None, example=3114, description="Tebra provider ID from the check_availability_v2 ids tag. Omit = same clinician as the old visit")
    location: Optional[str] = Field(None, example="Dana Point", description="Clinic from the ids tag. Omit = same clinic as the old visit")
    visit_type: Optional[VisitType] = Field(None, description="Omit = same as the old visit (evaluation 60 / follow_up 45)")
    service: Optional[str] = Field(None, example="Physical Therapy", description="Optional service (Rx Massage / StretchFix / Chiropractic -> front desk)")
    confirmed: Optional[bool] = Field(False, description="Two-step: first call returns CONFIRM FIRST; call again with true after the caller's clear yes")
    phone: Optional[str] = Field(None, description="Only for Swagger / tests; on calls the Vapi caller ID is used")


class InboundCancelAppointmentV2Request(BaseModel):
    tebra_appointment_id: str = Field(..., example="37018", description="Visit to cancel, from the inbound_lookup_appointments_v2 ids tag")
    patient_full_name: Optional[str] = Field(None, example="Test Inboundmp", description="Caller's first + last name (ownership check)")
    confirmed: Optional[bool] = Field(False, description="Two-step: first call returns CONFIRM FIRST; call again with true after the caller's clear yes")
    phone: Optional[str] = Field(None, description="Only for Swagger / tests; on calls the Vapi caller ID is used")
