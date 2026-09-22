import os
import asyncio
import logging

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.filters import Command
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.errors import SessionPasswordNeededError, PhoneCodeInvalidError

import db

log = logging.getLogger("admin")

ADMIN_USER_ID = int(os.environ["ADMIN_USER_ID"])
API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

bot = Bot(token=os.environ["ADMIN_BOT_TOKEN"])
dp = Dispatcher()


class AddAccount(StatesGroup):
    waiting_phone = State()
    waiting_code = State()
    waiting_password = State()


def is_admin(msg: Message) -> bool:
    return msg.from_user.id == ADMIN_USER_ID


# Хранилище временных клиентов при добавлении
pending_clients: dict[int, TelegramClient] = {}


@dp.message(Command("start"))
async def cmd_start(msg: Message):
    if not is_admin(msg):
        return
    await msg.answer(
        "🤖 <b>Управление аккаунтами</b>\n\n"
        "Команды:\n"
        "/add — добавить аккаунт\n"
        "/list — список аккаунтов\n"
        "/del N — удалить аккаунт N\n"
        "/logs — последние логи\n"
        "/help — помощь",
        parse_mode="HTML"
    )


@dp.message(Command("help"))
async def cmd_help(msg: Message):
    if not is_admin(msg):
        return
    await msg.answer(
        "📖 <b>Помощь</b>\n\n"
        "/add — пошагово: номер → код → (2FA)\n"
        "/list — все аккаунты и их статус\n"
        "/del N — удалить аккаунт по ID\n"
        "/logs — последние 20 строк логов",
        parse_mode="HTML"
    )


@dp.message(Command("add"))
async def cmd_add(msg: Message, state: FSMContext):
    if not is_admin(msg):
        return
    await msg.answer("📱 Отправь номер телефона в формате +79991234567")
    await state.set_state(AddAccount.waiting_phone)


@dp.message(AddAccount.waiting_phone)
async def step_phone(msg: Message, state: FSMContext):
    if not is_admin(msg):
        return
    phone = msg.text.strip().replace(" ", "")
    if not phone.startswith("+") or not phone[1:].isdigit():
        await msg.answer("❌ Неверный формат. Пример: +79991234567")
        return

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()

    try:
        sent = await client.send_code_request(phone)
        await state.update_data(phone=phone, phone_code_hash=sent.phone_code_hash)
        pending_clients[msg.from_user.id] = client
        await msg.answer("✅ Код отправлен. Введи код (только цифры):")
        await state.set_state(AddAccount.waiting_code)
    except Exception as e:
        await client.disconnect()
        await msg.answer(f"❌ Ошибка: {e}")
        await state.clear()


@dp.message(AddAccount.waiting_code)
async def step_code(msg: Message, state: FSMContext):
    if not is_admin(msg):
        return
    code = msg.text.strip().replace(" ", "")
    if not code.isdigit():
        await msg.answer("❌ Код должен быть из цифр. Попробуй снова:")
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
    if not is_admin(msg):
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

        # Запуск worker-а (импорт здесь, чтобы избежать циклических зависимостей)
        import main as app_main
        await app_main.start_worker(acc_id)

        await msg.answer(
            f"✅ Аккаунт добавлен!\n"
            f"ID: <b>{acc_id}</b>\n"
            f"Username: @{username}\n"
            f"Worker запущен. Рассылка в 07:07 Самары.",
            parse_mode="HTML"
        )
    except Exception as e:
        await msg.answer(f"❌ Ошибка сохранения: {e}")
    finally:
        await client.disconnect()
        pending_clients.pop(msg.from_user.id, None)
        await state.clear()


@dp.message(Command("list"))
async def cmd_list(msg: Message):
    if not is_admin(msg):
        return
    accounts = await db.get_accounts()
    if not accounts:
        await msg.answer("📭 Нет аккаунтов. Добавь: /add")
        return

    lines = ["📋 <b>Аккаунты</b>\n"]
    for a in accounts:
        status_emoji = {"active": "🟢", "dead": "🔴", "stopped": "⚪"}.get(a["status"], "❔")
        lines.append(
            f"{status_emoji} <b>ID {a['id']}</b> — @{a['username']} | "
            f"{a['phone']} | бонусов: {a['bonuses']} | {a['status']}"
        )
    await msg.answer("\n".join(lines), parse_mode="HTML")


@dp.message(Command("del"))
async def cmd_del(msg: Message):
    if not is_admin(msg):
        return
    parts = msg.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await msg.answer("❌ Формат: /del N (например /del 3)")
        return
    acc_id = int(parts[1])

    import main as app_main
    await app_main.stop_worker(acc_id)

    if await db.delete_account(acc_id):
        await msg.answer(f"✅ Аккаунт {acc_id} удалён")
    else:
        await msg.answer(f"❌ Аккаунт {acc_id} не найден")


@dp.message(Command("logs"))
async def cmd_logs(msg: Message):
    if not is_admin(msg):
        return
    logs = await db.get_recent_logs(20)
    if not logs:
        await msg.answer("📭 Логов пока нет")
        return
    lines = ["📜 <b>Последние логи</b>\n"]
    for entry in reversed(logs):
        ts = entry["created_at"].strftime("%H:%M:%S")
        lines.append(f"[{ts}] [акк {entry['account_id']}] {entry['level']}: {entry['message'][:100]}")
    await msg.answer("\n".join(lines)[:4000], parse_mode="HTML")


async def run_admin_bot():
    log.info("🤖 Admin bot запущен")
    await dp.start_polling(bot)
