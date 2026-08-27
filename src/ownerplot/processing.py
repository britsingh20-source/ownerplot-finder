from __future__ import annotations

import hashlib
import re
from collections import defaultdict

from .domain import Listing, SellerType

PHONE_RE = re.compile(r"(?:\+?91[-\s]?)?([6-9]\d{9})")
PLOT_WORDS = {"plot", "land", "site", "residential land", "house site", "vacant land"}
BROKER_WORDS = {"broker", "agent", "consultant", "agency", "realty", "commission", "brokerage"}
OWNER_WORDS = {"owner", "direct owner", "no brokerage", "owner posted", "owner property"}


def normalize_phone(value: str | None) -> str | None:
    if not value:
        return None
    compact = re.sub(r"[\s().-]", "", value)
    match = PHONE_RE.search(compact)
    return f"+91{match.group(1)}" if match else None


def is_plot(listing: Listing) -> bool:
    text = f"{listing.property_type} {listing.title} {listing.description}".lower()
    return any(word in text for word in PLOT_WORDS)


def validate_locality(listing: Listing, locality: str) -> int:
    wanted = set(re.findall(r"[a-z0-9]+", locality.lower()))
    stated = set(re.findall(r"[a-z0-9]+", f"{listing.locality} {listing.title} {listing.description}".lower()))
    score = 100 if wanted and wanted <= stated else 0
    listing.locality_confidence = score
    return score


def classify_seller(listing: Listing) -> Listing:
    text = f"{listing.seller_claim or ''} {listing.title} {listing.description}".lower()
    broker_text = text.replace("no brokerage", "").replace("without brokerage", "")
    broker_hits = sum(term in broker_text for term in BROKER_WORDS)
    owner_hits = sum(term in text for term in OWNER_WORDS)
    if broker_hits:
        listing.seller_type = SellerType.BROKER
        listing.owner_confidence = max(0, 20 - broker_hits * 5)
    elif owner_hits >= 2 and listing.phone_public:
        listing.seller_type = SellerType.VERIFIED_OWNER
        listing.owner_confidence = min(95, 70 + owner_hits * 8)
    elif owner_hits:
        listing.seller_type = SellerType.PROBABLE_OWNER
        listing.owner_confidence = 65
    else:
        listing.seller_type = SellerType.UNKNOWN
        listing.owner_confidence = 30
    return listing


def fingerprint(listing: Listing) -> str:
    phone = normalize_phone(listing.phone) or ""
    price_band = round((listing.price or 0) / 100_000)
    area_band = round((listing.area_sqft or 0) / 100)
    raw = f"{phone}|{listing.locality.lower()}|{price_band}|{area_band}"
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def deduplicate(listings: list[Listing]) -> list[Listing]:
    groups: dict[str, list[Listing]] = defaultdict(list)
    for listing in listings:
        groups[fingerprint(listing)].append(listing)
    output = []
    for group in groups.values():
        best = max(group, key=lambda item: (item.owner_confidence, bool(item.phone), item.locality_confidence))
        if len(group) > 1:
            best.evidence.append(f"Merged {len(group)} matching source records")
        output.append(best)
    return output
