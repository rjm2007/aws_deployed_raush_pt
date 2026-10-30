"""Structured output "rausch_outbound_call_outcome": pure decision logic + /vapi-webhook wiring.
Tools stay the source of truth; the extraction only fills gaps (and always flags do-not-call). No network."""
import asyncio

from app.api import leads as leads_api
from app.utils.call_outcome import STRUCTURED_OUTPUT_NAME, decide_lead_update, extract_call_outcome


def _so(outcome, conf=0.95, dnc=False, **kw):
    return {"outcome": outcome, "do_not_call_requested": dnc, "booking_confirmed_by_caller": False,
            "reason": f"{outcome} reason", "confidence": conf, **kw}


NEW = {"id": "L1", "queue_status": "in_progress", "lead_outcome": None, "call_attempts": 0}


# ── extract ──

def test_extract_finds_ours_by_name():
    report = {"artifact": {"structuredOutputs": {
        "x1": {"name": "something_else", "result": {"outcome": "booked"}},
        "x2": {"name": STRUCTURED_OUTPUT_NAME, "result": _so("voicemail")}}}}
    assert extract_call_outcome(report)["outcome"] == "voicemail"


def test_extract_missing_or_null():
    assert extract_call_outcome({}) is None
    assert extract_call_outcome({"artifact": {"structuredOutputs": {"x": {"name": STRUCTURED_OUTPUT_NAME, "result": None}}}}) is None
    assert extract_call_outcome({"artifact": {"structuredOutputs": None}}) is None


# ── lead untouched by tools (in_progress / new) ──

def test_none_keeps_current_behaviour():
    assert decide_lead_update(NEW, None)["mode"] == "none"


def test_do_not_call_applied():
    d = decide_lead_update(NEW, _so("do_not_call", dnc=True))
    assert d["mode"] == "apply"
    assert d["data"] == {"queue_status": "not_interested", "lead_outcome": "not_interested"}
    assert d["metadata"]["do_not_call"] is True


def test_dnc_flag_wins_over_other_outcome():
    d = decide_lead_update(NEW, _so("hung_up_mid_booking", dnc=True))
    assert d["mode"] == "apply" and d["metadata"]["do_not_call"] is True


def test_not_interested_applied():
    d = decide_lead_update(NEW, _so("not_interested"))
    assert d["mode"] == "apply" and d["data"]["queue_status"] == "not_interested" and not d["metadata"]


def test_wrong_number_applied():
    d = decide_lead_update(NEW, _so("wrong_number"))
    assert d["mode"] == "apply"
    assert d["data"] == {"queue_status": "manual_follow_up", "lead_outcome": "manual"}
    assert d["metadata"] == {"wrong_number": True}


def test_other_outcomes_keep_fallback_with_note():
    for o in ("voicemail", "someone_else_answered", "callback_requested", "hung_up_mid_booking",
              "transfer_requested", "other", "no_conversation"):
        d = decide_lead_update(NEW, _so(o, callback_time_text="after six" if o == "callback_requested" else None))
        assert d["mode"] == "fallback" and not d["data"] and o in d["note"]
    assert "after six" in decide_lead_update(NEW, _so("callback_requested", callback_time_text="after six"))["note"]


def test_low_confidence_falls_back():
    d = decide_lead_update(NEW, _so("do_not_call", conf=0.6, dnc=True))
    assert d["mode"] == "fallback" and not d["data"] and not d["metadata"]


# ── lead already updated by tools ──

def test_tool_set_status_never_changed():
    lead = {"queue_status": "manual_follow_up", "lead_outcome": "manual"}
    d = decide_lead_update(lead, _so("not_interested"))
    assert d["mode"] == "log" and not d["data"]


def test_dnc_flag_on_tool_updated_lead():
    lead = {"queue_status": "manual_follow_up", "lead_outcome": "manual", "metadata": {}}
    d = decide_lead_update(lead, _so("do_not_call", conf=0.9, dnc=True))
    assert d["mode"] == "flag" and d["metadata"]["do_not_call"] is True
    assert d["data"] == {"lead_outcome": "not_interested", "queue_status": "not_interested"}


def test_dnc_flag_never_touches_booked_status():
    lead = {"queue_status": "complete", "lead_outcome": "booked"}
    d = decide_lead_update(lead, _so("do_not_call", conf=0.9, dnc=True))
    assert d["mode"] == "flag" and d["data"] == {} and d["metadata"]["do_not_call"] is True


