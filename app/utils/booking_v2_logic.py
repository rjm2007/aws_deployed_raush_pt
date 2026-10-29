"""
Pure helpers for the v2 (multi-provider) booking endpoints — no I/O.

- resolve_clinic(): lead label / Tebra name / ID / loose text -> clinics row
- normalize_gender(): caller words -> 'male' | 'female' | None (any)
- match_clinician(): fuzzy spoken name -> clinicians rows (never guesses between two people)
- pick_spread(): choose n options from an ordered list, different clinician + time first
- phrasing: spoken_day(), options_sentence(), ids_tag()

Gender is NEVER inferred from a first name: it only comes from the clinicians table.
"""

import re
from datetime import date as date_cls, timedelta
from difflib import SequenceMatcher

from app.utils.availability_engine import Slot

# ─── Clinics ─────────────────────────────────────────────────────────────────

_CLINIC_ALIASES = {
    "laguna niguel": 1, "ln": 1, "rausch physical therapy, inc": 1, "rausch physical therapy inc": 1,
    "dana point": 7, "dp": 7, "rausch dana point": 7,
    "mission viejo": 29, "mv": 29, "rausch pt - mission viejo": 29,
    "fort fitness - laguna hills": 30, "fort fitness": 30, "laguna hills": 30, "fort fitness laguna hills": 30,
    "rausch physical therapy - fort f": 30, "ff": 30,
}


def resolve_clinic(location, clinics: list[dict]) -> dict | None:
    """Any way of naming a clinic -> its clinics row (or None)."""
    if location is None:
        return None
    s = str(location).strip()
    if not s:
        return None
    by_id = {int(c["tebra_location_id"]): c for c in clinics}
    if s.isdigit():
        return by_id.get(int(s))
    low = " ".join(s.lower().split())
    for c in clinics:
        for k in ("lead_location_label", "name", "tebra_location_name"):
            if c.get(k) and str(c[k]).strip().lower() == low:
                return c
    if low in _CLINIC_ALIASES:
        return by_id.get(_CLINIC_ALIASES[low])
    for alias, cid in _CLINIC_ALIASES.items():          # "our Dana Point office", "Laguna Niguel clinic"
        if len(alias) > 3 and alias in low:
            return by_id.get(cid)
    return None


def clinic_label(c: dict | None) -> str:
    if not c:
        return "that clinic"
    return c.get("lead_location_label") or c.get("name") or str(c.get("tebra_location_id"))


# ─── Gender ──────────────────────────────────────────────────────────────────

_MALE = {"male", "man", "men", "m", "guy", "gentleman", "he", "him", "boy", "masculine"}
_FEMALE = {"female", "woman", "women", "f", "lady", "girl", "she", "her", "feminine", "gal"}


def normalize_gender(raw) -> str | None:
    """'a woman' / 'Female' / 'F' -> 'female'; 'male' etc. -> 'male'; anything else (any / no pref) -> None."""
    s = re.sub(r"[^a-z ]", " ", str(raw or "").lower()).split()
    if any(w in _FEMALE for w in s):
        return "female"
    if any(w in _MALE for w in s):
        return "male"
    return None


# ─── Clinician name matching ─────────────────────────────────────────────────

_TITLE_WORDS = {"dr", "doctor", "doc", "mr", "mrs", "ms", "miss", "dpt", "pt", "mpt", "with", "the", "therapist"}

# Names people say that are not in full_name (bios / "goes by")
_NICKNAMES = {
    4918: ["austin", "austin mckee"],
    5875: ["jc", "jean charles", "jean-charles", "jean"],
    3114: ["clay"],
    4410: ["nick", "nicholas takekoshi"],
    1:    ["kevin rausch", "dr rausch"],
}


def _norm_name(s: str) -> list[str]:
    s = re.sub(r"[^a-z\s-]", " ", str(s or "").lower()).replace("-", " ")
    return [t for t in s.split() if t and t not in _TITLE_WORDS]


