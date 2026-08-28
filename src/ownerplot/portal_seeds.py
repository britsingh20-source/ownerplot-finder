from __future__ import annotations

import os
import re
from urllib.parse import urlparse

import httpx

from .domain import Listing, SellerType


AREA_RE = re.compile(r"([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)\s*(?:sq\.?\s*ft|sqft|square feet)", re.I)
PRICE_RE = re.compile(r"(?:₹|rs\.?|inr)?\s*([0-9]+(?:\.[0-9]+)?)\s*(crores?|cr|lakhs?|lacs?)", re.I)
OWNER_PATTERNS = [
    re.compile(r"\bowner\s*[:\-]\s*([A-Za-z][A-Za-z .]{1,60})", re.I),
    re.compile(r"\bcontact owner\b.*?\b([A-Z][A-Za-z .]{1,45})\s*\+?91", re.I),
]


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _area(text: str) -> float | None:
    m = AREA_RE.search(text or "")
    return float(m.group(1).replace(",", "")) if m else None


def _price(text: str) -> int | None:
    m = PRICE_RE.search(text or "")
    if not m:
        return None
    value = float(m.group(1)); unit = m.group(2).lower()
    return int(value * (10_000_000 if unit.startswith(("cr", "crore")) else 100_000))


def _owner(text: str) -> str | None:
    for pattern in OWNER_PATTERNS:
        match = pattern.search(text or "")
        if match:
            value = " ".join(match.group(1).split()).strip(" .,-")
            if value.lower() not in {"owner", "contact owner", "individual"}:
                return value
    return None


def _detail_like(url: str, text: str) -> bool:
    host = _host(url)
    path = urlparse(url).path.lower()
    lower = (text or "").lower()
    if host.endswith("magicbricks.com"):
        return "propertydetails" in path or ("contact owner" in lower and bool(AREA_RE.search(text)))
    if host.endswith("99acres.com"):
        # -npffid URLs are generally property/project detail surfaces; reject broad ffid category pages.
        return "-npffid" in path and not path.endswith("-ffid")
    return False


class PortalOwnerSeedCollector:
    """Google-CSE discovery of individual MagicBricks/99acres owner property detail pages."""

    def __init__(self, api_key: str, cse_id: str, client: httpx.AsyncClient | None = None) -> None:
        self.api_key = api_key
        self.cse_id = cse_id
        self.client = client or httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent":"OwnerPlotFinder/0.4 (+portal-owner-seeds)"})

    @classmethod
    def from_environment(cls) -> "PortalOwnerSeedCollector | None":
        key = os.getenv("GOOGLE_CSE_API_KEY", "").strip(); cse = os.getenv("GOOGLE_CSE_ID", "").strip()
        return cls(key, cse) if key and cse else None

    async def _search(self, q: str) -> list[dict]:
        try:
            response = await self.client.get("https://www.googleapis.com/customsearch/v1", params={"key":self.api_key,"cx":self.cse_id,"q":q,"num":10})
            response.raise_for_status()
            return response.json().get("items", [])
        except (httpx.HTTPError, ValueError, KeyError):
            return []

    async def _fetch_public_text(self, url: str) -> str:
        # Portal detail pages are discovery-only; no attempts are made to reveal protected contacts.
        try:
            response = await self.client.get(url)
            if response.status_code >= 400 or "text/html" not in response.headers.get("content-type", "").lower():
                return ""
            body = re.sub(r"<script\b[^>]*>.*?</script>", " ", response.text, flags=re.I | re.S)
            body = re.sub(r"<style\b[^>]*>.*?</style>", " ", body, flags=re.I | re.S)
            body = re.sub(r"<[^>]+>", " ", body)
            return re.sub(r"\s+", " ", body)[:100_000]
        except httpx.HTTPError:
            return ""

    async def search(self, locality: str) -> list[Listing]:
        queries = [
            f'site:magicbricks.com/propertyDetails "{locality}" Coimbatore (plot OR land) "Contact Owner"',
            f'site:magicbricks.com/propertyDetails "{locality}" Coimbatore "Owner:" plot',
            f'site:99acres.com "{locality}" Coimbatore residential land "owner"',
            f'site:99acres.com "{locality}" Coimbatore plot "posted by owner"',
        ]
        seen: set[str] = set(); output: list[Listing] = []
        for query in queries:
            for item in await self._search(query):
                url = item.get("link", "")
                if not url or url in seen or _host(url) not in {"magicbricks.com", "99acres.com"}:
                    continue
                seen.add(url)
                snippet = " ".join(str(item.get(key, "")) for key in ("title", "snippet", "htmlSnippet"))
                page = await self._fetch_public_text(url)
                text = re.sub(r"\s+", " ", f"{snippet} {page}").strip()
                if locality.lower() not in text.lower() or not re.search(r"\b(plot|land|residential plot|residential land)\b", text, re.I):
                    continue
                if not _detail_like(url, text):
                    continue
                owner = _owner(text)
                owner_marker = bool(re.search(r"\b(contact owner|owner\s*:|posted by owner|owner property|individual)\b", text, re.I))
                if not owner_marker:
                    continue
                output.append(Listing(
                    source=_host(url), source_id=url, url=url,
                    title=item.get("title") or "Owner plot listing",
                    description=text[:25_000], locality=locality, property_type="plot",
                    price=_price(text), area_sqft=_area(text), phone=None, phone_public=False,
                    seller_claim=owner or "owner", seller_type=SellerType.PROBABLE_OWNER,
                    owner_confidence=80 if owner else 65, locality_confidence=100,
                    contact_verification="portal_owner_seed",
                    evidence=["Individual portal detail discovered through Google CSE", "Portal explicitly labels advertiser as owner", "Protected portal contact not accessed"],
                ))
        return output