def test_dnc_flag_needs_0_8_and_not_repeated():
    lead = {"queue_status": "manual_follow_up", "lead_outcome": "manual"}
    assert decide_lead_update(lead, _so("do_not_call", conf=0.75, dnc=True))["mode"] == "log"
    lead["metadata"] = {"do_not_call": True}
    assert decide_lead_update(lead, _so("do_not_call", conf=0.95, dnc=True))["mode"] == "log"


def test_mismatch_logged():
    assert decide_lead_update({"queue_status": "complete", "lead_outcome": "booked"},
                              _so("hung_up_mid_booking"))["mismatch"]
    assert decide_lead_update({"queue_status": "not_interested", "lead_outcome": "not_interested"},
                              _so("booked"))["mismatch"]
    assert decide_lead_update({"queue_status": "complete", "lead_outcome": "booked"}, _so("booked"))["mismatch"] is None


# ── webhook wiring ──

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
        writes.append({"plain": data})
        return True

    async def fake_meta_update(lead_id, data, metadata_patch=None, rid="?"):
        writes.append({"meta": data, "metadata": metadata_patch})
        return True

    async def fake_log(**kw):
        writes.append({"call_log": kw})
        return True

    monkeypatch.setattr(leads_api.httpx, "AsyncClient", lambda *a, **k: _Client(row))
    monkeypatch.setattr(leads_api, "supabase_update_lead", fake_update)
    monkeypatch.setattr(leads_api.booking_v2_service, "update_lead", fake_meta_update)
    monkeypatch.setattr(leads_api, "_insert_call_log", fake_log)
    return writes


def _eoc(ended, so=None, summary=None):
    msg = {"type": "end-of-call-report", "endedReason": ended, "call": {
        "id": "c1", "assistantId": "a1", "type": "outboundPhoneCall",
        "assistantOverrides": {"variableValues": {"lead_id": "L1"}}}}
    if so is not None:
        msg["artifact"] = {"structuredOutputs": {"so1": {"name": STRUCTURED_OUTPUT_NAME, "result": so}}}
    if summary:
        msg["analysis"] = {"summary": summary}
    return {"message": msg}


def test_webhook_dnc_on_untouched_lead(monkeypatch):
    writes = _setup(monkeypatch, dict(NEW))
    asyncio.run(leads_api.vapi_webhook(_Req(_eoc("customer-ended-call", _so("do_not_call", dnc=True)))))
    w = writes[0]["meta"]
    assert w["queue_status"] == "not_interested" and w["lead_outcome"] == "not_interested"
    assert writes[0]["metadata"]["do_not_call"] is True
    assert writes[1]["call_log"]["structured_output"]["outcome"] == "do_not_call"


def test_webhook_no_structured_output_unchanged(monkeypatch):
    writes = _setup(monkeypatch, dict(NEW))
    asyncio.run(leads_api.vapi_webhook(_Req(_eoc("customer-ended-call"))))
    w = writes[0]["plain"]
    assert w["queue_status"] == "manual_follow_up" and w["lead_outcome"] == "manual"
    assert w["notes"] == "[auto-fallback] customer-ended-call"
    assert writes[1]["call_log"]["structured_output"] is None


def test_webhook_hangup_keeps_fallback_adds_reason_and_summary(monkeypatch):
    writes = _setup(monkeypatch, dict(NEW))
    asyncio.run(leads_api.vapi_webhook(_Req(_eoc("customer-ended-call", _so("hung_up_mid_booking"),
                                                 summary="Caller hung up while hearing times."))))
    w = writes[0]["plain"]
    assert w["queue_status"] == "manual_follow_up"
    assert "hung_up_mid_booking" in w["notes"] and "Caller hung up while hearing times." in w["notes"]


def test_webhook_dnc_flag_on_booked_lead(monkeypatch):
    writes = _setup(monkeypatch, {"id": "L1", "queue_status": "complete", "lead_outcome": "booked", "notes": "n"})
    asyncio.run(leads_api.vapi_webhook(_Req(_eoc("customer-ended-call", _so("do_not_call", conf=0.9, dnc=True)))))
    flag = writes[0]
    assert "queue_status" not in flag["meta"] and "lead_outcome" not in flag["meta"]
    assert flag["meta"]["notes"].startswith("n | [call-outcome do_not_call]")
    assert flag["metadata"]["do_not_call"] is True
    assert "call_log" in writes[1]


def test_webhook_tool_updated_lead_only_logs(monkeypatch):
    writes = _setup(monkeypatch, {"id": "L1", "queue_status": "not_interested", "lead_outcome": "not_interested"})
    asyncio.run(leads_api.vapi_webhook(_Req(_eoc("customer-ended-call", _so("not_interested")))))
    assert len(writes) == 1 and writes[0]["call_log"]["structured_output"]["outcome"] == "not_interested"
