from __future__ import annotations
import asyncio, os
from aiogram import Bot, Dispatcher
from aiogram.filters import Command, CommandStart
from aiogram.types import Message
from .cache import ListingCache, WatchStore
from .collectors import EmptyCollector, GooglePublicWebCollector
from .processing import fingerprint
from .query import parse_query
from .service import SearchService, format_results

dp=Dispatcher(); storage_path=os.getenv("CACHE_PATH","/tmp/ownerplot-cache.sqlite3")


def build_service():
    collector=GooglePublicWebCollector.from_environment()
    return SearchService([collector] if collector else [EmptyCollector()],ListingCache(storage_path))


service=build_service(); watches=WatchStore(storage_path)


def authorized(message):
    configured={int(x) for x in os.getenv("ALLOWED_TELEGRAM_USER_IDS","").split(",") if x.strip().isdigit()}
    return not configured or (message.from_user and message.from_user.id in configured)


async def deny(message):
    if authorized(message): return False
    await message.answer("This bot is private. Your Telegram user ID is not authorized."); return True


@dp.message(CommandStart())
async def start(message:Message):
    if await deny(message): return
    await message.answer("Search: /plots Kalapatti\nWatch new posts: /watch plots Kalapatti\nList: /watches\nStop: /unwatch Kalapatti")


@dp.message(Command("watch"))
async def add_watch(message:Message):
    if await deny(message) or not message.text: return
    try: query=parse_query(message.text.replace("/watch","",1))
    except ValueError as exc: await message.answer(str(exc)); return
    watches.add(message.chat.id,message.from_user.id,query)
    initial=await service.search(query,force_refresh=True)
    watches.new_fingerprints(message.chat.id,query.locality,[fingerprint(item) for item in initial])
    await message.answer(f"Watching new public owner-plot postings in {query.locality}. Existing results are the baseline; only new discoveries will alert you.")


@dp.message(Command("watches"))
async def list_watches(message:Message):
    if await deny(message): return
    rows=watches.list(message.chat.id)
    await message.answer("Active watches:\n"+"\n".join(f"• {q.locality}" for _,_,q in rows) if rows else "No active watches.")


@dp.message(Command("unwatch"))
async def remove_watch(message:Message):
    if await deny(message) or not message.text: return
    locality=message.text.replace("/unwatch","",1).strip()
    await message.answer(f"Stopped watching {locality}." if locality and watches.remove(message.chat.id,locality) else "Watch not found. Use /watches.")


@dp.message()
async def search(message:Message):
    if await deny(message) or not message.text: return
    try: query=parse_query(message.text)
    except ValueError as exc: await message.answer(str(exc)); return
    status=await message.answer(f"Searching permitted sources for owner plots in {query.locality}…")
    await status.edit_text(format_results(query,await service.search(query)),disable_web_page_preview=True)


async def watch_loop(bot):
    interval=max(15,int(os.getenv("WATCH_INTERVAL_MINUTES","60")))*60
    while True:
        for chat_id,_,query in watches.list():
            results=await service.search(query,force_refresh=True); keyed={fingerprint(item):item for item in results}
            new=watches.new_fingerprints(chat_id,query.locality,list(keyed))
            if new:
                await bot.send_message(chat_id,("NEW OWNER-PLOT POSTINGS\n\n"+format_results(query,[keyed[k] for k in new]))[:4096],disable_web_page_preview=True)
        await asyncio.sleep(interval)


async def main():
    token=os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token: raise RuntimeError("TELEGRAM_BOT_TOKEN is required")
    bot=Bot(token); task=asyncio.create_task(watch_loop(bot))
    try: await dp.start_polling(bot)
    finally: task.cancel()


if __name__=="__main__": asyncio.run(main())
