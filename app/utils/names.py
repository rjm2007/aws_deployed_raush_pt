from __future__ import annotations

import re

# Tokens that are labels, not part of a spoken name (e.g. "Kris Jorge Test 4").
_LABEL_TOKEN = re.compile(r"^(test|testing|demo|dummy|#?\d+)$", re.IGNORECASE)


def clean_person_name(raw: str | None) -> tuple[str, str]:
    """Return (spoken_full_name, spoken_first_name) with test labels and numbers removed.

    Only for what the voice agent *says*. Keep the raw name for Tebra/Supabase records.
    """
    tokens = [t for t in re.split(r"\s+", (raw or "").strip()) if t]
    kept = [t for t in tokens if not _LABEL_TOKEN.match(t.strip(".,"))]
    if not kept:
        return "", ""
    kept = [t if not t.islower() and not t.isupper() else t.capitalize() for t in kept]
    return " ".join(kept), kept[0]
