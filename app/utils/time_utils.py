import re
from datetime import datetime, timezone

from app.core.config import CLINIC_TZ_OFFSET


def _minutes(h: int, mn: int) -> int:
    return h * 60 + mn


def _format_hhmm(total_minutes: int) -> str:
    h = total_minutes // 60
    mn = total_minutes % 60
    return f"{h:02d}:{mn:02d}"


def get_location_hours(date_yyyy_mm_dd: str, location: str) -> tuple[int, int] | None:
    """
    Returns (start_minutes, end_minutes) for the clinic's working hours on the given date,
    or None if the clinic is closed that day.

    Times are local clinic time (America/Los_Angeles). Slots are 30-minute intervals.

    Source: clinic hours image provided by user (Apr 2026).
    """
    loc = (location or "").strip()
    try:
        weekday = datetime.strptime(date_yyyy_mm_dd, "%Y-%m-%d").weekday()
    except Exception:
        return None

    # Closed Sundays for all locations
    if weekday == 6:
        return None

    # Laguna Niguel
    if loc == "Laguna Niguel":
        if weekday <= 4:
            return (_minutes(7, 0), _minutes(19, 0))
        return (_minutes(7, 0), _minutes(13, 30))

    # Dana Point
    if loc == "Dana Point":
        if weekday <= 4:
            return (_minutes(7, 0), _minutes(19, 0))
        return (_minutes(7, 0), _minutes(13, 30))

    # Mission Viejo
    if loc == "Mission Viejo":
        if weekday <= 4:
            return (_minutes(7, 0), _minutes(17, 0))
        return None

    # Fort Fitness - Laguna Hills
    if loc == "Fort Fitness - Laguna Hills":
        if weekday <= 3:
            return (_minutes(8, 0), _minutes(17, 0))
        if weekday in (4, 5):
            return (_minutes(8, 0), _minutes(13, 0))
        return None

    # Unknown location: safe default Mon–Sat 7–5
    if weekday <= 5:
        return (_minutes(7, 0), _minutes(17, 0))
    return None


def parse_time_to_24hr(time_str: str):
    """Parse any time string to (hour, minute) tuple in 24-hr format. Returns None if unparseable."""
    if not time_str:
        return None
    time_str = time_str.strip()
    # HH:MM  e.g. "13:00", "9:30"
    m = re.match(r'^(\d{1,2}):(\d{2})$', time_str)
    if m:
        h, mn = int(m.group(1)), int(m.group(2))
        if 0 <= h <= 23:
            return (h, mn)
    # HH:MM AM/PM  e.g. "1:30 PM"
    m = re.match(r'^(\d{1,2}):(\d{2})\s*(AM|PM)$', time_str.upper())
    if m:
        h, mn, ampm = int(m.group(1)), int(m.group(2)), m.group(3)
        if ampm == "PM" and h != 12:
            h += 12
        if ampm == "AM" and h == 12:
            h = 0
        return (h, mn)
    # HH AM/PM (no minutes)  e.g. "1 PM", "12 PM", "9 AM"
    m = re.match(r'^(\d{1,2})\s*(AM|PM)$', time_str.upper())
    if m:
        h, ampm = int(m.group(1)), m.group(2)
        if ampm == "PM" and h != 12:
            h += 12
        if ampm == "AM" and h == 12:
            h = 0
        return (h, 0)
    return None


def format_12hr(h: int, mn: int) -> str:
    """Convert (hour, minute) to 12-hr display string e.g. '2:30 PM'."""
    ampm      = "AM" if h < 12 else "PM"
    display_h = h if h <= 12 else h - 12
    if display_h == 0:
        display_h = 12
    return f"{display_h}:{str(mn).zfill(2)} {ampm}"


def to_utc_string(date: str, h: int, mn: int) -> str:
    """Convert clinic local time (PDT = UTC-7) to UTC ISO string for Tebra API."""
    local_dt = datetime(
        int(date[:4]), int(date[5:7]), int(date[8:10]),
        h, mn, 0, tzinfo=timezone(CLINIC_TZ_OFFSET)
    )
    utc_dt = local_dt.astimezone(timezone.utc)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def generate_all_slots(date_yyyy_mm_dd: str, location: str) -> list[tuple[int, int]]:
    """Return all valid 30-minute booking slots for a clinic day and location."""
    hours = get_location_hours(date_yyyy_mm_dd, location)
    if not hours:
        return []
    start_min, end_min = hours

    slots: list[tuple[int, int]] = []
    t = start_min
    while t + 30 <= end_min:
        h = t // 60
        mn = t % 60
        slots.append((h, mn))
        t += 30
    return slots


