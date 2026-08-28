from __future__ import annotations

import asyncio
import os
import re
from html import unescape
from urllib.parse import urlparse

import httpx

from .domain import Listing, SellerType
from .processing import normalize_phone

AREA_RE = re.compile(r"([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)\s*(?:sq\.?\s*ft|sqft|square feet)", re.I)
PRICE_RE = re.compile(r"(?:₹|rs\.?|inr)?\s*([0-9]+(?:\.[0-9]+)?)\s*(crores?|cr|lakhs?|lacs?)", re.I)
FULL_PHONE_RE = re.compile(r"(?:\+?91[\s.-]?)?([6-9](?:[\s.-]?\d){9})(?!\d)")
MASKED_PHONE_RE = re.compile(r"(?:\+?91[\s.-]?)?([6-9]\d{3,5})[xX*•]{3,}")
OWNER_PATTERNS = [
    re.compile(r"\b(?:posted by|owner|contact owner|seller)\s*[:\-]\s*([A-Za-z][A-Za-z .]{1,60}?)(?=\s+(?:\+?91|₹|rs\.?|inr|plot|land|contact|property|\d{3,5}\s*(?:sq|square))|$)", re.I),
    re.compile(r"\b([A-Z][A-Za-z .]{1,50})\s+(?:Owner|Individual)\b", re.I),
]
BUSINESS_WORDS = {"realtor", "realtors", "realty", "properties", "property", "developers", "developer", "builders", "builder", "estate", "agency", "consultant", "consultants"}


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _strip_html(value: str) -> str:
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", value or "", flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


def _area(text: str) -> float | None:
    match = AREA_RE.search(text or "")
    return float(match.group(1).replace(",", "")) if match else None


def _price(text: str) -> int | None:
    match = PRICE_RE.search(text or "")
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).lower()
    return int(value * (10_000_000 if unit.startswith(("cr", "crore")) else 100_000))


def _owner(text: str) -> str | None:
    for pattern in OWNER_PATTERNS:
        match = pattern.search(text or "")
        if not match:
            continue
        name = " ".join(match.group(1).split()).strip(" .,-")
        if name.lower() not in {"owner", "individual", "contact owner", "seller"}:
            return name
    return None


def _business_like(owner: str | None, text: str) -> bool:
    haystack = f"{owner or ''} {text[:5000]}".lower()
    words = set(re.findall(r"[a-z]+", haystack))
    return bool(words & BUSINESS_WORDS) and any(marker in haystack for marker in ("agent", "agency", "realtor", "realty", "builder", "developer", "properties"))


def _full_phones(text: str) -> list[str]:
    seen: set[str] = set()
    phones: list[str] = []
    for match in FULL_PHONE_RE.finditer(text or ""):
        phone = normalize_phone(match.group(0))
        if phone and phone not in seen:
            seen.add(phone)
            phones.append(phone)
    return phones


def _masked_prefix(text: str) -> str | None:
    match = MASKED_PHONE_RE.search(text or "")
    return match.group(1) if match else None


def _detail_like(url: str) -> bool:
    path = urlparse(url).path.lower()
    return "/property-detail/" in path or path.endswith(".htm")


