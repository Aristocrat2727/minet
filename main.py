import os
import asyncio
import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
import zoneinfo

import db
from worker import Worker
from admin_bot import run_admin_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("main")
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("aiogram").setLevel(logging.WARNING)

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
TARGET_BOT = os.environ["TARGET_BOT"]
BONUS_TEXT = os.environ.get("BONUS_TEXT", "🎁Бонус")
BONUS_HOUR = int(os.environ.get("BONUS_HOUR", 7))
BONUS_MINUTE = int(os.environ.get("BONUS_MINUTE", 7))
BONUS_TZ = zoneinfo.ZoneInfo("Europe/Samara")

workers: dict[int, Worker] = {}
scheduler = AsyncIOScheduler()


async def start_worker(acc_id: int):
    if acc_id in workers:
        log.info(f"Worker {acc_id} уже запущен")
        return

    acc = await db.get_account(acc_id)
    if not acc:
        log.error(f"Аккаунт {acc_id} не найден в БД")
        return
    if acc["status"] == "dead":
        log.warning(f"Аккаунт {acc_id} dead — пропуск")
        return

    w = Worker(acc, API_ID, API_HASH, TARGET_BOT, BONUS_TEXT)
    try:
        await w.start()
    except Exception as e:
        log.error(f"Не удалось запустить worker {acc_id}: {e}")
        return

    workers[acc_id] = w
    scheduler.add_job(
        w.send_bonus,
        trigger="cron",
        hour=BONUS_HOUR,
        minute=BONUS_MINUTE,
        timezone=BONUS_TZ,
        id=f"bonus_{acc_id}",
        replace_existing=True,
    )
    log.info(f"⏰ Worker {acc_id} → {BONUS_HOUR:02d}:{BONUS_MINUTE:02d} Самары")


async def stop_worker(acc_id: int):
    w = workers.pop(acc_id, None)
    if w:
        await w.stop()
    try:
        scheduler.remove_job(f"bonus_{acc_id}")
    except Exception:
        pass


async def stop_all_workers():
    for acc_id in list(workers.keys()):
        await stop_worker(acc_id)


async def main():
    log.info("=" * 55)
    log.info(f"🚀 Запуск • Бот: {TARGET_BOT}")
    log.info("=" * 55)

    await db.init_db()
    log.info("✅ БД инициализирована")

    scheduler.start()

    accounts = await db.get_accounts(status="active")
    log.info(f"📦 Аккаунтов active: {len(accounts)}")
    for acc in accounts:
        try:
            await start_worker(acc["id"])
            await asyncio.sleep(2)
        except Exception as e:
            log.error(f"Ошибка запуска {acc['id']}: {e}")

    await run_admin_bot()


if __name__ == "__main__":
    asyncio.run(main())
