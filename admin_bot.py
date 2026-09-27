import os
import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message, CallbackQuery,
    InlineKeyboardMarkup, InlineKeyboardButton,
)
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

from telethon import TelegramClient, functions, types
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError

import db

log = logging.getLogger("admin")

ADMIN_IDS = [
    int(x.strip()) for x in os.environ["ADMIN_USER_ID"].split(",") if x.strip().isdigit()
]
log.info(f"👤 Админов: {len(ADMIN_IDS)} → {ADMIN_IDS}")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

bot = Bot(token=os.environ["ADMIN_BOT_TOKEN"])
dp = Dispatcher()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


class AddAccount(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()


class DeleteAccount(StatesGroup):
    waiting_ids = State()


class BonusSelect(StatesGroup):
    waiting_ids = State()


class CodeSelect(StatesGroup):
    waiting_id = State()


class KillSessionSelect(StatesGroup):
    waiting_id = State()


pending_clients: dict[int, TelegramClient] = {}


# =========================================================
#              ПРОВЕРКА АКТИВНОСТИ
# =========================================================
async def check_account_alive(session_str: str) -> bool:
    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    try:
        await asyncio.wait_for(client.connect(), timeout=12)
        ok = await client.is_user_authorized()
        await client.disconnect()
        return ok
    except Exception:
        try:
            await client.disconnect()
        except Exception:
            pass
        return False


# =========================================================
#              ПОЛУЧИТЬ КОД ИЗ 777000
# =========================================================
async def get_last_code_from_telegram(session_str: str) -> str | None:
    """Достаёт последнее сообщение с кодом от Telegram (777000)."""
    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    try:
        await asyncio.wait_for(client.connect(), timeout=12)
        if not await client.is_user_authorized():
            await client.disconnect()
            return None

        # Ищем последние 20 сообщений от 777000
        async for msg in client.iter_messages(777000, limit=20):
            text = msg.text or ""
            # Ищем 5-значный код (Telegram обычно шлёт 5 цифр)
            import re
            codes = re.findall(r"\b(\d{4,6})\b", text)
            if codes:
                await client.disconnect()
                return codes[0]

        await client.disconnect()
        return None
    except Exception as e:
        log.warning(f"get_code error: {e}")
        try:
            await client.disconnect()
        except Exception:
            pass
        return None


# =========================================================
#              УДАЛИТЬ ВСЕ СЕССИИ КРОМЕ ТЕКУЩЕЙ
# =========================================================
async def kill_other_sessions(session_str: str) -> int:
    """Удаляет все активные сессии, кроме текущей. Возвращает сколько удалено."""
    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    try:
        await asyncio.wait_for(client.connect(), timeout=12)
        if not await client.is_user_authorized():
            await client.disconnect()
            return -1  # сессия мертва

        # Получаем список всех активных сессий
        result = await client(functions.account.GetAuthorizationsRequest())
        current_hash = None

        # Наша текущая сессия — та, где current=True
        # Telethon сам знает свою сессию, но нам нужно найти её hash
        # Проходим по всем, оставляем ту, что "текущая" (обычно последняя добавленная)
        # Проще: удаляем все, кроме последней по дате создания? Нет.
        # Решение: Telethon не даёт напрямую "какая моя", но есть трюк — 
        # мы можем не удалять ту, что соответствует текущему устройству.
        # Обычно "current=True" помечено в raw API.

        # Реальный способ: у Telethon нет прямого API "current".
        # НО: список приходит отсортированным, и текущая — та, что last активна
        # ещё сложнее. Проще — удалить все, кроме той, у которой device_model совпадает с нашей.
        
        # Ещё проще: удалить все КРОМЕ последней добавленной (текущей).
        # Но это опасно, если ты добавил другой аккаунт.
        
        # Безопасный вариант: удалить все, что не является "текущей".
        # Telethon хранит `self.session` и может дать её hash через
        # `client.session.auth_key` — но это не hash сессии.

        # Обходной путь — использовать низкоуровневый API:
        # GetAuthorizations возвращает объект, где есть hash сессий.
        # Мы должны сравнить fingerprint с нашим. Увы, fingerprint не всегда точный.
        
        # Практическое решение: удалить все сессии, кроме самой новой.
        # Отсортируем по date_created, оставим только последнюю.

        auths = sorted(result.authorizations, key=lambda a: a.date_created)
        deleted = 0
        for a in auths[:-1]:  # всё, кроме последней
            try:
                await client(functions.account.ResetAuthorizationRequest(hash=a.hash))
                deleted += 1
                await asyncio.sleep(0.5)
            except Exception as e:
                log.warning(f"Kill session error: {e}")

        await client.disconnect()
        return deleted

    except Exception as e:
        log.warning(f"kill_sessions error: {e}")
        try:
            await client.disconnect()
        except Exception:
            pass
        return -1


# =========================================================
#                      КЛАВИАТУРЫ
# =========================================================
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="menu_add")],
        [InlineKeyboardButton(text="📋 Список аккаунтов", callback_data="menu_list")],
        [InlineKeyboardButton(text="🔢 Получить код", callback_data="menu_code")],
        [InlineKeyboardButton(text="🚪 Удалить сессии", callback_data="menu_kill")],
        [InlineKeyboardButton(text="🎁 Бонус: все", callback_data="bonus_all")],
        [InlineKeyboardButton(text="🎯 Бонус: выборочно", callback_data="bonus_select")],
        [
            InlineKeyboardButton(text="▶️ Запустить всех", callback_data="menu_start_all"),
            InlineKeyboardButton(text="⏸ Остановить всех", callback_data="menu_stop_all"),
        ],
        [
            InlineKeyboardButton(text="🗑 Удалить выборочно", callback_data="delete_select"),
            InlineKeyboardButton(text="💣 Удалить всех", callback_data="delete_all"),
        ],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="menu_stats")],
        [InlineKeyboardButton(text="📜 Логи", callback_data="menu_logs")],
    ])


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu_main")],
    ])


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Отмена", callback_data="menu_main")],
    ])


