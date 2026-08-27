from __future__ import annotations

import hashlib
import os
import re
import base64
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

from cryptography.fernet import Fernet, InvalidToken

from .domain import Listing, SellerType
from .processing import normalize_phone


SUPPORTED_PORTALS = {"99acres.com": "99acres", "magicbricks.com": "magicbricks"}
URL_RE = re.compile(r"https://[^\s<>]+", re.I)
PHONE_RE = re.compile(r"(?:\+?91[\s.-]?)?[6-9](?:[\s.-]?\d){9}")


def canonical_listing_url(url: str) -> tuple[str, str]:
    parsed = urlsplit(url.strip())
    host = (parsed.hostname or "").lower().removeprefix("www.")
    portal = next((name for domain, name in SUPPORTED_PORTALS.items() if host == domain or host.endswith(f".{domain}")), None)
    if parsed.scheme != "https" or not portal:
        raise ValueError("Only HTTPS 99acres or MagicBricks listing URLs can be captured")
    path = re.sub(r"/+", "/", parsed.path).rstrip("/") or "/"
    if path == "/":
        raise ValueError("Use the exact property-listing URL, not the portal home page")
    return urlunsplit(("https", host, path, "", "")), portal


def contact_key(url: str) -> str:
    canonical, _ = canonical_listing_url(url)
    return hashlib.sha256(canonical.encode()).hexdigest()[:24]


def _fernet() -> Fernet:
    key = os.environ.get("CONTACT_STORE_KEY", "").strip()
    if not key:
        bot_token=os.environ.get("TELEGRAM_BOT_TOKEN","").strip()
        if not bot_token:
            raise RuntimeError("CONTACT_STORE_KEY or TELEGRAM_BOT_TOKEN is required for encrypted contact capture")
        derived=hashlib.sha256(f"ownerplot-contact-store-v1:{bot_token}".encode()).digest()
        key=base64.urlsafe_b64encode(derived).decode()
    try:
        return Fernet(key.encode())
    except (TypeError, ValueError) as exc:
        raise RuntimeError("CONTACT_STORE_KEY is not a valid Fernet key") from exc


def parse_capture_command(text: str) -> tuple[str, str]:
    url = next(iter(URL_RE.findall(text)), None)
    phones = PHONE_RE.findall(text)
    phone = normalize_phone(phones[-1]) if phones else None
    if not url or not phone:
        raise ValueError("Use: /capture <99acres-or-MagicBricks-listing-URL> <displayed-phone-number>")
    url=url.rstrip(".,)")
    canonical_listing_url(url)
    return url, phone


def capture_contact(state: dict, url: str, phone: str) -> dict:
    canonical, portal = canonical_listing_url(url)
    normalized = normalize_phone(phone)
    if not normalized:
        raise ValueError("Enter a valid Indian mobile number")
    ciphertext=_fernet().encrypt(normalized.encode()).decode()
    key = contact_key(canonical)
    contacts = state.setdefault("authorized_contacts", {})
    existing = contacts.get(key)
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    ledger = state.setdefault("contact_credit_ledger", {}).setdefault(month, {})
    is_new = existing is None
    if is_new:
        ledger[portal] = int(ledger.get(portal, 0)) + 1
    contacts[key] = {
        "portal": portal,
        "url": canonical,
        "ciphertext": ciphertext,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    return {"portal": portal, "url": canonical, "new_credit": is_new, "used": int(ledger.get(portal, 0))}


def _decrypt(record: dict) -> str | None:
    try:
        return _fernet().decrypt(record["ciphertext"].encode()).decode()
    except (InvalidToken, KeyError, RuntimeError):
        return None


def enrich_authorized_contacts(listings: list[Listing], state: dict) -> list[Listing]:
    contacts = state.get("authorized_contacts", {})
    for item in listings:
        try:
            canonical, portal = canonical_listing_url(item.url)
        except ValueError:
            continue
        record = contacts.get(contact_key(canonical))
        phone = _decrypt(record) if record else None
        if phone:
            item.phone = phone
            item.reveal_required = False
            item.seller_type = SellerType.PROBABLE_OWNER
            item.owner_confidence = max(item.owner_confidence, 75)
            item.contact_verification = "authorized_captured_owner"
            item.evidence.append(f"Contact intentionally revealed through authorized {portal} account")
            continue
        if item.seller_type in {SellerType.BROKER, SellerType.BUILDER}:
            continue
        text = f"{item.seller_claim or ''} {item.title} {item.description}".lower()
        owner_label = bool(re.search(r"\b(owner|contact owner|posted by owner|individual)\b", text))
        if owner_label or item.seller_type == SellerType.PROBABLE_OWNER:
            item.reveal_required = True
            item.reveal_priority = min(100, 35 + item.locality_confidence // 4 + item.date_confidence // 4 + (20 if owner_label else 0))
            item.contact_verification = "authorized_reveal_required"
            item.evidence.append(f"{portal} contact credit required; no protected contact was accessed")
    return sorted(listings, key=lambda item: (bool(item.phone), item.reveal_priority, item.owner_confidence), reverse=True)


def credit_status(state: dict) -> str:
    month = datetime.now(timezone.utc).strftime("%Y-%m")
    ledger = state.get("contact_credit_ledger", {}).get(month, {})
    default_budget = int(os.environ.get("PORTAL_CONTACT_MONTHLY_BUDGET", "25"))
    lines = [f"AUTHORIZED CONTACT CREDITS — {month}"]
    for portal in ("99acres", "magicbricks"):
        used = int(ledger.get(portal, 0))
        lines.append(f"{portal}: {used}/{default_budget} captured · {max(0, default_budget-used)} planned credits remaining")
    lines.append("The displayed balance is OwnerPlot Finder's ledger; your portal account remains the official balance.")
    return "\n".join(lines)
