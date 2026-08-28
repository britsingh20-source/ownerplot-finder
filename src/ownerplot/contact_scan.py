from __future__ import annotations

import argparse
import asyncio

from .contact_resolver import PublicOwnerContactResolver
from .github_runner import search_profiles, send_message
from .processing import deduplicate
from .query import parse_query
from .service import format_results


async def run(locality: str, telegram: bool = False) -> str:
    query = parse_query(f"plots in {locality}")
    listings = await search_profiles(query)
    resolver = PublicOwnerContactResolver.from_environment()
    if resolver is not None:
        listings = await resolver.enrich(listings)
    listings = deduplicate(listings)
    message = "OWNERPLOT CONTACT-FIRST SEARCH\n\n" + format_results(query, listings)
    if telegram:
        await send_message(message)
    return message


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve publicly exposed owner contacts for owner-posted property listings")
    parser.add_argument("--locality", default="Kalapatti")
    parser.add_argument("--telegram", action="store_true")
    args = parser.parse_args()
    print(asyncio.run(run(args.locality, telegram=args.telegram)))


if __name__ == "__main__":
    main()
