"""Inbound multi-provider (v2): create without a lead (caller ID + confirm gate), lookup formatting,
reschedule (same / other clinician, create-new-then-delete-old, idempotent), cancel ownership + confirm.
No network: Tebra, Supabase and the slot search are faked."""
import asyncio
import json

import pytest

from app.api import booking_v2 as api
from app.api import booking_v2_inbound as inb
from app.utils.availability_engine import Slot
from app.utils.time_utils import la_to_utc

REF = {
    "clinics": [{"tebra_location_id": 7, "name": "Dana Point", "lead_location_label": "Dana Point"}],
    "clinicians": [
        {"tebra_provider_id": 3114, "full_name": "Clay Simons", "bookable": True, "active": True, "gender": "male"},
        {"tebra_provider_id": 3200, "full_name": "Sarah Young", "bookable": True, "active": True, "gender": "female"},
    ],
    "services": [
        {"tebra_provider_id": 3114, "tebra_location_id": 7, "service_line": "physical_therapy"},
        {"tebra_provider_id": 3200, "tebra_location_id": 7, "service_line": "physical_therapy"},
    ],
    "hours": [],
}
CALLER = "+15550001111"
OLD_TID = "40001"
OLD_START = la_to_utc("2026-11-03", 9, 0)


def _open(pid, name):
    return [Slot(pid, name, "2026-11-10", m, 60) for m in (9 * 60, 10 * 60)]


OPEN = {3114: _open(3114, "Clay Simons"), 3200: _open(3200, "Sarah Young")}


class _Req:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


def _call(fn, args, caller=CALLER, call_type="inboundPhoneCall"):
    body = {"message": {"call": {"type": call_type, "customer": {"number": caller}},
                        "toolCalls": [{"id": "t", "function": {"arguments": args}}]}}
    resp = asyncio.run(fn(_Req(body)))
    return json.loads(resp.body)["results"][0]["result"]


@pytest.fixture
def fake(monkeypatch):
    st = {"creates": [], "deleted": set(), "inserts": [], "updates": [], "sms": [], "lead_updates": 0,
          "exclude": [], "sb": {OLD_TID: {"id": "sb-old", "tebra_appointment_id": OLD_TID, "status": "scheduled",
                                         "patient_phone": CALLER, "patient_name": "Test Inboundmp",
                                         "visit_type": "evaluation", "tebra_provider_id": 3114,
                                         "provider_name": "Clay Simons", "location": "Dana Point",
                                         "service": "Physical Therapy", "lead_id": None}},
          "booked": None}
    api._PENDING_CONFIRMS.clear()
    inb._RESCHEDULED.clear()

    async def ref(rid="?"):
        return REF

    async def slots(ref_, clinic, pids, duration, d0, d1, gender=None, extra_bookings=None, rid="?", exclude_tebra_ids=None):
        st["exclude"].append(exclude_tebra_ids)
        return [s for p in pids for s in OPEN.get(p, []) if d0 <= s.date <= d1]

    async def live(pid, date_s, rid="?"):
        # the caller's own old visit is in the live rows; it must not block the move
        return {"ok": True, "error": None, "rows": [{"tebra_appointment_id": int(OLD_TID), "status": "scheduled",
                                                     "start_at": OLD_START.isoformat(), "end_at": OLD_START.isoformat()}]}

    async def get_appt(tid, rid="?"):
        if tid in st["deleted"] or tid != OLD_TID:
            return None
        return {"AppointmentId": tid, "PatientId": "555", "ServiceLocationId": "7",
                "StartTime": OLD_START.strftime("%Y-%m-%dT%H:%M:%SZ")}

    async def delete(tid, rid="?"):
        st["deleted"].add(tid)
        return {"success": True, "error": None}

    async def details(tid, la_date=None, rid="?", live_cache=None):
        return {"tebra_appointment_id": tid, "provider_id": 3114, "location_id": 7, "start_utc": OLD_START,
                "end_utc": OLD_START, "duration": 60, "status": "scheduled", "patient_id": "555", "source": "cache"}

    async def by_tebra(tid, rid="?"):
        return st["sb"].get(str(tid))

    async def child(old_id, rid="?"):
        return None

    async def patients(first, last, rid="?"):
        if (first, last) == ("Test", "Inboundmp"):
            return {"ok": True, "error": None, "patients": [{"id": "555", "phones": ["5550001111"]}]}
        return {"ok": True, "error": None, "patients": [{"id": "777", "phones": ["9998887777"]}]}

    async def create(patient_id, loc, date_s, time_s, reason, rid, provider_id=None, duration_minutes=None, notes=None):
        st["creates"].append({"patient": patient_id, "pid": provider_id, "date": date_s, "time": time_s,
                              "minutes": duration_minutes, "deleted_before": OLD_TID in st["deleted"]})
        return {"success": True, "appointment_id": f"9900{len(st['creates'])}", "error": None}

    async def insert_appt(row):
        st["inserts"].append(row)
        h, m = map(int, row["appointment_time"].split(":"))
        st["booked"] = {"id": f"sb-{len(st['inserts'])}", "appointment_time": row["appointment_time"] + ":00",
                        "visit_type": row["visit_type"], "provider_name": row["provider_name"],
                        "tebra_appointment_id": row["tebra_appointment_id"], "tebra_patient_id": "555", "_start": h * 60 + m}
        return {"id": f"sb-{len(st['inserts'])}"}

    async def update_appt(appt_id, data):
        st["updates"].append((appt_id, data))
        return True

    async def recent(lead_id, phone, pid, date_s, start_min, minutes, rid="?"):
        b = st["booked"]
        return b if b and b["_start"] == start_min else None

    async def resolve_patient(first, last, phone, gender, rid="?"):
        return {"ok": True, "patient_id": "555", "created": False, "error": None}

    async def sms(**kw):
        st["sms"].append(kw)

    async def lead_update(*a, **k):
        st["lead_updates"] += 1
        return True

    async def noop(*a, **k):
        return 0

    for mod in (api.svc,):
        monkeypatch.setattr(mod, "load_reference", ref)
        monkeypatch.setattr(mod, "open_slots_for", slots)
        monkeypatch.setattr(mod, "live_provider_rows", live)
        monkeypatch.setattr(mod, "appointment_details", details)
        monkeypatch.setattr(mod, "fetch_appointment_by_tebra", by_tebra)
        monkeypatch.setattr(mod, "fetch_rescheduled_child", child)
        monkeypatch.setattr(mod, "find_recent_booking", recent)
        monkeypatch.setattr(mod, "resolve_patient", resolve_patient)
        monkeypatch.setattr(mod, "update_lead", lead_update)
    monkeypatch.setattr(api.calendar_store, "insert_tebra_appointment", noop)
    monkeypatch.setattr(api.calendar_store, "delete_tebra_appointment", noop)
    for mod in (api, inb):
        monkeypatch.setattr(mod, "create_appointment_in_tebra", create)
        monkeypatch.setattr(mod, "supabase_insert_appointment", insert_appt)
        monkeypatch.setattr(mod, "supabase_update_appointment", update_appt)
        monkeypatch.setattr(mod, "send_appointment_sms_if_needed", sms)
    monkeypatch.setattr(inb, "call_tebra_get_appointment", get_appt)
    monkeypatch.setattr(inb, "call_tebra_delete_appointment", delete)
    monkeypatch.setattr(inb, "find_patients_by_name", patients)
    return st


