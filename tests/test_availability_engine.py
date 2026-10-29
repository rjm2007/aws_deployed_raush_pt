"""Unit tests for app/utils/availability_engine.py (pure — no network)."""
from datetime import datetime, timezone

from app.utils.availability_engine import (
    first_open_dates, first_options, nearest_slots, open_slots, slots_for_date,
)
from app.utils.time_utils import CLINIC_TZ

LN, LN_LABEL = 1, "Laguna Niguel"
MV, MV_LABEL = 29, "Mission Viejo"

# Monday 2026-11-02, 6:00 AM in LA (first weekday after DST ends) -> earliest start = 7:00
NOW = datetime(2026, 11, 2, 6, 0, tzinfo=CLINIC_TZ)

CLIN = [
    {"tebra_provider_id": 3708, "full_name": "Sarah Young", "gender": "female", "bookable": True, "active": True},
    {"tebra_provider_id": 5490, "full_name": "Byron Samayoa", "gender": "male", "bookable": True, "active": True},
    {"tebra_provider_id": 4919, "full_name": "Emily Rodriguez", "gender": "unknown", "bookable": True, "active": True},
    {"tebra_provider_id": 1, "full_name": "Kevin M Rausch", "gender": "male", "bookable": False, "active": True},
]


def hrs(pid, loc, wd, s, e):
    return {"tebra_provider_id": pid, "tebra_location_id": loc, "weekday": wd, "start_time": s, "end_time": e}


def week(pid, loc=LN, s="07:00:00", e="19:00:00"):
    return [hrs(pid, loc, wd, s, e) for wd in range(5)]


def utc(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=timezone.utc).isoformat()


def bk(pid, start_iso, end_iso, status="scheduled", appt_type="P", loc=LN):
    return {"tebra_provider_id": pid, "tebra_location_id": loc, "start_at": start_iso, "end_at": end_iso,
            "status": status, "appt_type": appt_type}


def run(hours, bookings=(), time_off=(), duration=60, start="2026-11-02", end="2026-11-02", **kw):
    kw.setdefault("now", NOW)
    kw.setdefault("location_id", LN)
    kw.setdefault("location_label", LN_LABEL)
    return open_slots(clinicians=kw.pop("clinicians", CLIN), hours=list(hours), time_off=list(time_off),
                      bookings=list(bookings), duration=duration, start_date=start, end_date=end, **kw)


def times(slots, pid=None):
    return [s.time_24 for s in slots if pid is None or s.provider_id == pid]


# ── full visit length / grid / blocks ────────────────────────────────────────

def test_full_day_grid_60_min():
    t = times(run(week(5490)), 5490)
    assert t[0] == "07:00" and t[-1] == "18:00"          # 18:00 + 60 = 19:00 close
    assert all(x.endswith(":00") or x.endswith(":30") for x in t)


def test_60_min_booking_blocks_its_whole_length():
    # 9:00-10:00 PST on Mon Nov 2 = 17:00-18:00 UTC
    t = times(run(week(5490), [bk(5490, utc(2026, 11, 2, 17, 0), utc(2026, 11, 2, 18, 0))]), 5490)
    for blocked in ("08:30", "09:00", "09:30"):
        assert blocked not in t
    assert "08:00" in t and "10:00" in t


def test_45_min_follow_up_overlap_and_fit():
    # booking 10:00-10:45 PST -> a 45-min visit at 9:30 ends 10:15 (overlap), 9:00 ok, 10:30 blocked? no: 10:30 starts before 10:45
    t = times(run(week(5490), [bk(5490, utc(2026, 11, 2, 18, 0), utc(2026, 11, 2, 18, 45))], duration=45), 5490)
    assert "09:00" in t and "09:30" not in t and "10:00" not in t and "10:30" not in t and "11:00" in t
    assert t[-1] == "18:00"                                # 18:00 + 45 = 18:45 <= 19:00


def test_visit_must_fit_inside_one_split_block():
    # DEMO split: Mon 7-11 and 14-19
    t = times(run([hrs(3708, LN, 0, "07:00:00", "11:00:00"), hrs(3708, LN, 0, "14:00:00", "19:00:00")]), 3708)
    assert "10:00" in t and "10:30" not in t and "11:00" not in t and "13:30" not in t and "14:00" in t


