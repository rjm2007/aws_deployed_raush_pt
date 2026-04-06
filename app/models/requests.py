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


class RescheduleAppointmentRequest(BaseModel):
    tebra_appointment_id: str = Field(..., example="33463", description="Old Tebra appointment ID")
    appointment_id: str = Field(..., description="Supabase appointment UUID of the old appointment")
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
