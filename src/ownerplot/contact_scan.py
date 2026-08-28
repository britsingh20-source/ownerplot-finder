from __future__ import annotations

import argparse
import asyncio
from urllib.parse import urlparse

from .contact_resolver import PublicOwnerContactResolver
from .exact_detail_resolver import ExactPortalDetailResolver
from .image_contact_resolver import PropertyImageContactResolver
from .portal_native_contact import PortalNativeContactResolver
from .domain import SellerType
from .github_runner import search_profiles, send_message
from .portal_seeds import PortalOwnerSeedCollector
from .processing import deduplicate
from .query import parse_query
from .realestateindia_collector import RealEstateIndiaOwnerCollector
from .seed_cleanup import clean_seed_owner_names

PORTAL_HOSTS = {"realestateindia.com", "magicbricks.com", "99acres.com"}
HARD_CONTACT_ANCHORS = ("same owner name", "same dimensions", "same visible phone prefix", "same property image", "same property via exact portal listing", "same property via exact realestateindia listing")

def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")

def _is_primary_portal(url: str) -> bool:
    host = _host(url)
    return host in PORTAL_HOSTS or any(host.endswith(f".{domain}") for domain in PORTAL_HOSTS)

def _owner_seed(item) -> bool:
    if not _is_primary_portal(item.url): return False
    if item.seller_type in {SellerType.BROKER, SellerType.BUILDER}: return False
    text = f"{item.seller_claim or ''} {item.title} {item.description}".lower()
    return item.seller_type in {SellerType.PROBABLE_OWNER, SellerType.VERIFIED_OWNER} or any(marker in text for marker in ("contact owner", "posted by owner", "owner property", "individual", "owner"))

def _has_hard_contact_anchor(item) -> bool:
    evidence = " ".join(item.evidence).lower()
    return any(anchor in evidence for anchor in HARD_CONTACT_ANCHORS)

def _reject_weak_contacts(seeds: list) -> list:
    for item in seeds:
        if item.phone and item.phone_public and not _has_hard_contact_anchor(item):
            item.evidence.append(f"Rejected public phone {item.phone}: no hard owner/property anchor")
            item.phone = None; item.phone_public = False; item.contact_verification = "weak_cross_post_rejected"
    return seeds

def _priority(item) -> tuple:
    area = item.area_sqft or 0
    target_size = 1 if 1200 <= area <= 3000 else 0
    named_owner = 1 if item.seller_claim and item.seller_claim.lower() not in {"owner", "individual"} else 0
    realestateindia = 1 if _host(item.url).endswith("realestateindia.com") else 0
    detail_url = 1 if "property-detail" in item.url.lower() or "propertydetails" in item.url.lower() or "-npffid" in item.url.lower() else 0
    direct_rei = 1 if item.contact_verification == "realestateindia_public_owner_contact" else 0
    portal_native = 1 if item.contact_verification == "portal_native_public_contact" else 0
    return (direct_rei, portal_native, realestateindia, detail_url, target_size, named_owner, item.owner_confidence)

