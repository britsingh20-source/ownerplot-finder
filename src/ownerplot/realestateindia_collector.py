from __future__ import annotations

import asyncio
import os
import re
from html import unescape
from urllib.parse import urlparse

import httpx
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

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
BUSINESS_WORDS = {"realtor", "realtors", "realty", "properties", "developers", "developer", "builders", "builder", "estate", "agency", "consultant", "consultants", "promoters", "promoter"}


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _strip_html(value: str) -> str:
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", value or "", flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", unescape(text)).strip()


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
        m = pattern.search(text or "")
        if not m:
            continue
        name = " ".join(m.group(1).split()).strip(" .,-")
        if name.lower() not in {"owner", "individual", "contact owner", "seller"}:
            return name
    return None


def _business_like(owner: str | None, text: str) -> bool:
    haystack = f"{owner or ''} {text[:5000]}".lower()
    words = set(re.findall(r"[a-z]+", haystack))
    return bool(words & BUSINESS_WORDS) and any(x in haystack for x in ("agent", "agency", "realtor", "realty", "builder", "developer", "properties", "promoter"))


def _masked_prefix(text: str) -> str | None:
    m = MASKED_PHONE_RE.search(text or "")
    return m.group(1) if m else None


def _detail_like(url: str) -> bool:
    return "/property-detail/" in urlparse(url).path.lower()


