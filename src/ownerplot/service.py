from __future__ import annotations

import asyncio

from .domain import Collector, Listing, SearchQuery, SellerType
from .policy import enforce_contact_policy
from .processing import classify_seller, deduplicate, is_plot, normalize_phone, validate_locality
from .cache import ListingCache


class SearchService:
    def __init__(self, collectors: list[Collector], cache: ListingCache | None = None) -> None:
        self.collectors = collectors
        self.cache = cache

    async def search(self, query: SearchQuery, force_refresh: bool = False) -> list[Listing]:
        if not force_refresh and self.cache and (cached := self.cache.get(query)) is not None:
            return cached
        batches = await asyncio.gather(*(c.search(query) for c in self.collectors), return_exceptions=True)
        failures = [batch for batch in batches if isinstance(batch, BaseException)]
        if failures and len(failures) == len(batches):
            first = failures[0]
            raise RuntimeError(f"All search collectors failed: {type(first).__name__}: {first}") from first
        normalized: list[Listing] = []
        for batch in batches:
            if isinstance(batch, BaseException):
                continue
            for listing in batch:
                listing.phone = normalize_phone(listing.phone)
                enforce_contact_policy(listing)
                if not is_plot(listing) or validate_locality(listing, query.locality) < 80:
                    continue
                if query.max_price and listing.price and listing.price > query.max_price:
                    continue
                classify_seller(listing)
                if listing.seller_type in {SellerType.BROKER, SellerType.BUILDER}:
                    continue
                normalized.append(listing)
        results = sorted(deduplicate(normalized), key=lambda x: (x.owner_confidence, bool(x.phone)), reverse=True)
        if self.cache:
            self.cache.put(query, results)
        return results


def format_results(query: SearchQuery, listings: list[Listing]) -> str:
    with_contacts = sum(bool(item.phone) for item in listings)
    lines = [f"OWNER PLOTS — {query.locality.upper()}", f"Freshness: posted or updated within the last {query.max_age_days} days", f"Found {len(listings)} unique plots; {with_contacts} have public owner contacts."]
    for index, item in enumerate(listings[:10], 1):
        price = f"₹{item.price/100_000:g} lakh" if item.price else "Price not stated"
        area = f"{item.area_sqft:g} sq.ft." if item.area_sqft else "Area not stated"
        contact = item.phone or "Not publicly displayed — use source enquiry"
        lines.extend(["", f"{index}. {item.title}", f"{area} · {price}", f"Owner confidence: {item.owner_confidence}%", f"Contact: {contact}", f"Source: {item.url}"])
    if not listings:
        lines.append("No verified public owner-plot results are currently available from enabled sources.")
    return "\n".join(lines)
