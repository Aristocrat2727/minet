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

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError

import db

log = logging.getLogger("admin")

# ===== НЕСКОЛЬКО АДМИНОВ ЧЕРЕЗ ЗАПЯТУЮ =====
ADMIN_IDS = [
    int(x.strip()) for x in os.environ["ADMIN_USER_ID"].split(",") if x.strip().isdigit()
]
log.info(f"👤 Админов загружено: {len(ADMIN_IDS)} → {ADMIN_IDS}")

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


pending_clients: dict[int, TelegramClient] = {}


# =========================================================
#                   ГЛАВНОЕ МЕНЮ (inline)
# =========================================================
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="menu_add")],
        [InlineKeyboardButton(text="📋 Список аккаунтов", callback_data="menu_list")],
        [
            InlineKeyboardButton(text="🎁 Получить бонус", callback_data="menu_bonus"),
            InlineKeyboardButton(text="▶️ Запустить всех", callback_data="menu_start_all"),
        ],
        [
            InlineKeyboardButton(text="⏸ Остановить всех", callback_data="menu_stop_all"),
            InlineKeyboardButton(text="📊 Статистика", callback_data="menu_stats"),
        ],
        [InlineKeyboardButton(text="📜 Логи", callback_data="menu_logs")],
    ])


def back_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ Назад в меню", callback_data="menu_main")],
    ])


async def send_main_menu(target, edit: bool = False):
    text = (
        "🤖 <b>Управление аккаунтами</b>\n\n"
        "Выбери действие кнопкой ниже.\n"
        "Или используй команды: /add /list /logs /help"
    )
    if edit:
        await target.edit_text(text, reply_markup=main_menu_kb(), parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=main_menu_kb(), parse_mode="HTML")


# =========================================================
#                   /start, /help
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
        "/list — список аккаунтов\n"
        "/logs — последние логи\n"
        "/help — помощь",
        parse_mode="HTML"
    )