class RealEstateIndiaOwnerCollector:
    """Discover RealEstateIndia owner/individual plot listings and public contact evidence.

    Uses only public search/page content. It does not activate "View Phone No.", login, OTP,
    CAPTCHA, or subscription-controlled contact flows.
    """

    def __init__(self, google_key: str = "", cse_id: str = "", tavily_key: str = "", client: httpx.AsyncClient | None = None) -> None:
        self.google_key = google_key
        self.cse_id = cse_id
        self.tavily_key = tavily_key
        self.client = client or httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 OwnerPlotFinder/1.1 (+realestateindia)"})

    @classmethod
    def from_environment(cls):
        google = os.getenv("GOOGLE_CSE_API_KEY", "").strip()
        cse = os.getenv("GOOGLE_CSE_ID", "").strip()
        tavily = os.getenv("TAVILY_API_KEY", "").strip()
        return cls(google, cse, tavily) if (google and cse) or tavily else None

    async def _google(self, query: str) -> list[dict]:
        if not self.google_key or not self.cse_id:
            return []
        try:
            response = await self.client.get("https://www.googleapis.com/customsearch/v1", params={"key": self.google_key, "cx": self.cse_id, "q": query, "num": 10})
            response.raise_for_status()
            return [{"url": item.get("link", ""), "title": item.get("title", ""), "snippet": item.get("snippet", "") or "", "engine": "google"} for item in response.json().get("items", [])]
        except (httpx.HTTPError, ValueError, KeyError):
            return []

    async def _tavily(self, query: str) -> list[dict]:
        if not self.tavily_key:
            return []
        try:
            response = await self.client.post("https://api.tavily.com/search", headers={"Authorization": f"Bearer {self.tavily_key}", "Content-Type": "application/json"}, json={"query": query, "topic": "general", "search_depth": "advanced", "max_results": 15, "include_answer": False, "include_raw_content": "text", "include_domains": ["realestateindia.com"]})
            response.raise_for_status()
            return [{"url": item.get("url", ""), "title": item.get("title", ""), "snippet": f"{item.get('content','')} {item.get('raw_content','')}", "engine": "tavily"} for item in response.json().get("results", [])]
        except (httpx.HTTPError, ValueError, KeyError):
            return []

    async def _search(self, query: str) -> list[dict]:
        groups = await asyncio.gather(self._google(query), self._tavily(query))
        seen: set[str] = set()
        output: list[dict] = []
        for group in groups:
            for item in group:
                url = item.get("url", "")
                if url and url not in seen and _host(url).endswith("realestateindia.com"):
                    seen.add(url)
                    output.append(item)
        return output

    async def _page_text(self, url: str) -> str:
        try:
            response = await self.client.get(url)
            if response.status_code >= 400 or "text/html" not in response.headers.get("content-type", "").lower():
                return ""
            return _strip_html(response.text)[:140000]
        except httpx.HTTPError:
            return ""

    def _listing(self, url: str, title: str, text: str, locality: str, engine: str) -> Listing | None:
        lower = text.lower()
        if locality.lower() not in lower or not re.search(r"\b(plot|land|residential plot|residential land)\b", lower):
            return None
        area = _area(text)
        if not area:
            return None
        owner = _owner(text)
        owner_marker = bool(re.search(r"\b(owner|individual|contact owner|posted by)\b", lower))
        if not owner_marker:
            return None
        business = _business_like(owner, text)
        seller_type = SellerType.BROKER if business else SellerType.PROBABLE_OWNER
        owner_confidence = 20 if business else (88 if owner else 72)
        phones = _full_phones(text)
        prefix = _masked_prefix(text)
        evidence = [f"RealEstateIndia seed discovered via {engine}", "RealEstateIndia owner/individual marker present"]
        if prefix:
            evidence.append(f"RealEstateIndia visible masked phone prefix: {prefix}")
        if business:
            evidence.append("RealEstateIndia advertiser looks like realtor/business; rejected as private-owner contact")
        phone = None
        phone_public = False
        verification = "realestateindia_owner_seed"
        if phones and not business:
            phone = phones[0]
            phone_public = True
            verification = "realestateindia_public_owner_contact"
            evidence.extend(["Public phone found directly on RealEstateIndia detail/indexed page", "same property via exact RealEstateIndia listing"])
        elif phones and business:
            evidence.append(f"Public phone present but not accepted as owner number: {phones[0]}")
        return Listing(source="realestateindia.com", source_id=url, url=url, title=title or "RealEstateIndia owner plot", description=text[:30000], locality=locality, property_type="plot", price=_price(text), area_sqft=area, phone=phone, phone_public=phone_public, seller_claim=owner or "owner", seller_type=seller_type, owner_confidence=owner_confidence, locality_confidence=100, contact_verification=verification, matching_contact_sources=1 if phone_public else 0, evidence=evidence)

    async def search(self, locality: str) -> list[Listing]:
        queries = [
            f'site:realestateindia.com/property-detail "{locality}" Coimbatore residential plot owner',
            f'site:realestateindia.com "{locality}" Coimbatore residential land "Individual"',
            f'site:realestateindia.com "{locality}" Coimbatore plot "View Phone No"',
            f'site:realestateindia.com "{locality}" Coimbatore plot "Contact Owner"',
        ]
        results: list[Listing] = []
        seen: set[str] = set()
        for query in queries:
            for item in await self._search(query):
                url = item.get("url", "")
                if not url or url in seen:
                    continue
                page = await self._page_text(url) if _detail_like(url) else ""
                combined = re.sub(r"\s+", " ", f"{item.get('title','')} {item.get('snippet','')} {page}").strip()
                listing = self._listing(url, item.get("title", ""), combined, locality, item.get("engine", "search"))
                if listing:
                    seen.add(url)
                    results.append(listing)
        return results
