from __future__ import annotations

import asyncio
import os
import re
from html import unescape
from urllib.parse import urljoin, urlparse

import httpx

from .domain import Listing, SellerType
from .processing import normalize_phone

AREA_RE = re.compile(r"([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)\s*(?:sq\.?\s*ft|sqft|square feet)", re.I)
PRICE_RE = re.compile(r"(?:₹|rs\.?|inr)?\s*([0-9]+(?:\.[0-9]+)?)\s*(crores?|cr|lakhs?|lacs?)", re.I)
FULL_PHONE_RE = re.compile(r"(?:\+?91[\s.-]?)?([6-9](?:[\s.-]?\d){9})(?!\d)")
MASKED_PHONE_RE = re.compile(r"(?:\+?91[\s.-]?)?([6-9]\d{3,5})[xX*•]{3,}")
DETAIL_LINK_RE = re.compile(r'href=["\']([^"\']*(?:/property-detail/|property-detail)[^"\']+)["\']', re.I)
OWNER_CARD_RE = re.compile(r"([A-Za-z][A-Za-z .]{1,60})\s+(Owner|Individual)\b", re.I)
OWNER_PATTERNS = [
    re.compile(r"\b(?:posted by|owner|contact owner|seller)\s*[:\-]\s*([A-Za-z][A-Za-z .]{1,60}?)(?=\s+(?:\+?91|₹|rs\.?|inr|plot|land|contact|property|\d{3,5}\s*(?:sq|square))|$)", re.I),
    re.compile(r"\b([A-Z][A-Za-z .]{1,50})\s+(?:Owner|Individual)\b", re.I),
]
BUSINESS_WORDS = {"realtor", "realtors", "realty", "properties", "developers", "developer", "builders", "builder", "estate", "agency", "consultant", "consultants", "promoters", "promoter"}


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
    return bool(words & BUSINESS_WORDS) and any(marker in haystack for marker in ("agent", "agency", "realtor", "realty", "builder", "developer", "properties", "promoter"))


def _full_phones(text: str) -> list[str]:
    seen: set[str] = set(); phones: list[str] = []
    for match in FULL_PHONE_RE.finditer(text or ""):
        phone = normalize_phone(match.group(0))
        if phone and phone not in seen:
            seen.add(phone); phones.append(phone)
    return phones


def _masked_prefix(text: str) -> str | None:
    match = MASKED_PHONE_RE.search(text or "")
    return match.group(1) if match else None


def _detail_like(url: str) -> bool:
    return "/property-detail/" in urlparse(url).path.lower()


def _category_urls(locality: str) -> list[str]:
    slug = re.sub(r"[^a-z0-9]+", "-", locality.lower()).strip("-")
    base = f"https://www.realestateindia.com/coimbatore-property/residential-land-for-sale-in-{slug}.htm"
    return [
        base,
        f"https://www.realestateindia.com/coimbatore-property/residential-land-for-sale-in-{slug}-price-11-lakhs-to-20-lakhs.htm",
        f"https://www.realestateindia.com/coimbatore-property/residential-land-for-sale-in-{slug}-price-21-lakhs-to-30-lakhs.htm",
        f"https://www.realestateindia.com/coimbatore-property/residential-land-for-sale-in-{slug}-price-31-lakhs-to-40-lakhs.htm",
        f"https://www.realestateindia.com/coimbatore-property/residential-land-for-sale-in-{slug}-price-41-lakhs-to-50-lakhs.htm",
        f"https://www.realestateindia.com/coimbatore-property/residential-land-for-sale-in-{slug}-price-51-lakhs-to-60-lakhs.htm",
        f"https://www.realestateindia.com/coimbatore-property/residential-land-for-sale-in-{slug}-price-61-lakhs-to-70-lakhs.htm",
        f"https://www.realestateindia.com/coimbatore-property/residential-land-for-sale-in-{slug}-price-71-lakhs-to-80-lakhs.htm",
        f"https://www.realestateindia.com/coimbatore-property/residential-land-for-sale-in-{slug}-price-81-lakhs-to-90-lakhs.htm",
        f"https://www.realestateindia.com/coimbatore-property/residential-land-for-sale-in-{slug}-price-91-lakhs-to-1-crore.htm",
        f"https://www.realestateindia.com/coimbatore-property/residential-land-for-sale-in-{slug}-price-1-crore-to-2-crores.htm",
        f"https://www.realestateindia.com/coimbatore-property/residential-land-for-sale-in-{slug}-price-2-crores-to-3-crores.htm",
        f"https://www.realestateindia.com/coimbatore-property/residential-land-for-sale-in-{slug}-price-3-crores-to-4-crores.htm",
    ]


