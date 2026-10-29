"""
Multi-provider availability engine — PURE (no I/O). Callers load the data (calendar_store) and pass it in.

For each clinician, a start time is open when:
  - it is on the 30-min grid (:00 / :30),
  - the WHOLE visit (duration) fits inside ONE working block of that clinician at that clinic
    (weekly clinician_hours + 'extra' time off rows, minus 'off' rows), clipped to clinic hours,
  - the clinic is open that day (get_location_hours: Sundays, holidays, past dates, DP/MV Saturdays),
  - it does not overlap any non-cancelled booking or blocked time (appt_type 'O') of that clinician
    — at ANY clinic (a clinician can't be in two places),
  - it starts at or after now + lead time (default 60 min),
  - the date is within the window (default today .. today + 60 days).

Times: hours / time off are clinic wall-clock; bookings are UTC (start_at / end_at); output is clinic time.
"""

from dataclasses import dataclass
from datetime import date as date_cls, datetime, time as time_cls, timedelta, timezone

from app.core.config import AVAILABILITY_WINDOW_DAYS, BOOKING_LEAD_MINUTES, SLOT_GRID_MINUTES
from app.utils.time_utils import CLINIC_TZ, format_12hr, get_location_hours

# Statuses that do NOT block the clinician's time
NON_BLOCKING_STATUSES = {"cancelled", "canceled", "rescheduled"}


