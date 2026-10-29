"""update-lead-status: callback times are LA wall time; same-status repeats still save callback/notes.
vapi-webhook: a voicemail ending leaves a note on follow-up leads. No network: httpx + Supabase are faked."""
import asyncio
import json

from app.api import leads as leads_api
from app.utils.time_utils import callback_time_to_utc_iso


def test_callback_time_naive_is_la_wall_time():
    assert callback_time_to_utc_iso("2026-10-31T15:00:00") == "2026-10-31T22:00:00Z"   # PDT
    assert callback_time_to_utc_iso("2026-11-02 15:00") == "2026-11-02T23:00:00Z"      # PST
    assert callback_time_to_utc_iso("2026-10-31T15:00:00Z") == "2026-10-31T15:00:00Z"
    assert callback_time_to_utc_iso("2026-10-31T15:00:00-07:00") == "2026-10-31T22:00:00Z"
    assert callback_time_to_utc_iso("tomorrow at 3") is None
    assert callback_time_to_utc_iso(None) is None


class _Resp:
    def __init__(self, rows):
        self.status_code, self._rows = 200, rows

    def json(self):
        return self._rows


class _Client:
    def __init__(self, row):
        self.row = row

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, headers=None):
        return _Resp([self.row])


class _Req:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


def _setup(monkeypatch, row):
    writes = []

    async def fake_update(lead_id, data):
        writes.append(data)
        return True

    async def fake_log(**kw):
        writes.append({"call_log": kw})
        return True

    monkeypatch.setattr(leads_api.httpx, "AsyncClient", lambda *a, **k: _Client(row))
    monkeypatch.setattr(leads_api, "supabase_update_lead", fake_update)
    monkeypatch.setattr(leads_api, "_insert_call_log", fake_log)
    return writes


def _result(resp):
    return json.loads(resp.body)["results"][0]["result"]


def test_same_status_still_saves_callback(monkeypatch):
    writes = _setup(monkeypatch, {"id": "L1", "queue_status": "manual_follow_up", "call_attempts": 0})
    body = {"message": {"toolCalls": [{"id": "t1", "function": {"arguments": {
        "lead_id": "L1", "queue_status": "manual_follow_up", "lead_outcome": "manual",
        "callback_requested_at": "2026-10-31T15:00:00", "callback_notes": "Driving; call back tomorrow 3 PM"}}}]}}
    out = _result(asyncio.run(leads_api.update_lead_status(_Req(body))))
    assert out.startswith("Lead updated successfully")
    assert len(writes) == 1
    w = writes[0]
    assert w["callback_requested_at"] == "2026-10-31T22:00:00Z"
    assert w["callback_notes"] == "Driving; call back tomorrow 3 PM"
    assert "call_attempts" not in w and "queue_status" not in w


def test_same_status_without_details_is_still_skipped(monkeypatch):
    writes = _setup(monkeypatch, {"id": "L1", "queue_status": "complete", "lead_outcome": "booked"})
    body = {"message": {"toolCalls": [{"id": "t2", "function": {"arguments": {
        "lead_id": "L1", "queue_status": "complete", "lead_outcome": "booked"}}}]}}
    out = _result(asyncio.run(leads_api.update_lead_status(_Req(body))))
    assert out.startswith("Lead already updated") and writes == []


def test_new_status_with_callback_sets_utc(monkeypatch):
    writes = _setup(monkeypatch, {"id": "L1", "queue_status": "in_progress", "call_attempts": 1})
    asyncio.run(leads_api.update_lead_status(_Req({
        "lead_id": "L1", "queue_status": "manual_follow_up", "lead_outcome": "manual",
        "callback_requested_at": "2026-10-30T18:00:00", "notes": "Someone else answered"})))
    w = writes[0]
    assert w["callback_requested_at"] == "2026-10-31T01:00:00Z"
    assert w["call_attempts"] == 2 and w["notes"] == "Someone else answered"


def _eoc(ended):
    return {"message": {"type": "end-of-call-report", "endedReason": ended, "call": {
        "id": "c1", "assistantId": "a1", "type": "outboundPhoneCall",
        "assistantOverrides": {"variableValues": {"lead_id": "L1"}}}}}


def test_webhook_voicemail_notes_follow_up_lead(monkeypatch):
    writes = _setup(monkeypatch, {"id": "L1", "queue_status": "manual_follow_up", "notes": "form note"})
    asyncio.run(leads_api.vapi_webhook(_Req(_eoc("voicemail"))))
    assert writes[0]["notes"].startswith("form note | Reached voicemail")
    assert "queue_status" not in writes[0]
    assert writes[1]["call_log"]["outcome"] == "voicemail"


def test_webhook_voicemail_note_not_repeated(monkeypatch):
    writes = _setup(monkeypatch, {"id": "L1", "queue_status": "manual_follow_up", "notes": "Reached voicemail"})
    asyncio.run(leads_api.vapi_webhook(_Req(_eoc("voicemail"))))
    assert len(writes) == 1 and "call_log" in writes[0]


def test_webhook_voicemail_on_new_lead_uses_fallback(monkeypatch):
    writes = _setup(monkeypatch, {"id": "L1", "queue_status": "in_progress", "call_attempts": 0})
    asyncio.run(leads_api.vapi_webhook(_Req(_eoc("voicemail"))))
    assert writes[0]["queue_status"] == "in_progress" and "voicemail" in writes[0]["notes"]
