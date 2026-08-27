from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class SourceMode(StrEnum):
    ALLOWED = "allowed"
    AUTHORIZED_ONLY = "authorized_only"
    MANUAL_IMPORT = "manual_import"
    DISABLED = "disabled"


class SellerType(StrEnum):
    VERIFIED_OWNER = "verified_owner"
    PROBABLE_OWNER = "probable_owner"
    BROKER = "broker"
    BUILDER = "builder"
    UNKNOWN = "unknown"


@dataclass(slots=True)
class SearchQuery:
    locality: str
    property_type: str = "plot"
    max_price: int | None = None
    transaction: str = "sale"
    max_age_days: int = 90


@dataclass(slots=True)
class Listing:
    source: str
    source_id: str
    url: str
    title: str
    description: str
    locality: str
    property_type: str
    price: int | None = None
    area_sqft: float | None = None
    phone: str | None = None
    phone_public: bool = False
    seller_claim: str | None = None
    seller_type: SellerType = SellerType.UNKNOWN
    owner_confidence: int = 0
    locality_confidence: int = 0
    original_posted_at: datetime | None = None
    date_confidence: int = 0
    date_status: str = "unverified"
    seller_history_count: int = 0
    broker_risk: int = 0
    contact_verification: str = "hidden_contact"
    matching_contact_sources: int = 0
    reveal_required: bool = False
    reveal_priority: int = 0
    evidence: list[str] = field(default_factory=list)


class Collector(Protocol):
    source_id: str

    async def search(self, query: SearchQuery) -> list[Listing]: ...
