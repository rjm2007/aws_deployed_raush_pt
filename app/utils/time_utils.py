import re
from datetime import date as date_cls, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.core.config import CLINIC_TIMEZONE, DEFAULT_SLOT_MINUTES

# Real IANA zone: DST is handled per date (PDT UTC-7 / PST UTC-8). No fixed offsets anywhere.
CLINIC_TZ  = ZoneInfo(CLINIC_TIMEZONE)
_LA_CLINIC = CLINIC_TZ


def now_la() -> datetime:
    """Current wall-clock time at the clinic (tz-aware)."""
    return datetime.now(CLINIC_TZ)


def la_to_utc(date_yyyy_mm_dd: str, h: int, mn: int) -> datetime:
    """Clinic wall time on that date -> aware UTC datetime, using the real offset for THAT date."""
    local_dt = datetime(int(date_yyyy_mm_dd[:4]), int(date_yyyy_mm_dd[5:7]), int(date_yyyy_mm_dd[8:10]),
                        h, mn, 0, tzinfo=CLINIC_TZ)
    return local_dt.astimezone(timezone.utc)


def clinic_utc_offset_hours(date_yyyy_mm_dd: str | None = None) -> int:
    """
    Clinic offset from GMT in whole hours for that date (-7 in PDT, -8 in PST).
    Used for Tebra's TimeZoneOffsetFromGMT filter. Measured at local noon so the
    2 AM DST switch itself never matters. No date = today.
    """
    if date_yyyy_mm_dd:
        try:
            d = datetime.strptime(str(date_yyyy_mm_dd)[:10], "%Y-%m-%d")
        except Exception:
            d = now_la().replace(tzinfo=None)
    else:
        d = now_la().replace(tzinfo=None)
    noon = datetime(d.year, d.month, d.day, 12, 0, tzinfo=CLINIC_TZ)
    return int(noon.utcoffset().total_seconds() // 3600)


def _minutes(h: int, mn: int) -> int:
    return h * 60 + mn


def _format_hhmm(total_minutes: int) -> str:
    h = total_minutes // 60
    mn = total_minutes % 60
    return f"{h:02d}:{mn:02d}"


def closed_day_reason(date_yyyy_mm_dd: str, today: date_cls | None = None) -> str | None:
    """
    Why the whole clinic is closed on this date, or None.
    - Past dates (before today in Los Angeles) can't be booked.
    - Major holidays. ASSUMED closed: New Year's Day, July 4th, Thanksgiving, Christmas Day —
      confirm the list with the client (VoiceFAQ only says hours can change around holidays).
    `today` (clinic date) can be passed in for tests / the availability engine; default = now in LA.
    """
    try:
        d = datetime.strptime(date_yyyy_mm_dd, "%Y-%m-%d").date()
    except Exception:
        return None
    if d < (today or now_la().date()):
        return "past"
    if (d.month, d.day) == (1, 1):
        return "New Year's Day"
    if (d.month, d.day) == (7, 4):
        return "the Fourth of July"
    if (d.month, d.day) == (12, 25):
        return "Christmas Day"
    if d.month == 11 and d.weekday() == 3 and 22 <= d.day <= 28:  # 4th Thursday of November
        return "Thanksgiving"
    return None


def get_location_hours(date_yyyy_mm_dd: str, location: str,
                       today: date_cls | None = None) -> tuple[int, int] | None:
    """
    Returns (start_minutes, end_minutes) for the clinic's working hours on the given date,
    or None if the clinic is closed that day.

    Times are local clinic time (America/Los_Angeles). Slots are 30-minute intervals.

    Source: client VoiceFAQ (Sep 2026) for Laguna Niguel, Dana Point, Mission Viejo.
    Dana Point and Mission Viejo open on *alternate* Saturdays (8 AM–2 PM), but we
    don't know which ones, so Saturdays there are not bookable by the agents.
    Fort Fitness is not in the FAQ; its hours are unchanged (Apr 2026 source).
    """
    loc = (location or "").strip()
    try:
        weekday = datetime.strptime(date_yyyy_mm_dd, "%Y-%m-%d").weekday()
    except Exception:
        return None

    # Past dates and holidays: closed everywhere
    if closed_day_reason(date_yyyy_mm_dd, today=today):
        return None

    # Closed Sundays for all locations
    if weekday == 6:
        return None

    # Laguna Niguel — every Saturday 8 AM–2 PM
    if loc == "Laguna Niguel":
        if weekday <= 4:
            return (_minutes(7, 0), _minutes(19, 0))
        return (_minutes(8, 0), _minutes(14, 0))

    # Dana Point — alternate Saturdays only (not bookable, see docstring)
    if loc == "Dana Point":
        if weekday <= 4:
            return (_minutes(7, 0), _minutes(19, 0))
        return None

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


def iso_utc_z_to_la_parts(iso_z: str | None) -> tuple[str, int, int] | None:
    """
    Parse Tebra GetAppointment StartTime / EndTime (ISO with Z) to clinic wall clock
    in America/Los_Angeles. Returns (YYYY-MM-DD, hour24, minute) or None.
    """
    if not iso_z:
        return None
    s = str(iso_z).strip()
    if not s:
        return None
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        utc = datetime.fromisoformat(s)
        if utc.tzinfo is None:
            utc = utc.replace(tzinfo=timezone.utc)
        la = utc.astimezone(_LA_CLINIC)
        return la.strftime("%Y-%m-%d"), la.hour, la.minute
    except Exception:
        return None


def parse_tebra_local_start_datetime(raw: str | None) -> tuple[str, int, int] | None:
    """
    Parse Tebra GetAppointments StartDate field which is typically local clinic time, like:
      "4/17/2026 9:30:00 AM"
    Returns (YYYY-MM-DD, hour24, minute) or None.
    """
    if not raw:
        return None
    s = str(raw).strip()
    if not s:
        return None
    # Expected: M/D/YYYY H:MM:SS AM|PM
    if " " not in s or "/" not in s:
        return None
    parts = s.split(" ")
    if len(parts) < 3:
        return None
    try:
        mdy = parts[0].split("/")
        month, day, year = int(mdy[0]), int(mdy[1]), int(mdy[2])
        hms = parts[1].split(":")
        h, mn = int(hms[0]), int(hms[1])
        ampm = parts[2].upper()
        if ampm == "PM" and h != 12:
            h += 12
        if ampm == "AM" and h == 12:
            h = 0
        date_s = f"{year:04d}-{month:02d}-{day:02d}"
        return (date_s, h, mn)
    except Exception:
        return None


def to_utc_string(date: str, h: int, mn: int) -> str:
    """Convert clinic local time to the UTC ISO string Tebra expects, using that date's real offset (DST-safe)."""
    return la_to_utc(date, h, mn).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def parse_tebra_datetime(raw: str | None) -> datetime | None:
    """
    Parse a Tebra GetAppointments StartDate / EndDate to a NAIVE datetime, in whatever
    zone the request asked for (TimeZoneOffsetFromGMT). Formats:
      "4/17/2026 9:30:00 AM"   (normal)
      "2026-04-17T09:30:00"    (ISO, Z ignored)
    """
    if not raw:
        return None
    s = str(raw).strip()
    try:
        if " " in s and "/" in s:
            parts = s.split(" ")
            if len(parts) < 3:
                return None
            month, day, year = (int(x) for x in parts[0].split("/"))
            tp = parts[1].split(":")
            h, mn = int(tp[0]), int(tp[1])
            ampm = parts[2].upper()
            if ampm == "PM" and h != 12:
                h += 12
            if ampm == "AM" and h == 12:
                h = 0
            return datetime(year, month, day, h, mn)
        if "T" in s:
            dt_part, tm_part = s.split("T", 1)
            dp = dt_part.split("-")
            tp = re.split(r"[:Z+.-]", tm_part)
            return datetime(int(dp[0]), int(dp[1]), int(dp[2]), int(tp[0]), int(tp[1]))
    except Exception:
        return None
    return None


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
    """Parse booked 30-min slot starts from Tebra GetAppointments XML.
    Tebra returns times in the TimeZoneOffsetFromGMT we sent (clinic local for that date).
    Only includes slots on target_date (YYYY-MM-DD).
    Skips ConfirmationStatus=Cancelled so cancelled slots are free.
    Blocks the booking's FULL length: EndDate if given, else AppointmentDuration, else 30 min.
    A 60-min booking at 9:00 blocks 9:00 and 9:30; any 30-min slot that overlaps a booking is blocked."""
    booked = set()
    # Iterate per-appointment blocks so we can inspect ConfirmationStatus.
    # Fall back to loose StartDate scan only if no blocks are present.
    blocks = re.findall(r'<AppointmentData>(.*?)</AppointmentData>', xml, re.DOTALL)
    if not blocks:
        blocks = [xml]
    try:
        day_start = datetime.strptime(target_date, "%Y-%m-%d")
    except Exception:
        return booked
    day_end = day_start + timedelta(days=1)
    for block in blocks:
        status_m = re.search(r'<ConfirmationStatus>([^<]*)</ConfirmationStatus>', block)
        status = (status_m.group(1).strip().lower() if status_m else "")
        if status == "cancelled":
            continue
        sd_m = re.search(r'<StartDate>([^<]+)</StartDate>', block)
        if not sd_m:
            continue
        start = parse_tebra_datetime(sd_m.group(1))
        if start is None:
            continue

        end = None
        ed_m = re.search(r'<EndDate>([^<]+)</EndDate>', block)
        if ed_m:
            end = parse_tebra_datetime(ed_m.group(1))
        if end is None or end <= start:
            dur_m = re.search(r'<AppointmentDuration>\s*(\d+)', block)
            minutes = int(dur_m.group(1)) if dur_m and int(dur_m.group(1)) > 0 else DEFAULT_SLOT_MINUTES
            end = start + timedelta(minutes=minutes)

        # Clip to the target day, then mark every 30-min grid slot that overlaps [start, end)
        s_clip, e_clip = max(start, day_start), min(end, day_end)
        if s_clip >= e_clip:
            continue
        s_min = int((s_clip - day_start).total_seconds() // 60)
        e_min = int((e_clip - day_start).total_seconds() // 60)
        t = (s_min // 30) * 30
        while t < e_min:
            booked.add((t // 60, t % 60))
            t += 30
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