def _owner_cards(text: str) -> list[str]:
    """Slice category/indexed text around explicit '<name> Owner' / Individual cards."""
    matches = list(OWNER_CARD_RE.finditer(text or ""))
    cards: list[str] = []
    for index, match in enumerate(matches):
        start = max(0, match.start() - 2200)
        end = matches[index + 1].start() if index + 1 < len(matches) else min(len(text), match.end() + 1800)
        card = text[start:end]
        if AREA_RE.search(card) and re.search(r"\b(plot|land)\b", card, re.I):
            cards.append(card)
    return cards


class RealEstateIndiaOwnerCollector:
    """Direct-first RealEstateIndia owner plot collector.

    Category/price pages are fetched first because they expose owner-labelled cards more reliably
    than search engines. Search-engine discovery remains a fallback. No View Phone/OTP/CAPTCHA or
    subscription-controlled action is triggered.
    """

    def __init__(self, google_key: str = "", cse_id: str = "", tavily_key: str = "", client: httpx.AsyncClient | None = None) -> None:
        self.google_key = google_key; self.cse_id = cse_id; self.tavily_key = tavily_key
        self.client = client or httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"})

    @classmethod
    def from_environment(cls):
        google = os.getenv("GOOGLE_CSE_API_KEY", "").strip(); cse = os.getenv("GOOGLE_CSE_ID", "").strip(); tavily = os.getenv("TAVILY_API_KEY", "").strip()
        return cls(google, cse, tavily)

    async def _google(self, query: str) -> list[dict]:
        if not self.google_key or not self.cse_id: return []
        try:
            response = await self.client.get("https://www.googleapis.com/customsearch/v1", params={"key": self.google_key, "cx": self.cse_id, "q": query, "num": 10}); response.raise_for_status()
            return [{"url": item.get("link", ""), "title": item.get("title", ""), "snippet": item.get("snippet", "") or "", "engine": "google"} for item in response.json().get("items", [])]
        except (httpx.HTTPError, ValueError, KeyError): return []

    async def _tavily(self, query: str) -> list[dict]:
        if not self.tavily_key: return []
        try:
            response = await self.client.post("https://api.tavily.com/search", headers={"Authorization": f"Bearer {self.tavily_key}", "Content-Type": "application/json"}, json={"query": query, "topic": "general", "search_depth": "advanced", "max_results": 15, "include_answer": False, "include_raw_content": "text", "include_domains": ["realestateindia.com"]}); response.raise_for_status()
            return [{"url": item.get("url", ""), "title": item.get("title", ""), "snippet": f"{item.get('content','')} {item.get('raw_content','')}", "engine": "tavily"} for item in response.json().get("results", [])]
        except (httpx.HTTPError, ValueError, KeyError): return []

    async def _fetch_html(self, url: str) -> str:
        try:
            response = await self.client.get(url)
            if response.status_code >= 400 or "text/html" not in response.headers.get("content-type", "").lower(): return ""
            return response.text
        except httpx.HTTPError: return ""

    def _listing(self, url: str, title: str, text: str, locality: str, engine: str) -> Listing | None:
        lower = text.lower()
        if locality.lower() not in lower or not re.search(r"\b(plot|land|residential plot|residential land)\b", lower): return None
        area = _area(text)
        if not area: return None
        owner = _owner(text); owner_marker = bool(re.search(r"\b(owner|individual|contact owner|posted by)\b", lower))
        if not owner_marker: return None
        business = _business_like(owner, text)
        seller_type = SellerType.BROKER if business else SellerType.PROBABLE_OWNER
        phones = _full_phones(text); prefix = _masked_prefix(text)
        evidence = [f"RealEstateIndia seed discovered via {engine}", "RealEstateIndia owner/individual marker present"]
        if prefix: evidence.append(f"RealEstateIndia visible masked phone prefix: {prefix}")
        if business: evidence.append("RealEstateIndia advertiser looks like realtor/business; rejected as private-owner contact")
        phone = None; phone_public = False; verification = "realestateindia_owner_seed"
        if phones and not business and _detail_like(url):
            phone = phones[0]; phone_public = True; verification = "realestateindia_public_owner_contact"
            evidence.extend(["Public phone found directly on exact RealEstateIndia listing", "same property via exact RealEstateIndia listing"])
        elif phones:
            evidence.append(f"Phone observed in category/card context but not accepted without exact listing attribution: {phones[0]}")
        return Listing(source="realestateindia.com", source_id=f"{url}#{owner or area}", url=url, title=title or "RealEstateIndia owner plot", description=text[:30000], locality=locality, property_type="plot", price=_price(text), area_sqft=area, phone=phone, phone_public=phone_public, seller_claim=owner or "owner", seller_type=seller_type, owner_confidence=20 if business else (90 if owner else 72), locality_confidence=100, contact_verification=verification, matching_contact_sources=1 if phone_public else 0, evidence=evidence)

    async def _direct_category_seeds(self, locality: str) -> list[Listing]:
        urls = _category_urls(locality)
        html_pages = await asyncio.gather(*(self._fetch_html(url) for url in urls))
        out: list[Listing] = []; seen: set[tuple[str, int]] = set()
        for url, html in zip(urls, html_pages):
            if not html: continue
            text = _strip_html(html)[:220000]
            detail_links = [urljoin(url, unescape(raw)) for raw in DETAIL_LINK_RE.findall(html)]
            for card in _owner_cards(text):
                owner = _owner(card) or "owner"; area = int(_area(card) or 0); key = (owner.lower(), area)
                if key in seen: continue
                # Prefer a detail link whose nearby HTML/text mentions same area/owner; otherwise keep category URL.
                chosen = url
                for candidate in detail_links[:80]:
                    if str(area) in candidate or owner.lower().replace(" ", "-") in candidate.lower():
                        chosen = candidate; break
                listing = self._listing(chosen, f"RealEstateIndia owner card — {owner}", card, locality, "direct category page")
                if listing:
                    seen.add(key); out.append(listing)
        return out

    async def _search_fallback(self, locality: str) -> list[Listing]:
        queries = [
            f'site:realestateindia.com/coimbatore-property/residential-land-for-sale-in-{locality.lower()}.htm "Owner"',
            f'site:realestateindia.com "{locality}" Coimbatore residential land "Owner"',
            f'site:realestateindia.com/property-detail "{locality}" Coimbatore residential plot owner',
            f'site:realestateindia.com "{locality}" Coimbatore plot "Contact Owner"',
        ]
        groups = []
        for q in queries:
            groups.extend(await asyncio.gather(self._google(q), self._tavily(q)))
        out: list[Listing] = []; seen: set[str] = set()
        for group in groups:
            for item in group:
                url = item.get("url", "")
                if not url or url in seen or not _host(url).endswith("realestateindia.com"): continue
                page_html = await self._fetch_html(url) if _detail_like(url) else ""
                combined = re.sub(r"\s+", " ", f"{item.get('title','')} {item.get('snippet','')} {_strip_html(page_html)}").strip()
                listing = self._listing(url, item.get("title", ""), combined, locality, item.get("engine", "search"))
                if listing: seen.add(url); out.append(listing)
        return out

    async def search(self, locality: str) -> list[Listing]:
        direct = await self._direct_category_seeds(locality)
        fallback = await self._search_fallback(locality)
        seen: set[tuple[str, int]] = set(); out: list[Listing] = []
        for listing in [*direct, *fallback]:
            key = ((listing.seller_claim or "owner").lower(), int(listing.area_sqft or 0))
            if key not in seen:
                seen.add(key); out.append(listing)
        return out
