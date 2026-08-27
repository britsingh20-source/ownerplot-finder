from __future__ import annotations

from dataclasses import dataclass

from .domain import Listing, SourceMode


@dataclass(frozen=True)
class SourcePolicy:
    source_id: str
    mode: SourceMode
    enabled: bool

    def assert_collection_allowed(self, authorized: bool = False) -> None:
        if not self.enabled or self.mode == SourceMode.DISABLED:
            raise PermissionError(f"Source {self.source_id} is disabled")
        if self.mode == SourceMode.AUTHORIZED_ONLY and not authorized:
            raise PermissionError(f"Source {self.source_id} requires authorization")


def enforce_contact_policy(listing: Listing) -> Listing:
    if listing.phone and not listing.phone_public:
        listing.phone = None
        listing.evidence.append("Non-public contact removed")
    return listing

