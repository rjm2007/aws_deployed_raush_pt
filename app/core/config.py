import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

# ─── Tebra Credentials ────────────────────────────────────────────────────────
TEBRA_URL      = os.getenv("TEBRA_URL", "https://webservice.kareo.com/services/soap/2.1/KareoServices.svc")
CUSTOMER_KEY   = os.getenv("CUSTOMER_KEY")
TEBRA_PASSWORD = os.getenv("TEBRA_PASSWORD")
TEBRA_USER     = os.getenv("TEBRA_USER")

# ─── Hardcoded Tebra Config (confirmed from debug endpoints) ─────────────────
PRACTICE_ID = os.getenv("PRACTICE_ID", "1")
PROVIDER_ID = os.getenv("PROVIDER_ID", "1")
RESOURCE_ID = os.getenv("RESOURCE_ID", "1")

# ─── VAPI Assistant IDs ───────────────────────────────────────────────────────
VAPI_LEAD_ASSISTANT_ID     = os.getenv("VAPI_LEAD_ASSISTANT_ID",     "a4fef714-66cf-4dd5-869c-5f2ebe4cadf0")
VAPI_REMINDER_ASSISTANT_ID = os.getenv("VAPI_REMINDER_ASSISTANT_ID", "cdee681d-59d6-47a6-b222-a22827c62e3e")
VAPI_INBOUND_ASSISTANT_ID  = os.getenv("VAPI_INBOUND_ASSISTANT_ID", "").strip()
VAPI_API_KEY               = os.getenv("VAPI_API_KEY", "").strip()

# ─── Twilio (SMS) ─────────────────────────────────────────────────────────────
TWILIO_ACCOUNT_SID  = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN   = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER  = os.getenv("TWILIO_FROM_NUMBER")  # E.164 recommended, e.g. +19491234567

# ─── Supabase Config ──────────────────────────────────────────────────────────
SUPABASE_URL         = os.getenv("SUPABASE_URL")
SUPABASE_API_KEY     = os.getenv("SUPABASE_API_KEY")
SUPABASE_PRACTICE_ID = os.getenv("SUPABASE_PRACTICE_ID", "0ff191f3-0d09-4b43-ae7e-7515bae3f410")  # Rausch PT
SUPABASE_HEADERS = {
    "apikey":        SUPABASE_API_KEY,
    "Authorization": f"Bearer {SUPABASE_API_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "return=representation",
}

# ─── Location Mapping ─────────────────────────────────────────────────────────
LOCATION_MAP = {
    "Dana Point":                  {"name": "Rausch Dana Point",               "id": "7"},
    "Laguna Niguel":               {"name": "Rausch Physical Therapy, Inc",     "id": "1"},
    "Mission Viejo":               {"name": "Rausch PT - Mission Viejo",        "id": "29"},
    "Fort Fitness - Laguna Hills": {"name": "Rausch Physical Therapy - Fort F", "id": "30"},
}

TEBRA_VALID_NAMES = {
    "Rausch Dana Point":                "7",
    "Rausch Physical Therapy, Inc":     "1",
    "Rausch PT - Mission Viejo":        "29",
    "Rausch Physical Therapy - Fort F": "30",
    "Rausch Laguna Hills":              "20",
}

# ─── Appointment Reason Map (verified from Tebra 2026-04-02) ─────────────────
# Exact Tebra names: 95=Follow up | 96=Evaluation | 97=Consultaion (Tebra typo)
#                   98=Re-Eval   | 99=Alter-G    | 100=Bike Right
APPOINTMENT_REASON_MAP = {
    # ── Exact Tebra names (as returned by GetAppointmentReasons) ──
    "follow up":          "95",
    "evaluation":         "96",
    "consultaion":        "97",   # Tebra's own typo — keep as-is
    "re-eval":            "98",
    "alter-g":            "99",
    "bike right":         "100",
    # ── VAPI / spoken aliases ──
    "follow-up":          "95",
    "initial evaluation": "96",
    "physical therapy":   "96",
    "pt":                 "96",
    "consultation":       "97",  # common spelling → same ID as Tebra typo
    "re-evaluation":      "98",
    "re eval":            "98",
    "alter g":            "99",
    "pelvic health pt":   "96",
    "pelvic health":      "96",
    "pelvic floor":       "96",
    "default":            "96",  # fallback → Evaluation
}

# ─── Clinic Timezone ──────────────────────────────────────────────────────────
# All time math uses the real America/Los_Angeles zone (zoneinfo), so DST is automatic:
# PDT (Mar–Nov) = UTC-7, PST (Nov–Mar) = UTC-8. See app/utils/time_utils.py
# (clinic_utc_offset_hours / to_utc_string / la_to_utc).
CLINIC_TIMEZONE = "America/Los_Angeles"

# LEGACY (kept so old .env files still load) — NOT used for any time math any more.
# Tebra's TimeZoneOffsetFromGMT is now computed per requested date.
CLINIC_TZ_OFFSET = timedelta(hours=-7)
TEBRA_TIMEZONE_OFFSET_FROM_GMT = int(os.getenv("TEBRA_TIMEZONE_OFFSET_FROM_GMT", "-7"))

