"""SFU campus vocabulary — the canonical form of ``jobs.location``.

Requested by the sponsor after seeing the jobs list: Location means **campus**,
and there are three — Burnaby (BBY), Vancouver (YVR), Surrey (SRY).

**Why this canonicalises a value rather than scanning for one.** The obvious
implementation reads the job description looking for a campus name. Measured on
the pilot box, exactly one of 26 JDs mentions a campus at all, and it reads
"…Student Services, and the Surrey and Vancouver campuses" — the role's SCOPE,
not its location. A first-match scanner would have been wrong on the only row
it ever fired for, and confidently so. So nothing here touches
``description_raw``: every caller passes a value that some source has already
asserted IS the position's location (the LLM's extracted ``location`` field, a
Taleo listing's location cell, or a human typing into the form), and this
module only agrees on how to spell it.

Text naming two campuses therefore resolves to ``None`` rather than to the
first one found. "I cannot tell" and "it is Surrey" are different answers and
must not share a representation.
"""

from __future__ import annotations

import re
from typing import Final, Literal, get_args

Campus = Literal["Burnaby", "Vancouver", "Surrey"]

CAMPUSES: Final[tuple[Campus, ...]] = get_args(Campus)

#: The codes SFU uses in its own systems. Accepted as input, never stored —
#: the list shows a name, and "BBY" in a column is a lookup nobody should have
#: to do. Exposed for anywhere a compact label is genuinely wanted.
CAMPUS_CODES: Final[dict[Campus, str]] = {
    "Burnaby": "BBY",
    "Vancouver": "YVR",
    "Surrey": "SRY",
}

#: Every spelling seen in the wild, lowercased. The building names are here
#: because a Taleo listing and an internal JD both use them in place of the
#: campus: Harbour Centre and the Segal Graduate School are the Vancouver
#: campus, Central City is Surrey.
#:
#: Kept as a flat alias -> campus map rather than per-campus lists so a
#: spelling can only ever belong to one campus, by construction.
_ALIASES: Final[dict[str, Campus]] = {
    "burnaby": "Burnaby",
    "bby": "Burnaby",
    "burnaby mountain": "Burnaby",
    "university drive": "Burnaby",
    "vancouver": "Vancouver",
    "yvr": "Vancouver",
    "harbour centre": "Vancouver",
    "harbour center": "Vancouver",
    "segal": "Vancouver",
    "west hastings": "Vancouver",
    "surrey": "Surrey",
    "sry": "Surrey",
    "central city": "Surrey",
}

# Longest alias first, so "burnaby mountain" is not consumed as "burnaby" —
# it resolves to the same campus either way, but the ordering also makes the
# ambiguity check below count PHRASES rather than words.
_ALIAS_RE: Final[re.Pattern[str]] = re.compile(
    r"\b(?:"
    + "|".join(re.escape(a) for a in sorted(_ALIASES, key=len, reverse=True))
    + r")\b"
)


def canonical_campus(text: str | None) -> Campus | None:
    """The campus ``text`` names, or ``None``.

    ``None`` covers all three ways this can fail to be a campus: nothing was
    given, the value names somewhere that is not an SFU campus ("Remote",
    "Toronto"), or it names more than one — see the module docstring for why
    the last case must not resolve to the first match.
    """
    if not text:
        return None
    found = {_ALIASES[m] for m in _ALIAS_RE.findall(text.casefold())}
    if len(found) != 1:
        return None
    return found.pop()


def canonicalise_location(text: str | None) -> str | None:
    """What to actually store in ``jobs.location``.

    A recognised campus becomes its canonical name, so "bby", "Burnaby, BC"
    and "SFU Burnaby Campus" converge and the column can be grouped and
    filtered. Anything else is kept verbatim (trimmed): the column is TEXT,
    somebody may legitimately need "Remote" or a fourth site, and a field that
    silently discards what was typed into it teaches people not to use it.

    Empty or whitespace-only becomes ``None`` — "not set", which is what the
    list renders as an em-dash, rather than an empty string that sorts and
    compares as a real value.
    """
    campus = canonical_campus(text)
    if campus is not None:
        return campus
    if text is None:
        return None
    return text.strip() or None