def _tok_sim(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if len(a) >= 3 and (b.startswith(a) or a.startswith(b)) and min(len(a), len(b)) >= 3:
        return 0.9
    return SequenceMatcher(None, a, b).ratio()


def _score(query: list[str], c: dict) -> float:
    """0..1: how well the spoken tokens fit this clinician's name (or a nickname)."""
    variants = [_norm_name(c.get("full_name") or "")]
    variants += [_norm_name(n) for n in _NICKNAMES.get(int(c["tebra_provider_id"]), [])]
    best = 0.0
    for name in variants:
        if not name:
            continue
        if query == name:
            return 1.0
        # every spoken token must match some name token
        sims = [max(_tok_sim(q, n) for n in name) for q in query]
        if min(sims) >= 0.78:
            # full-name coverage bonus: 'sarah young' beats 'sarah' alone
            coverage = min(1.0, len(query) / len(name))
            best = max(best, 0.80 + 0.15 * (sum(sims) / len(sims)) * coverage)
        else:
            best = max(best, 0.9 * SequenceMatcher(None, " ".join(query), " ".join(name)).ratio())
    return best


def _exact_tokens(query: list[str], c: dict) -> bool:
    """True when every spoken token is a whole token of the clinician's name (or a nickname) - no prefix/fuzzy."""
    variants = [_norm_name(c.get("full_name") or "")]
    variants += [_norm_name(n) for n in _NICKNAMES.get(int(c["tebra_provider_id"]), [])]
    return any(name and all(q in name for q in query) for name in variants)


def match_clinician(raw_name, clinicians: list[dict], location_id: int | None = None,
                    providers_at_clinic: set[int] | None = None) -> dict:
    """
    Spoken name -> {"status": "match" | "ambiguous" | "none", "clinician": row | None, "candidates": [rows]}.
    Ties (e.g. 'Sarah' = Sarah Young + Sarah MacMillan) are broken only by "works at the lead's clinic";
    otherwise they come back as ambiguous so the agent asks.
    """
    q = _norm_name(raw_name)
    if not q:
        return {"status": "none", "clinician": None, "candidates": []}
    scored = sorted(((round(_score(q, c), 3), c) for c in clinicians), key=lambda x: -x[0])
    good = [(s, c) for s, c in scored if s >= 0.80]
    if not good:
        return {"status": "none", "clinician": None, "candidates": []}
    top = good[0][0]
    tier = [c for s, c in good if s >= top - 0.02]
    # An exact token match beats a prefix/fuzzy one: 'danielle' -> Danielle Simons, not Daniel Cerfogli
    exact = [c for c in tier if _exact_tokens(q, c)]
    if exact:
        tier = exact
    if len(tier) == 1:
        return {"status": "match", "clinician": tier[0], "candidates": tier}
    # Prefer bookable, then the lead's clinic
    bookable = [c for c in tier if c.get("bookable") is not False and c.get("active") is not False]
    if len(bookable) == 1:
        return {"status": "match", "clinician": bookable[0], "candidates": tier}
    if providers_at_clinic is not None:
        here = [c for c in (bookable or tier) if int(c["tebra_provider_id"]) in providers_at_clinic]
        if len(here) == 1:
            return {"status": "match", "clinician": here[0], "candidates": tier}
    return {"status": "ambiguous", "clinician": None, "candidates": bookable or tier}


# ─── Choosing options ────────────────────────────────────────────────────────

FAR_MINUTES = 180   # same-clinician options: different day, or at least 3 hours apart (morning / midday / afternoon)


def _far(a: Slot, b: Slot) -> bool:
    return a.date != b.date or abs(a.start_min - b.start_min) >= FAR_MINUTES


def pick_spread(ordered: list[Slot], n: int = 3, spread: bool = True, far: bool = True) -> list[Slot]:
    """
    n options keeping the caller's ORDER as priority (e.g. earliest-first or closest-to-wanted-time).
    spread=True: different clinician AND different start first, then relax the rules.
    far=True: when the same clinician has to repeat, prefer a different day or 3+ hours apart
    (so one clinician's options aren't 7:00 / 7:30 / 8:00). Use far=False for "closest to X" lists.
    Result is sorted chronologically for speech.
    """
    passes = []
    if spread:
        passes += [("new_provider", "new_time"), ("new_provider", None)]
    if far:
        passes.append((None, "far"))
    passes += [(None, "new_time"), (None, None)]
    chosen: list[Slot] = []
    for prov_rule, time_rule in passes:
        for s in ordered:
            if len(chosen) >= n:
                break
            if s in chosen:
                continue
            if prov_rule and any(c.provider_id == s.provider_id for c in chosen):
                continue
            if time_rule == "new_time" and any((c.date, c.start_min) == (s.date, s.start_min) for c in chosen):
                continue
            if time_rule == "far" and any(c.provider_id == s.provider_id and not _far(c, s) for c in chosen):
                continue
            chosen.append(s)
    return sorted(chosen, key=lambda s: (s.date, s.start_min, s.provider_name))


PART_OF_DAY = {"morning": (0, 12 * 60), "afternoon": (12 * 60, 17 * 60), "evening": (17 * 60, 24 * 60)}


def normalize_part_of_day(raw) -> str | None:
    s = str(raw or "").strip().lower()
    for k in PART_OF_DAY:
        if k in s:
            return k
    if "am" == s:
        return "morning"
    if "pm" == s:
        return "afternoon"
    return None


def in_part_of_day(slot: Slot, part: str | None) -> bool:
    if not part:
        return True
    lo, hi = PART_OF_DAY[part]
    return lo <= slot.start_min < hi


# ─── Phrasing ────────────────────────────────────────────────────────────────

def spoken_day(d: str, today: date_cls | None = None) -> str:
    """'2026-11-02' -> 'Monday, November 2' (prefixed 'today' / 'tomorrow' when it is)."""
    dd = date_cls.fromisoformat(d)
    base = f"{dd.strftime('%A')}, {dd.strftime('%B')} {dd.day}"
    if today is not None:
        if dd == today:
            return f"today, {base}"
        if dd == today + timedelta(days=1):
            return f"tomorrow, {base}"
    return base


def option_text(s: Slot, today: date_cls | None = None, with_name: bool = True) -> str:
    who = f"{s.provider_name} on " if with_name else ""
    return f"{who}{spoken_day(s.date, today)} at {s.time_12}"


def options_sentence(slots: list[Slot], today: date_cls | None = None, with_name: bool = True) -> str:
    parts = [option_text(s, today, with_name) for s in slots]
    if len(parts) <= 1:
        return "".join(parts)
    return "; ".join(parts[:-1]) + "; or " + parts[-1]


def ids_tag(slots: list[Slot], location_label: str, visit_type: str) -> str:
    """Machine values for the model to pass to create_appointment (not to be read aloud)."""
    items = " | ".join(f"{s.provider_name}={s.provider_id} {s.date} {s.time_24}" for s in slots)
    return f"[ids (do not read aloud): location={location_label} visit_type={visit_type} | {items}]"


def first_name(full_name: str | None) -> str:
    parts = (full_name or "").split()
    return parts[0] if parts else ""