def _category_urls(locality: str) -> list[str]:
    slug = re.sub(r"[^a-z0-9]+", "-", locality.lower()).strip("-")
    base = f"https://www.realestateindia.com/coimbatore-property/residential-land-for-sale-in-{slug}.htm"
    return [
        base,
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


def _full_phone_near_owner(text: str, owner: str | None) -> str | None:
    """Accept only a complete phone locally tied to an owner/contact-owner context.

    This avoids treating site support numbers or unrelated advertiser numbers as the owner's number.
    """
    lower = text.lower()
    for m in FULL_PHONE_RE.finditer(text or ""):
        start = max(0, m.start() - 260); end = min(len(text), m.end() + 260)
        window = lower[start:end]
        owner_hit = bool(owner and owner.lower() in window)
        marker_hit = any(marker in window for marker in ("contact owner", "posted by owner", "owner", "individual"))
        if owner_hit or marker_hit:
            return normalize_phone(m.group(0))
    return None


class RealEstateIndiaOwnerCollector:
    """RealEstateIndia owner collector with browser rendering fallback.

    It uses only public pages and public network responses. It never clicks View Number/Contact Owner,
    never submits login/OTP/CAPTCHA, and never bypasses subscription or access controls.
    """

    def __init__(self, google_key: str = "", cse_id: str = "", tavily_key: str = "", client: httpx.AsyncClient | None = None) -> None:
        self.google_key = google_key
        self.cse_id = cse_id
        self.tavily_key = tavily_key
        self.client = client or httpx.AsyncClient(timeout=20, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/126 Safari/537.36"})

    @classmethod
    def from_environment(cls):
        return cls(
            os.getenv("GOOGLE_CSE_API_KEY", "").strip(),
            os.getenv("GOOGLE_CSE_ID", "").strip(),
            os.getenv("TAVILY_API_KEY", "").strip(),
        )

    def _listing(self, url: str, title: str, text: str, locality: str, engine: str) -> Listing | None:
        lower = text.lower()
        if locality.lower() not in lower or not re.search(r"\b(plot|land|residential plot|residential land)\b", lower):
            return None
        area = _area(text)
        if not area:
            return None
        owner = _owner(text)
        owner_marker = bool(re.search(r"\b(owner|individual|contact owner|posted by owner)\b", lower))
        if not owner_marker:
            return None
        business = _business_like(owner, text)
        evidence = [f"RealEstateIndia seed discovered via {engine}", "RealEstateIndia owner/individual marker present"]
        prefix = _masked_prefix(text)
        if prefix:
            evidence.append(f"RealEstateIndia visible masked phone prefix: {prefix}")
        if business:
            evidence.append("RealEstateIndia advertiser looks like realtor/business; rejected as private-owner contact")
        phone = None
        phone_public = False
        verification = "realestateindia_owner_seed"
        if _detail_like(url) and not business:
            phone = _full_phone_near_owner(text, owner)
            if phone:
                phone_public = True
                verification = "realestateindia_public_owner_contact"
                evidence.extend(["Public phone found in exact RealEstateIndia owner context", "same property via exact RealEstateIndia listing"])
        return Listing(
            source="realestateindia.com", source_id=f"{url}#{owner or area}", url=url,
            title=title or "RealEstateIndia owner plot", description=text[:30000], locality=locality,
            property_type="plot", price=_price(text), area_sqft=area, phone=phone,
            phone_public=phone_public, seller_claim=owner or "owner",
            seller_type=SellerType.BROKER if business else SellerType.PROBABLE_OWNER,
            owner_confidence=20 if business else (90 if owner else 72), locality_confidence=100,
            contact_verification=verification, matching_contact_sources=1 if phone_public else 0,
            evidence=evidence,
        )

    async def _http_has_owner_content(self, url: str) -> bool:
        try:
            r = await self.client.get(url)
            if r.status_code >= 400:
                return False
            text = _strip_html(r.text)
            return bool(re.search(r"\bOwner\b|\bIndividual\b", text, re.I) and re.search(r"\b(plot|land)\b", text, re.I))
        except httpx.HTTPError:
            return False

    async def _browser_collect(self, locality: str) -> list[Listing]:
        output: list[Listing] = []
        seen: set[tuple[str, int]] = set()
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            context = await browser.new_context(
                viewport={"width": 1440, "height": 1200},
                locale="en-IN",
                user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
            )
            page = await context.new_page()
            for category_url in _category_urls(locality):
                try:
                    await page.goto(category_url, wait_until="domcontentloaded", timeout=30000)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=8000)
                    except PlaywrightTimeoutError:
                        pass
                    await page.wait_for_timeout(1800)
                    body = re.sub(r"\s+", " ", await page.locator("body").inner_text()).strip()
                    if locality.lower() not in body.lower():
                        continue
                    anchors = await page.locator("a[href*='property-detail']").evaluate_all(
                        "els => els.map(a => ({href: a.href, text: (a.innerText||'').trim(), parent: (a.closest('article,li,div')?.innerText||'').slice(0,5000)}))"
                    )
                    for item in anchors:
                        href = str(item.get("href") or "")
                        card = re.sub(r"\s+", " ", str(item.get("parent") or "")).strip()
                        if not href or _host(href) != "realestateindia.com":
                            continue
                        if not re.search(r"\bOwner\b|\bIndividual\b", card, re.I):
                            continue
                        if locality.lower() not in card.lower() or not re.search(r"\b(plot|land)\b", card, re.I):
                            continue
                        owner = _owner(card) or "owner"
                        area = int(_area(card) or 0)
                        key = (owner.lower(), area)
                        if not area or key in seen:
                            continue
                        detail_text = card
                        try:
                            detail = await context.new_page()
                            await detail.goto(href, wait_until="domcontentloaded", timeout=30000)
                            try:
                                await detail.wait_for_load_state("networkidle", timeout=7000)
                            except PlaywrightTimeoutError:
                                pass
                            await detail.wait_for_timeout(1200)
                            detail_text = re.sub(r"\s+", " ", await detail.locator("body").inner_text()).strip()
                            await detail.close()
                        except Exception:
                            detail_text = card
                        listing = self._listing(href, item.get("text") or f"RealEstateIndia owner card — {owner}", detail_text, locality, "Playwright rendered category/detail")
                        if listing:
                            listing.evidence.append("RealEstateIndia browser-rendered page used")
                            seen.add(key)
                            output.append(listing)
                except Exception:
                    continue
            await browser.close()
        return output

    async def _google(self, query: str) -> list[dict]:
        if not self.google_key or not self.cse_id:
            return []
        try:
            r = await self.client.get("https://www.googleapis.com/customsearch/v1", params={"key": self.google_key, "cx": self.cse_id, "q": query, "num": 10})
            r.raise_for_status()
            return [{"url": i.get("link", ""), "title": i.get("title", ""), "snippet": i.get("snippet", "") or ""} for i in r.json().get("items", [])]
        except (httpx.HTTPError, ValueError, KeyError):
            return []

    async def _search_fallback(self, locality: str) -> list[Listing]:
        out: list[Listing] = []
        seen: set[str] = set()
        for q in (
            f'site:realestateindia.com/property-detail "{locality}" Coimbatore residential plot owner',
            f'site:realestateindia.com "{locality}" Coimbatore residential land "Owner"',
        ):
            for item in await self._google(q):
                url = item.get("url", "")
                if not url or url in seen or not _detail_like(url):
                    continue
                text = f"{item.get('title','')} {item.get('snippet','')}"
                listing = self._listing(url, item.get("title", ""), text, locality, "Google fallback")
                if listing:
                    seen.add(url); out.append(listing)
        return out

    async def search(self, locality: str) -> list[Listing]:
        # A quick HTTP probe is kept only as a diagnostic; browser rendering is the primary path.
        main_url = _category_urls(locality)[0]
        http_owner_content = await self._http_has_owner_content(main_url)
        browser = await self._browser_collect(locality)
        fallback = await self._search_fallback(locality)
        seen: set[tuple[str, int]] = set(); out: list[Listing] = []
        for listing in [*browser, *fallback]:
            key = ((listing.seller_claim or "owner").lower(), int(listing.area_sqft or 0))
            if key in seen:
                continue
            listing.evidence.append(f"RealEstateIndia plain-HTTP owner content: {'yes' if http_owner_content else 'no'}")
            seen.add(key); out.append(listing)
        return out