@dataclass(frozen=True)
class Slot:
    provider_id: int
    provider_name: str
    date: str            # YYYY-MM-DD (clinic date)
    start_min: int       # minutes after clinic midnight
    duration: int        # minutes

    @property
    def time_24(self) -> str:
        return f"{self.start_min // 60:02d}:{self.start_min % 60:02d}"

    @property
    def time_12(self) -> str:
        return format_12hr(self.start_min // 60, self.start_min % 60)

    @property
    def start_local(self) -> datetime:
        d = date_cls.fromisoformat(self.date)
        return datetime(d.year, d.month, d.day, self.start_min // 60, self.start_min % 60, tzinfo=CLINIC_TZ)

    @property
    def start_utc(self) -> datetime:
        return self.start_local.astimezone(timezone.utc)

    @property
    def end_utc(self) -> datetime:
        return self.start_utc + timedelta(minutes=self.duration)

    def day_label(self) -> str:
        """'Monday, November 2'"""
        d = date_cls.fromisoformat(self.date)
        return f"{d.strftime('%A')}, {d.strftime('%B')} {d.day}"

    def spoken(self) -> str:
        """'Sarah Young on Monday, November 2 at 9:00 AM'"""
        return f"{self.provider_name} on {self.day_label()} at {self.time_12}"


# ─── small helpers ───────────────────────────────────────────────────────────

def _to_min(t) -> int | None:
    """'07:00:00' / '7:00' / time -> minutes after midnight."""
    if t is None:
        return None
    if isinstance(t, time_cls):
        return t.hour * 60 + t.minute
    parts = str(t).strip().split(":")
    if len(parts) < 2:
        return None
    return int(parts[0]) * 60 + int(parts[1])


def _to_dt(v) -> datetime | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        dt = v
    else:
        s = str(v).strip().replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _merge(blocks: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Merge overlapping or touching blocks (7-12 + 12-17 -> 7-17)."""
    out: list[list[int]] = []
    for s, e in sorted(b for b in blocks if b[1] > b[0]):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return [(s, e) for s, e in out]


def _subtract(blocks: list[tuple[int, int]], cut: tuple[int, int]) -> list[tuple[int, int]]:
    cs, ce = cut
    out = []
    for s, e in blocks:
        if ce <= s or cs >= e:
            out.append((s, e))
            continue
        if s < cs:
            out.append((s, cs))
        if ce < e:
            out.append((ce, e))
    return out


def _date_range(start: date_cls, end: date_cls):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


# ─── core ────────────────────────────────────────────────────────────────────

def working_blocks(provider_id: int, location_id: int, location_label: str, day: date_cls,
                   hours: list[dict], time_off: list[dict], today: date_cls) -> list[tuple[int, int]]:
    """Clinician's working blocks (minutes) at that clinic on that date, clipped to clinic hours."""
    clinic = get_location_hours(day.isoformat(), location_label, today=today)
    if not clinic:
        return []
    c_start, c_end = clinic
    wd = day.weekday()
    blocks = [
        (_to_min(h["start_time"]), _to_min(h["end_time"]))
        for h in hours
        if int(h["tebra_provider_id"]) == provider_id
        and int(h["tebra_location_id"]) == location_id
        and int(h["weekday"]) == wd
    ]
    day_s = day.isoformat()
    mine = [t for t in time_off if int(t["tebra_provider_id"]) == provider_id and str(t["date"])[:10] == day_s]
    for t in mine:
        if (t.get("kind") or "off") == "extra":
            loc = t.get("tebra_location_id")
            s, e = _to_min(t.get("start_time")), _to_min(t.get("end_time"))
            if loc is not None and int(loc) == location_id and s is not None and e is not None:
                blocks.append((s, e))
    blocks = _merge([(max(s, c_start), min(e, c_end)) for s, e in blocks])
    for t in mine:
        if (t.get("kind") or "off") != "off":
            continue
        s, e = _to_min(t.get("start_time")), _to_min(t.get("end_time"))
        if s is None or e is None:
            return []                       # whole day off
        blocks = _subtract(blocks, (s, e))
    return blocks


def busy_intervals(provider_id: int, day: date_cls, bookings: list[dict]) -> list[tuple[int, int]]:
    """Clinician's booked / blocked minutes on that clinic date (any clinic). Cancelled rows ignored."""
    day_start = datetime(day.year, day.month, day.day, tzinfo=CLINIC_TZ)
    day_end = day_start + timedelta(days=1)
    out = []
    for b in bookings:
        if b.get("tebra_provider_id") is None or int(b["tebra_provider_id"]) != provider_id:
            continue
        if str(b.get("status") or "").strip().lower() in NON_BLOCKING_STATUSES:
            continue
        s, e = _to_dt(b.get("start_at")), _to_dt(b.get("end_at"))
        if s is None:
            continue
        if e is None or e <= s:
            e = s + timedelta(minutes=int(b.get("duration_minutes") or 30))
        s_l, e_l = s.astimezone(CLINIC_TZ), e.astimezone(CLINIC_TZ)
        if e_l <= day_start or s_l >= day_end:
            continue
        s_l, e_l = max(s_l, day_start), min(e_l, day_end)
        # wall-clock minutes (DST-day safe: use the local clock reading, not elapsed time)
        s_m = s_l.hour * 60 + s_l.minute
        e_m = 24 * 60 if e_l >= day_end else e_l.hour * 60 + e_l.minute
        if e_m > s_m:
            out.append((s_m, e_m))
    return out


def open_slots(*, clinicians: list[dict], hours: list[dict], time_off: list[dict], bookings: list[dict],
               location_id: int, location_label: str, duration: int, start_date: str, end_date: str,
               now: datetime | None = None, provider_ids=None, gender: str | None = None,
               window_days: int = AVAILABILITY_WINDOW_DAYS, lead_minutes: int = BOOKING_LEAD_MINUTES,
               grid: int = SLOT_GRID_MINUTES) -> list[Slot]:
    """
    Every open start for every matching clinician at one clinic, dates start_date..end_date (inclusive,
    clipped to today..today+window_days). Sorted by date, time, clinician name.

    provider_ids: only these clinicians (e.g. who offers the service here, or a named clinician).
    gender: 'male' / 'female' -> only clinicians with that gender; 'unknown' never matches a filter.
    Clinicians with bookable=false or active=false are always skipped.
    """
    now = (now or datetime.now(timezone.utc)).astimezone(CLINIC_TZ)
    today = now.date()
    earliest = now + timedelta(minutes=lead_minutes)
    last_day = today + timedelta(days=window_days)
    d0 = max(date_cls.fromisoformat(start_date), today)
    d1 = min(date_cls.fromisoformat(end_date), last_day)
    want_ids = {int(p) for p in provider_ids} if provider_ids is not None else None
    g = (gender or "").strip().lower() or None

    picked = []
    for c in clinicians:
        pid = int(c["tebra_provider_id"])
        if c.get("bookable") is False or c.get("active") is False:
            continue
        if want_ids is not None and pid not in want_ids:
            continue
        if g and (c.get("gender") or "unknown").lower() != g:
            continue
        picked.append((pid, c.get("full_name") or str(pid)))

    slots: list[Slot] = []
    for day in _date_range(d0, d1):
        for pid, name in picked:
            blocks = working_blocks(pid, location_id, location_label, day, hours, time_off, today)
            if not blocks:
                continue
            busy = busy_intervals(pid, day, bookings)
            for bs, be in blocks:
                t = -(-bs // grid) * grid               # round up to the grid
                while t + duration <= be:
                    if not any(t < e and s < t + duration for s, e in busy):
                        slot = Slot(pid, name, day.isoformat(), t, duration)
                        if slot.start_local >= earliest:
                            slots.append(slot)
                    t += grid
    slots.sort(key=lambda s: (s.date, s.start_min, s.provider_name))
    return slots


# ─── answer helpers ──────────────────────────────────────────────────────────

def first_options(slots: list[Slot], n: int = 3, spread: bool = True) -> list[Slot]:
    """
    Up to n options, earliest first.
    spread=True ("anyone"): prefer a different clinician AND a different start time per option,
    then relax the time rule, then the clinician rule, so we still return n when we can.
    spread=False (one named clinician): simply the n earliest.
    """
    ordered = sorted(slots, key=lambda s: (s.date, s.start_min, s.provider_name))
    if not spread:
        return ordered[:n]
    chosen: list[Slot] = []
    for need_new_provider, need_new_time in ((True, True), (True, False), (False, True), (False, False)):
        for s in ordered:
            if len(chosen) >= n:
                break
            if s in chosen:
                continue
            if need_new_provider and any(c.provider_id == s.provider_id for c in chosen):
                continue
            if need_new_time and any((c.date, c.start_min) == (s.date, s.start_min) for c in chosen):
                continue
            chosen.append(s)
    return sorted(chosen, key=lambda s: (s.date, s.start_min, s.provider_name))


def slots_for_date(slots: list[Slot], date_yyyy_mm_dd: str) -> list[Slot]:
    return [s for s in slots if s.date == date_yyyy_mm_dd]


def first_open_dates(slots: list[Slot], start_date: str, end_date: str, n: int = 3) -> list[tuple[str, list[Slot]]]:
    """First n dates in [start_date, end_date] that have any open slot -> [(date, that day's slots), ...]."""
    by_date: dict[str, list[Slot]] = {}
    for s in sorted(slots, key=lambda s: (s.date, s.start_min, s.provider_name)):
        if start_date <= s.date <= end_date:
            by_date.setdefault(s.date, []).append(s)
    return [(d, by_date[d]) for d in sorted(by_date)[:n]]


def nearest_slots(slots: list[Slot], date_yyyy_mm_dd: str, start_min: int, n: int = 2) -> list[Slot]:
    """Alternatives closest to a wanted time (same date first, then the next dates), chronological."""
    same = sorted(slots_for_date(slots, date_yyyy_mm_dd), key=lambda s: abs(s.start_min - start_min))
    picks = same[:n]
    if len(picks) < n:
        later = [s for s in sorted(slots, key=lambda s: (s.date, s.start_min)) if s.date > date_yyyy_mm_dd]
        picks += later[: n - len(picks)]
    return sorted(picks, key=lambda s: (s.date, s.start_min))
