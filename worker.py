import asyncio
import random
import logging

from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import AuthKeyUnregisteredError, FloodWaitError, SessionRevokedError

from captcha import solve_captcha
import db

log = logging.getLogger("worker")

# Максимум капч за один цикл
MAX_CAPTCHA_ATTEMPTS = 5


class Worker:
    def __init__(self, acc: dict, api_id: int, api_hash: str,
                 target_bot: str, bonus_text: str):
        self.acc = acc
        self.id = acc["id"]
        self.target_bot = target_bot
        self.bonus_text = bonus_text
        self.client = TelegramClient(StringSession(acc["session_str"]), api_id, api_hash)
        self.stopped = False

        # Состояние
        self._last_msg_id = None         # ID последнего обработанного сообщения
        self._captcha_attempts = 0        # сколько капч за цикл
        self._awaiting_new_captcha = False  # ждём ли новую капчу после нажатия "Новый код"

    async def log(self, level: str, msg: str):
        log.info(f"[акк {self.id}] {msg}")
        await db.add_log(self.id, level, msg)

    async def human_pause(self, a=1.0, b=3.0):
        await asyncio.sleep(random.uniform(a, b))

    async def _owner_subscribed(self) -> bool:
        owner_id = self.acc.get("owner_id")
        if not owner_id:
            return True
        try:
            from admin_bot import check_subscription, is_admin
            if is_admin(owner_id):
                return True
            return await check_subscription(owner_id)
        except Exception as e:
            log.warning(f"[акк {self.id}] check_subscription error: {e}")
            return True

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

    async def _press_new_code_button(self, msg):
        """Нажимает кнопку '🔄 Новый код' в сообщении."""
        if not msg.buttons:
            return False
        for row in msg.buttons:
            for btn in row:
                btn_text = btn.text.lower()
                # Ищем кнопку "Новый код" / "New code" / "Обновить"
                if any(k in btn_text for k in ["новый код", "новый", "new code", "refresh", "обновить"]):
                    if btn.url:
                        continue
                    try:
                        await btn.click()
                        await self.log("INFO", f"👆 Нажал '{btn.text}' — жду новую капчу")
                        return True
                    except Exception as e:
                        await self.log("WARN", f"Не смог нажать '{btn.text}': {e}")
        return False

    async def on_message(self, event):
        msg = event.message

        # Не обрабатываем дубликаты одного и того же сообщения
        if msg.id == self._last_msg_id:
            return
        self._last_msg_id = msg.id

        try:
            # ===== ФОТО = КАПЧА =====
            if msg.photo:
                # Если это первая капча — обнуляем попытки
                if not self._awaiting_new_captcha:
                    self._captcha_attempts = 0

                self._captcha_attempts += 1
                self._awaiting_new_captcha = False  # получили новую — сбрасываем флаг

                await self.log("INFO", f"📩 Капча получена (попытка {self._captcha_attempts}/{MAX_CAPTCHA_ATTEMPTS})")

                # Проверка на лимит
                if self._captcha_attempts > MAX_CAPTCHA_ATTEMPTS:
                    await self.log("WARN", f"🚫 Лимит {MAX_CAPTCHA_ATTEMPTS} капч за цикл — стоп")
                    return

                # Скачиваем и распознаём
                image_bytes = await msg.download_media(bytes)
                await self.human_pause(2.0, 4.5)

                code = solve_captcha(image_bytes)
                await self.log("INFO", f"🔍 Распознан код: {code!r}")

                if not code or len(code) < 3:
                    await self.log("WARN", "Слишком короткий код — жму 'Новый код'")
                    # Пробуем получить новую капчу
                    if await self._press_new_code_button(msg):
                        self._awaiting_new_captcha = True
                    return

                # Отправляем код
                await self.human_pause(1.5, 4.0)
                await event.reply(code)
                await self.log("INFO", f"📤 Отправлен код: {code}")

            # ===== ТЕКСТ =====
            elif msg.text:
                text = msg.text
                await self.log("INFO", f"💬 Бот: {text[:120]}")
                text_lower = text.lower()

                # ===== УСПЕХ =====
                if any(k in text_lower for k in ["начислен", "получен", "успешн", "зачислен"]):
                    await self.log("INFO", "✅ БОНУС ПОЛУЧЕН")
                    self._captcha_attempts = 0
                    self._awaiting_new_captcha = False
                    try:
                        await db.increment_bonus(self.id)
                    except Exception:
                        pass
                    return

                # ===== НЕВЕРНЫЙ КОД =====
                if any(k in text_lower for k in ["неверн", "попробуй", "ошибк"]):
                    await self.log("WARN", f"❌ Неверный код (попытка {self._captcha_attempts}/{MAX_CAPTCHA_ATTEMPTS})")

                    # Проверяем лимит
                    if self._captcha_attempts >= MAX_CAPTCHA_ATTEMPTS:
                        await self.log("WARN", f"🚫 Лимит {MAX_CAPTCHA_ATTEMPTS} попыток — стоп на этот цикл")
                        return

                    # Ждём немного и жмём "Новый код"
                    await self.human_pause(2.0, 4.0)

                    pressed = await self._press_new_code_button(msg)
                    if pressed:
                        self._awaiting_new_captcha = True
                    else:
                        await self.log("WARN", "Кнопка 'Новый код' не найдена — стоп")
                        return

                # ===== КНОПКИ В ТЕКСТОВОМ СООБЩЕНИИ =====
                # (иногда кнопка идёт вместе с текстом)
                if msg.buttons and not self._awaiting_new_captcha:
                    # Если это не сообщение о неверном коде, но есть кнопка "Новый код" — жмём
                    pass

        except Exception as e:
            await self.log("ERROR", f"Ошибка обработки: {e}")

    async def send_bonus(self):
        if self.stopped:
            return

        if not await self._owner_subscribed():
            await self.log("WARN", "🚫 Владелец не подписан — пропуск")
            return

        # Обнуляем счётчики для нового цикла
        self._captcha_attempts = 0
        self._awaiting_new_captcha = False
        self._last_msg_id = None

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
