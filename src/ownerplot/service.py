from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from .domain import Collector, Listing, SearchQuery, SellerType
from .policy import enforce_contact_policy
from .processing import analyze_seller_history, classify_seller, deduplicate, is_plot, normalize_phone, validate_locality
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
                normalized.append(listing)
        analyze_seller_history(normalized)
        retained=[]
        cutoff=datetime.now(timezone.utc)-timedelta(days=query.max_age_days)
        for listing in normalized:
            if listing.original_posted_at and listing.original_posted_at<cutoff:
                continue
            classify_seller(listing)
            if listing.seller_type in {SellerType.BROKER, SellerType.BUILDER}:
                continue
            retained.append(listing)
        results = sorted(deduplicate(retained), key=lambda x: (x.date_status=="verified_recent",x.owner_confidence,bool(x.phone)), reverse=True)
        if self.cache:
            self.cache.put(query, results)
        return results


def format_results(query: SearchQuery, listings: list[Listing]) -> str:
    public_contacts = sum(bool(item.phone) and item.phone_public for item in listings)
    captured_contacts = sum(item.contact_verification=="authorized_captured_owner" for item in listings)
    reveal_required = sum(item.reveal_required for item in listings)
    verified_dates=sum(item.date_status=="verified_recent" for item in listings)
    lines = [f"OWNER PLOTS — {query.locality.upper()}", f"Freshness: posted or updated within the last {query.max_age_days} days", f"Found {len(listings)} unique plots; {public_contacts} public contacts; {captured_contacts} authorized saved contacts; {reveal_required} require portal reveal; {verified_dates} verified dates."]
    for index, item in enumerate(listings[:10], 1):
        price = f"₹{item.price/100_000:g} lakh" if item.price else "Price not stated"
        area = f"{item.area_sqft:g} sq.ft." if item.area_sqft else "Area not stated"
        if item.phone:
            contact=item.phone
        elif item.reveal_required:
            contact=f"Authorized reveal required · priority {item.reveal_priority}/100"
        else:
            contact="Not publicly displayed — use source enquiry"
        posted=item.original_posted_at.date().isoformat() if item.original_posted_at else "Unverified — review before contact"
        lines.extend(["", f"{index}. {item.title}", f"{area} · {price}", f"Seller: {item.seller_type.value.replace('_',' ').title()} · owner {item.owner_confidence}% · broker risk {item.broker_risk}%", f"Original post/update: {posted}", f"Contact status: {item.contact_verification.replace('_',' ').title()}", f"Contact: {contact}", f"Source: {item.url}"])
        if item.reveal_required:
            lines.append("After legitimately revealing it, send: /capture <listing URL> <displayed number>")
    if not listings:
        lines.append("No verified public owner-plot results are currently available from enabled sources.")
    return "\n".join(lines)
