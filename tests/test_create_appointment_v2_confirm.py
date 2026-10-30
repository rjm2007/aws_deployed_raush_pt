"""create-appointment v2 two-step confirm (sim MP11 booked on "I'll take the second one" before "Shall I book that?").
First call only returns CONFIRM FIRST (no Tebra call); a second call with confirmed=true for the SAME slot books.
No network: Supabase, Tebra and the slot search are faked."""
import asyncio
import json

import pytest

from app.api import booking_v2 as api
from app.utils.availability_engine import Slot

REF = {
    "clinics": [{"tebra_location_id": 7, "name": "Dana Point", "lead_location_label": "Dana Point"}],
    "clinicians": [{"tebra_provider_id": 3114, "full_name": "Clay Simons", "bookable": True, "active": True, "gender": "male"}],
    "services": [{"tebra_provider_id": 3114, "tebra_location_id": 7, "service_line": "physical_therapy"}],
    "hours": [],
}
OPEN = [Slot(3114, "Clay Simons", "2026-11-03", 9 * 60, 60), Slot(3114, "Clay Simons", "2026-11-03", 10 * 60, 60)]


class _Req:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


@pytest.fixture
def fake(monkeypatch):
    state = {"tebra_calls": 0, "creates": 0, "booked": None}
    api._PENDING_CONFIRMS.clear()

    async def ref(rid="?"):
        return REF

    async def slots(ref_, clinic, pids, duration, d0, d1, gender=None, extra_bookings=None, rid="?"):
        return [s for s in OPEN if d0 <= s.date <= d1]

    async def recent(lead_id, phone, pid, date_s, start_min, minutes, rid="?"):
        b = state["booked"]
        return b if b and b["_start"] == start_min else None

    async def live(pid, date_s, rid="?"):
        state["tebra_calls"] += 1
        return {"ok": True, "rows": [], "error": None}

    async def patient(first, last, phone, gender, rid="?"):
        state["tebra_calls"] += 1
        return {"ok": True, "patient_id": "555", "created": True, "error": None}

    async def create(patient_id, loc, date_s, time_s, reason, rid, provider_id=None, duration_minutes=None, notes=None):
        state["tebra_calls"] += 1
        state["creates"] += 1
        return {"success": True, "appointment_id": "99001", "error": None}

    async def insert_cache(row, rid="?"):
        return None

    async def insert_appt(row):
        h, m = map(int, row["appointment_time"].split(":"))
        state["booked"] = {"id": "sb-1", "appointment_time": row["appointment_time"] + ":00", "visit_type": row["visit_type"],
                           "provider_name": row["provider_name"], "tebra_appointment_id": "99001",
                           "tebra_patient_id": "555", "_start": h * 60 + m}
        return {"id": "sb-1"}

    async def noop(*a, **k):
        return True

    monkeypatch.setattr(api.svc, "load_reference", ref)
    monkeypatch.setattr(api.svc, "open_slots_for", slots)
    monkeypatch.setattr(api.svc, "find_recent_booking", recent)
    monkeypatch.setattr(api.svc, "live_provider_rows", live)
    monkeypatch.setattr(api.svc, "resolve_patient", patient)
    monkeypatch.setattr(api.svc, "update_lead", noop)
    monkeypatch.setattr(api, "create_appointment_in_tebra", create)
    monkeypatch.setattr(api.calendar_store, "insert_tebra_appointment", insert_cache)
    monkeypatch.setattr(api, "supabase_insert_appointment", insert_appt)
    monkeypatch.setattr(api, "send_appointment_sms_if_needed", noop)
    return state


def _book(time="09:00", **extra):
    args = {"name": "MPTEST Jordan Lee", "phone": "+15550001111", "location": "Dana Point", "service": "Physical Therapy",
            "visit_type": "evaluation", "clinician_id": 3114, "date": "2026-11-03", "time": time, **extra}
    resp = asyncio.run(api.create_appointment_v2(_Req({"message": {"toolCalls": [{"id": "t", "function": {"arguments": args}}]}})))
    return json.loads(resp.body)["results"][0]["result"]


def test_first_call_asks_to_confirm_without_tebra(fake):
    out = _book()
    assert out.startswith("CONFIRM FIRST") and "Shall I book that?" in out and "Clay Simons" in out
    assert fake["tebra_calls"] == 0 and fake["creates"] == 0


def test_second_call_with_confirmed_books(fake):
    _book()
    out = _book(confirmed=True)
    assert out.startswith("BOOKED") and fake["creates"] == 1


def test_confirmed_without_pending_asks_first(fake):
    out = _book(confirmed="true")
    assert out.startswith("CONFIRM FIRST") and fake["creates"] == 0
    assert _book(confirmed="true").startswith("BOOKED")   # now the pending exists


def test_changed_time_needs_new_confirm(fake):
    _book("09:00")
    out = _book("10:00", confirmed=True)
    assert out.startswith("CONFIRM FIRST") and "10:00 AM" in out and fake["creates"] == 0


def test_repeat_after_booked_is_idempotent(fake):
    _book()
    _book(confirmed=True)
    out = _book(confirmed=True)
    assert out.startswith("BOOKED") and fake["creates"] == 1
    out = _book()   # even without confirmed, the same booking comes back
    assert out.startswith("BOOKED") and fake["creates"] == 1
