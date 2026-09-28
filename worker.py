import asyncio
import random
import logging

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import AuthKeyUnregisteredError, FloodWaitError, SessionRevokedError

from captcha import solve_captcha
import db

log = logging.getLogger("worker")


class Worker:
    def __init__(self, acc: dict, api_id: int, api_hash: str,
                 target_bot: str, bonus_text: str):
        self.acc = acc
        self.id = acc["id"]
        self.target_bot = target_bot
        self.bonus_text = bonus_text
        self.client = TelegramClient(StringSession(acc["session_str"]), api_id, api_hash)
        self.stopped = False

    async def log(self, level: str, msg: str):
        log.info(f"[акк {self.id}] {msg}")
        await db.add_log(self.id, level, msg)

    async def human_pause(self, a=1.0, b=3.0):
        await asyncio.sleep(random.uniform(a, b))

    async def _owner_subscribed(self) -> bool:
        """Проверяет — подписан ли владелец аккаунта на канал."""
        owner_id = self.acc.get("owner_id")
        if not owner_id:
            return True  # нет владельца — пропускаем
        try:
            from admin_bot import check_subscription, is_admin
            if is_admin(owner_id):
                return True
            return await check_subscription(owner_id)
        except Exception as e:
            log.warning(f"[акк {self.id}] check_subscription error: {e}")
            return True  # если ошибка проверки — не блокируем

    async def start(self):
        try:
            await self.client.connect()
            if not await self.client.is_user_authorized():
                await self.log("ERROR", "🚫 Сессия не авторизована — пересоздай")
                await db.update_status(self.id, "dead", "session not authorized")
                await self.client.disconnect()
                raise Exception("Session not authorized")

            me = await self.client.get_me()
            await self.log("INFO", f"✅ Запущен как @{me.username or me.id}")

            self.client.add_event_handler(
                self.on_message, events.NewMessage(from_users=self.target_bot)
            )
        except (AuthKeyUnregisteredError, SessionRevokedError):
            await self.log("ERROR", "🚫 Сессия отозвана — пересоздай")
            await db.update_status(self.id, "dead", "session revoked")
            raise

    async def stop(self):
        self.stopped = True
        try:
            await self.client.disconnect()
        except Exception:
            pass

    async def on_message(self, event):
        msg = event.message
        try:
            if msg.photo:
                await self.log("INFO", "📩 Капча получена")
                await self.human_pause(2.5, 6.5)

                image_bytes = await msg.download_media(bytes)
                code = solve_captcha(image_bytes)
                await self.log("INFO", f"🔍 Распознан код: {code!r}")

                if len(code) < 3:
                    await self.log("WARN", "Слишком короткий код — пропуск")
                    return

                await self.human_pause(1.5, 4.0)
                await event.reply(code)
                await self.log("INFO", f"📤 Отправлен код: {code}")

            elif msg.text:
                await self.log("INFO", f"💬 Бот: {msg.text[:120]}")

        except Exception as e:
            await self.log("ERROR", f"Ошибка обработки: {e}")

    async def send_bonus(self):
        if self.stopped:
            return

        # ===== ПРОВЕРКА ПОДПИСКИ ВЛАДЕЛЬЦА =====
        if not await self._owner_subscribed():
            await self.log("WARN", "🚫 Владелец не подписан на канал — пропуск")
            return

        # ===== ОТПРАВКА БОНУСА =====
        try:
            await self.human_pause(2.0, 8.0)
            await self.client.send_message(self.target_bot, self.bonus_text)
            await self.log("INFO", f"📨 Отправлено '{self.bonus_text}'")
        except FloodWaitError as e:
            await self.log("WARN", f"FloodWait {e.seconds}с")
            await asyncio.sleep(e.seconds)
        except (AuthKeyUnregisteredError, SessionRevokedError):
            await self.log("ERROR", "🚫 Сессия отозвана")
            await db.update_status(self.id, "dead", "session revoked")
            self.stopped = True
        except Exception as e:
            await self.log("ERROR", f"Ошибка отправки: {e}")
