from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path

import httpx

from .authorized_contacts import capture_contact, credit_status, enrich_authorized_contacts, parse_capture_command
from .collectors import DirectPublicFeedCollector, TavilyPublicWebCollector
from .processing import correlate_public_contacts, deduplicate, fingerprint
from .query import parse_query
from .service import SearchService, format_results


ROOT = Path(__file__).resolve().parents[2]
STATE_PATH = ROOT / "data" / "state.json"
LOCALITIES_PATH = ROOT / "config" / "coimbatore-localities.txt"
DISCOVERY_PROFILES=("public_contacts","portals","local_sites")
PROFILES=("direct_sources",)+DISCOVERY_PROFILES
_DIRECT_COLLECTOR=None


def load_state() -> dict:
    default = {"telegram_update_offset": 0, "scan_cursor": 0, "baselined": [], "seen": {}, "initialized": False}
    try:
        return default | json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def service_from_environment(profile: str) -> SearchService:
    global _DIRECT_COLLECTOR
    if profile=="direct_sources":
        _DIRECT_COLLECTOR=_DIRECT_COLLECTOR or DirectPublicFeedCollector.from_environment()
        collector=_DIRECT_COLLECTOR
    else:
        collector=TavilyPublicWebCollector.from_environment(profile=profile)
    if collector is None:
        raise RuntimeError(f"Collector configuration is unavailable for {profile}")
    return SearchService([collector])


async def search_profiles(query,profiles=PROFILES):
    batches=await asyncio.gather(*(service_from_environment(profile).search(query,force_refresh=True) for profile in profiles),return_exceptions=True)
    successful=[batch for batch in batches if not isinstance(batch,BaseException)]
    if not successful:
        first=batches[0]
        raise RuntimeError(f"All configured search profiles failed: {first}")
    correlated=correlate_public_contacts([item for batch in successful for item in batch])
    enriched=enrich_authorized_contacts(correlated,load_state())
    return deduplicate(enriched)


def authorized_chat(chat_id: int) -> bool:
    expected = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    return not expected or str(chat_id) == expected


async def telegram(method: str, payload: dict) -> dict:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(f"https://api.telegram.org/bot{token}/{method}", json=payload)
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.is_error:
            raise RuntimeError(f"Telegram {method} failed ({response.status_code}): {body.get('description', 'unknown error')}")
        if not body.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {body.get('description', 'unknown error')}")
        return body


async def send_message(text: str, chat_id: int | str | None = None) -> None:
    destination = chat_id or os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not destination:
        raise RuntimeError("TELEGRAM_CHAT_ID is required")
    for start in range(0, len(text), 4000):
        await telegram("sendMessage", {"chat_id": destination, "text": text[start:start + 4000], "disable_web_page_preview": True})


async def scan(localities: list[str], notify: bool = True, rotate: bool = False, report: bool = False) -> tuple[int, int]:
    state = load_state()
    jobs=[]
    if rotate and localities:
        total=len(localities)*len(DISCOVERY_PROFILES)
        cursor=int(state.get("scan_cursor",0))%total
        jobs=[(locality,("direct_sources",)) for locality in localities]
        jobs.append((localities[cursor//len(DISCOVERY_PROFILES)],(DISCOVERY_PROFILES[cursor%len(DISCOVERY_PROFILES)],)))
        state["scan_cursor"]=(cursor+1)%total
    else:
        jobs=[(locality,PROFILES) for locality in localities]
    discovered = sent = 0
    baselined=set(state.get("baselined",[]))
    for locality,profiles in jobs:
        query = parse_query(f"plots in {locality}")
        results = await search_profiles(query,profiles)
        discovered += len(results)
        state["last_run"]={
            "locality":locality,
            "freshness_days":query.max_age_days,
            "profiles":list(profiles),
            "unique_listings":len(results),
            "public_contacts":sum(bool(item.phone) for item in results),
            "verified_original_dates":sum(item.date_status=="verified_recent" for item in results),
            "verified_owners":sum(item.contact_verification=="property_matched_public_contact" for item in results),
            "probable_owners":sum(item.contact_verification=="probable_owner_call_to_confirm" for item in results),
            "sources":sorted({item.source for item in results}),
        }
        if report:
            await send_message("OWNERPLOT FINDER DEEP SEARCH\n\n" + format_results(query, results))
        old = set(state["seen"].get(locality, []))
        keyed = {fingerprint(item): item for item in results}
        new_keys = [key for key in keyed if key not in old]
        baseline_keys={f"{locality}|{profile}" for profile in profiles}
        first_profile_scan=not baseline_keys<=baselined
        if not first_profile_scan and notify and new_keys:
            await send_message("NEW PUBLIC OWNER-PLOT POSTINGS\n\n" + format_results(query, [keyed[key] for key in new_keys]))
            sent += len(new_keys)
        baselined.update(baseline_keys)
        state["seen"][locality] = sorted(old | set(keyed))[-5000:]
    state["baselined"]=sorted(baselined)
    state["initialized"] = True
    save_state(state)
    return discovered, sent


async def process_commands() -> int:
    state = load_state()
    body = await telegram("getUpdates", {"offset": state["telegram_update_offset"], "timeout": 0, "allowed_updates": ["message"]})
    handled = 0
    for update in body.get("result", []):
        state["telegram_update_offset"] = max(state["telegram_update_offset"], update["update_id"] + 1)
        message = update.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        text = message.get("text", "").strip()
        if not chat_id or not text or not authorized_chat(chat_id):
            continue
        if text == "/start":
            await send_message("OwnerPlot Finder is active.\n\nSearch: /plots Kalapatti\nCredits: /credits\nSave a legitimately revealed contact: /capture <listing URL> <displayed number>", chat_id)
        elif text == "/credits":
            await send_message(credit_status(state),chat_id)
        elif text.startswith("/capture"):
            try:
                url,phone=parse_capture_command(text)
                result=capture_contact(state,url,phone)
                budget=int(os.environ.get("PORTAL_CONTACT_MONTHLY_BUDGET","25"))
                action="One contact credit recorded" if result["new_credit"] else "Existing listing updated; no additional credit recorded"
                warning="\nWarning: local planned budget reached." if result["used"]>=budget else ""
                await send_message(f"AUTHORIZED CONTACT SAVED\nPortal: {result['portal']}\n{action}\nLedger: {result['used']}/{budget}\nThe number is encrypted in repository state.{warning}",chat_id)
            except (ValueError,RuntimeError) as exc:
                await send_message(str(exc),chat_id)
        else:
            try:
                query = parse_query(text)
                await send_message(format_results(query, await search_profiles(query)), chat_id)
            except ValueError as exc:
                await send_message(str(exc), chat_id)
        handled += 1
    save_state(state)
    return handled


async def smoke_test() -> None:
    await send_message(
        "✅ OWNERPLOT FINDER LIVE TEST PASSED\n\n"
        "GitHub Actions connected successfully to Telegram.\n"
        "Search: /plots Kalapatti\n"
        "Credits: /credits\n"
        "Authorized capture: /capture <listing URL> <displayed number>\n\n"
        "This test used no Tavily search credit and no portal contact credit."
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["scan", "commands", "smoke"])
    parser.add_argument("--locality")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()
    if args.mode == "commands":
        asyncio.run(process_commands())
        return
    if args.mode == "smoke":
        asyncio.run(smoke_test())
        return
    localities = [args.locality] if args.locality else [line.strip() for line in LOCALITIES_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    asyncio.run(scan(localities, rotate=not bool(args.locality), report=args.report))


if __name__ == "__main__":
    main()
