from __future__ import annotations

import argparse
import asyncio
from urllib.parse import urlparse

from .contact_resolver import PublicOwnerContactResolver
from .domain import SellerType
from .github_runner import search_profiles, send_message
from .processing import deduplicate
from .query import parse_query


PORTAL_HOSTS = {"magicbricks.com", "99acres.com"}


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _is_primary_portal(url: str) -> bool:
    host = _host(url)
    return host in PORTAL_HOSTS or any(host.endswith(f".{domain}") for domain in PORTAL_HOSTS)


def _owner_seed(item) -> bool:
    if not _is_primary_portal(item.url):
        return False
    if item.seller_type in {SellerType.BROKER, SellerType.BUILDER}:
        return False
    text = f"{item.seller_claim or ''} {item.title} {item.description}".lower()
    return item.seller_type in {SellerType.PROBABLE_OWNER, SellerType.VERIFIED_OWNER} or any(
        marker in text for marker in ("contact owner", "posted by owner", "owner property", "individual", "owner")
    )


def _format_contact_first(locality: str, seeds: list) -> str:
    resolved = [item for item in seeds if item.contact_verification == "public_cross_post_owner_contact" and item.phone]
    unresolved = [item for item in seeds if not item.phone]
    lines = [
        "OWNERPLOT CONTACT-FIRST SEARCH",
        "",
        f"PRIMARY OWNER SEEDS — {locality.upper()}",
        "Only MagicBricks/99acres owner-posted listings are shown. Other public websites/social sources are contact donors only.",
        f"Portal owner seeds: {len(seeds)} · Resolved public owner contacts: {len(resolved)} · Unresolved: {len(unresolved)}",
    ]
    if not seeds:
        lines.append("No MagicBricks/99acres owner seeds were returned by the configured portal discovery source.")
        return "\n".join(lines)
    for index, item in enumerate(seeds[:12], 1):
        price = f"₹{item.price/100_000:g} lakh" if item.price else "Price not stated"
        area = f"{item.area_sqft:g} sq.ft." if item.area_sqft else "Area not stated"
        if item.contact_verification == "public_cross_post_owner_contact" and item.phone:
            contact = f"VERIFIED/PUBLIC CROSS-POST CONTACT: {item.phone}"
        elif item.phone and item.phone_public:
            contact = f"PUBLIC CONTACT — NEEDS PROPERTY CONFIRMATION: {item.phone}"
        else:
            contact = "UNRESOLVED — no qualifying public phone matched yet"
        lines.extend([
            "",
            f"{index}. {item.title}",
            f"{area} · {price}",
            f"Portal: {_host(item.url)} · Seller: {item.seller_type.value.replace('_',' ').title()}",
            f"Contact: {contact}",
            f"Source: {item.url}",
        ])
        diagnostics = [e for e in item.evidence if e.startswith("Contact hunt checked") or e.startswith("Public phone found") or e.startswith("Contact match score")]
        for evidence in diagnostics[-3:]:
            lines.append(f"Evidence: {evidence}")
    return "\n".join(lines)


async def run(locality: str, telegram: bool = False) -> str:
    query = parse_query(f"plots in {locality}")
    portal_results = await search_profiles(query, profiles=("portals",))
    seeds = deduplicate([item for item in portal_results if _owner_seed(item)])
    resolver = PublicOwnerContactResolver.from_environment()
    if resolver is not None and seeds:
        seeds = await resolver.enrich(seeds)
    seeds = deduplicate(seeds)
    message = _format_contact_first(locality, seeds)
    if telegram:
        await send_message(message)
    return message


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve publicly exposed contacts for MagicBricks/99acres owner listings")
    parser.add_argument("--locality", default="Kalapatti")
    parser.add_argument("--telegram", action="store_true")
    args = parser.parse_args()
    print(asyncio.run(run(args.locality, telegram=args.telegram)))


if __name__ == "__main__":
    main()
