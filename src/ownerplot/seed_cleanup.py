from __future__ import annotations

import re

from .domain import Listing


TRAILING_CARD_LABEL_RE = re.compile(
    r"\s+(?:Residential(?:\s+(?:Plot|Land|Property))?|Plot|Land|Property|Premium\s+Member|Updated|Posted|Contact\s+Owner|Image)\b.*$",
    re.I,
)


def clean_owner_claim(value: str | None) -> str | None:
    if not value:
        return value
    clean = " ".join(value.split()).strip(" .,-")
    clean = TRAILING_CARD_LABEL_RE.sub("", clean).strip(" .,-")
    return clean or value


def clean_seed_owner_names(listings: list[Listing]) -> list[Listing]:
    for item in listings:
        before = item.seller_claim
        after = clean_owner_claim(before)
        if after and before != after:
            item.seller_claim = after
            item.evidence.append(f"Normalized indexed owner label from {before!r} to {after!r}")
    return listings
