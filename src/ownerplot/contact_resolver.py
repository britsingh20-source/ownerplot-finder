from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx

from .domain import Listing, SellerType
from .processing import normalize_phone, classify_seller


PHONE_RE = re.compile(r"(?:\+?91[\s.-]?)?([6-9](?:[\s.-]?\d){9})(?!\d)")
OWNER_NAME_RE = re.compile(r"\bowner\s*[:\-]\s*([A-Za-z][A-Za-z .]{1,60})", re.I)
DIM_RE = re.compile(r"\b(\d{2,3}(?:\.\d+)?)\s*[xX×]\s*(\d{2,3}(?:\.\d+)?)\b")
PORTAL_HOSTS = {"magicbricks.com", "99acres.com"}
GENERIC = {
    "plot","land","property","sale","owner","residential","coimbatore","kalapatti",
    "sqft","square","feet","road","near","contact","price","facing","resale","freehold",
}


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _is_portal(item: Listing) -> bool:
    host = _host(item.url)
    return host in PORTAL_HOSTS or any(host.endswith(f".{domain}") for domain in PORTAL_HOSTS)


def _owner_name(item: Listing) -> str | None:
    for text in (item.seller_claim or "", item.title, item.description):
        match = OWNER_NAME_RE.search(text or "")
        if match:
            name = " ".join(match.group(1).split()).strip(" .,-")
            if name.lower() not in {"owner", "contact owner", "individual"}:
                return name
    claim = (item.seller_claim or "").strip()
    if claim and claim.lower() not in {"owner", "contact owner", "posted by owner", "individual"}:
        return claim
    return None


def _dimensions(text: str) -> set[tuple[int, int]]:
    found = set()
    for left, right in DIM_RE.findall(text or ""):
        pair = tuple(sorted((round(float(left)), round(float(right)))))
        found.add(pair)
    return found


def _tokens(text: str) -> set[str]:
    values = set(re.findall(r"[a-z0-9]+", (text or "").lower()))
    return {value for value in values if len(value) > 2 and value not in GENERIC}


def _page_text(item: Listing) -> str:
    return f"{item.title} {item.description}"


def _score(target: Listing, candidate_text: str, candidate_url: str) -> tuple[int, list[str]]:
    text = candidate_text.lower()
    evidence: list[str] = []
    score = 0
    locality = target.locality.lower().strip()
    if locality and locality in text:
        score += 20
        evidence.append("same locality")
    owner = _owner_name(target)
    if owner and owner.lower() in text:
        score += 25
        evidence.append("same owner name")
    if target.area_sqft:
        area = round(target.area_sqft)
        area_patterns = {str(area), f"{area:,}", f"{target.area_sqft:g}"}
        if any(re.search(rf"\b{re.escape(value)}\s*(?:sq\.?\s*ft|sqft|square\s*feet)\b", text, re.I) for value in area_patterns):
            score += 30
            evidence.append("same plot area")
    target_dims = _dimensions(_page_text(target))
    candidate_dims = _dimensions(candidate_text)
    if target_dims and target_dims & candidate_dims:
        score += 20
        evidence.append("same dimensions")
    if target.price:
        lakhs = target.price / 100_000
        crores = target.price / 10_000_000
        price_terms = [f"{lakhs:g} lakh", f"{lakhs:g} lac", f"{crores:g} cr", f"{crores:g} crore"]
        if any(term.lower() in text for term in price_terms):
            score += 10
            evidence.append("same asking price")
    target_tokens = _tokens(_page_text(target))
    candidate_tokens = _tokens(candidate_text)
    overlap = target_tokens & candidate_tokens
    if len(overlap) >= 5:
        score += 15
        evidence.append("strong description/project overlap")
    elif len(overlap) >= 3:
        score += 8
        evidence.append("description/project overlap")
    if _host(candidate_url) in PORTAL_HOSTS:
        score -= 15
    return max(0, min(100, score)), evidence


def _queries(target: Listing) -> list[str]:
    owner = _owner_name(target)
    parts = [f'"{target.locality}"']
    if target.area_sqft:
        parts.append(f'"{round(target.area_sqft)} sqft"')
    dims = sorted(_dimensions(_page_text(target)))
    if dims:
        parts.append(f'"{dims[0][0]} X {dims[0][1]}"')
    if owner:
        parts.append(f'"{owner}"')
    distinctive = sorted(_tokens(_page_text(target)))
    if distinctive:
        parts.extend(f'"{token}"' for token in distinctive[:2])
    base = " ".join(parts)
    return [
        f"{base} (plot OR land) (phone OR contact OR whatsapp)",
        f"{base} (owner OR individual) property",
    ]


@dataclass(slots=True)
class ContactCandidate:
    url: str
    phone: str
    score: int
    evidence: list[str]


