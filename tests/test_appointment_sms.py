"""Appointment texts: friendly template, hold-until-end-of-call planning, queue vs send-now, end-of-call flush.
No network: Supabase / Twilio helpers are faked."""
import asyncio
import json

import pytest

from app.services import appointment_sms_service as sms
from app.api import booking_v2 as api

CALL = "+1 (877) 283-2944"


# ─── template ────────────────────────────────────────────────────────────────

def test_friendly_date_and_time():
    assert sms.friendly_date("2026-12-02") == "Wednesday, December 2, 2026"
    assert sms.friendly_time("07:00:00") == "7:00 AM"
    assert sms.friendly_time("13:30") == "1:30 PM"
    assert sms.friendly_time("00:15") == "12:15 AM"
    assert sms.friendly_time("12:00") == "12:00 PM"
    assert sms.friendly_time("2:30 PM") == "2:30 PM"      # already friendly → unchanged
    assert sms.friendly_date("") == ""


def test_booked_text_v2_row():
    body = sms.build_appointment_sms("sms_appointment_booked", {
        "patient_name": "Test Smsflow", "appointment_date": "2026-12-02", "appointment_time": "07:00:00",
        "location": "Laguna Niguel", "provider_name": "Clay Simons", "patient_phone": "+19495550000"})
    assert "Hi Test, your appointment is booked." in body
    assert "When: Wednesday, December 2, 2026 at 7:00 AM" in body
    assert "With: Clay Simons" in body
    assert "Where: Laguna Niguel" in body
    assert body.endswith(f"To reschedule or cancel, call us at {CALL}.")
    assert "reply" not in body.lower()
    assert "2026-12-02" not in body and "07:00:00" not in body
    assert len(body) <= 320


def test_v1_row_without_provider_and_headers():
    appt = {"patient_name": "", "appointment_date": "2026-12-02", "appointment_time": "09:30", "location": "Dana Point"}
    booked = sms.build_appointment_sms("sms_appointment_booked", appt)
    assert "With:" not in booked and booked.splitlines()[1] == "Your appointment is booked."
    assert "has been rescheduled" in sms.build_appointment_sms("sms_appointment_rescheduled", appt)
    cancelled = sms.build_appointment_sms("sms_appointment_cancelled", appt)
    assert "has been cancelled" in cancelled
    assert cancelled.endswith(f"To book a new time, call us at {CALL}.")


# ─── planning from the final state ───────────────────────────────────────────

def _p(aid, typ):
    return {"appointment_id": aid, "notification_type": typ}


def test_plan_booked_still_scheduled():
    plan = sms.plan_call_notifications([_p("A", sms.BOOKED)], {"A": {"id": "A", "status": "scheduled"}})
    assert plan == [_p("A", sms.BOOKED)]


def test_plan_booked_then_moved_sends_one_booked_for_final():
    pending = [_p("A", sms.BOOKED), _p("B", sms.RESCHEDULED), _p("C", sms.RESCHEDULED)]
    appts = {"A": {"status": "rescheduled"}, "B": {"status": "rescheduled", "rescheduled_from_id": "A"},
             "C": {"status": "scheduled", "rescheduled_from_id": "B"}}
    assert sms.plan_call_notifications(pending, appts) == [_p("C", sms.BOOKED)]


def test_plan_booked_then_cancelled_sends_nothing():
    pending = [_p("A", sms.BOOKED), _p("A", sms.CANCELLED)]
    assert sms.plan_call_notifications(pending, {"A": {"status": "cancelled"}}) == []


def test_plan_booked_moved_then_cancelled_sends_nothing():
    pending = [_p("A", sms.BOOKED), _p("B", sms.RESCHEDULED), _p("B", sms.CANCELLED)]
    appts = {"A": {"status": "rescheduled"}, "B": {"status": "cancelled", "rescheduled_from_id": "A"}}
    assert sms.plan_call_notifications(pending, appts) == []


def test_plan_existing_visit_cancelled():
    assert sms.plan_call_notifications([_p("X", sms.CANCELLED)], {"X": {"status": "cancelled"}}) == [_p("X", sms.CANCELLED)]


def test_plan_existing_visit_cancel_failed_sends_nothing():
    assert sms.plan_call_notifications([_p("X", sms.CANCELLED)], {"X": {"status": "scheduled"}}) == []


def test_plan_existing_visit_moved():
    appts = {"X": {"status": "rescheduled"}, "Y": {"status": "scheduled", "rescheduled_from_id": "X"}}
    assert sms.plan_call_notifications([_p("Y", sms.RESCHEDULED)], appts) == [_p("Y", sms.RESCHEDULED)]


def test_plan_existing_visit_moved_then_cancelled_cancels_original():
    pending = [_p("Y", sms.RESCHEDULED), _p("Y", sms.CANCELLED)]
    appts = {"X": {"status": "rescheduled"}, "Y": {"status": "cancelled", "rescheduled_from_id": "X"}}
    assert sms.plan_call_notifications(pending, appts) == [_p("X", sms.CANCELLED)]


