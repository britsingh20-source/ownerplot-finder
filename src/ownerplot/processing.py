from __future__ import annotations

import hashlib
import re
from collections import defaultdict

from .domain import Listing, SellerType

PHONE_RE = re.compile(r"(?:\+?91[-\s]?)?([6-9]\d{9})")
PLOT_WORDS = {"plot", "land", "site", "residential land", "house site", "vacant land"}
BROKER_WORDS = {"broker", "agent", "consultant", "agency", "realty", "commission", "brokerage"}
OWNER_WORDS = {"owner", "direct owner", "no brokerage", "owner posted", "owner property"}
ITEM_HISTORY_RE = re.compile(r"\b(\d{1,5})\s+(?:items?|ads?|listings?)\s+(?:listed|posted)\b", re.I)
PORTAL_DOMAINS={"99acres.com","magicbricks.com"}
GENERIC_TOKENS={"plot","land","sale","owner","property","residential","coimbatore","kalapatti","contact","price","sqft","square","feet","road","near"}


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
    advertised_history=max((int(value) for value in ITEM_HISTORY_RE.findall(text)),default=0)
    listing.seller_history_count=max(listing.seller_history_count,advertised_history)
    if listing.seller_history_count>=25:
        listing.broker_risk=max(listing.broker_risk,95)
        listing.evidence.append(f"High-volume seller history: {listing.seller_history_count} advertisements")
    elif listing.seller_history_count>=5:
        listing.broker_risk=max(listing.broker_risk,70)
    if broker_hits or listing.broker_risk>=70:
        listing.seller_type = SellerType.BROKER
        listing.owner_confidence = max(0, 20 - broker_hits * 5 - listing.broker_risk//5)
        listing.contact_verification="broker_or_promoter"
    elif owner_hits and listing.phone_public and listing.matching_contact_sources>=2:
        listing.seller_type = SellerType.VERIFIED_OWNER
        listing.owner_confidence = min(95, 75 + owner_hits * 5)
        listing.contact_verification="property_matched_public_contact"
        listing.evidence.append(f"Same property/contact matched across {listing.matching_contact_sources} sources")
    elif owner_hits and listing.phone_public:
        listing.seller_type = SellerType.PROBABLE_OWNER
        listing.owner_confidence = 65
        listing.contact_verification="probable_owner_call_to_confirm"
    elif owner_hits:
        listing.seller_type = SellerType.PROBABLE_OWNER
        listing.owner_confidence = 50
        listing.contact_verification="hidden_contact"
    else:
        listing.seller_type = SellerType.UNKNOWN
        listing.owner_confidence = 30
        listing.contact_verification="public_contact_unverified" if listing.phone_public else "hidden_contact"
    return listing


def property_signature(listing: Listing) -> str:
    title_tokens=" ".join(re.findall(r"[a-z0-9]+",listing.title.lower())[:12])
    price_band=round((listing.price or 0)/100_000)
    area_band=round((listing.area_sqft or 0)/100)
    return f"{listing.locality.lower()}|{price_band}|{area_band}|{title_tokens}"


def analyze_seller_history(listings: list[Listing]) -> list[Listing]:
    by_phone: dict[str,list[Listing]]=defaultdict(list)
    for item in listings:
        if phone:=normalize_phone(item.phone): by_phone[phone].append(item)
    for phone,group in by_phone.items():
        sources={item.source for item in group}
        properties={property_signature(item) for item in group}
        for item in group:
            item.seller_history_count=max(item.seller_history_count,len(properties))
            same_property_sources={candidate.source for candidate in group if property_signature(candidate)==property_signature(item)}
            item.matching_contact_sources=len(same_property_sources)
            if len(properties)>=5:
                item.broker_risk=max(item.broker_risk,90)
                item.evidence.append(f"Phone appears across {len(properties)} different property fingerprints")
            elif len(properties)>=3:
                item.broker_risk=max(item.broker_risk,70)
            elif len(sources)>=2 and len(properties)==1:
                item.evidence.append("Public contact corroborated on multiple sources for one property")
    return listings


def _portal_listing(item: Listing) -> bool:
    return any(domain in f"{item.source} {item.url}".lower() for domain in PORTAL_DOMAINS)


def _match_score(first: Listing,second: Listing) -> int:
    if first.locality.lower()!=second.locality.lower(): return 0
    score=40
    if first.area_sqft and second.area_sqft:
        difference=abs(first.area_sqft-second.area_sqft)/max(first.area_sqft,second.area_sqft)
        if difference>0.03: return 0
        score+=25
    else: return 0
    if first.price and second.price:
        difference=abs(first.price-second.price)/max(first.price,second.price)
        if difference>0.05: return 0
        score+=25
    else: return 0
    first_tokens=set(re.findall(r"[a-z0-9]+",f"{first.title} {first.description}".lower()))-GENERIC_TOKENS
    second_tokens=set(re.findall(r"[a-z0-9]+",f"{second.title} {second.description}".lower()))-GENERIC_TOKENS
    if len(first_tokens & second_tokens)>=2: score+=10
    return score


def correlate_public_contacts(listings: list[Listing]) -> list[Listing]:
    """Attach only strongly matched, openly published contacts to owner-labelled portal ads."""
    donors=[item for item in listings if item.phone_public and item.phone and not _portal_listing(item) and item.seller_type not in {SellerType.BROKER,SellerType.BUILDER}]
    for target in listings:
        if target.phone or not _portal_listing(target): continue
        text=f"{target.seller_claim or ''} {target.title} {target.description}".lower()
        if not re.search(r"\b(owner|contact owner|posted by owner|individual)\b",text): continue
        matches=[(score,donor) for donor in donors if (score:=_match_score(target,donor))>=90]
        if not matches: continue
        score,donor=max(matches,key=lambda pair:pair[0])
        target.phone=normalize_phone(donor.phone)
        target.phone_public=bool(target.phone)
        target.matching_contact_sources=max(2,target.matching_contact_sources)
        target.contact_verification="public_cross_source_match"
        target.evidence.extend([f"Public contact matched from {donor.url}",f"Cross-source property match score: {score}/100"])
        classify_seller(target)
    return listings


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
