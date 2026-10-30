"""check-availability v2 date handling: a one-day range on a closed day and an empty range both say why
and offer the next openings (round-2 sim MP11 got 'NO OPENINGS' for date_from = date_to = a Sunday).
No network: the Supabase reference + slot search are faked."""
import asyncio
import json
import sys
from datetime import datetime

from app.api import booking_v2 as api
from app.utils.availability_engine import Slot
from app.utils.time_utils import CLINIC_TZ

REF = {
    "clinics": [{"tebra_location_id": 7, "name": "Dana Point", "lead_location_label": "Dana Point"}],
    "clinicians": [{"tebra_provider_id": 3114, "full_name": "Clay Simons", "bookable": True, "active": True, "gender": "male"}],
    "services": [{"tebra_provider_id": 3114, "tebra_location_id": 7, "service_line": "physical_therapy"}],
    "hours": [],
}
# open days at Dana Point: Mon 11-02 is "fully booked" in this fake, Tue 11-03 and Wed 11-04 are open
OPEN = [Slot(3114, "Clay Simons", "2026-11-03", 8 * 60 + 30, 60), Slot(3114, "Clay Simons", "2026-11-04", 9 * 60, 60)]


class _Req:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


def _setup(monkeypatch):
    calls = []

    async def ref(rid="?"):
        return REF

    async def slots(ref_, clinic, pids, duration, d0, d1, gender=None, extra_bookings=None, rid="?"):
        calls.append((d0, d1))
        return [s for s in OPEN if d0 <= s.date <= d1]

    monkeypatch.setattr(api.svc, "load_reference", ref)
    monkeypatch.setattr(api.svc, "open_slots_for", slots)
    monkeypatch.setattr(api, "now_la", lambda: datetime(2026, 10, 30, 3, 0, tzinfo=CLINIC_TZ))
    return calls


def _ask(args):
    resp = asyncio.run(api.check_availability_v2(_Req({"message": {"toolCalls": [{"id": "t", "function": {"arguments": {
        "location": "Dana Point", "service": "Physical Therapy", "visit_type": "evaluation", **args}}}]}})))
    return json.loads(resp.body)["results"][0]["result"]


def test_one_day_range_on_sunday_says_closed_and_offers_next(monkeypatch):
    _setup(monkeypatch)
    out = _ask({"date_from": "2026-11-01", "date_to": "2026-11-01"})
    assert "NO OPENINGS" not in out
    assert "closed on Sundays" in out and "Next openings" in out
    assert "Tuesday, November 3" in out


def test_empty_range_offers_openings_after_it(monkeypatch):
    _setup(monkeypatch)
    out = _ask({"date_from": "2026-11-01", "date_to": "2026-11-02"})
    assert "NO OPENINGS" not in out
    assert "no openings between" in out and "Next openings after that" in out
    assert "Tuesday, November 3" in out


def test_range_with_openings_unchanged(monkeypatch):
    _setup(monkeypatch)
    out = _ask({"date_from": "2026-11-01", "date_to": "2026-11-06"})
    assert out.startswith("First open days in that range:")


def test_part_of_day_full_says_part_not_whole_day(monkeypatch):
    # MP10: Monday morning full but Monday noon open -> "no morning openings on Monday", not "no openings on Monday"
    monkeypatch.setattr(sys.modules[__name__], "OPEN", OPEN + [Slot(3114, "Clay Simons", "2026-11-02", 12 * 60, 60),
                                                              Slot(3114, "Clay Simons", "2026-11-09", 8 * 60, 60)])
    _setup(monkeypatch)
    out = _ask({"date": "2026-11-02", "part_of_day": "morning"})
    assert "There are no morning openings on Monday, November 2" in out
    assert "12:00 PM" in out and "There are no openings on Monday" not in out
    assert "Next morning openings" in out
