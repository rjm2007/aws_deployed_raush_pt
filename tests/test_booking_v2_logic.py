"""Unit tests for app/utils/booking_v2_logic.py + config.service_line_for (pure — no network)."""
from app.core.config import service_line_for
from app.utils.availability_engine import Slot
from app.utils.booking_v2_logic import (
    match_clinician, normalize_gender, pick_spread, resolve_clinic, spoken_day, ids_tag, normalize_part_of_day,
)

CLINICS = [
    {"tebra_location_id": 1, "name": "Laguna Niguel", "tebra_location_name": "Rausch Physical Therapy, Inc", "lead_location_label": "Laguna Niguel"},
    {"tebra_location_id": 7, "name": "Dana Point", "tebra_location_name": "Rausch Dana Point", "lead_location_label": "Dana Point"},
    {"tebra_location_id": 29, "name": "Mission Viejo", "tebra_location_name": "Rausch PT - Mission Viejo", "lead_location_label": "Mission Viejo"},
    {"tebra_location_id": 30, "name": "Fort Fitness - Laguna Hills", "tebra_location_name": "Rausch Physical Therapy - Fort F", "lead_location_label": "Fort Fitness - Laguna Hills"},
]

def C(pid, name, bookable=True):
    return {"tebra_provider_id": pid, "full_name": name, "bookable": bookable, "active": True}

CLIN = [C(3708, "Sarah Young"), C(2172, "Sarah MacMillan"), C(4138, "Danielle Simons"), C(3114, "Clay Simons"),
        C(4918, "Joseph Austin McKee"), C(6199, "Kevin Takao"), C(1, "Kevin M Rausch", False),
        C(5875, "Jean Charles Plantier"), C(5955, "Analise Richtsmeier"), C(6018, "Daniel Cerfogli")]


def test_resolve_clinic():
    assert resolve_clinic("Laguna Niguel", CLINICS)["tebra_location_id"] == 1
    assert resolve_clinic("dana point", CLINICS)["tebra_location_id"] == 7
    assert resolve_clinic("Rausch PT - Mission Viejo", CLINICS)["tebra_location_id"] == 29
    assert resolve_clinic("Fort Fitness - Laguna Hills", CLINICS)["tebra_location_id"] == 30
    assert resolve_clinic("laguna hills", CLINICS)["tebra_location_id"] == 30
    assert resolve_clinic("30", CLINICS)["tebra_location_id"] == 30
    assert resolve_clinic("Irvine", CLINICS) is None


def test_service_lines():
    assert service_line_for("Physical Therapy") == ("physical_therapy", True)
    assert service_line_for("General inquiry") == ("physical_therapy", True)
    assert service_line_for("Pelvic Health PT") == ("pelvic_health", True)
    assert service_line_for(None) == ("physical_therapy", True)
    for s in ("Rx Massage", "StretchFix", "Chiropractic Services"):
        assert service_line_for(s)[1] is False


def test_gender():
    assert normalize_gender("female") == "female"
    assert normalize_gender("a woman please") == "female"
    assert normalize_gender("Male") == "male"
    assert normalize_gender("any") is None
    assert normalize_gender(None) is None


def test_match_exact_and_fuzzy():
    assert match_clinician("Sarah Young", CLIN)["clinician"]["tebra_provider_id"] == 3708
    assert match_clinician("Dr. Sara Yung", CLIN)["clinician"]["tebra_provider_id"] == 3708
    assert match_clinician("Austin", CLIN)["clinician"]["tebra_provider_id"] == 4918
    assert match_clinician("Danielle", CLIN)["clinician"]["tebra_provider_id"] == 4138
    assert match_clinician("analise", CLIN)["clinician"]["tebra_provider_id"] == 5955
    assert match_clinician("Jean Charles", CLIN)["clinician"]["tebra_provider_id"] == 5875
    # exact first-name token beats a prefix of another first name (Danielle vs Daniel)
    for q, pid in (("danielle", 4138), ("Daniel", 6018), ("daniel", 6018), ("DANIELLE", 4138),
                   ("Daniel Cerfogli", 6018), ("Danielle Simons", 4138)):
        m = match_clinician(q, CLIN)
        assert m["status"] == "match" and m["clinician"]["tebra_provider_id"] == pid, q


def test_match_ambiguous_and_tiebreak():
    m = match_clinician("Sarah", CLIN)
    assert m["status"] == "ambiguous"
    assert {c["tebra_provider_id"] for c in m["candidates"]} == {3708, 2172}
    m = match_clinician("Sarah", CLIN, providers_at_clinic={3708, 4138})
    assert m["status"] == "match" and m["clinician"]["tebra_provider_id"] == 3708
    # Kevin -> the bookable Kevin
    assert match_clinician("Kevin", CLIN)["clinician"]["tebra_provider_id"] == 6199
    # Simons: Danielle vs Clay
    assert match_clinician("Simons", CLIN)["status"] == "ambiguous"


def test_match_none():
    assert match_clinician("Dr. Zebulon Smith", CLIN)["status"] == "none"
    assert match_clinician("", CLIN)["status"] == "none"


def test_pick_spread():
    s = [Slot(1, "A", "2026-11-02", 420, 60), Slot(2, "B", "2026-11-02", 420, 60),
         Slot(1, "A", "2026-11-02", 450, 60), Slot(3, "C", "2026-11-02", 480, 60)]
    out = pick_spread(s, 3)
    assert [x.provider_id for x in out] == [1, 2, 3] or len({x.provider_id for x in out}) == 3
    assert pick_spread(s, 2, spread=False) == s[:2]


def test_phrasing():
    from datetime import date
    assert spoken_day("2026-11-02") == "Monday, November 2"
    assert spoken_day("2026-11-02", date(2026, 11, 1)) == "tomorrow, Monday, November 2"
    tag = ids_tag([Slot(3708, "Sarah Young", "2026-11-03", 540, 60)], "Laguna Niguel", "evaluation")
    assert "Sarah Young=3708 2026-11-03 09:00" in tag
    assert normalize_part_of_day("Morning") == "morning"
