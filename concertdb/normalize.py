"""Name normalization used to match artists and venues across sources."""

import re
import unicodedata

_PUNCT = re.compile(r"[^\w\s]")
_SPACE = re.compile(r"\s+")


def norm(name: str | None) -> str:
    """'The Oh Sees' → 'oh sees', 'Beyoncé & Co.' → 'beyonce and co'."""
    if not name:
        return ""
    s = unicodedata.normalize("NFKD", name)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ")
    s = _PUNCT.sub(" ", s)
    s = _SPACE.sub(" ", s).strip()
    if s.startswith("the "):
        s = s[4:]
    return s
