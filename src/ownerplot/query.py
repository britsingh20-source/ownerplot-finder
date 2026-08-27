from __future__ import annotations

import re

from .domain import SearchQuery

PRICE_RE = re.compile(r"(?:under|below|max(?:imum)?)\s*(?:₹|rs\.?\s*)?([0-9.]+)\s*(crore|cr|lakhs?|lacs?)?", re.I)
COMMAND_RE = re.compile(r"^/(?:plots?|search)\s+", re.I)


def parse_query(text: str) -> SearchQuery:
    cleaned = COMMAND_RE.sub("", text.strip())
    cleaned = re.sub(r"\b(?:search|find|show|get|me|for|owner|owned|sale|properties|property|plots?|lands?|sites?|in|at)\b", " ", cleaned, flags=re.I)
    price_match = PRICE_RE.search(text)
    max_price = None
    if price_match:
        value = float(price_match.group(1))
        unit = (price_match.group(2) or "lakh").lower()
        max_price = int(value * (10_000_000 if unit in {"crore", "cr"} else 100_000))
        cleaned = PRICE_RE.sub(" ", cleaned)
    locality = " ".join(cleaned.split()).strip(" ,-.")
    if not locality:
        raise ValueError("Please include a locality, for example: /plots Kalapatti")
    return SearchQuery(locality=locality.title(), max_price=max_price)

