"""DST + full-length blocking for the old single-calendar helpers, and sync row mapping."""
from datetime import date, datetime, timezone

from app.core.config import reason_duration_minutes, visit_duration_minutes, visit_reason_id
from app.services.calendar_store import appointment_key
from app.services.tebra_sync import map_row
from app.utils.time_utils import (
    clinic_utc_offset_hours, closed_day_reason, get_available_slots, parse_booked_slots, to_utc_string,
)


def test_to_utc_string_uses_real_offset_for_that_date():
    assert to_utc_string("2026-10-30", 9, 0) == "2026-10-30T16:00:00.000Z"   # PDT
    assert to_utc_string("2026-11-02", 9, 0) == "2026-11-02T17:00:00.000Z"   # PST
    assert to_utc_string("2027-03-15", 9, 0) == "2027-03-15T16:00:00.000Z"   # PDT again


def test_tebra_offset_per_date():
    assert clinic_utc_offset_hours("2026-10-31") == -7
    assert clinic_utc_offset_hours("2026-11-01") == -8
    assert clinic_utc_offset_hours("2026-12-15") == -8


def test_visit_durations_single_source():
    assert visit_duration_minutes("evaluation") == 60 and visit_reason_id("evaluation") == "96"
    assert visit_duration_minutes("Follow-up") == 45 and visit_reason_id("follow up") == "95"
    assert reason_duration_minutes("96") == 60 and reason_duration_minutes(95) == 45
    assert reason_duration_minutes("97") == 30


def _appt(start, end=None, dur=None, status="Scheduled"):
    parts = [f"<StartDate>{start}</StartDate>", f"<ConfirmationStatus>{status}</ConfirmationStatus>"]
    if end:
        parts.append(f"<EndDate>{end}</EndDate>")
    if dur:
        parts.append(f"<AppointmentDuration>{dur}</AppointmentDuration>")
    return "<AppointmentData>" + "".join(parts) + "</AppointmentData>"


def test_parse_booked_slots_blocks_full_length():
    xml = _appt("11/2/2026 9:00:00 AM", "11/2/2026 10:00:00 AM", 60)
    assert parse_booked_slots(xml, "2026-11-02") == {(9, 0), (9, 30)}


def test_parse_booked_slots_duration_without_end_and_default():
    xml = _appt("11/2/2026 9:00:00 AM", dur=45) + _appt("11/2/2026 2:00:00 PM")
    assert parse_booked_slots(xml, "2026-11-02") == {(9, 0), (9, 30), (14, 0)}


def test_parse_booked_slots_skips_cancelled_and_other_dates():
    xml = _appt("11/2/2026 9:00:00 AM", dur=60, status="Cancelled") + _appt("11/3/2026 9:00:00 AM", dur=60)
    assert parse_booked_slots(xml, "2026-11-02") == set()


def test_old_available_slots_respect_full_length():
    # far-future Tuesday so the real "today" never makes it a past date
    booked = parse_booked_slots(_appt("12/3/2030 9:00:00 AM", dur=60), "2030-12-03")
    avail = get_available_slots(booked, "2030-12-03", "Laguna Niguel")
    assert (9, 0) not in avail and (9, 30) not in avail and (10, 0) in avail


def test_closed_day_reason_with_injected_today():
    assert closed_day_reason("2026-11-01", today=date(2026, 11, 2)) == "past"
    assert closed_day_reason("2026-11-26", today=date(2026, 11, 2)) == "Thanksgiving"
    assert closed_day_reason("2026-11-03", today=date(2026, 11, 2)) is None


def test_appointment_key():
    assert appointment_key("37018") == 37018
    k = appointment_key("5LGN")
    assert k < 0 and k == appointment_key("5LGN") and k != appointment_key("5LGU")


def test_map_row_provider_type_status():
    now = datetime(2026, 10, 30, tzinfo=timezone.utc)
    r = {"id": "37018", "start_utc": datetime(2026, 11, 20, 18, 0, tzinfo=timezone.utc),
         "end_utc": datetime(2026, 11, 20, 19, 0, tzinfo=timezone.utc), "duration": 60,
         "status": "Scheduled", "type": "Patient", "location_id": "1", "patient_id": "99",
         "reason_id": "96", "resources": [(2172, 1), (1, 2)]}
    m = map_row(r, now)
    assert m["tebra_provider_id"] == 2172 and m["appt_type"] == "P" and m["status"] == "scheduled"
    assert m["tebra_appointment_id"] == 37018 and m["tebra_appointment_ref"] == "37018"
    r2 = dict(r, id="5LGN", status="Cancelled", type="Other", resources=[(1, 2)])
    m2 = map_row(r2, now)
    assert m2["status"] == "cancelled" and m2["appt_type"] == "O" and m2["tebra_provider_id"] is None
    assert set(m) == set(m2)