def test_touching_blocks_are_merged():
    t = times(run([hrs(3708, LN, 0, "07:00:00", "12:00:00"), hrs(3708, LN, 0, "12:00:00", "17:00:00")]), 3708)
    assert "11:30" in t and t[-1] == "16:00"


def test_clinician_hours_clipped_to_clinic_hours():
    # MV closes at 17:00 even if the row says 19:00
    t = times(run(week(5490, MV, e="19:00:00"), location_id=MV, location_label=MV_LABEL), 5490)
    assert t[-1] == "16:00"


def test_hours_at_other_clinic_not_used():
    assert run(week(5490, MV)) == []                       # asked for LN, hours only at MV


# ── bookings: status / type / other clinic ───────────────────────────────────

def test_cancelled_booking_does_not_block():
    b = bk(5490, utc(2026, 11, 2, 17, 0), utc(2026, 11, 2, 18, 0), status="cancelled")
    assert "09:00" in times(run(week(5490), [b]), 5490)


def test_blocked_time_type_o_blocks():
    b = bk(5490, utc(2026, 11, 2, 20, 0), utc(2026, 11, 2, 22, 0), appt_type="O")   # 12-2 PM lunch block
    t = times(run(week(5490), [b]), 5490)
    assert "11:30" not in t and "12:00" not in t and "13:30" not in t and "14:00" in t and "11:00" in t


def test_booking_at_other_clinic_blocks_clinician():
    b = bk(5490, utc(2026, 11, 2, 17, 0), utc(2026, 11, 2, 18, 0), loc=MV)
    assert "09:00" not in times(run(week(5490), [b]), 5490)


def test_other_clinicians_booking_does_not_block():
    b = bk(3708, utc(2026, 11, 2, 17, 0), utc(2026, 11, 2, 18, 0))
    assert "09:00" in times(run(week(5490), [b]), 5490)


def test_missing_end_uses_duration_minutes():
    b = {"tebra_provider_id": 5490, "start_at": utc(2026, 11, 2, 17, 0), "end_at": None,
         "duration_minutes": 60, "status": "scheduled"}
    t = times(run(week(5490), [b]), 5490)
    assert "09:30" not in t and "10:00" in t


# ── DST ──────────────────────────────────────────────────────────────────────

def test_dst_booking_utc_maps_to_pst_wall_clock():
    # 17:00Z on Nov 2 is 9:00 PST (was 10:00 PDT a week earlier) — must block 9:00, not 10:00
    t = times(run(week(5490), [bk(5490, utc(2026, 11, 2, 17, 0), utc(2026, 11, 2, 17, 30))], duration=30), 5490)
    assert "09:00" not in t and "10:00" in t


def test_slot_start_utc_both_sides_of_dst():
    s_pdt = run(week(5490), start="2026-10-30", end="2026-10-30",
                now=datetime(2026, 10, 30, 6, 0, tzinfo=CLINIC_TZ), duration=60)[0]
    s_pst = run(week(5490), duration=60)[0]
    assert s_pdt.time_24 == "07:00" and s_pdt.start_utc.hour == 14     # PDT UTC-7
    assert s_pst.time_24 == "07:00" and s_pst.start_utc.hour == 15     # PST UTC-8
    assert (s_pst.end_utc - s_pst.start_utc).total_seconds() == 3600


# ── closed days / lead time / window ─────────────────────────────────────────

def test_sunday_holiday_and_past_have_no_slots():
    all_week = [hrs(5490, LN, wd, "07:00:00", "19:00:00") for wd in range(7)]
    assert run(all_week, start="2026-11-08", end="2026-11-08") == []                # Sunday
    assert run(all_week, start="2026-11-26", end="2026-11-26") == []                # Thanksgiving
    assert run(all_week, start="2026-11-01", end="2026-11-01") == []                # past (yesterday)


def test_saturday_ln_only_8_to_14():
    t = times(run([hrs(5490, LN, 5, "07:00:00", "19:00:00")], start="2026-11-07", end="2026-11-07"), 5490)
    assert t[0] == "08:00" and t[-1] == "13:00"
    assert run([hrs(5490, MV, 5, "08:00:00", "14:00:00")], start="2026-11-07", end="2026-11-07",
               location_id=MV, location_label=MV_LABEL) == []


