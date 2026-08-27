from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx

from .collectors import GooglePublicWebCollector
from .processing import fingerprint
from .query import parse_query
from .service import SearchService, format_results


ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = ROOT / "data" / "state.json"
LOCALITIES_PATH = ROOT / "config" / "coimbatore-localities.txt"


def load_state() -> dict:
    default = {"telegram_update_offset": 0, "locality_cursor": 0, "seen": {}, "initialized": False}
    try:
        return default | json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def service_from_environment() -> SearchService:
    collector = GooglePublicWebCollector.from_environment()
    if collector is None:
        raise RuntimeError("GOOGLE_CSE_API_KEY, GOOGLE_CSE_ID and ALLOWED_PUBLIC_DOMAINS are required")
    return SearchService([collector])


def authorized_chat(chat_id: int) -> bool:
    expected = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return not expected or str(chat_id) == expected


async def telegram(method: str, payload: dict) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"https://api.telegram.org/bot{token}/{method}", json=payload)
        response.raise_for_status()
        body = response.json()
        if not body.get("ok"):
            raise RuntimeError(f"Telegram {method} failed")
        return body


async def send_message(text: str, chat_id: int | str | None = None) -> None:
    destination = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not destination:
        raise RuntimeError("TELEGRAM_CHAT_ID is required")
    for start in range(0, len(text), 4000):
        await telegram("sendMessage", {"chat_id": destination, "text": text[start:start + 4000], "disable_web_page_preview": True})


async def scan(localities: list[str], notify: bool = True, rotate: bool = False) -> tuple[int, int]:
    state = load_state()
    if rotate and localities:
        batch_size = max(1, int(os.environ.get("SCAN_BATCH_SIZE", "4")))
        total = len(localities)
        cursor = int(state.get("locality_cursor", 0)) % total
        localities = (localities + localities)[cursor:cursor + min(batch_size, total)]
        state["locality_cursor"] = (cursor + len(localities)) % total
    service = service_from_environment()
    discovered = sent = 0
    for locality in localities:
        query = parse_query(f"plots in {locality}")
        results = await service.search(query, force_refresh=True)
        discovered += len(results)
        old = set(state["seen"].get(locality, []))
        keyed = {fingerprint(item): item for item in results}
        new_keys = [key for key in keyed if key not in old]
        if state["initialized"] and notify and new_keys:
            await send_message("NEW PUBLIC OWNER-PLOT POSTINGS\n\n" + format_results(query, [keyed[key] for key in new_keys]))
            sent += len(new_keys)
        state["seen"][locality] = sorted(old | set(keyed))[-5000:]
    state["initialized"] = True
    save_state(state)
    return discovered, sent


async def process_commands() -> int:
    state = load_state()
    body = await telegram("getUpdates", {"offset": state["telegram_update_offset"], "timeout": 0, "allowed_updates": ["message"]})
    handled = 0
    service = service_from_environment()
    for update in body.get("result", []):
        state["telegram_update_offset"] = max(state["telegram_update_offset"], update["update_id"] + 1)
        message = update.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        text = message.get("text", "").strip()
        if not chat_id or not text or not authorized_chat(chat_id):
            continue
        if text == "/start":
            await send_message("OwnerPlot Finder is active. Try: /plots Kalapatti", chat_id)
        else:
            try:
                query = parse_query(text)
                await send_message(format_results(query, await service.search(query, force_refresh=True)), chat_id)
            except ValueError as exc:
                await send_message(str(exc), chat_id)
        handled += 1
    save_state(state)
    return handled


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["scan", "commands"])
    parser.add_argument("--locality")
    args = parser.parse_args()
    if args.mode == "commands":
        asyncio.run(process_commands())
        return
    localities = [args.locality] if args.locality else [line.strip() for line in LOCALITIES_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    asyncio.run(scan(localities, rotate=not bool(args.locality)))


if __name__ == "__main__":
    main()