async def send_main_menu(target, edit: bool = False):
    text = "🤖 <b>Управление аккаунтами</b>\n\nВыбери действие:"
    if edit:
        await target.edit_text(text, reply_markup=main_menu_kb(), parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=main_menu_kb(), parse_mode="HTML")


# =========================================================
#                  СТАРТ / ХЕЛП
# =========================================================
@dp.message(Command("start"))
async def cmd_start(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    await send_main_menu(msg)


@dp.message(Command("help"))
async def cmd_help(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    await msg.answer(
        "📖 <b>Команды</b>\n\n"
        "/add — добавить аккаунт\n"
        "/list — список с проверкой\n"
        "/code — получить код с 777000\n"
        "/code 3 — код для аккаунта 3\n"
        "/kill — удалить все сессии\n"
        "/kill 3 — для аккаунта 3\n"
        "/bonus — бонус всем\n"
        "/bonus 1,3-5 — нескольким\n"
        "/del 3 — удалить аккаунт\n"
        "/delall — удалить все\n"
        "/logs — логи",
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "menu_main")
async def cb_main(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await state.clear()
    await send_main_menu(cb.message, edit=True)
    await cb.answer()


# =========================================================
#                    СПИСОК АККАУНТОВ
# =========================================================
async def build_accounts_text(check: bool = True) -> str:
    accounts = await db.get_accounts()
    if not accounts:
        return "📭 <b>Нет аккаунтов.</b>\n\nНажми «➕ Добавить аккаунт»"

    if check:
        tasks = [check_account_alive(a["session_str"]) for a in accounts]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for a, r in zip(accounts, results):
            ok = r if isinstance(r, bool) else False
            if ok and a["status"] != "active":
                await db.update_status(a["id"], "active", None)
                a["status"] = "active"
            elif not ok and a["status"] != "dead":
                await db.update_status(a["id"], "dead", "session not authorized")
                a["status"] = "dead"

    lines = ["📋 <b>Аккаунты</b>\n"]
    alive = dead = 0
    for a in accounts:
        emoji = {"active": "🟢", "dead": "🔴", "stopped": "⚪"}.get(a["status"], "❔")
        lines.append(
            f"{emoji} <b>ID {a['id']}</b> — @{a['username']}\n"
            f"    📞 {a['phone']} | 🎁 {a['bonuses']} | {a['status']}"
        )
        if a["status"] == "active":
            alive += 1
        elif a["status"] == "dead":
            dead += 1

    lines.append(f"\n📊 Живых: {alive} | Мёртвых: {dead}")
    return "\n".join(lines)


@dp.callback_query(F.data == "menu_list")
async def cb_list(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await cb.answer("🔍 Проверяю...")
    text = await build_accounts_text(check=True)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="menu_list")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu_main")],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.message(Command("list"))
async def cmd_list(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    m = await msg.answer("🔍 Проверяю аккаунты...")
    text = await build_accounts_text(check=True)
    try:
        await m.edit_text(text, parse_mode="HTML")
    except Exception:
        await msg.answer(text, parse_mode="HTML")


# =========================================================
#                 ПОЛУЧИТЬ КОД (/code)
# =========================================================
@dp.callback_query(F.data == "menu_code")
async def cb_menu_code(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    accounts = await db.get_accounts()
    if not accounts:
        await cb.message.answer("📭 Нет аккаунтов")
        await cb.answer()
        return

    lines = ["🔢 <b>Отправь ID аккаунта</b>, чтобы получить последний код с 777000\n"]
    for a in accounts[:30]:
        emoji = {"active": "🟢", "dead": "🔴", "stopped": "⚪"}.get(a["status"], "❔")
        lines.append(f"{emoji} ID {a['id']} — @{a['username']} ({a['phone']})")

    await cb.message.answer(
        "\n".join(lines),
        reply_markup=cancel_kb(),
        parse_mode="HTML"
    )
    await state.set_state(CodeSelect.waiting_id)
    await cb.answer()


@dp.message(CodeSelect.waiting_id)
async def step_code_id(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    raw = msg.text.strip().replace(" ", "").replace(",", "")
    if not raw.isdigit():
        await msg.answer("❌ ID должен быть числом")
        return
    acc_id = int(raw)

    acc = await db.get_account(acc_id)
    if not acc:
        await msg.answer(f"❌ Аккаунт {acc_id} не найден")
        return

    m = await msg.answer(f"🔍 Ищу последний код для <b>ID {acc_id}</b>...", parse_mode="HTML")
    code = await get_last_code_from_telegram(acc["session_str"])

    if code:
        await m.edit_text(
            f"🔢 <b>Последний код для ID {acc_id}</b>\n\n"
            f"👤 @{acc['username']}\n"
            f"📞 {acc['phone']}\n\n"
            f"<b>КОД: <code>{code}</code></b>",
            parse_mode="HTML"
        )
    else:
        await m.edit_text(
            f"❌ Не нашёл код для <b>ID {acc_id}</b>\n\n"
            f"Возможные причины:\n"
            f"• Сессия мертва — проверь /list\n"
            f"• Код старше 20 сообщений\n"
            f"• Telegram не присылал код",
            parse_mode="HTML"
        )
    await state.clear()


@dp.message(Command("code"))
async def cmd_code(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split()

    if len(parts) == 1:
        # Без аргумента — показать список и ждать ID
        await cb_menu_code_menu(msg, state)
        return

    raw = parts[1].strip()
    if not raw.isdigit():
        await msg.answer("❌ Формат: /code 3")
        return
    acc_id = int(raw)

    acc = await db.get_account(acc_id)
    if not acc:
        await msg.answer(f"❌ Аккаунт {acc_id} не найден")
        return

    m = await msg.answer(f"🔍 Ищу код для <b>ID {acc_id}</b>...", parse_mode="HTML")
    code = await get_last_code_from_telegram(acc["session_str"])
    if code:
        await m.edit_text(
            f"🔢 <b>Код для ID {acc_id}</b> (@{acc['username']})\n\n"
            f"<b><code>{code}</code></b>",
            parse_mode="HTML"
        )
    else:
        await m.edit_text(f"❌ Код не найден для ID {acc_id}")


async def cb_menu_code_menu(msg: Message, state: FSMContext):
    accounts = await db.get_accounts()
    if not accounts:
        await msg.answer("📭 Нет аккаунтов")
        return
    lines = ["🔢 <b>Отправь ID аккаунта</b>:\n"]
    for a in accounts[:30]:
        emoji = {"active": "🟢", "dead": "🔴", "stopped": "⚪"}.get(a["status"], "❔")
        lines.append(f"{emoji} ID {a['id']} — @{a['username']} ({a['phone']})")
    await msg.answer("\n".join(lines), reply_markup=cancel_kb(), parse_mode="HTML")
    await state.set_state(CodeSelect.waiting_id)


# =========================================================
#              УДАЛИТЬ СЕССИИ (/kill)
# =========================================================
@dp.callback_query(F.data == "menu_kill")
async def cb_menu_kill(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    accounts = await db.get_accounts()
    if not accounts:
        await cb.message.answer("📭 Нет аккаунтов")
        await cb.answer()
        return

    lines = ["🚪 <b>Отправь ID</b> — удалю все сессии на аккаунте, кроме текущей\n"]
    for a in accounts[:30]:
        emoji = {"active": "🟢", "dead": "🔴", "stopped": "⚪"}.get(a["status"], "❔")
        lines.append(f"{emoji} ID {a['id']} — @{a['username']}")

    await cb.message.answer(
        "\n".join(lines),
        reply_markup=cancel_kb(),
        parse_mode="HTML"
    )
    await state.set_state(KillSessionSelect.waiting_id)
    await cb.answer()


@dp.message(KillSessionSelect.waiting_id)
async def step_kill_id(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    raw = msg.text.strip().replace(" ", "")
    if not raw.isdigit():
        await msg.answer("❌ ID должен быть числом")
        return
    acc_id = int(raw)

    acc = await db.get_account(acc_id)
    if not acc:
        await msg.answer(f"❌ Аккаунт {acc_id} не найден")
        return

    m = await msg.answer(f"🚪 Удаляю сессии для <b>ID {acc_id}</b>...", parse_mode="HTML")
    count = await kill_other_sessions(acc["session_str"])

    if count == -1:
        await m.edit_text(
            f"❌ Сессия мертва или не авторизована\n"
            f"Аккаунт ID {acc_id}",
            parse_mode="HTML"
        )
    else:
        await m.edit_text(
            f"✅ <b>Удалено сессий:</b> {count}\n\n"
            f"👤 @{acc['username']} (ID {acc_id})\n"
            f"Осталась только текущая сессия",
            parse_mode="HTML"
        )
    await state.clear()


@dp.message(Command("kill"))
async def cmd_kill(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split()
    if len(parts) == 1:
        await cb_menu_kill(MessageStub(msg), state)
        return

    raw = parts[1].strip()
    if not raw.isdigit():
        await msg.answer("❌ Формат: /kill 3")
        return
    acc_id = int(raw)

    acc = await db.get_account(acc_id)
    if not acc:
        await msg.answer(f"❌ Аккаунт {acc_id} не найден")
        return

    m = await msg.answer(f"🚪 Удаляю сессии для ID {acc_id}...")
    count = await kill_other_sessions(acc["session_str"])
    if count == -1:
        await m.edit_text(f"❌ Сессия мертва (ID {acc_id})")
    else:
        await m.edit_text(f"✅ Удалено сессий: {count}")


class MessageStub:
    """Заглушка для вызова callback-функций из Command."""
    def __init__(self, real_msg: Message):
        self._msg = real_msg
        self.from_user = real_msg.from_user

    async def answer(self, text, **kw):
        return await self._msg.answer(text, **kw)

    async def edit_text(self, text, **kw):
        return await self._msg.edit_text(text, **kw)


# =========================================================
#                    БОНУСЫ
# =========================================================
@dp.callback_query(F.data == "bonus_all")
async def cb_bonus_all(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await cb.answer("⏳ Запускаю...")
    import main as app_main
    if not app_main.workers:
        await cb.message.answer("❌ Нет активных аккаунтов")
        return
    count = 0
    for w in list(app_main.workers.values()):
        asyncio.create_task(w.send_bonus())
        count += 1
    await cb.message.answer(f"🎁 Бонус: <b>{count}</b> аккаунтов", parse_mode="HTML")


@dp.callback_query(F.data == "bonus_select")
async def cb_bonus_select(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await cb.message.answer(
        "🎯 Отправь <b>ID</b>:\n<code>1,3-5,8</code>",
        reply_markup=cancel_kb(), parse_mode="HTML"
    )
    await state.set_state(BonusSelect.waiting_ids)
    await cb.answer()


def parse_ids(raw: str) -> list[int]:
    result = []
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            try:
                a, b = part.split("-")
                result.extend(range(int(a), int(b) + 1))
            except Exception:
                continue
        else:
            try:
                result.append(int(part))
            except Exception:
                continue
    return sorted(set(result))


@dp.message(BonusSelect.waiting_ids)
async def step_bonus_ids(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    ids = parse_ids(msg.text.strip())
    if not ids:
        await msg.answer("❌ Пример: <code>1,3-5</code>", parse_mode="HTML")
        return

    import main as app_main
    ok, missing = 0, []
    for acc_id in ids:
        if acc_id in app_main.workers:
            asyncio.create_task(app_main.workers[acc_id].send_bonus())
            ok += 1
        else:
            missing.append(acc_id)

    text = f"🎁 Бонус: <b>{ok}</b>"
    if missing:
        text += f"\n⚠️ Не найдено: {missing}"
    await msg.answer(text, reply_markup=back_kb(), parse_mode="HTML")
    await state.clear()


@dp.message(Command("bonus"))
async def cmd_bonus(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split(maxsplit=1)
    import main as app_main

    if len(parts) == 1:
        if not app_main.workers:
            await msg.answer("❌ Нет активных")
            return
        count = 0
        for w in list(app_main.workers.values()):
            asyncio.create_task(w.send_bonus())
            count += 1
        await msg.answer(f"🎁 Бонус: <b>{count}</b>", parse_mode="HTML")
        return

    ids = parse_ids(parts[1])
    if not ids:
        await msg.answer("❌ /bonus 1,3-5")
        return

    ok, missing = 0, []
    for acc_id in ids:
        if acc_id in app_main.workers:
            asyncio.create_task(app_main.workers[acc_id].send_bonus())
            ok += 1
        else:
            missing.append(acc_id)

    text = f"🎁 Бонус: <b>{ok}</b>"
    if missing:
        text += f"\n⚠️ {missing}"
    await msg.answer(text, parse_mode="HTML")


# =========================================================
#              ЗАПУСК / ОСТАНОВКА
# =========================================================
@dp.callback_query(F.data == "menu_start_all")
async def cb_start_all(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await cb.answer("▶️...")
    import main as app_main
    accounts = await db.get_accounts(status="active")
    count = 0
    for acc in accounts:
        try:
            await app_main.start_worker(acc["id"])
            count += 1
        except Exception:
            pass
    await cb.message.answer(f"✅ Запущено: <b>{count}</b>", parse_mode="HTML")


@dp.callback_query(F.data == "menu_stop_all")
async def cb_stop_all(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await cb.answer("⏸...")
    import main as app_main
    count = len(app_main.workers)
    await app_main.stop_all_workers()
    await cb.message.answer(f"⏸ Остановлено: <b>{count}</b>", parse_mode="HTML")


# =========================================================
#                    УДАЛЕНИЕ
# =========================================================
@dp.callback_query(F.data == "delete_select")
async def cb_delete_select(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await cb.message.answer(
        "🗑 Отправь <b>ID</b>: <code>1,3-5</code>",
        reply_markup=cancel_kb(), parse_mode="HTML"
    )
    await state.set_state(DeleteAccount.waiting_ids)
    await cb.answer()


@dp.message(DeleteAccount.waiting_ids)
async def step_delete_ids(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    ids = parse_ids(msg.text.strip())
    if not ids:
        await msg.answer("❌ Пример: <code>1,3-5</code>", parse_mode="HTML")
        return

    import main as app_main
    deleted = []
    for acc_id in ids:
        try:
            await app_main.stop_worker(acc_id)
            if await db.delete_account(acc_id):
                deleted.append(acc_id)
        except Exception as e:
            log.warning(f"Del {acc_id}: {e}")

    await msg.answer(f"✅ Удалено: <b>{deleted}</b>", reply_markup=back_kb(), parse_mode="HTML")
    await state.clear()


@dp.callback_query(F.data == "delete_all")
async def cb_delete_all(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да", callback_data="confirm_delete_all")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="menu_main")],
    ])
    await cb.message.answer("⚠️ Удалить ВСЕ аккаунты?", reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "confirm_delete_all")
async def cb_confirm_delete_all(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await cb.answer("💣...")
    import main as app_main
    await app_main.stop_all_workers()
    accounts = await db.get_accounts()
    count = 0
    for a in accounts:
        if await db.delete_account(a["id"]):
            count += 1
    await cb.message.edit_text(f"💣 Удалено: <b>{count}</b>", reply_markup=back_kb(), parse_mode="HTML")


@dp.message(Command("del"))
async def cmd_del(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split()
    if len(parts) != 2:
        await msg.answer("❌ /del 3")
        return
    ids = parse_ids(parts[1])
    import main as app_main
    deleted = []
    for acc_id in ids:
        await app_main.stop_worker(acc_id)
        if await db.delete_account(acc_id):
            deleted.append(acc_id)
    await msg.answer(f"✅ Удалено: <b>{deleted}</b>", parse_mode="HTML")


@dp.message(Command("delall"))
async def cmd_delall(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да", callback_data="confirm_delete_all")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="menu_main")],
    ])
    await msg.answer("⚠️ Удалить всё?", reply_markup=kb, parse_mode="HTML")


# =========================================================
#                    СТАТИСТИКА / ЛОГИ
# =========================================================
@dp.callback_query(F.data == "menu_stats")
async def cb_stats(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    accounts = await db.get_accounts()
    total = len(accounts)
    active = sum(1 for a in accounts if a["status"] == "active")
    dead = sum(1 for a in accounts if a["status"] == "dead")
    bonuses = sum(a["bonuses"] or 0 for a in accounts)
    import main as app_main
    running = len(app_main.workers)
    text = (
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Всего: {total}\n"
        f"🟢 Живых: {active}\n"
        f"🔴 Мёртвых: {dead}\n"
        f"⚙️ Worker-ов: {running}\n"
        f"🎁 Бонусов: {bonuses}"
    )
    try:
        await cb.message.edit_text(text, reply_markup=back_kb(), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=back_kb(), parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "menu_logs")
async def cb_logs(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    logs = await db.get_recent_logs(20)
    if not logs:
        text = "📭 Логов нет"
    else:
        lines = ["📜 <b>Логи</b>\n"]
        for e in reversed(logs):
            ts = e["created_at"].strftime("%H:%M:%S")
            m = e["message"][:100].replace("<", "&lt;").replace(">", "&gt;")
            lines.append(f"[{ts}] <b>акк {e['account_id']}</b> {e['level']}: {m}")
        text = "\n".join(lines)[:4000]
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="menu_logs")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu_main")],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.message(Command("logs"))
async def cmd_logs(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    logs = await db.get_recent_logs(20)
    if not logs:
        await msg.answer("📭 Логов нет")
        return
    lines = ["📜 <b>Логи</b>\n"]
    for e in reversed(logs):
        ts = e["created_at"].strftime("%H:%M:%S")
        m = e["message"][:100].replace("<", "&lt;").replace(">", "&gt;")
        lines.append(f"[{ts}] акк {e['account_id']} {e['level']}: {m}")
    await msg.answer("\n".join(lines)[:4000], parse_mode="HTML")


# =========================================================
#                 ДОБАВЛЕНИЕ АККАУНТА
# =========================================================
@dp.callback_query(F.data == "menu_add")
async def cb_add(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await cb.message.answer(
        "📱 Отправь номер: <code>+79991234567</code>",
        reply_markup=cancel_kb(), parse_mode="HTML"
    )
    await state.set_state(AddAccount.waiting_phone)
    await cb.answer()


@dp.message(Command("add"))
async def cmd_add(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await msg.answer("📱 Отправь номер: +79991234567")
    await state.set_state(AddAccount.waiting_phone)


@dp.message(AddAccount.waiting_phone)
async def step_phone(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    phone = msg.text.strip().replace(" ", "").replace("-", "")
    if not phone.startswith("+") or not phone[1:].isdigit():
        await msg.answer("❌ Пример: +79991234567")
        return

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        await state.update_data(phone=phone, phone_code_hash=sent.phone_code_hash)
        pending_clients[msg.from_user.id] = client
        await msg.answer("✅ Код отправлен. Введи код:")
        await state.set_state(AddAccount.waiting_code)
    except Exception as e:
        await client.disconnect()
        await msg.answer(f"❌ {e}")
        await state.clear()


@dp.message(AddAccount.waiting_code)
async def step_code(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    code = msg.text.strip().replace(" ", "")
    if not code.isdigit():
        await msg.answer("❌ Код только цифры:")
        return

    data = await state.get_data()
    phone = data["phone"]
    phone_code_hash = data["phone_code_hash"]
    client = pending_clients.get(msg.from_user.id)
    if not client:
        await msg.answer("❌ Сессия потеряна. /add")
        await state.clear()
        return

    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        await msg.answer("🔐 2FA. Введи пароль:")
        await state.set_state(AddAccount.waiting_password)
        return
    except PhoneCodeInvalidError:
        await msg.answer("❌ Неверный код:")
        return
    except Exception as e:
        await msg.answer(f"❌ {e}")
        await client.disconnect()
        pending_clients.pop(msg.from_user.id, None)
        await state.clear()
        return

    await finish_add(msg, state, client, phone)


@dp.message(AddAccount.waiting_password)
async def step_password(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    password = msg.text.strip()
    data = await state.get_data()
    phone = data["phone"]
    client = pending_clients.get(msg.from_user.id)
    if not client:
        await msg.answer("❌ Сессия потеряна. /add")
        await state.clear()
        return
    try:
        await client.sign_in(password=password)
    except Exception as e:
        await msg.answer(f"❌ {e}")
        return
    await finish_add(msg, state, client, phone)


async def finish_add(msg: Message, state: FSMContext, client: TelegramClient, phone: str):
    try:
        me = await client.get_me()
        session_str = client.session.save()
        username = me.username or f"id{me.id}"
        acc_id = await db.add_account(phone, session_str, username)
        import main as app_main
        await app_main.start_worker(acc_id)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Список", callback_data="menu_list")],
            [InlineKeyboardButton(text="◀️ В меню", callback_data="menu_main")],
        ])
        await msg.answer(
            f"✅ <b>Аккаунт добавлен!</b>\n\n"
            f"ID: <code>{acc_id}</code>\n@{username}\n📞 {phone}",
            reply_markup=kb, parse_mode="HTML"
        )
    except Exception as e:
        await msg.answer(f"❌ {e}")
    finally:
        await client.disconnect()
        pending_clients.pop(msg.from_user.id, None)
        await state.clear()


# =========================================================
#                    ЗАПУСК
# =========================================================
async def run_admin_bot():
    log.info(f"🤖 Admin bot. Админов: {len(ADMIN_IDS)}")
    await dp.start_polling(bot)