# ─── Visit types / lengths (single source for durations) ────────────────────
# Decided with the client: Evaluation = 60 min, Follow-up = 45 min. Bookings start on a 30-min grid.
# Same values are seeded in the Supabase `visit_types` table (migrations/002).
VISIT_TYPES = {
    "evaluation": {"reason_id": "96", "minutes": 60, "label": "Evaluation"},
    "follow_up":  {"reason_id": "95", "minutes": 45, "label": "Follow up"},
}
REASON_DURATION_MINUTES = {v["reason_id"]: v["minutes"] for v in VISIT_TYPES.values()}
DEFAULT_SLOT_MINUTES   = 30   # old single-calendar endpoints + Tebra rows with no length
SLOT_GRID_MINUTES      = 30   # starts on :00 / :30
AVAILABILITY_WINDOW_DAYS = int(os.getenv("AVAILABILITY_WINDOW_DAYS", "60"))
BOOKING_LEAD_MINUTES     = int(os.getenv("BOOKING_LEAD_MINUTES", "60"))   # earliest start = now + this

# Tebra practice (multi-provider sync / live checks). Clinician bookings use ResourceId 1 (practice resource).
TEBRA_PRACTICE_NAME = os.getenv("TEBRA_PRACTICE_NAME", "Rausch Physical Therapy, Inc")


def normalize_visit_type(visit_type: str | None) -> str:
    """'Follow-up' / 'follow up' / 'followup' -> 'follow_up'; anything else -> 'evaluation'."""
    v = (visit_type or "").strip().lower().replace("-", "_").replace(" ", "_")
    if v in ("follow_up", "followup", "follow"):
        return "follow_up"
    return "evaluation"


def visit_duration_minutes(visit_type: str | None) -> int:
    return VISIT_TYPES[normalize_visit_type(visit_type)]["minutes"]


def visit_reason_id(visit_type: str | None) -> str:
    return VISIT_TYPES[normalize_visit_type(visit_type)]["reason_id"]


def reason_duration_minutes(reason_id, default: int = DEFAULT_SLOT_MINUTES) -> int:
    """Tebra AppointmentReasonId -> minutes (96 -> 60, 95 -> 45), else default."""
    return REASON_DURATION_MINUTES.get(str(reason_id).strip() if reason_id is not None else "", default)

# Inbound lookup: GetAppointments by PatientID is scoped to one Tebra practice name (SOAP Filter).
TEBRA_INBOUND_APPOINTMENTS_PRACTICE_NAME = os.getenv(
    "TEBRA_INBOUND_APPOINTMENTS_PRACTICE_NAME",
    "Rausch Physical Therapy, Inc",
)
# Calendar window in America/Los_Angeles for listing upcoming appointments (today → +N days).
TEBRA_INBOUND_APPOINTMENTS_WINDOW_DAYS = int(os.getenv("TEBRA_INBOUND_APPOINTMENTS_WINDOW_DAYS", "90"))


# ─── Resolver Helpers ─────────────────────────────────────────────────────────

def resolve_location(location: str) -> dict:
    if location in TEBRA_VALID_NAMES:
        return {"name": location, "id": TEBRA_VALID_NAMES[location]}
    if location in LOCATION_MAP:
        return LOCATION_MAP[location]
    return {"name": location, "id": None}


def resolve_appointment_reason_id(service: str | None) -> str:
    if not service:
        return APPOINTMENT_REASON_MAP["default"]
    return APPOINTMENT_REASON_MAP.get(
        service.strip().lower(),
        APPOINTMENT_REASON_MAP["default"]
    )


# ─── Multi-provider booking (v2 endpoints) ───────────────────────────────────
FRONT_DESK_PHONE        = os.getenv("FRONT_DESK_PHONE", "(949) 276-5401")
FRONT_DESK_PHONE_E164   = os.getenv("FRONT_DESK_PHONE_E164", "+19492765401")
# Optional floor for v2 offers/bookings (YYYY-MM-DD). Empty = no floor. Used while testing so the
# test agent never books near-term real slots (e.g. 2026-11-02).
V2_EARLIEST_BOOKING_DATE = os.getenv("V2_EARLIEST_BOOKING_DATE", "").strip()
# Same lead + clinician + start within this many minutes = the same booking (no second Tebra call)
V2_IDEMPOTENCY_MINUTES   = 10
# Phones that never get SMS (test numbers)
TEST_PHONE_PREFIXES      = ("+1555",)

# Nearest other clinics (Tebra location ID -> closest first). Used when a service (pelvic health)
# has nobody at the lead's clinic. Rough driving distance: DP->LN ~6 mi, FF(Laguna Hills)->MV ~5 mi.
NEAREST_CLINICS = {
    1:  [29, 7, 30],
    7:  [1, 29, 30],
    29: [1, 30, 7],
    30: [29, 1, 7],
}

# Lead service_of_interest -> clinician service line. None = the AI must not book it (front desk).
# Matched on lowercase substrings, first hit wins.
SERVICE_LINE_RULES = [
    ("pelvic",        "pelvic_health"),
    ("massage",       None),
    ("stretch",       None),
    ("chiro",         None),
    ("acupunct",      None),
    ("alter",         None),
    ("bike",          None),
    ("physical",      "physical_therapy"),
    ("general",       "physical_therapy"),
    ("inquiry",       "physical_therapy"),
    ("evaluation",    "physical_therapy"),
    ("pt",            "physical_therapy"),
]


def service_line_for(service: str | None) -> tuple[str | None, bool]:
    """
    Lead service -> (service_line, ai_bookable).
    Empty / unknown service = general physical therapy (bookable). Rx Massage / StretchFix / Chiropractic = not bookable.
    """
    s = (service or "").strip().lower()
    if not s:
        return "physical_therapy", True
    for key, line in SERVICE_LINE_RULES:
        if key in s:
            return line, line is not None
    return "physical_therapy", True