def parse_booked_slots(xml: str, target_date: str) -> set:
    """Parse booked slots from Tebra XML.
    Tebra returns times in CLINIC LOCAL time (PDT/PST), NOT UTC.
    Only includes slots whose date matches target_date (YYYY-MM-DD)."""
    booked = set()
    for raw in re.findall(r'<StartDate>([^<]+)</StartDate>', xml):
        raw = raw.strip()
        try:
            local_dt = None
            if " " in raw and "/" in raw:
                # Format: "4/2/2026 7:00:00 AM" — already local clinic time
                parts = raw.split(" ")
                if len(parts) >= 3:
                    date_parts = parts[0].split("/")
                    month, day, year = int(date_parts[0]), int(date_parts[1]), int(date_parts[2])
                    tp   = parts[1].split(":")
                    h    = int(tp[0])
                    mn   = int(tp[1])
                    ampm = parts[2].upper()
                    if ampm == "PM" and h != 12:
                        h += 12
                    if ampm == "AM" and h == 12:
                        h = 0
                    local_dt = datetime(year, month, day, h, mn)
            elif "T" in raw:
                dt_part, tm_part = raw.split("T")
                dp = dt_part.split("-")
                tp = tm_part.replace("Z", "").split(":")
                local_dt = datetime(int(dp[0]), int(dp[1]), int(dp[2]), int(tp[0]), int(tp[1]))

            if local_dt is None:
                continue

            local_date_str = local_dt.strftime("%Y-%m-%d")
            if local_date_str != target_date:
                continue

            booked.add((local_dt.hour, local_dt.minute))
        except Exception:
            continue
    return booked


def get_available_slots(booked: set, date_yyyy_mm_dd: str, location: str) -> list:
    """Return all slots not in the booked set, constrained by location working hours."""
    return [s for s in generate_all_slots(date_yyyy_mm_dd, location) if s not in booked]


def get_free_ranges(available: list) -> list:
    """Group consecutive free slots into human-readable ranges."""
    if not available:
        return []
    ranges = []
    start  = available[0]
    prev   = available[0]
    for slot in available[1:]:
        if (slot[0] * 60 + slot[1]) - (prev[0] * 60 + prev[1]) == 30:
            prev = slot
        else:
            ranges.append((start, prev))
            start = prev = slot
    ranges.append((start, prev))
    return [
        format_12hr(*rs) if rs == re_ else f"{format_12hr(*rs)} to {format_12hr(*re_)}"
        for rs, re_ in ranges
    ]


def get_nearest_available_slots(requested_h: int, requested_mn: int,
                                available: list, n: int = 3) -> list:
    """Return up to n available slot strings closest to the requested time, sorted chronologically."""
    req_mins = requested_h * 60 + requested_mn
    by_dist  = sorted(available, key=lambda s: abs(s[0] * 60 + s[1] - req_mins))
    nearest  = sorted(by_dist[:n], key=lambda s: s[0] * 60 + s[1])
    return [format_12hr(h, mn) for h, mn in nearest]


def is_valid_clinic_slot(date_yyyy_mm_dd: str, location: str, h: int, mn: int) -> bool:
    """Return True if the slot falls within open clinic hours for that location/date."""
    hours = get_location_hours(date_yyyy_mm_dd, location)
    if not hours:
        return False
    start_min, end_min = hours
    t = _minutes(h, mn)
    return (t >= start_min) and (t + 30 <= end_min) and (t % 30 == 0)


def format_location_hours(date_yyyy_mm_dd: str, location: str) -> str:
    """Human-readable hours string for error messages."""
    hours = get_location_hours(date_yyyy_mm_dd, location)
    if not hours:
        return "closed"
    start_min, end_min = hours
    sh, sm = start_min // 60, start_min % 60
    eh, em = end_min // 60, end_min % 60
    return f"{format_12hr(sh, sm)} to {format_12hr(eh, em)}"
