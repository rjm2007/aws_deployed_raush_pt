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
# PDT (Mar–Nov) = UTC-7, PST (Nov–Mar) = UTC-8
# Change CLINIC_TZ_OFFSET to -8 in November.
CLINIC_TZ_OFFSET = timedelta(hours=-7)   # Pacific Daylight Time


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