def test_lead_time_60_minutes():
    t = times(run(week(5490), now=datetime(2026, 11, 2, 9, 10, tzinfo=CLINIC_TZ)), 5490)
    assert t[0] == "10:30"                                   # 9:10 + 60 = 10:10 -> next grid 10:30


def test_window_is_60_days():
    slots = run(week(5490), start="2026-11-02", end="2027-02-01")
    assert max(s.date for s in slots) <= "2027-01-01"         # today + 60 days
    assert "2026-12-31" in {s.date for s in slots}


# ── clinician filters ────────────────────────────────────────────────────────

def test_gender_filter_excludes_unknown():
    h = week(3708) + week(5490) + week(4919)
    assert {s.provider_id for s in run(h, gender="female")} == {3708}
    assert {s.provider_id for s in run(h, gender="male")} == {5490}
    assert {s.provider_id for s in run(h)} == {3708, 5490, 4919}


def test_not_bookable_clinician_never_returned():
    assert run(week(1)) == []


def test_provider_ids_filter():
    h = week(3708) + week(5490)
    assert {s.provider_id for s in run(h, provider_ids=[3708])} == {3708}
    assert run(h, provider_ids=[]) == []


# ── time off ─────────────────────────────────────────────────────────────────

def test_time_off_whole_day_partial_and_extra():
    off_day = [{"tebra_provider_id": 5490, "date": "2026-11-02", "start_time": None, "end_time": None, "kind": "off"}]
    assert run(week(5490), time_off=off_day) == []
    partial = [{"tebra_provider_id": 5490, "date": "2026-11-02", "start_time": "07:00:00", "end_time": "12:00:00", "kind": "off"}]
    assert times(run(week(5490), time_off=partial), 5490)[0] == "12:00"
    extra = [{"tebra_provider_id": 5490, "date": "2026-11-07", "start_time": "09:00:00", "end_time": "12:00:00",
              "kind": "extra", "tebra_location_id": LN}]
    t = times(run(week(5490), time_off=extra, start="2026-11-07", end="2026-11-07"), 5490)
    assert t == ["09:00", "09:30", "10:00", "10:30", "11:00"]


# ── answer helpers ───────────────────────────────────────────────────────────

def test_first_options_spread_across_clinicians_and_times():
    h = week(3708) + week(5490) + week(4919)
    opts = first_options(run(h), n=3)
    assert len(opts) == 3
    assert len({o.provider_id for o in opts}) == 3
    assert len({o.time_24 for o in opts}) == 3
    assert opts == sorted(opts, key=lambda s: (s.date, s.start_min))


def test_first_options_single_clinician_earliest():
    opts = first_options(run(week(5490)), n=3, spread=False)
    assert [o.time_24 for o in opts] == ["07:00", "07:30", "08:00"]


def test_first_options_spread_with_one_clinician_still_returns_n():
    opts = first_options(run(week(5490)), n=3)
    assert len(opts) == 3 and opts[0].time_24 == "07:00"


def test_first_open_dates_skips_full_day():
    full = [{"tebra_provider_id": 5490, "date": "2026-11-03", "start_time": None, "end_time": None, "kind": "off"}]
    slots = run(week(5490), time_off=full, start="2026-11-02", end="2026-11-30")
    got = [d for d, _ in first_open_dates(slots, "2026-11-03", "2026-11-30", n=3)]
    assert got == ["2026-11-04", "2026-11-05", "2026-11-06"]


def test_slots_for_date_and_nearest():
    slots = run(week(5490), start="2026-11-02", end="2026-11-03")
    assert {s.date for s in slots_for_date(slots, "2026-11-03")} == {"2026-11-03"}
    near = nearest_slots(slots, "2026-11-02", 9 * 60, n=2)
    assert [s.time_24 for s in near] == ["08:30", "09:00"]   # 9:00 exact, then the closer-earlier tie


def test_spoken_text():
    s = run(week(5490))[0]
    assert s.spoken() == "Byron Samayoa on Monday, November 2 at 7:00 AM"
