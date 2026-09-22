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


pending_clients: dict[int, TelegramClient] = {}


# =========================================================
#                      КЛАВИАТУРЫ
# =========================================================
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ Добавить аккаунт", callback_data="menu_add")],
        [InlineKeyboardButton(text="📋 Список аккаунтов", callback_data="menu_list")],
        [InlineKeyboardButton(text="🎁 Бонус: все аккаунты", callback_data="bonus_all")],
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
        "/start — меню\n"
        "/add — добавить аккаунт\n"
        "/list — список аккаунтов\n"
        "/del N — удалить аккаунт N\n"
        "/delall — удалить все\n"
        "/bonus — бонус всем\n"
        "/bonus N — бонус аккаунту N\n"
        "/bonus 1,2,3 — бонус нескольким\n"
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
#                      СПИСОК
# =========================================================
async def build_accounts_text() -> str:
    accounts = await db.get_accounts()
    if not accounts:
        return "📭 <b>Нет аккаунтов.</b>\n\nНажми «➕ Добавить аккаунт»"
    lines = ["📋 <b>Аккаунты</b>\n"]
    for a in accounts:
        emoji = {"active": "🟢", "dead": "🔴", "stopped": "⚪"}.get(a["status"], "❔")
        lines.append(
            f"{emoji} <b>ID {a['id']}</b> — @{a['username']}\n"
            f"    📞 {a['phone']} | 🎁 {a['bonuses']} | {a['status']}"
        )
    lines.append("\n💡 /bonus N — бонус конкретному")
    lines.append("💡 /del N — удалить конкретный")
    return "\n".join(lines)


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


