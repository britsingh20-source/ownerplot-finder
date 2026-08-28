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
PORTAL_HOSTS = {"magicbricks.com", "99acres.com"}


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def _portal(url: str) -> bool:
    host = _host(url)
    return host in PORTAL_HOSTS or any(host.endswith(f".{domain}") for domain in PORTAL_HOSTS)


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
    """Extract complete contacts that the exact public portal page already exposes.

    This deliberately does not bypass login, OTP, CAPTCHA, subscription, or reveal controls.
    It only reads the public HTTP response returned for the listing URL and structured payloads
    embedded in that response.
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self.client = client or httpx.AsyncClient(
            timeout=20,
            follow_redirects=True,
            headers={"User-Agent": "OwnerPlotFinder/0.8 (+portal-native-public-data)"},
        )

    async def _resolve_one(self, listing: Listing) -> PortalNativeFinding | None:
        if not _portal(listing.url):
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

        # 1) Structured JSON embedded by SSR/Next/JSON-LD or equivalent.
        for blob in _extract_json_blobs(text):
            for path, value in _walk_json(blob):
                key = (path[-1] if path else "").lower().replace("-", "").replace(" ", "")
                normalized_key = key.replace("_", "")
                if normalized_key in {k.replace("_", "") for k in CONTACT_KEYS}:
                    for phone in _phones_from_value(value):
                        candidates.append((phone, [f"portal-native structured field: {'.'.join(path)}"]))

        # 2) Public page text fallback. Exact-listing page itself is a hard property anchor.
        visible_phones = {normalize_phone(m.group(0)) for m in PHONE_RE.finditer(text)}
        visible_phones.discard(None)
        for phone in visible_phones:
            candidates.append((phone, ["portal-native exact listing page exposed complete phone"]))

        if not candidates:
            masked = bool(MASKED_RE.search(text))
            listing.evidence.append(
                "Portal-native probe: no complete public phone" + ("; masked contact detected" if masked else "")
            )
            return None

        # Prefer structured contact fields over arbitrary page-text matches.
        candidates.sort(key=lambda item: 0 if any("structured field" in e for e in item[1]) else 1)
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
