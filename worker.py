import asyncio
import random
import logging

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.tl.types import User
from telethon.errors import AuthKeyUnregisteredError, FloodWaitError

from captcha import solve_captcha
import db

log = logging.getLogger("worker")

# Сколько раз пробуем ОДНУ капчу, прежде чем жать "Новый код"
ATTEMPTS_PER_CAPTCHA = 2
# Максимум капч за цикл
MAX_CAPTCHAS = 3


class Worker:
    def __init__(self, acc, api_id, api_hash, target_bot, bonus_text):
        self.acc = acc
        self.id = acc["id"]
        self.target_bot = target_bot
        self.bonus_text = bonus_text
        self.client = TelegramClient(StringSession(acc["session_str"]), api_id, api_hash)
        self.stopped = False

        # Состояние цикла
        self._current_image_bytes = None   # байты текущей капчи
        self._current_code = None          # текущий распознанный код
        self._attempts_on_current = 0      # сколько раз пытались на текущей
        self._captchas_used = 0            # сколько капч использовали за цикл
        self._awaiting_new = False         # ждём ли новую капчу после нажатия "Новый код"
        self._last_id = None

    async def log(self, level, msg):
        log.info(f"[акк {self.id}] {msg}")
        await db.add_log(self.id, level, msg)

    async def pause(self, a=1.0, b=3.0):
        await asyncio.sleep(random.uniform(a, b))

    async def _owner_subscribed(self):
        owner_id = self.acc.get("owner_id")
        if not owner_id:
            return True
        try:
            from admin_bot import check_subscription, is_admin
            if is_admin(owner_id):
                return True
            return await check_subscription(owner_id)
        except Exception:
            return True

    async def start(self):
        await self.client.connect()
        if not await self.client.is_user_authorized():
            await self.log("ERROR", "Сессия не авторизована")
            await db.update_status(self.id, "dead", "not authorized")
            await self.client.disconnect()
            raise Exception("not authorized")

        me = await self.client.get_me()
        await self.log("INFO", f"✅ Запущен @{me.username or me.id}")

        # Фильтр: только ЛС от целевого бота
        target_username = self.target_bot.lstrip("@").lower()

        async def filt(event):
            if not event.is_private:
                return False
            sender = await event.get_sender()
            if not isinstance(sender, User) or not sender.bot:
                return False
            if (sender.username or "").lower() != target_username:
                return False
            return True

        self.client.add_event_handler(self.on_msg, events.NewMessage(func=filt))

    async def stop(self):
        self.stopped = True
        try:
            await self.client.disconnect()
        except Exception:
            pass

    async def press_new_code(self, msg):
        """Нажимает кнопку 'Новый код'."""
        if not msg.buttons:
            return False
        for row in msg.buttons:
            for btn in row:
                if btn.url:
                    continue
                t = btn.text.lower()
                if any(k in t for k in ["новый код", "новый", "new", "refresh", "обновить"]):
                    try:
                        await btn.click()
                        await self.log("INFO", f"👆 Нажал '{btn.text}'")
                        return True
                    except Exception as e:
                        await self.log("WARN", f"click err: {e}")
        return False

    async def solve_and_send(self, event, image_bytes):
        """Распознаёт и отправляет капчу. Не сбрасывает состояние."""
        self._current_image_bytes = image_bytes
        await self.pause(2.0, 4.0)
        code = solve_captcha(image_bytes)
        self._current_code = code
        self._attempts_on_current += 1

        await self.log(
            "INFO",
            f"📩 Капча #{self._captchas_used} | "
            f"попытка {self._attempts_on_current}/{ATTEMPTS_PER_CAPTCHA} | "
            f"код: {code!r}"
        )

        if not code or len(code) < 3:
            await self.log("WARN", "Короткий код")
            # Сразу жмём "Новый код"
            return False

        await self.pause(1.5, 4.0)
        await event.reply(code)
        await self.log("INFO", f"📤 Отправлен: {code}")
        return True

    async def on_msg(self, event):
        if not event.is_private:
            return

        msg = event.message
        if msg.id == self._last_id:
            return
        self._last_id = msg.id

        try:
            # ===== ФОТО = КАПЧА =====
            if msg.photo:
                # Если это НЕ после нажатия "Новый код" — значит, первая капча цикла
                if not self._awaiting_new and self._captchas_used == 0:
                    self._captchas_used = 1
                    self._attempts_on_current = 0
                elif self._awaiting_new:
                    # Это новая капча после нажатия "Новый код"
                    self._captchas_used += 1
                    self._attempts_on_current = 0
                    self._awaiting_new = False

                if self._captchas_used > MAX_CAPTCHAS:
                    await self.log("WARN", f"🚫 Лимит {MAX_CAPTCHAS} капч — стоп")
                    return

                image_bytes = await msg.download_media(bytes)
                await self.solve_and_send(event, image_bytes)

            # ===== ТЕКСТ =====
            elif msg.text:
                t = msg.text.lower()
                await self.log("INFO", f"💬 Бот: {msg.text[:100]}")

                # УСПЕХ
                if any(k in t for k in ["начислен", "получен", "успешн", "зачислен"]):
                    await self.log("INFO", "✅ БОНУС ПОЛУЧЕН")
                    self._reset_cycle()
                    try:
                        await db.increment_bonus(self.id)
                    except Exception:
                        pass
                    return

                # НЕВЕРНЫЙ КОД
                if any(k in t for k in ["неверн", "попробуй", "ошибк"]):
                    await self.log(
                        "WARN",
                        f"❌ Неверно | "
                        f"капча #{self._captchas_used} | "
                        f"попытка {self._attempts_on_current}/{ATTEMPTS_PER_CAPTCHA}"
                    )

                    # ===== ГЛАВНАЯ ЛОГИКА =====
                    if self._attempts_on_current < ATTEMPTS_PER_CAPTCHA:
                        # Ещё не пробовали повторно — решаем ТУ ЖЕ капчу заново
                        await self.log("INFO", f"🔁 Повторное решение той же капчи")
                        await self.pause(1.5, 3.0)
                        if self._current_image_bytes:
                            await self.solve_and_send(event, self._current_image_bytes)
                        return

                    # Уже 2 раза неверно — жмём "Новый код"
                    if self._captchas_used >= MAX_CAPTCHAS:
                        await self.log("WARN", f"🚫 Лимит {MAX_CAPTCHAS} капч — стоп")
                        self._reset_cycle()
                        return

                    await self.log("INFO", "🔄 2 попытки неверно — жму 'Новый код'")
                    await self.pause(2.0, 4.0)

                    pressed = await self.press_new_code(msg)
                    if pressed:
                        self._awaiting_new = True
                    else:
                        await self.log("WARN", "Кнопка 'Новый код' не найдена")
                        self._reset_cycle()

        except Exception as e:
            await self.log("ERROR", f"{e}")

    def _reset_cycle(self):
        self._current_image_bytes = None
        self._current_code = None
        self._attempts_on_current = 0
        self._captchas_used = 0
        self._awaiting_new = False

    async def send_bonus(self):
        if self.stopped:
            return

        if not await self._owner_subscribed():
            await self.log("WARN", "🚫 Владелец не подписан")
            return

        self._reset_cycle()
        self._last_id = None

        try:
            await self.pause(2.0, 8.0)
            await self.client.send_message(self.target_bot, self.bonus_text)
            await self.log("INFO", f"📨 Отправлено '{self.bonus_text}'")
        except FloodWaitError as e:
            await self.log("WARN", f"FloodWait {e.seconds}")
            await asyncio.sleep(e.seconds)
        except AuthKeyUnregisteredError:
            await self.log("ERROR", "Сессия отозвана")
            await db.update_status(self.id, "dead", "revoked")
            self.stopped = True
        except Exception as e:
            await self.log("ERROR", f"{e}")