def test_plan_missing_row_is_skipped():
    assert sms.plan_call_notifications([_p("A", sms.BOOKED)], {}) == []


# ─── queue vs send now ───────────────────────────────────────────────────────

@pytest.fixture
def fake_io(monkeypatch):
    st = {"inserted": [], "sent": [], "patched": [], "log": [], "appts": {}, "existing": []}

    async def insert(row):
        row = {"id": f"log-{len(st['log']) + 1}", **row}
        st["inserted"].append(row)
        st["log"].append(row)
        return row

    async def send(**kw):
        st["sent"].append(kw)
        if kw.get("log_row_id"):
            st["patched"].append((kw["log_row_id"], "sent"))
        return True

    async def claim(call_id):
        rows = [r for r in st["log"] if r.get("vapi_call_id") == call_id and r["status"] == "pending"]
        for r in rows:
            r["status"] = "processing"
        return rows

    async def fetch(ids):
        return {i: st["appts"][i] for i in ids if i in st["appts"]}

    async def patch(row_id, data, only_status=None):
        st["patched"].append((row_id, data.get("status")))
        return [{"id": row_id}]

    class _Resp:
        status_code = 200

        def __init__(self, rows):
            self._rows = rows

        def json(self):
            return self._rows

    class _Client:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url, headers=None):
            return _Resp(st["existing"])

    monkeypatch.setattr(sms, "supabase_insert_notification_log", insert)
    monkeypatch.setattr(sms, "send_appointment_sms_if_needed", send)
    monkeypatch.setattr(sms, "_claim_pending", claim)
    monkeypatch.setattr(sms, "_fetch_appointments", fetch)
    monkeypatch.setattr(sms, "_patch_log", patch)
    monkeypatch.setattr(sms.httpx, "AsyncClient", _Client)
    return st


def _notify(typ, aid="A", call="call-1"):
    return asyncio.run(sms.notify_appointment_sms(
        rid="t", appointment_id=aid, notification_type=typ,
        appt={"patient_phone": "+19490000000", "location": "Dana Point"}, vapi_call_id=call))


def test_direct_request_sends_now(fake_io):
    assert _notify(sms.BOOKED, call=None)
    assert len(fake_io["sent"]) == 1 and fake_io["inserted"] == []


def test_vapi_call_queues_pending_without_sending(fake_io):
    assert _notify(sms.BOOKED)
    assert fake_io["sent"] == []
    row = fake_io["inserted"][0]
    assert row["status"] == "pending" and row["vapi_call_id"] == "call-1" and row["sent_at"] is None


def test_vapi_retry_does_not_queue_twice(fake_io):
    fake_io["existing"] = [{"id": "log-1"}]
    assert _notify(sms.BOOKED)
    assert fake_io["inserted"] == [] and fake_io["sent"] == []


def test_flush_sends_one_text_for_final_visit(fake_io):
    _notify(sms.BOOKED, aid="A")
    _notify(sms.RESCHEDULED, aid="B")
    fake_io["appts"] = {"A": {"id": "A", "status": "rescheduled"},
                        "B": {"id": "B", "status": "scheduled", "rescheduled_from_id": "A", "patient_phone": "+19490000000"}}
    out = asyncio.run(sms.flush_pending_for_call("call-1", "t"))
    assert out == {"pending": 2, "sent": 1, "skipped": 1}
    assert len(fake_io["sent"]) == 1
    sent = fake_io["sent"][0]
    assert sent["appointment_id"] == "B" and sent["notification_type"] == sms.BOOKED and sent["log_row_id"] == "log-2"
    assert ("log-1", "skipped") in fake_io["patched"]
    # second flush (scheduler after webhook): nothing left to claim → no double send
    assert asyncio.run(sms.flush_pending_for_call("call-1", "t"))["pending"] == 0
    assert len(fake_io["sent"]) == 1


def test_flush_booked_then_cancelled_sends_nothing(fake_io):
    _notify(sms.BOOKED, aid="A")
    _notify(sms.CANCELLED, aid="A")
    fake_io["appts"] = {"A": {"id": "A", "status": "cancelled"}}
    out = asyncio.run(sms.flush_pending_for_call("call-1", "t"))
    assert out["sent"] == 0 and out["skipped"] == 2 and fake_io["sent"] == []


# ─── v2 create passes the Vapi call id through ───────────────────────────────

def test_vapi_call_id_only_for_tool_calls():
    tool = {"message": {"call": {"id": "abc", "type": "outboundPhoneCall"}, "toolCalls": [{"id": "t"}]}}
    assert api._vapi_call_id(tool) == "abc"
    assert api._vapi_call_id({"message": {"call": {"id": "abc"}}}) is None
    assert api._vapi_call_id({"name": "x"}) is None
    assert api._vapi_call_id({"message": {"call": {}, "toolCalls": [{"id": "t"}]}}) is None
