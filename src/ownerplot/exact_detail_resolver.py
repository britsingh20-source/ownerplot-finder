from __future__ import annotations

import asyncio
import html as html_lib
import os
import re
from urllib.parse import urljoin, urlparse

import httpx

from .domain import Listing

DETAIL_LINK_RE = re.compile(r'href=["\']([^"\']*propertyDetails[^"\']+)["\']', re.I)
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


def _queries(seed: Listing) -> list[str]:
    owner = " ".join(_owner_tokens(seed.seller_claim))
    locality = seed.locality or "Kalapatti"
    area = f'"{int(seed.area_sqft)} Sq-ft"' if seed.area_sqft else ""
    dims = _seed_dimensions(seed)
    dim = f'"{dims[0]:g} X {dims[1]:g}"' if dims else ""
    pieces = [
        f'site:magicbricks.com/propertyDetails "{owner}" {area} "{locality}" Coimbatore' if owner else "",
        f'site:magicbricks.com/propertyDetails {area} {dim} "{locality}" Coimbatore' if area else "",
        f'site:magicbricks.com/propertyDetails "{owner}" {dim} "{locality}"' if owner and dim else "",
    ]
    return [re.sub(r"\s+", " ", q).strip() for q in pieces if q.strip()]


class ExactPortalDetailResolver:
    """Resolve MagicBricks owner seeds to exact public propertyDetails URLs.

    Uses public category links first, then Google CSE/Tavily exact-detail discovery.
    Candidate verification is bounded and parallelized. It never triggers contact reveal,
    login, OTP, CAPTCHA, or subscription controls.
    """

    def __init__(self, client: httpx.AsyncClient | None = None, max_links: int = 12, max_seed_concurrency: int = 4) -> None:
        self.client = client or httpx.AsyncClient(timeout=12, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0 OwnerPlotFinder/1.1"})
        self.max_links = max_links
        self.google_key = os.getenv("GOOGLE_CSE_API_KEY", "").strip()
        self.google_cse = os.getenv("GOOGLE_CSE_ID", "").strip()
        self.tavily_key = os.getenv("TAVILY_API_KEY", "").strip()
        self._seed_sem = asyncio.Semaphore(max_seed_concurrency)

    async def _get(self, url: str) -> str:
        try:
            response = await self.client.get(url)
            if response.status_code >= 400 or "text/html" not in response.headers.get("content-type", "").lower():
                return ""
            return response.text
        except httpx.HTTPError:
            return ""

    async def _google(self, query: str) -> list[tuple[str, str]]:
        if not self.google_key or not self.google_cse:
            return []
        try:
            r = await self.client.get("https://www.googleapis.com/customsearch/v1", params={"key": self.google_key, "cx": self.google_cse, "q": query, "num": 10})
            r.raise_for_status()
            return [(i.get("link", ""), f"{i.get('title','')} {i.get('snippet','')}") for i in r.json().get("items", []) if "propertydetails" in i.get("link", "").lower()]
        except (httpx.HTTPError, ValueError, KeyError):
            return []

    async def _tavily(self, query: str) -> list[tuple[str, str]]:
        if not self.tavily_key:
            return []
        try:
            r = await self.client.post("https://api.tavily.com/search", headers={"Authorization": f"Bearer {self.tavily_key}", "Content-Type": "application/json"}, json={"query": query, "search_depth": "advanced", "max_results": 10, "include_answer": False, "include_raw_content": "text", "include_domains": ["magicbricks.com"]})
            r.raise_for_status()
            out = []
            for i in r.json().get("results", []):
                url = i.get("url", "")
                if "propertydetails" in url.lower():
                    out.append((url, f"{i.get('title','')} {i.get('content','')} {i.get('raw_content','')}"))
            return out
        except (httpx.HTTPError, ValueError, KeyError):
            return []

    async def _search_candidates(self, seed: Listing) -> list[tuple[str, str]]:
        queries = _queries(seed)
        if not queries:
            return []
        tasks = []
        for q in queries:
            tasks.extend((self._google(q), self._tavily(q)))
        results = await asyncio.gather(*tasks)
        seen: set[str] = set()
        out: list[tuple[str, str]] = []
        for group in results:
            for url, snippet in group:
                if url and url not in seen:
                    seen.add(url)
                    out.append((url, snippet))
        return out

    async def _evaluate(self, seed: Listing, url: str, snippet: str) -> tuple[str, int]:
        if _host(url) != "magicbricks.com" or "propertydetails" not in url.lower():
            return "", 0
        detail_html = await self._get(url)
        combined = f"{snippet} {_text(detail_html)}" if detail_html else snippet
        return url, _score(seed, combined)

    async def _pick(self, seed: Listing, candidates: list[tuple[str, str]]) -> tuple[str, int]:
        selected = candidates[: self.max_links]
        if not selected:
            return "", 0
        results = await asyncio.gather(*(self._evaluate(seed, url, snippet) for url, snippet in selected))
        valid = [(url, score) for url, score in results if url]
        return max(valid, key=lambda item: item[1]) if valid else ("", 0)

    async def resolve_one(self, seed: Listing) -> Listing:
        async with self._seed_sem:
            if _host(seed.url) != "magicbricks.com" or "propertydetails" in seed.url.lower():
                return seed

            html_candidates: list[tuple[str, str]] = []
            category_html = await self._get(seed.url)
            if category_html:
                seen: set[str] = set()
                for raw in DETAIL_LINK_RE.findall(category_html):
                    url = urljoin(seed.url, html_lib.unescape(raw))
                    if url not in seen:
                        seen.add(url)
                        html_candidates.append((url, ""))
                    if len(html_candidates) >= self.max_links:
                        break
            else:
                seed.evidence.append("Exact-detail resolver: category page unavailable")

            best_url, best_score = await self._pick(seed, html_candidates)
            source = "category HTML"
            if not best_url or best_score < 70:
                search_candidates = await self._search_candidates(seed)
                search_url, search_score = await self._pick(seed, search_candidates)
                seed.evidence.append(f"Exact-detail search fallback: {len(search_candidates)} candidate detail URLs")
                if search_score > best_score:
                    best_url, best_score, source = search_url, search_score, "Google/Tavily"

            if best_url and best_score >= 70:
                seed.evidence.append(f"Exact-detail resolver: matched detail URL score {best_score} via {source}")
                seed.evidence.append(f"Exact-detail URL: {best_url}")
                seed.url = best_url
                seed.source_id = best_url
            else:
                seed.evidence.append(f"Exact-detail resolver: no qualifying detail URL; best score {best_score}")
            return seed

    async def enrich(self, seeds: list[Listing]) -> list[Listing]:
        await asyncio.gather(*(self.resolve_one(seed) for seed in seeds))
        return seeds