# ─── create (no lead) ────────────────────────────────────────────────────────

def _create(**extra):
    args = {"name": "Test Inboundmp", "phone": "+19490000000", "location": "Dana Point", "service": "Physical Therapy",
            "visit_type": "evaluation", "clinician_id": 3200, "date": "2026-11-10", "time": "09:00", **extra}
    return _call(api.create_appointment_v2, args)


def test_inbound_create_without_lead_confirms_then_books_with_caller_id(fake):
    out = _create()
    assert out.startswith("CONFIRM FIRST") and "Sarah Young" in out and not fake["creates"]
    out = _create(confirmed=True)
    assert out.startswith("BOOKED") and len(fake["creates"]) == 1 and fake["creates"][0]["pid"] == 3200
    row = fake["inserts"][0]
    assert row["patient_phone"] == CALLER          # caller ID, not the spoken phone arg
    assert "lead_id" not in row and fake["lead_updates"] == 0
    assert fake["sms"] == []                         # +1555 test number: no SMS


def test_inbound_create_confirm_is_keyed_by_caller_phone(fake):
    _create()
    out = _call(api.create_appointment_v2, {"name": "Test Inboundmp", "location": "Dana Point", "clinician_id": 3200,
                                            "date": "2026-11-10", "time": "09:00", "confirmed": True},
                caller="+15550009999")
    assert out.startswith("CONFIRM FIRST") and not fake["creates"]


def test_check_availability_inbound_wording(monkeypatch, fake):
    out = _call(api.check_availability_v2, {"location": "Dana Point", "service": "Rx Massage"})
    assert out.startswith("HANDOFF") and "update_inbound_status" in out and "update_lead" not in out


# ─── lookup ──────────────────────────────────────────────────────────────────

def test_lookup_reply_lists_clinician_clinic_and_ids():
    items = [
        {"tebra_appointment_id": "40001", "appointment_id": "sb-old", "date": "2026-11-03", "start_min": 540,
         "clinician_id": 3114, "clinician": "Clay Simons", "clinic": "Dana Point", "visit_type": "evaluation"},
        {"tebra_appointment_id": "40002", "appointment_id": None, "date": "2026-11-05", "start_min": 870,
         "clinician_id": 3200, "clinician": "Sarah Young", "clinic": "Dana Point", "visit_type": "follow_up"},
    ]
    out = inb.format_lookup_reply(items, 90)
    assert out.startswith("Found 2 upcoming appointments:")
    assert "(1) Tuesday, November 3 at 9:00 AM with Clay Simons at our Dana Point clinic (Evaluation)" in out
    assert "(2) Thursday, November 5 at 2:30 PM with Sarah Young" in out
    assert "tebra_appointment_id=40001 clinician_id=3114" in out and "time=14:30" in out
    assert "appointment_id=sb-old" in out
    assert inb.format_lookup_reply([], 90).startswith("NO APPOINTMENTS")