def _format_contact_first(locality: str, seeds: list, diagnostics: list[str] | None = None) -> str:
    resolved = [item for item in seeds if item.phone and item.phone_public and _has_hard_contact_anchor(item)]
    rei_resolved = [item for item in resolved if item.contact_verification == "realestateindia_public_owner_contact"]
    portal_native = [item for item in resolved if item.contact_verification == "portal_native_public_contact"]
    unresolved = [item for item in seeds if not item.phone]
    rei_seeds = [item for item in seeds if _host(item.url).endswith("realestateindia.com")]
    exact_details = sum(1 for item in seeds if "property-detail" in item.url.lower() or "propertydetails" in item.url.lower() or "-npffid" in item.url.lower())
    lines = ["OWNERPLOT CONTACT-FIRST SEARCH", "", f"PRIMARY OWNER SEEDS — {locality.upper()}", "RealEstateIndia is searched first for direct-owner/individual plot listings and public contact evidence; MagicBricks/99acres then supplement the seed pool. Structured portal contact, public cross-post and image correlation are fallbacks.", "No OTP/CAPTCHA/subscription bypass is used; a phone is shown only if publicly exposed on the exact property or validated with a hard same-owner/property anchor.", f"RealEstateIndia seeds: {len(rei_seeds)} · Total owner seeds: {len(seeds)} · Exact detail URLs: {exact_details} · RealEstateIndia direct public contacts: {len(rei_resolved)} · Portal-native contacts: {len(portal_native)} · Total hard-anchored contacts: {len(resolved)} · Unresolved/rejected: {len(unresolved)}"]
    for note in diagnostics or []:
        lines.append(f"Diagnostic: {note}")
    if not seeds:
        lines.append("No qualifying owner listings were found in this run."); return "\n".join(lines)
    for index, item in enumerate(sorted(seeds, key=_priority, reverse=True)[:15], 1):
        price = f"₹{item.price/100_000:g} lakh" if item.price else "Price not stated"
        area = f"{item.area_sqft:g} sq.ft." if item.area_sqft else "Area not stated"
        owner = item.seller_claim if item.seller_claim and item.seller_claim.lower() != "owner" else "Owner-labelled"
        if item.phone and item.phone_public and _has_hard_contact_anchor(item):
            if item.contact_verification == "realestateindia_public_owner_contact": label = "REALESTATEINDIA PUBLIC OWNER CONTACT"
            elif item.contact_verification == "portal_native_public_contact": label = "PORTAL-NATIVE PUBLIC OWNER CONTACT"
            else: label = "HARD-ANCHORED PUBLIC OWNER CONTACT"
            contact = f"{label}: {item.phone}"
        else:
            contact = "UNRESOLVED — no qualifying public phone matched yet"
        lines.extend(["", f"{index}. {item.title}", f"{area} · {price}", f"Portal: {_host(item.url)} · Advertiser: {owner}", f"Contact: {contact}", f"Source: {item.url}"])
        item_diagnostics = [e for e in item.evidence if e.startswith("RealEstateIndia") or e.startswith("Exact-detail") or e.startswith("Portal-native") or e.startswith("portal-native") or e.startswith("Contact hunt checked") or e.startswith("Image hunt") or e.startswith("Public phone found") or e.startswith("Contact match score") or e.startswith("Rejected public phone") or e.startswith("same property")]
        for evidence in item_diagnostics[-10:]: lines.append(f"Evidence: {evidence}")
    return "\n".join(lines)

async def run(locality: str, telegram: bool = False) -> str:
    query = parse_query(f"plots in {locality}")
    seeds = []
    diagnostics: list[str] = []

    rei_collector = RealEstateIndiaOwnerCollector.from_environment()
    if rei_collector is not None:
        try:
            seeds.extend(await rei_collector.search(locality))
        except Exception as exc:
            diagnostics.append(f"RealEstateIndia collector failed but scan continued: {type(exc).__name__}: {exc}")

    detail_collector = PortalOwnerSeedCollector.from_environment()
    if detail_collector is not None:
        try:
            seeds.extend(await detail_collector.search(locality))
        except Exception as exc:
            diagnostics.append(f"Portal seed collector failed but scan continued: {type(exc).__name__}: {exc}")

    if not seeds:
        try:
            portal_results = await search_profiles(query, profiles=("portals", "public_contacts"))
            seeds = [item for item in portal_results if _owner_seed(item)]
        except RuntimeError as exc:
            diagnostics.append(f"Optional Tavily/search fallback unavailable; continuing without it: {exc}")

    seeds = [item for item in clean_seed_owner_names(deduplicate(seeds)) if _owner_seed(item)]

    exact_resolver = ExactPortalDetailResolver()
    if seeds: seeds = await exact_resolver.enrich(seeds)

    native_resolver = PortalNativeContactResolver()
    if seeds: seeds = await native_resolver.enrich(seeds)
    seeds = _reject_weak_contacts(deduplicate(seeds))

    text_resolver = PublicOwnerContactResolver.from_environment()
    if text_resolver is not None and seeds:
        try:
            seeds = await text_resolver.enrich(seeds)
        except Exception as exc:
            diagnostics.append(f"Public cross-post resolver failed but scan continued: {type(exc).__name__}: {exc}")
    seeds = _reject_weak_contacts(deduplicate(seeds))

    image_resolver = PropertyImageContactResolver.from_environment()
    if image_resolver is not None and seeds:
        try:
            seeds = await image_resolver.enrich(seeds)
        except Exception as exc:
            diagnostics.append(f"Image resolver failed but scan continued: {type(exc).__name__}: {exc}")
    seeds = _reject_weak_contacts(deduplicate(seeds))

    message = _format_contact_first(locality, seeds, diagnostics)
    if telegram: await send_message(message)
    return message

def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve publicly exposed contacts for RealEstateIndia/MagicBricks/99acres owner listings")
    parser.add_argument("--locality", default="Kalapatti"); parser.add_argument("--telegram", action="store_true"); args = parser.parse_args()
    print(asyncio.run(run(args.locality, telegram=args.telegram)))

if __name__ == "__main__": main()
