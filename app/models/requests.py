"""
Pydantic request models for all API endpoints.

These are used ONLY for Swagger / OpenAPI documentation and "Try it out".
Every endpoint still accepts the raw VAPI tool-call wrapper format too —
the dual-format parsing logic in each endpoint is unchanged.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


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
    phone: str = Field(..., example="9491234567", description="Patient phone number")
    location: str = Field(..., example="Dana Point", description="Clinic location name")
    service: Optional[str] = Field(None, example="evaluation", description="Service name")
    lead_id: Optional[str] = Field(None, description="Supabase lead UUID")


class UpdateAppointmentStatusRequest(BaseModel):
    tebra_appointment_id: str = Field(..., example="33463", description="Tebra appointment ID (integer as string)")
    new_status: AppointmentStatus = Field(..., description="New appointment status in Tebra")
    appointment_id: Optional[str] = Field(None, description="Supabase appointment UUID (also updates local DB if provided)")


class InboundLookupAppointmentsRequest(BaseModel):
    patient_full_name: str = Field(
        ...,
        example="Jane Doe",
        description="Patient full name — must match the booked appointment (case-insensitive, whitespace normalized)",
    )
    date: str = Field(
        ...,
        example="2026-04-04",
        description="The patient's current appointment date (YYYY-MM-DD)",
    )
    time: str = Field(
        ...,
        example="7:00 PM",
        description="The patient's current appointment time (HH:MM 24h or natural e.g. 7 PM)",
    )
    timezone_offset_from_gmt: Optional[int] = Field(
        None,
        example=7,
        description="Tebra fallback only: override TEBRA_TIMEZONE_OFFSET_FROM_GMT from env",
    )


class RescheduleAppointmentRequest(BaseModel):
    tebra_appointment_id: Optional[str] = Field(
        None,
        example="33463",
        description="Old Tebra appointment ID — omit if using resolve_* (current appointment name + date + time)",
    )
    resolve_patient_full_name: Optional[str] = Field(
        None,
        description="With resolve_appointment_date + resolve_appointment_time: find the appointment to reschedule (Supabase then Tebra)",
    )
    resolve_appointment_date: Optional[str] = Field(
        None,
        example="2026-04-04",
        description="Current appointment date YYYY-MM-DD (must match resolve_patient_full_name + resolve_appointment_time)",
    )
    resolve_appointment_time: Optional[str] = Field(
        None,
        example="7:00 PM",
        description="Current appointment time (HH:MM or natural); must resolve to exactly one booking",
    )
    appointment_id: Optional[str] = Field(
        None,
        description="Supabase UUID of the old row — optional if Tebra id exists in Supabase or for inbound-only Tebra reschedule",
    )
    new_date: str = Field(..., example="2026-04-16", description="New date in YYYY-MM-DD format")
    new_time: str = Field(..., example="10:00", description="New time in HH:MM 24-hour format")
    location: Optional[str] = Field(None, example="Dana Point", description="New clinic location (defaults to same)")
    service: Optional[str] = Field(None, example="evaluation", description="New service name (defaults to same)")
    lead_id: Optional[str] = Field(None, description="Supabase lead UUID")
    patient_name: Optional[str] = Field(
        None,
        description="When no Supabase row: stored on the new appointment row",
    )
    patient_phone: Optional[str] = Field(
        None,
        description="When no Supabase row: optional phone for the new appointment row",
    )
    timezone_offset_from_gmt: Optional[int] = Field(
        None,
        description="When using resolve_* fields: Tebra lookup timezone offset (same as inbound-lookup-appointments)",
    )


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


class InboundCallEventRequest(BaseModel):
    call_id: str = Field(..., description="Unique inbound call ID from provider (used for idempotency)")
    crm_status: QueueStatus = Field(..., description="Inbound CRM status")
    caller_phone: Optional[str] = Field(None, example="9491234567", description="Inbound caller phone number")
    called_number: Optional[str] = Field(None, example="9495550000", description="Clinic destination number")
    lead_id: Optional[str] = Field(None, description="Supabase lead UUID for status mirroring")
    appointment_id: Optional[str] = Field(None, description="Supabase appointment UUID when linked")
    vapi_call_id: Optional[str] = Field(None, description="VAPI call id (if available)")
    call_status: Optional[str] = Field(None, example="answered", description="Telephony provider call status")
    started_at: Optional[str] = Field(None, example="2026-04-07T10:15:00Z", description="Call start timestamp (ISO-8601)")
    ended_at: Optional[str] = Field(None, example="2026-04-07T10:20:30Z", description="Call end timestamp (ISO-8601)")
    duration_seconds: Optional[int] = Field(None, example=330, description="Call duration in seconds")
    route: Optional[str] = Field(None, example="appointment_lookup", description="Inbound route selected by IVR/agent")
    disposition: Optional[str] = Field(None, example="resolved", description="Inbound call disposition")
    lead_outcome: Optional[LeadOutcome] = Field(None, description="Optional lead outcome to mirror")
    notes: Optional[str] = Field(None, description="Operator/agent notes for this inbound call")