def test_lookup_endpoint_uses_details(monkeypatch, fake):
    async def by_patient(pid, start, end, rid="?", **k):
        return {"ok": True, "appointments": [{"tebra_appointment_id": OLD_TID, "appointment_date": "2099-11-03",
                                              "appointment_time_24hr": "09:00", "confirmation_status": "Scheduled"}]}
    monkeypatch.setattr(inb, "call_tebra_get_appointments_by_patient_id", by_patient)
    out = _call(inb.inbound_lookup_appointments_v2, {"patient_full_name": "Test Inboundmp"})
    assert out.startswith("Found 1 upcoming appointment:") and "Clay Simons" in out and "Dana Point" in out
    assert f"tebra_appointment_id={OLD_TID}" in out


# ─── reschedule ──────────────────────────────────────────────────────────────

def _resched(**extra):
    args = {"tebra_appointment_id": OLD_TID, "new_date": "2026-11-10", "new_time": "09:00",
            "patient_full_name": "Test Inboundmp", **extra}
    return _call(inb.reschedule_appointment_v2, args)


def test_reschedule_same_clinician_by_default(fake):
    out = _resched()
    assert out.startswith("CONFIRM FIRST") and "Clay Simons" in out and not fake["creates"]
    assert fake["exclude"][-1] == [OLD_TID]          # their own old visit doesn't block the new time
    out = _resched(confirmed=True)
    assert out.startswith("RESCHEDULED") and "Clay Simons" in out
    c = fake["creates"][0]
    assert c["pid"] == 3114 and c["patient"] == "555" and c["minutes"] == 60
    assert c["deleted_before"] is False and OLD_TID in fake["deleted"]   # new first, then old deleted
    assert ("sb-old", fake["updates"][0][1]) and fake["updates"][0][1]["status"] == "rescheduled"
    new_row = fake["inserts"][0]
    assert new_row["rescheduled_from_id"] == "sb-old" and new_row["tebra_provider_id"] == 3114
    # retry is idempotent: no second create
    out = _resched(confirmed=True)
    assert out.startswith("RESCHEDULED") and "repeat" in out and len(fake["creates"]) == 1


def test_reschedule_to_other_clinician(fake):
    _resched(clinician_id=3200, new_time="10:00")
    out = _resched(clinician_id=3200, new_time="10:00", confirmed=True)
    assert out.startswith("RESCHEDULED") and "Sarah Young" in out
    assert fake["creates"][0]["pid"] == 3200 and fake["creates"][0]["time"] == "10:00"


def test_reschedule_confirm_for_other_slot_does_not_carry_over(fake):
    _resched()
    out = _resched(clinician_id=3200, confirmed=True)
    assert out.startswith("CONFIRM FIRST") and not fake["creates"]


def test_reschedule_not_open_offers_alternatives(fake):
    out = _resched(new_time="13:00", confirmed=True)
    assert out.startswith("NOT AVAILABLE") and not fake["creates"]


def test_reschedule_rejects_someone_elses_visit(fake):
    fake["sb"][OLD_TID]["patient_phone"] = "+19998887777"
    out = _call(inb.reschedule_appointment_v2, {"tebra_appointment_id": OLD_TID, "new_date": "2026-11-10",
                                                "new_time": "09:00", "patient_full_name": "Other Person"})
    assert out.startswith("NOT FOUND") and not fake["creates"]


# ─── cancel ──────────────────────────────────────────────────────────────────

def test_cancel_ownership_check(fake):
    fake["sb"][OLD_TID]["patient_phone"] = "+19998887777"     # our row isn't the caller's phone
    out = _call(inb.inbound_cancel_appointment_v2, {"tebra_appointment_id": OLD_TID, "patient_full_name": "Other Person"})
    assert out.startswith("NOT FOUND") and not fake["deleted"]
    out = _call(inb.inbound_cancel_appointment_v2, {"tebra_appointment_id": OLD_TID})   # no name, phone mismatch
    assert out.startswith("NOT FOUND") and not fake["deleted"]


def test_cancel_confirm_then_cancel_then_idempotent(fake):
    out = _call(inb.inbound_cancel_appointment_v2, {"tebra_appointment_id": OLD_TID, "patient_full_name": "Test Inboundmp"})
    assert out.startswith("CONFIRM FIRST") and "Clay Simons" in out and not fake["deleted"]
    out = _call(inb.inbound_cancel_appointment_v2, {"tebra_appointment_id": OLD_TID, "confirmed": True})  # phone matches row
    assert out.startswith("CANCELLED") and OLD_TID in fake["deleted"]
    assert fake["updates"][-1][1]["status"] == "cancelled" and fake["sms"] == []
    fake["sb"][OLD_TID]["status"] = "cancelled"
    out = _call(inb.inbound_cancel_appointment_v2, {"tebra_appointment_id": OLD_TID, "confirmed": True})
    assert out.startswith("CANCELLED") and "already" in out