class PublicOwnerContactResolver:
    """Resolve only openly published numbers on strongly matching public cross-posts."""

    def __init__(self, api_key: str, cse_id: str, allowed_domains: set[str], client: httpx.AsyncClient | None = None) -> None:
        self.api_key = api_key
        self.cse_id = cse_id
        self.allowed_domains = {d.lower().removeprefix("www.") for d in allowed_domains}
        self.client = client or httpx.AsyncClient(timeout=12, follow_redirects=True, headers={"User-Agent":"OwnerPlotFinder/0.2 (+public-contact-correlation)"})
        self.max_listings = int(os.getenv("PUBLIC_CONTACT_RESOLVE_MAX_LISTINGS", "5"))
        self.max_queries = int(os.getenv("PUBLIC_CONTACT_RESOLVE_QUERIES", "2"))
        self.min_score = int(os.getenv("PUBLIC_CONTACT_MIN_MATCH_SCORE", "75"))

    @classmethod
    def from_environment(cls) -> "PublicOwnerContactResolver | None":
        key = os.getenv("GOOGLE_CSE_API_KEY", "").strip()
        cse = os.getenv("GOOGLE_CSE_ID", "").strip()
        if not key or not cse:
            return None
        root = Path(__file__).resolve().parents[2]
        registry = root / "config" / "allowed-public-domains.txt"
        domains: set[str] = set()
        try:
            domains = {line.strip() for line in registry.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")}
        except OSError:
            pass
        domains |= {"realestateindia.com", "housing.com", "nobroker.in", "quikr.com", "olx.in", "facebook.com", "instagram.com"}
        return cls(key, cse, domains)

    def _allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        host = _host(url)
        return parsed.scheme == "https" and bool(host) and any(host == domain or host.endswith(f".{domain}") for domain in self.allowed_domains)

    async def _robots_allowed(self, url: str) -> bool:
        parsed = urlparse(url)
        robots_url = f"{parsed.scheme}://{parsed.netloc}/robots.txt"
        try:
            response = await self.client.get(robots_url)
            if response.status_code >= 400:
                return False
            parser = RobotFileParser(); parser.set_url(robots_url); parser.parse(response.text.splitlines())
            return parser.can_fetch("OwnerPlotFinder", url)
        except httpx.HTTPError:
            return False

    async def _search(self, query: str) -> list[dict]:
        try:
            response = await self.client.get("https://www.googleapis.com/customsearch/v1", params={"key":self.api_key,"cx":self.cse_id,"q":query,"num":10})
            response.raise_for_status()
            return response.json().get("items", [])
        except (httpx.HTTPError, ValueError, KeyError):
            return []

    async def _candidate_text(self, item: dict) -> tuple[str, str]:
        url = item.get("link", "")
        snippet = " ".join(str(item.get(key, "")) for key in ("title", "snippet", "htmlSnippet"))
        if not url or not self._allowed(url) or not await self._robots_allowed(url):
            return url, snippet
        try:
            response = await self.client.get(url)
            if response.status_code >= 400:
                return url, snippet
            ctype = response.headers.get("content-type", "").lower()
            if "text/html" not in ctype and "text/plain" not in ctype:
                return url, snippet
            body = re.sub(r"<script\b[^>]*>.*?</script>", " ", response.text, flags=re.I | re.S)
            body = re.sub(r"<style\b[^>]*>.*?</style>", " ", body, flags=re.I | re.S)
            body = re.sub(r"<[^>]+>", " ", body)
            body = re.sub(r"\s+", " ", body)
            return url, f"{snippet} {body[:80_000]}"
        except httpx.HTTPError:
            return url, snippet

    async def _resolve_one(self, target: Listing) -> ContactCandidate | None:
        seen: set[str] = set(); best: ContactCandidate | None = None
        for query in _queries(target)[:self.max_queries]:
            for item in await self._search(query):
                url = item.get("link", "")
                if not url or url in seen or not self._allowed(url):
                    continue
                seen.add(url)
                if url.rstrip("/") == target.url.rstrip("/"):
                    continue
                candidate_url, text = await self._candidate_text(item)
                phones = {normalize_phone(match.group(0)) for match in PHONE_RE.finditer(text)}
                phones.discard(None)
                if not phones:
                    continue
                score, evidence = _score(target, text, candidate_url)
                if score < self.min_score or not any(key in evidence for key in ("same plot area", "same dimensions", "strong description/project overlap")):
                    continue
                for phone in phones:
                    candidate = ContactCandidate(candidate_url, phone, score, evidence)
                    if best is None or candidate.score > best.score:
                        best = candidate
        return best

    async def enrich(self, listings: list[Listing]) -> list[Listing]:
        targets = [item for item in listings if _is_portal(item) and not item.phone and item.seller_type not in {SellerType.BROKER, SellerType.BUILDER} and (item.seller_type in {SellerType.PROBABLE_OWNER, SellerType.VERIFIED_OWNER} or re.search(r"\b(owner|contact owner|posted by owner|individual)\b", f"{item.seller_claim or ''} {item.title} {item.description}", re.I))][:self.max_listings]
        resolved = await asyncio.gather(*(self._resolve_one(item) for item in targets), return_exceptions=True)
        for target, result in zip(targets, resolved):
            if isinstance(result, BaseException) or result is None:
                continue
            target.phone = result.phone
            target.phone_public = True
            target.matching_contact_sources = max(2, target.matching_contact_sources)
            target.contact_verification = "public_cross_post_owner_contact"
            target.evidence.append(f"Public phone found on strongly matching cross-post: {result.url}")
            target.evidence.append(f"Contact match score: {result.score}/100 ({', '.join(result.evidence)})")
            classify_seller(target)
        return listings
