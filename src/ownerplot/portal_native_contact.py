from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html import unescape
from urllib.parse import urlparse

import httpx

from .domain import Listing
from .processing import normalize_phone

PHONE_RE = re.compile(r"(?:\+?91[\s.-]?)?([6-9](?:[\s.-]?\d){9})(?!\d)")
MASKED_RE = re.compile(r"(?:\+?91[\s.-]?)?[6-9]\d{1,4}[xX*•]{3,}")
JSON_SCRIPT_RE = re.compile(r"<script\b[^>]*>(.*?)</script>", re.I | re.S)
CONTACT_KEYS = {
    "phone", "mobile", "contact", "contactnumber", "contact_number", "mobilenumber",
    "mobile_number", "ownerphone", "owner_phone", "sellerphone", "seller_phone",
    "advertiserphone", "advertiser_phone", "primaryphone", "primary_phone",
}
PORTAL_HOSTS = {"realestateindia.com", "magicbricks.com", "99acres.com"}


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _portal(url: str) -> bool:
    host = _host(url)
    return host in PORTAL_HOSTS or any(host.endswith(f".{domain}") for domain in PORTAL_HOSTS)


def _detail_page(url: str) -> bool:
    lower = url.lower()
    host = _host(url)
    if host.endswith("realestateindia.com"):
        return "/property-detail/" in lower or lower.endswith(".htm")
    return "propertydetails" in lower or "-npffid" in lower


def _walk_json(value, path: tuple[str, ...] = ()):
    if isinstance(value, dict):
        for key, child in value.items():
            key_s = str(key)
            yield from _walk_json(child, (*path, key_s))
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            yield from _walk_json(child, (*path, str(idx)))
    else:
        yield path, value


def _phones_from_value(value) -> set[str]:
    text = str(value or "")
    phones = {normalize_phone(m.group(0)) for m in PHONE_RE.finditer(text)}
    phones.discard(None)
    return phones


def _extract_json_blobs(html: str) -> list[object]:
    blobs: list[object] = []
    for raw in JSON_SCRIPT_RE.findall(html or ""):
        text = unescape(raw.strip())
        if not text or text[0] not in "[{":
            continue
        try:
            blobs.append(json.loads(text))
        except (json.JSONDecodeError, TypeError):
            continue
    return blobs


@dataclass(slots=True)
class PortalNativeFinding:
    phone: str
    evidence: list[str]


class PortalNativeContactResolver:
    """Extract complete contacts explicitly exposed in structured data on an exact portal detail page.

    It never triggers login/contact reveal actions and does not bypass OTP, CAPTCHA, subscription,
    or access controls. Category-page and arbitrary HTML phone strings are intentionally rejected.
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client or httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 OwnerPlotFinder/1.1"},
        )

    async def _resolve_one(self, listing: Listing) -> PortalNativeFinding | None:
        if not _portal(listing.url) or not _detail_page(listing.url):
            listing.evidence.append("Portal-native probe skipped: exact detail URL not resolved")
            return None
        try:
            response = await self.client.get(listing.url)
        except httpx.HTTPError:
            listing.evidence.append("Portal-native probe failed: HTTP error")
            return None
        if response.status_code >= 400:
            listing.evidence.append(f"Portal-native probe failed: HTTP {response.status_code}")
            return None

        content_type = response.headers.get("content-type", "").lower()
        if "text/html" not in content_type and "application/json" not in content_type:
            listing.evidence.append("Portal-native probe: unsupported response type")
            return None

        text = response.text
        candidates: list[tuple[str, list[str]]] = []
        contact_paths: list[str] = []
        structured_blobs = _extract_json_blobs(text)

        for blob in structured_blobs:
            for path, value in _walk_json(blob):
                key = (path[-1] if path else "").lower().replace("-", "").replace(" ", "")
                normalized_key = key.replace("_", "")
                if normalized_key in {k.replace("_", "") for k in CONTACT_KEYS}:
                    path_s = ".".join(path)
                    if path_s not in contact_paths:
                        contact_paths.append(path_s)
                    for phone in _phones_from_value(value):
                        candidates.append((phone, [f"portal-native structured field: {path_s}"]))

        masked = bool(MASKED_RE.search(text))
        listing.evidence.append(
            f"Portal-native diagnostics: structured blobs {len(structured_blobs)}; contact-key paths {len(contact_paths)}; complete structured phones {len(candidates)}; masked contact {'yes' if masked else 'no'}"
        )
        if contact_paths:
            listing.evidence.append("Portal-native contact paths: " + ", ".join(contact_paths[:8]))

        if not candidates:
            listing.evidence.append("Portal-native probe: no explicit complete structured contact")
            return None

        phone, evidence = candidates[0]
        return PortalNativeFinding(phone=phone, evidence=evidence)

    async def enrich(self, listings: list[Listing]) -> list[Listing]:
        for listing in listings:
            if listing.phone or not _portal(listing.url):
                continue
            finding = await self._resolve_one(listing)
            if finding is None:
                continue
            listing.phone = finding.phone
            listing.phone_public = True
            listing.contact_verification = "portal_native_public_contact"
            listing.matching_contact_sources = max(1, listing.matching_contact_sources)
            listing.evidence.extend(finding.evidence)
            if _host(listing.url).endswith("realestateindia.com"):
                listing.evidence.append("same property via exact RealEstateIndia listing")
            else:
                listing.evidence.append("same property via exact portal listing")
        return listings