@dp.message(Command("list"))
async def cmd_list(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    text = await build_accounts_text()
    await msg.answer(text, parse_mode="HTML")


# =========================================================
#              БОНУС: ВСЕ / ВЫБОРОЧНО
# =========================================================
@dp.callback_query(F.data == "bonus_all")
async def cb_bonus_all(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await cb.answer("⏳ Запускаю рассылку...")

    import main as app_main
    if not app_main.workers:
        await cb.message.answer("❌ Нет активных аккаунтов")
        return

    count = 0
    for acc_id, worker in list(app_main.workers.items()):
        asyncio.create_task(worker.send_bonus())
        count += 1

    await cb.message.answer(f"🎁 Бонус запущен для <b>{count}</b> аккаунтов", parse_mode="HTML")


@dp.callback_query(F.data == "bonus_select")
async def cb_bonus_select(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await cb.message.answer(
        "🎯 Отправь <b>ID аккаунтов</b> через запятую или диапазон.\n\n"
        "Примеры:\n"
        "<code>1</code> — только аккаунт 1\n"
        "<code>1,3,5</code> — аккаунты 1, 3, 5\n"
        "<code>2-5</code> — аккаунты 2, 3, 4, 5\n"
        "<code>1,3-5,8</code> — комбинированно",
        reply_markup=cancel_kb(),
        parse_mode="HTML"
    )
    await state.set_state(BonusSelect.waiting_ids)
    await cb.answer()


def parse_ids(raw: str) -> list[int]:
    """'1,3-5,8' → [1,3,4,5,8]"""
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
        await msg.answer("❌ Не смог распарсить ID. Пример: <code>1,3-5</code>", parse_mode="HTML")
        return

    import main as app_main

    ok, missing = 0, []
    for acc_id in ids:
        if acc_id in app_main.workers:
            asyncio.create_task(app_main.workers[acc_id].send_bonus())
            ok += 1
        else:
            missing.append(acc_id)

    text = f"🎁 Бонус запущен для <b>{ok}</b> аккаунтов"
    if missing:
        text += f"\n⚠️ Не найдено/остановлено: {missing}"

    await msg.answer(text, reply_markup=back_kb(), parse_mode="HTML")
    await state.clear()


@dp.message(Command("bonus"))
async def cmd_bonus(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split(maxsplit=1)
    import main as app_main

    # /bonus (без аргументов) → все
    if len(parts) == 1:
        if not app_main.workers:
            await msg.answer("❌ Нет активных аккаунтов")
            return
        count = 0
        for acc_id, w in list(app_main.workers.items()):
            asyncio.create_task(w.send_bonus())
            count += 1
        await msg.answer(f"🎁 Бонус запущен для <b>{count}</b> аккаунтов", parse_mode="HTML")
        return

    # /bonus 1,3-5
    ids = parse_ids(parts[1])
    if not ids:
        await msg.answer("❌ Формат: /bonus 1,3-5")
        return

    ok, missing = 0, []
    for acc_id in ids:
        if acc_id in app_main.workers:
            asyncio.create_task(app_main.workers[acc_id].send_bonus())
            ok += 1
        else:
            missing.append(acc_id)

    text = f"🎁 Бонус запущен для <b>{ok}</b> аккаунтов"
    if missing:
        text += f"\n⚠️ Не найдено: {missing}"
    await msg.answer(text, parse_mode="HTML")


# =========================================================
#              ЗАПУСК / ОСТАНОВКА ВСЕХ
# =========================================================
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
    await cb.message.answer(f"✅ Запущено: <b>{count}</b>", parse_mode="HTML")


@dp.callback_query(F.data == "menu_stop_all")
async def cb_stop_all(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await cb.answer("⏸ Останавливаю...")
    import main as app_main
    count = len(app_main.workers)
    await app_main.stop_all_workers()
    await cb.message.answer(f"⏸ Остановлено: <b>{count}</b>", parse_mode="HTML")


# =========================================================
#              УДАЛЕНИЕ: ВЫБОРОЧНО / ВСЕ
# =========================================================
@dp.callback_query(F.data == "delete_select")
async def cb_delete_select(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await cb.message.answer(
        "🗑 Отправь <b>ID аккаунтов</b> для удаления.\n\n"
        "Примеры:\n"
        "<code>1</code> — удалить 1\n"
        "<code>1,3,5</code> — удалить 1, 3, 5\n"
        "<code>2-5</code> — удалить 2, 3, 4, 5",
        reply_markup=cancel_kb(),
        parse_mode="HTML"
    )
    await state.set_state(DeleteAccount.waiting_ids)
    await cb.answer()


@dp.message(DeleteAccount.waiting_ids)
async def step_delete_ids(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    ids = parse_ids(msg.text.strip())
    if not ids:
        await msg.answer("❌ Не смог распарсить ID. Пример: <code>1,3-5</code>", parse_mode="HTML")
        return

    import main as app_main

    deleted = []
    for acc_id in ids:
        try:
            await app_main.stop_worker(acc_id)
            if await db.delete_account(acc_id):
                deleted.append(acc_id)
        except Exception as e:
            log.warning(f"Удаление {acc_id}: {e}")

    text = f"✅ Удалено: <b>{len(deleted)}</b>"
    if deleted:
        text += f"\n🗑 ID: {deleted}"
    await msg.answer(text, reply_markup=back_kb(), parse_mode="HTML")
    await state.clear()


@dp.callback_query(F.data == "delete_all")
async def cb_delete_all(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return

    # Подтверждение через две кнопки
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Да, удалить ВСЁ", callback_data="confirm_delete_all")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="menu_main")],
    ])
    await cb.message.answer(
        "⚠️ <b>Удалить ВСЕ аккаунты?</b>\n\nЭто действие необратимо.",
        reply_markup=kb, parse_mode="HTML"
    )
    await cb.answer()


@dp.callback_query(F.data == "confirm_delete_all")
async def cb_confirm_delete_all(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await cb.answer("💣 Удаляю...")

    import main as app_main
    await app_main.stop_all_workers()

    accounts = await db.get_accounts()
    count = 0
    for a in accounts:
        if await db.delete_account(a["id"]):
            count += 1

    await cb.message.edit_text(
        f"💣 Удалено <b>{count}</b> аккаунтов",
        reply_markup=back_kb(),
        parse_mode="HTML"
    )


@dp.message(Command("del"))
async def cmd_del(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split()
    if len(parts) != 2:
        await msg.answer("❌ Формат: /del N")
        return
    ids = parse_ids(parts[1])
    if not ids:
        await msg.answer("❌ Формат: /del 1,3-5")
        return

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
        [InlineKeyboardButton(text="✅ Да, удалить всё", callback_data="confirm_delete_all")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="menu_main")],
    ])
    await msg.answer("⚠️ <b>Удалить ВСЕ аккаунты?</b>", reply_markup=kb, parse_mode="HTML")


# =========================================================
#                      СТАТИСТИКА
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
        f"👥 Всего аккаунтов: {total}\n"
        f"🟢 Активных: {active}\n"
        f"🔴 Мёртвых: {dead}\n"
        f"⚙️ Работающих worker-ов: {running}\n"
        f"🎁 Всего бонусов: {bonuses}"
    )
    try:
        await cb.message.edit_text(text, reply_markup=back_kb(), parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=back_kb(), parse_mode="HTML")
    await cb.answer()


# =========================================================
#                      ЛОГИ
# =========================================================
@dp.callback_query(F.data == "menu_logs")
async def cb_logs(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    logs = await db.get_recent_logs(20)
    if not logs:
        text = "📭 Логов пока нет"
    else:
        lines = ["📜 <b>Последние логи</b>\n"]
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
#                  ДОБАВЛЕНИЕ АККАУНТА
# =========================================================
@dp.callback_query(F.data == "menu_add")
async def cb_add(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await cb.message.answer(
        "📱 Отправь номер в формате <code>+79991234567</code>",
        reply_markup=cancel_kb(),
        parse_mode="HTML"
    )
    await state.set_state(AddAccount.waiting_phone)
    await cb.answer()


@dp.message(Command("add"))
async def cmd_add(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await msg.answer("📱 Отправь номер в формате +79991234567")
    await state.set_state(AddAccount.waiting_phone)


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
        await msg.answer("✅ Код отправлен. Введи код:")
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
        await msg.answer("❌ Код только из цифр:")
        return

    data = await state.get_data()
    phone = data["phone"]
    phone_code_hash = data["phone_code_hash"]
    client = pending_clients.get(msg.from_user.id)
    if not client:
        await msg.answer("❌ Сессия потеряна. /add заново")
        await state.clear()
        return

    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        await msg.answer("🔐 2FA. Введи пароль:")
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
        await msg.answer("❌ Сессия потеряна. /add заново")
        await state.clear()
        return
    try:
        await client.sign_in(password=password)
    except Exception as e:
        await msg.answer(f"❌ Пароль неверный: {e}")
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
            f"ID: <code>{acc_id}</code>\n"
            f"Username: @{username}\n"
            f"Phone: {phone}\n\n"
            f"⏰ Рассылка: ежедневно в 07:07 Самары",
            reply_markup=kb, parse_mode="HTML"
        )
    except Exception as e:
        await msg.answer(f"❌ Ошибка сохранения: {e}")
    finally:
        await client.disconnect()
        pending_clients.pop(msg.from_user.id, None)
        await state.clear()


# =========================================================
#                   ЗАПУСК БОТА
# =========================================================
async def run_admin_bot():
    log.info(f"🤖 Admin bot запущен. Админов: {len(ADMIN_IDS)}")
    await dp.start_polling(bot)
