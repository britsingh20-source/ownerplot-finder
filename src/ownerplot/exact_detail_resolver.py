from __future__ import annotations

import html as html_lib
import re
from urllib.parse import urljoin, urlparse

import httpx

from .domain import Listing

DETAIL_LINK_RE = re.compile(
    r'href=["\']([^"\']*propertyDetails[^"\']+)["\']',
    re.I,
)
AREA_RE = re.compile(r"([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)\s*(?:sq\.?\s*ft|sqft|square feet)", re.I)
DIM_RE = re.compile(r"\b([0-9]+(?:\.[0-9]+)?)\s*[xX]\s*([0-9]+(?:\.[0-9]+)?)\b")


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _text(html: str) -> str:
    body = re.sub(r"<script\b[^>]*>.*?</script>", " ", html or "", flags=re.I | re.S)
    body = re.sub(r"<style\b[^>]*>.*?</style>", " ", body, flags=re.I | re.S)
    body = re.sub(r"<[^>]+>", " ", body)
    return re.sub(r"\s+", " ", html_lib.unescape(body)).strip()


def _owner_tokens(value: str | None) -> list[str]:
    if not value:
        return []
    tokens = [t.lower() for t in re.findall(r"[A-Za-z]{2,}", value)]
    return [t for t in tokens if t not in {"owner", "individual", "residential", "plot", "land"}]


def _dimensions(text: str) -> tuple[float, float] | None:
    m = DIM_RE.search(text or "")
    if not m:
        return None
    a, b = float(m.group(1)), float(m.group(2))
    return tuple(sorted((round(a, 2), round(b, 2))))


def _seed_dimensions(seed: Listing) -> tuple[float, float] | None:
    return _dimensions(f"{seed.title} {seed.description}")


def _score(seed: Listing, detail_text: str) -> int:
    lower = detail_text.lower()
    score = 0
    owner_tokens = _owner_tokens(seed.seller_claim)
    if owner_tokens and all(token in lower for token in owner_tokens):
        score += 45
    if seed.area_sqft:
        areas = [float(m.group(1).replace(",", "")) for m in AREA_RE.finditer(detail_text)]
        if any(abs(area - seed.area_sqft) <= max(5.0, seed.area_sqft * 0.01) for area in areas):
            score += 35
    dims = _seed_dimensions(seed)
    if dims:
        found = {_dimensions(m.group(0)) for m in DIM_RE.finditer(detail_text)}
        if dims in found:
            score += 30
    if seed.locality and seed.locality.lower() in lower:
        score += 10
    return score


class ExactPortalDetailResolver:
    """Resolve MagicBricks category/overview owner seeds to exact property detail URLs.

    Only follows links already present in the public category/overview HTML. It does not
    trigger contact-reveal actions or bypass authentication controls.
    """

    def __init__(self, client: httpx.AsyncClient | None = None, max_links: int = 40) -> None:
        self.client = client or httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 OwnerPlotFinder/0.9"},
        )
        self.max_links = max_links

    async def _get(self, url: str) -> str:
        try:
            response = await self.client.get(url)
            if response.status_code >= 400 or "text/html" not in response.headers.get("content-type", "").lower():
                return ""
            return response.text
        except httpx.HTTPError:
            return ""

    async def resolve_one(self, seed: Listing) -> Listing:
        if _host(seed.url) != "magicbricks.com" or "propertydetails" in seed.url.lower():
            return seed
        category_html = await self._get(seed.url)
        if not category_html:
            seed.evidence.append("Exact-detail resolver: category page unavailable")
            return seed

        links: list[str] = []
        seen: set[str] = set()
        for raw in DETAIL_LINK_RE.findall(category_html):
            url = urljoin(seed.url, html_lib.unescape(raw))
            if url not in seen:
                seen.add(url)
                links.append(url)
            if len(links) >= self.max_links:
                break

        best_url = ""
        best_score = 0
        for url in links:
            detail_html = await self._get(url)
            if not detail_html:
                continue
            score = _score(seed, _text(detail_html))
            if score > best_score:
                best_score = score
                best_url = url
            if score >= 80:
                break

        if best_url and best_score >= 70:
            seed.evidence.append(f"Exact-detail resolver: matched detail URL score {best_score}")
            seed.evidence.append(f"Exact-detail URL: {best_url}")
            seed.url = best_url
            seed.source_id = best_url
        else:
            seed.evidence.append(f"Exact-detail resolver: no qualifying detail URL; best score {best_score}")
        return seed

    async def enrich(self, seeds: list[Listing]) -> list[Listing]:
        for seed in seeds:
            await self.resolve_one(seed)
        return seeds
