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
    "phone", "mobile", "contactnumber", "contact_number", "mobilenumber",
    "mobile_number", "ownerphone", "owner_phone", "sellerphone", "seller_phone",
    "advertiserphone", "advertiser_phone", "primaryphone", "primary_phone",
}
PORTAL_HOSTS = {"magicbricks.com", "99acres.com"}


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _portal(url: str) -> bool:
    host = _host(url)
    return host in PORTAL_HOSTS or any(host.endswith(f".{domain}") for domain in PORTAL_HOSTS)


def _individual_detail_url(url: str) -> bool:
    host = _host(url)
    path = urlparse(url).path.lower()
    if host.endswith("magicbricks.com"):
        return "propertydetails" in path
    if host.endswith("99acres.com"):
        return "-npffid" in path and not path.endswith("-ffid")
    return False


def _walk_json(value, path: tuple[str, ...] = ()):
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _walk_json(child, (*path, str(key)))
    elif isinstance(value, list):
        for idx, child in enumerate(value):
            yield from _walk_json(child, (*path, str(idx)))
    else:
        yield path, value


def _phones_from_value(value) -> set[str]:
    phones = {normalize_phone(m.group(0)) for m in PHONE_RE.finditer(str(value or ""))}
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
    """Read only complete contacts explicitly exposed in structured data on an individual portal detail page.

    Category/overview pages and arbitrary HTML phone strings are intentionally rejected because they can
    contain support numbers, advertiser numbers for other cards, or unrelated page furniture.
    No login, OTP, CAPTCHA, subscription, reveal, or protected endpoint is bypassed.
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client or httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": "OwnerPlotFinder/0.9 (+portal-native-public-data)"},
        )

    async def _resolve_one(self, listing: Listing) -> PortalNativeFinding | None:
        if not _portal(listing.url):
            return None
        if not _individual_detail_url(listing.url):
            listing.evidence.append("Portal-native probe skipped: not an individual property detail URL")
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

        candidates: list[tuple[str, list[str]]] = []
        for blob in _extract_json_blobs(response.text):
            for path, value in _walk_json(blob):
                key = (path[-1] if path else "").lower().replace("-", "").replace(" ", "").replace("_", "")
                allowed = {k.replace("_", "") for k in CONTACT_KEYS}
                if key not in allowed:
                    continue
                for phone in _phones_from_value(value):
                    candidates.append((phone, [f"portal-native structured contact field: {'.'.join(path)}"]))

        if not candidates:
            masked = bool(MASKED_RE.search(response.text))
            listing.evidence.append(
                "Portal-native probe: no complete structured public phone" + ("; masked contact detected" if masked else "")
            )
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
            listing.evidence.append("same property via exact portal listing")
        return listings
