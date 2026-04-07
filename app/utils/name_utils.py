import re


def normalize_person_name_key(name: str) -> str:
    """
    Strict person-name normalizer used for exact matching only.

    Rules:
    - trim leading/trailing whitespace
    - collapse internal whitespace runs to a single space
    - lowercase for case-insensitive comparisons
    """
    return re.sub(r"\s+", " ", (name or "").strip()).lower()