# =========================================================
#                   CALLBACK: ГЛАВНОЕ МЕНЮ
# =========================================================
@dp.callback_query(F.data == "menu_main")
async def cb_main(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await send_main_menu(cb.message, edit=True)
    await cb.answer()


@dp.callback_query(F.data == "menu_list")
async def cb_list(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    text = await build_accounts_text()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить", callback_data="menu_list")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu_main")],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "menu_bonus")
async def cb_bonus(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await cb.answer("⏳ Отправляю бонус всем активным аккаунтам...", show_alert=False)

    import main as app_main
    count = 0
    for acc_id, worker in list(app_main.workers.items()):
        try:
            asyncio.create_task(worker.send_bonus())
            count += 1
        except Exception as e:
            log.error(f"Bonus trigger {acc_id}: {e}")

    await cb.message.answer(f"🎁 Запущена отправка бонуса для {count} аккаунтов")


@dp.callback_query(F.data == "menu_start_all")
async def cb_start_all(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await cb.answer("▶️ Запускаю...")
    import main as app_main
    accounts = await db.get_accounts(status="active")
    count = 0
    for acc in accounts:
        try:
            await app_main.start_worker(acc["id"])
            count += 1
        except Exception:
            pass
    await cb.message.answer(f"✅ Запущено: {count} аккаунтов")


@dp.callback_query(F.data == "menu_stop_all")
async def cb_stop_all(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await cb.answer("⏸ Останавливаю...")
    import main as app_main
    count = len(app_main.workers)
    for acc_id in list(app_main.workers.keys()):
        await app_main.stop_worker(acc_id)
    await cb.message.answer(f"⏸ Остановлено: {count} аккаунтов")


@dp.callback_query(F.data == "menu_logs")
async def cb_logs(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    logs = await db.get_recent_logs(20)
    if not logs:
        text = "📭 Логов пока нет"
    else:
        lines = ["📜 <b>Последние логи</b>\n"]
        for entry in reversed(logs):
            ts = entry["created_at"].strftime("%H:%M:%S")
            msg_text = entry["message"][:100].replace("<", "&lt;").replace(">", "&gt;")
            lines.append(f"[{ts}] <b>акк {entry['account_id']}</b> {entry['level']}: {msg_text}")
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


@dp.callback_query(F.data == "menu_stats")
async def cb_stats(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    accounts = await db.get_accounts()
    total = len(accounts)
    active = sum(1 for a in accounts if a["status"] == "active")
    dead = sum(1 for a in accounts if a["status"] == "dead")
    stopped = sum(1 for a in accounts if a["status"] == "stopped")
    bonuses = sum(a["bonuses"] or 0 for a in accounts)

    text = (
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Всего аккаунтов: {total}\n"
        f"🟢 Активных: {active}\n"
        f"🔴 Мёртвых: {dead}\n"
        f"⚪ Остановленных: {stopped}\n"
        f"🎁 Всего бонусов: {bonuses}"
    )
    try:
        await cb.message.edit_text(text, reply_markup=back_kb(), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=back_kb(), parse_mode="HTML")
    await cb.answer()


# =========================================================
#                   CALLBACK: ДОБАВЛЕНИЕ АККАУНТА
# =========================================================
@dp.callback_query(F.data == "menu_add")
async def cb_add(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await cb.message.answer(
        "📱 Отправь номер телефона в формате <code>+79991234567</code>",
        parse_mode="HTML"
    )
    await state.set_state(AddAccount.waiting_phone)
    await cb.answer()


# =========================================================
#                   КОМАНДА /add (текстом)
# =========================================================
@dp.message(Command("add"))
async def cmd_add(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await msg.answer("📱 Отправь номер телефона в формате +79991234567")
    await state.set_state(AddAccount.waiting_phone)


# =========================================================
#                   FSM: ВВОД НОМЕРА / КОДА / 2FA
# =========================================================
@dp.message(AddAccount.waiting_phone)
async def step_phone(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    phone = msg.text.strip().replace(" ", "").replace("-", "")
    if not phone.startswith("+") or not phone[1:].isdigit():
        await msg.answer("❌ Неверный формат. Пример: +79991234567")
        return

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()

    try:
        sent = await client.send_code_request(phone)
        await state.update_data(phone=phone, phone_code_hash=sent.phone_code_hash)
        pending_clients[msg.from_user.id] = client
        await msg.answer("✅ Код отправлен в Telegram. Введи код (только цифры):")
        await state.set_state(AddAccount.waiting_code)
    except Exception as e:
        await client.disconnect()
        await msg.answer(f"❌ Ошибка: {e}")
        await state.clear()


@dp.message(AddAccount.waiting_code)
async def step_code(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    code = msg.text.strip().replace(" ", "")
    if not code.isdigit():
        await msg.answer("❌ Код должен состоять из цифр. Попробуй снова:")
        return

    data = await state.get_data()
    phone = data["phone"]
    phone_code_hash = data["phone_code_hash"]
    client = pending_clients.get(msg.from_user.id)
    if not client:
        await msg.answer("❌ Сессия ввода потеряна. Начни заново /add")
        await state.clear()
        return

    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        await msg.answer("🔐 Включена 2FA. Введи пароль:")
        await state.set_state(AddAccount.waiting_password)
        return
    except PhoneCodeInvalidError:
        await msg.answer("❌ Неверный код. Попробуй снова:")
        return
    except Exception as e:
        await msg.answer(f"❌ Ошибка: {e}")
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
        await msg.answer("❌ Сессия ввода потеряна. Начни заново /add")
        await state.clear()
        return

    try:
        await client.sign_in(password=password)
    except Exception as e:
        await msg.answer(f"❌ Неверный пароль или ошибка: {e}")
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
            [InlineKeyboardButton(text="📋 Список аккаунтов", callback_data="menu_list")],
            [InlineKeyboardButton(text="◀️ В меню", callback_data="menu_main")],
        ])
        await msg.answer(
            f"✅ <b>Аккаунт добавлен!</b>\n\n"
            f"ID: <code>{acc_id}</code>\n"
            f"Username: @{username}\n"
            f"Phone: {phone}\n\n"
            f"⏰ Рассылка: каждый день в 07:07 Самары",
            reply_markup=kb,
            parse_mode="HTML"
        )
    except Exception as e:
        await msg.answer(f"❌ Ошибка сохранения: {e}")
    finally:
        await client.disconnect()
        pending_clients.pop(msg.from_user.id, None)
        await state.clear()


# =========================================================
#                   КОМАНДА /list
# =========================================================
async def build_accounts_text() -> str:
    accounts = await db.get_accounts()
    if not accounts:
        return "📭 <b>Нет аккаунтов.</b>\n\nНажми «➕ Добавить аккаунт» или напиши /add"

    lines = ["📋 <b>Аккаунты</b>\n"]
    for a in accounts:
        emoji = {"active": "🟢", "dead": "🔴", "stopped": "⚪"}.get(a["status"], "❔")
        lines.append(
            f"{emoji} <b>ID {a['id']}</b> — @{a['username']}\n"
            f"    📞 {a['phone']} | 🎁 {a['bonuses']} | {a['status']}"
        )
    return "\n".join(lines)


@dp.message(Command("list"))
async def cmd_list(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    text = await build_accounts_text()
    await msg.answer(text, reply_markup=main_menu_kb(), parse_mode="HTML")


# =========================================================
#                   КОМАНДА /logs
# =========================================================
@dp.message(Command("logs"))
async def cmd_logs(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    logs = await db.get_recent_logs(20)
    if not logs:
        await msg.answer("📭 Логов пока нет")
        return
    lines = ["📜 <b>Логи</b>\n"]
    for entry in reversed(logs):
        ts = entry["created_at"].strftime("%H:%M:%S")
        m = entry["message"][:100].replace("<", "&lt;").replace(">", "&gt;")
        lines.append(f"[{ts}] акк {entry['account_id']} {entry['level']}: {m}")
    await msg.answer("\n".join(lines)[:4000], parse_mode="HTML")


# =========================================================
#                   ЗАПУСК БОТА
# =========================================================
async def run_admin_bot():
    log.info(f"🤖 Admin bot запущен. Админов: {len(ADMIN_IDS)}")
    await dp.start_polling(bot)
