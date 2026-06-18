import asyncio
import logging
import os
import random
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from html import escape
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message


TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID_RAW = os.getenv("ADMIN_ID")
EXTRA_ADMIN_IDS_RAW = os.getenv("EXTRA_ADMIN_IDS", "")
CHANNEL_ID_RAW = os.getenv("CHANNEL_ID")
CRYPTO_PAY_TOKEN = os.getenv("CRYPTO_PAY_TOKEN")
CRYPTO_PAY_API_URL = os.getenv("CRYPTO_PAY_API_URL", "https://pay.crypt.bot/api")
USERS_FILE = Path(__file__).with_name("broadcast_users.json")
ADMINS_FILE = Path(__file__).with_name("extra_admins.json")

# Меняй только эту одну строку.
BRAND_USERNAME, BRAND_AUTHOR = "@brazers_promo", "от Илюшки"

MAX_MINI_PLAYERS = 6
MINI_JOIN_COOLDOWN_SECONDS = 5
ROLL_DELETE_DELAY_SECONDS = 12
KIND_TITLES = {
    "mini_money2": "Mini Babki 2",
    "mini": "Мини-розыгрыш",
    "classic": "Розыгрыш",
    "duel": "Дуэль",
    "darts": "Дартс-дуэль",
    "bowling": "Боулинг-дуэль",
    "football": "Футбол-дуэль",
}

if not TOKEN or not ADMIN_ID_RAW or not CHANNEL_ID_RAW:
    raise ValueError("Set BOT_TOKEN, ADMIN_ID and CHANNEL_ID environment variables")

try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError as exc:
    raise ValueError("ADMIN_ID must be a number") from exc

try:
    CHANNEL_ID: int | str = int(CHANNEL_ID_RAW)
except ValueError:
    CHANNEL_ID = CHANNEL_ID_RAW


@dataclass
class Giveaway:
    kind: str
    prize: str
    winners_count: int = 1
    max_players: Optional[int] = None
    message_id: Optional[int] = None
    participants: List[dict] = field(default_factory=list)
    finished: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class CompletedGiveaway:
    kind: str
    prize: str
    participants: List[dict] = field(default_factory=list)
    winners: List[dict] = field(default_factory=list)
    winners_count: int = 1
    message_id: Optional[int] = None
    meta: Dict[str, Any] = field(default_factory=dict)


bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
dp = Dispatcher()
active_giveaways: Dict[str, Giveaway] = {}
completed_giveaways: Dict[str, CompletedGiveaway] = {}
admin_state: Dict[int, dict] = {}
mini_join_cooldowns: Dict[int, float] = {}
giveaway_join_locks: Dict[str, asyncio.Lock] = {kind: asyncio.Lock() for kind in KIND_TITLES}


def usd_decimal(value: str | Decimal) -> Decimal:
    amount = Decimal(str(value).replace("$", "").replace(",", ".").strip())
    if amount <= 0:
        raise InvalidOperation
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def format_usd(value: str | Decimal) -> str:
    return f"{usd_decimal(value):.2f}"


async def notify_admins(text: str) -> None:
    for admin_id in sorted(all_admin_ids()):
        try:
            await bot.send_message(admin_id, text, disable_web_page_preview=True)
        except Exception:
            logging.exception("Could not notify admin %s", admin_id)


def crypto_invoice_url(result: dict) -> Optional[str]:
    return result.get("pay_url") or result.get("bot_invoice_url") or result.get("mini_app_invoice_url") or result.get("invoice_url")


def crypto_check_url(result: dict) -> Optional[str]:
    return result.get("bot_check_url") or result.get("mini_app_check_url") or result.get("check_url") or result.get("send_url")


async def crypto_pay_request(method: str, payload: dict) -> dict:
    if not CRYPTO_PAY_TOKEN:
        raise RuntimeError("Set CRYPTO_PAY_TOKEN for Mini Babki 2")

    async with aiohttp.ClientSession(headers={"Crypto-Pay-API-Token": CRYPTO_PAY_TOKEN}) as session:
        async with session.post(f"{CRYPTO_PAY_API_URL.rstrip('/')}/{method}", json=payload) as response:
            data = await response.json(content_type=None)

    if not data.get("ok"):
        raise RuntimeError(data.get("error", f"Crypto Pay API error on {method}"))
    return data["result"]


async def create_crypto_invoice(amount_usd: str, title: str) -> dict:
    result = await crypto_pay_request(
        "createInvoice",
        {
            "asset": "USDT",
            "amount": amount_usd,
            "description": title,
            "paid_btn_name": "viewItem",
            "paid_btn_url": f"https://t.me/{BRAND_USERNAME.lstrip('@')}",
        },
    )
    url = crypto_invoice_url(result)
    if not url:
        raise RuntimeError("Crypto Pay API did not return invoice URL")
    return {"invoice_id": result.get("invoice_id") or result.get("id"), "url": url}


async def create_crypto_check(amount_usd: str, winner: dict) -> dict:
    payload = {"asset": "USDT", "amount": amount_usd}
    if winner.get("username"):
        payload["pin_to_username"] = winner["username"]
    else:
        payload["pin_to_user_id"] = winner["id"]

    result = await crypto_pay_request("createCheck", payload)
    url = crypto_check_url(result)
    if not url:
        raise RuntimeError("Crypto Pay API did not return check URL")
    return {"check_id": result.get("check_id") or result.get("id"), "url": url}


def load_extra_admins() -> set[int]:
    try:
        return {
            int(item.strip())
            for item in EXTRA_ADMIN_IDS_RAW.replace(";", ",").split(",")
            if item.strip() and int(item.strip()) != ADMIN_ID
        }
    except Exception:
        logging.exception("Could not parse EXTRA_ADMIN_IDS")
        return set()


def save_extra_admins() -> None:
    return None


def load_known_users() -> set[int]:
    if not USERS_FILE.exists():
        return set()

    try:
        raw_items = USERS_FILE.read_text(encoding="utf-8").splitlines()
        return {int(item.strip()) for item in raw_items if item.strip()}
    except Exception:
        logging.exception("Could not load known users")
        return set()


def save_known_users() -> None:
    try:
        payload = "\n".join(str(user_id) for user_id in sorted(known_users))
        USERS_FILE.write_text(payload, encoding="utf-8")
    except Exception:
        logging.exception("Could not save known users")


def remember_user(user_id: int) -> None:
    if user_id in known_users:
        return
    known_users.add(user_id)
    save_known_users()


known_users = load_known_users()
extra_admin_ids = load_extra_admins()


def is_owner(user_id: int) -> bool:
    return user_id == ADMIN_ID


def all_admin_ids() -> set[int]:
    return {ADMIN_ID, *extra_admin_ids}


def is_admin(user_id: int) -> bool:
    return user_id in all_admin_ids()


def admin_list_text() -> str:
    lines = ["👑 <b>Список админов</b>", "", f"• Главный админ: <code>{ADMIN_ID}</code>"]
    if extra_admin_ids:
        lines.append("")
        lines.append("Дополнительные админы:")
        lines.extend(f"• <code>{admin_id}</code>" for admin_id in sorted(extra_admin_ids))
    else:
        lines.append("")
        lines.append("Дополнительных админов пока нет.")
    lines.append("")
    lines.append("EXTRA_ADMIN_IDS Р±РµСЂС‘С‚СЃСЏ РёР· РїРµСЂРµРјРµРЅРЅРѕР№ РѕРєСЂСѓР¶РµРЅРёСЏ.")
    return "\n".join(lines)


def user_label(user_data: dict) -> str:
    username = user_data.get("username")
    if username:
        return f"@{escape(username)}"
    return escape(user_data.get("name") or "Без имени")


def signature_line() -> str:
    return f"{escape(BRAND_USERNAME)} • {escape(BRAND_AUTHOR)}"


def branded_title(title: str) -> str:
    return f"{title} {escape(BRAND_AUTHOR)}"


def promo_lines() -> List[str]:
    return [
        f"👉 <b>Участвовать тут</b> {escape(BRAND_USERNAME)}",
    ]


def participants_block(giveaway: Giveaway, empty_text: str) -> List[str]:
    if not giveaway.participants:
        return [empty_text]
    return [f"{index}. {user_label(user)}" for index, user in enumerate(giveaway.participants, start=1)]


def mini_text(giveaway: Giveaway) -> str:
    lines = [
        f"🎉 <b>{branded_title('БЫСТРЫЙ МИНИ-РОЗЫГРЫШ')}</b>",
        "",
        f"🎁 <b>Приз:</b> {escape(giveaway.prize)}",
        f"👥 <b>Участников:</b> {len(giveaway.participants)}/{giveaway.max_players}",
        "",
        *promo_lines(),
        "",
        "📋 <b>Список участников:</b>",
        *participants_block(giveaway, "Пока пусто, можешь быть первым."),
    ]
    return "\n".join(lines)


def mini_money2_text(giveaway: Giveaway) -> str:
    lines = [
        f"🤑 <b>{branded_title('MINI BABKI 2')}</b>",
        "",
        f"💵 <b>Приз:</b> ${escape(str(giveaway.meta.get('prize_amount_usd', giveaway.prize)))}",
        f"👥 <b>Участников:</b> {len(giveaway.participants)}/{giveaway.max_players}",
        "",
        "🎁 <b>Приз выдаётся победителю автоматически.</b>",
        "",
        *promo_lines(),
        "",
        "📋 <b>Список участников:</b>",
        *participants_block(giveaway, "Пока пусто, можешь быть первым."),
    ]
    return "\n".join(lines)


def classic_text(giveaway: Giveaway) -> str:
    lines = [
        f"🎊 <b>{branded_title('НОВЫЙ РОЗЫГРЫШ')}</b>",
        "",
        f"🏆 <b>Приз:</b> {escape(giveaway.prize)}",
        f"🥇 <b>Количество победителей:</b> {giveaway.winners_count}",
        "",
        *promo_lines(),
        "",
        f"👥 <b>Участников:</b> {len(giveaway.participants)}",
        "📋 <b>Список участников:</b>",
        *participants_block(giveaway, "Пока пусто, можешь быть первым."),
    ]
    return "\n".join(lines)


def duel_text(giveaway: Giveaway) -> str:
    lines = [
        f"⚔️ <b>{branded_title('ДУЭЛЬ НА ДВОИХ')}</b>",
        "",
        f"🎁 <b>Приз:</b> {escape(giveaway.prize)}",
        *promo_lines(),
        "",
        f"👤 <b>Игроки:</b> {len(giveaway.participants)}/2",
        *participants_block(giveaway, "Пока никто не вошёл."),
    ]
    return "\n".join(lines)


def darts_text(giveaway: Giveaway) -> str:
    lines = [
        f"🎯 <b>{branded_title('ДАРТС-БИТВА НА ДВОИХ')}</b>",
        "",
        f"🎁 <b>Приз:</b> {escape(giveaway.prize)}",
        *promo_lines(),
        "",
        f"👤 <b>Игроки:</b> {len(giveaway.participants)}/2",
        *participants_block(giveaway, "Пока никто не вошёл."),
    ]
    return "\n".join(lines)


def bowling_text(giveaway: Giveaway) -> str:
    lines = [
        f"🎳 <b>{branded_title('БОУЛИНГ-БИТВА НА ДВОИХ')}</b>",
        "",
        f"🎁 <b>Приз:</b> {escape(giveaway.prize)}",
        *promo_lines(),
        "",
        f"👤 <b>Игроки:</b> {len(giveaway.participants)}/2",
        *participants_block(giveaway, "Пока никто не вошёл."),
    ]
    return "\n".join(lines)


def football_text(giveaway: Giveaway) -> str:
    lines = [
        f"⚽ <b>{branded_title('ФУТБОЛ-БИТВА НА ДВОИХ')}</b>",
        "",
        f"🎁 <b>Приз:</b> {escape(giveaway.prize)}",
        *promo_lines(),
        "",
        f"👤 <b>Игроки:</b> {len(giveaway.participants)}/2",
        *participants_block(giveaway, "Пока никто не вошёл."),
    ]
    return "\n".join(lines)


def result_text(title: str, prize: str, winners: List[dict]) -> str:
    winner_lines = [f"• {user_label(winner)}" for winner in winners] or ["• Участников не было"]
    lines = [
        f"✅ <b>{escape(title)}</b>",
        "",
        f"🎁 <b>Приз:</b> {escape(prize)}",
        "",
        "🏅 <b>Победители:</b>",
        *winner_lines,
        "",
        f"🔖 {signature_line()}",
    ]
    return "\n".join(lines)


def mini_money2_result_text(completed: CompletedGiveaway, winner_score: int) -> str:
    winner = completed.winners[0]
    lines = [
        f"🤑 <b>{branded_title('MINI BABKI 2')}</b>",
        "",
        f"💵 <b>Приз:</b> ${escape(str(completed.meta.get('prize_amount_usd', completed.prize)))}",
        f"🎲 <b>Победный бросок:</b> {winner_score}",
        "",
        f"🏆 <b>Победитель:</b> {user_label(winner)}",
        "🎁 <b>Забрать приз:</b> кнопка ниже доступна победителю.",
        "",
        f"🔖 {signature_line()}",
    ]
    return "\n".join(lines)


def duel_result_text(giveaway: Giveaway, first: dict, second: dict, first_roll: int, second_roll: int, winner: dict, loser: dict) -> str:
    lines = [
        "🔥 <b>ДУЭЛЬ ЗАВЕРШЕНА</b>",
        "",
        f"🎁 <b>Приз:</b> {escape(giveaway.prize)}",
        "",
        f"🎲 {user_label(first)} выбил <b>{first_roll}</b>",
        f"🎲 {user_label(second)} выбил <b>{second_roll}</b>",
        "",
        f"🏆 <b>Победитель:</b> {user_label(winner)}",
        f"💔 <b>Не повезло:</b> {user_label(loser)}",
        "",
        f"🔖 {signature_line()}",
    ]
    return "\n".join(lines)


def darts_result_text(giveaway: Giveaway, first: dict, second: dict, first_score: int, second_score: int, winner: dict, loser: dict, title: str = "ДАРТС ЗАВЕРШЁН") -> str:
    lines = [
        f"🎯 <b>{title}</b>",
        "",
        f"🎁 <b>Приз:</b> {escape(giveaway.prize)}",
        "",
        f"🏹 {user_label(first)} попал на <b>{first_score}</b>",
        f"🏹 {user_label(second)} попал на <b>{second_score}</b>",
        "",
        f"🏆 <b>Победитель:</b> {user_label(winner)}",
        f"💨 <b>Чуть не хватило:</b> {user_label(loser)}",
        "",
        f"🔖 {signature_line()}",
    ]
    return "\n".join(lines)


def bowling_result_text(giveaway: Giveaway, first: dict, second: dict, first_score: int, second_score: int, winner: dict, loser: dict, title: str = "БОУЛИНГ ЗАВЕРШЁН") -> str:
    lines = [
        f"🎳 <b>{title}</b>",
        "",
        f"🎁 <b>Приз:</b> {escape(giveaway.prize)}",
        "",
        f"🎳 {user_label(first)} выбил <b>{first_score}</b>",
        f"🎳 {user_label(second)} выбил <b>{second_score}</b>",
        "",
        f"🏆 <b>Победитель:</b> {user_label(winner)}",
        f"💨 <b>Не хватило чуть-чуть:</b> {user_label(loser)}",
        "",
        f"🔖 {signature_line()}",
    ]
    return "\n".join(lines)


def football_result_text(giveaway: Giveaway, first: dict, second: dict, first_score: int, second_score: int, winner: dict, loser: dict, title: str = "ФУТБОЛ ЗАВЕРШЁН") -> str:
    lines = [
        f"⚽ <b>{title}</b>",
        "",
        f"🎁 <b>Приз:</b> {escape(giveaway.prize)}",
        "",
        f"🥅 {user_label(first)} выбил <b>{first_score}</b>",
        f"🥅 {user_label(second)} выбил <b>{second_score}</b>",
        "",
        f"🏆 <b>Победитель:</b> {user_label(winner)}",
        f"💨 <b>Не хватило чуть-чуть:</b> {user_label(loser)}",
        "",
        f"🔖 {signature_line()}",
    ]
    return "\n".join(lines)


def public_keyboard(kind: str, active: bool = True) -> InlineKeyboardMarkup:
    labels = {
        "mini_money2": "🤑 Участвовать",
        "mini": "🎉 Участвовать",
        "classic": "🎟 Войти в розыгрыш",
        "duel": "⚔️ Войти в дуэль",
        "darts": "🎯 Войти в дартс",
        "bowling": "🎳 Войти в боулинг",
        "football": "⚽ Войти в футбол",
    }
    closed_labels = {
        "mini_money2": "🔒 Розыгрыш завершён",
        "mini": "🔒 Набор закрыт",
        "classic": "🔒 Розыгрыш завершён",
        "duel": "🔒 Дуэль завершена",
        "darts": "🔒 Дартс завершён",
        "bowling": "🔒 Боулинг завершён",
        "football": "🔒 Футбол завершён",
    }
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=labels[kind], callback_data=f"join:{kind}")]
            if active
            else [InlineKeyboardButton(text=closed_labels[kind], callback_data="closed")]
        ]
    )


def mini_money2_claim_keyboard(check_url: Optional[str] = None) -> InlineKeyboardMarkup:
    button = (
        InlineKeyboardButton(text="🎁 Забрать приз", url=check_url)
        if check_url
        else InlineKeyboardButton(text="🎁 Забрать приз", callback_data="claim:mini_money2")
    )
    return InlineKeyboardMarkup(
        inline_keyboard=[[button]]
    )


def start_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = []
    if is_admin(user_id):
        rows.append([InlineKeyboardButton(text="🛠 Открыть админку", callback_data="open_admin")])
    rows.append([InlineKeyboardButton(text="📢 Открыть канал", url=f"https://t.me/{BRAND_USERNAME.lstrip('@')}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admin_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="🤑 Создать Mini Babki 2", callback_data="create:mini_money2")],
        [InlineKeyboardButton(text="🎉 Создать мини", callback_data="create:mini")],
        [InlineKeyboardButton(text="🎊 Создать розыгрыш", callback_data="create:classic")],
        [InlineKeyboardButton(text="⚔️ Создать дуэль", callback_data="create:duel")],
        [InlineKeyboardButton(text="🎯 Создать дартс", callback_data="create:darts")],
        [InlineKeyboardButton(text="🎳 Создать боулинг", callback_data="create:bowling")],
        [InlineKeyboardButton(text="⚽ Создать футбол", callback_data="create:football")],
        [InlineKeyboardButton(text="📣 Рассылка", callback_data="broadcast:start")],
        [InlineKeyboardButton(text="🗂 Активные посты", callback_data="manage")],
        [InlineKeyboardButton(text="📊 Статус", callback_data="status")],
    ]
    rows.append([InlineKeyboardButton(text="👑 Админы", callback_data="admins:menu")])
    rows.append([InlineKeyboardButton(text="🧹 Сбросить ввод", callback_data="reset")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def admins_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📋 Список админов", callback_data="admins:list")],
        [InlineKeyboardButton(text="➕ Выдать админку", callback_data="admins:add")],
    ]
    if extra_admin_ids:
        rows.append([InlineKeyboardButton(text="➖ Удалить админа", callback_data="admins:remove_menu")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def remove_admin_keyboard() -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for admin_id in sorted(extra_admin_ids):
        rows.append([InlineKeyboardButton(text=f"➖ Удалить {admin_id}", callback_data=f"admins:remove:{admin_id}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admins:menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def manage_keyboard() -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for kind in ("mini_money2", "mini", "classic", "duel", "darts", "bowling", "football"):
        if kind in active_giveaways:
            rows.append([InlineKeyboardButton(text=f"👥 Участники: {KIND_TITLES[kind]}", callback_data=f"admin:members:{kind}")])
            rows.append([InlineKeyboardButton(text=f"🏁 Завершить: {KIND_TITLES[kind]}", callback_data=f"admin:finish:{kind}")])
            rows.append([InlineKeyboardButton(text=f"🗑 Удалить: {KIND_TITLES[kind]}", callback_data=f"admin:delete:{kind}")])
        if kind in completed_giveaways:
            rows.append([InlineKeyboardButton(text=f"🎲 Рерол: {KIND_TITLES[kind]}", callback_data=f"admin:reroll:{kind}")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def current_text(giveaway: Giveaway) -> str:
    if giveaway.kind == "mini_money2":
        return mini_money2_text(giveaway)
    if giveaway.kind == "mini":
        return mini_text(giveaway)
    if giveaway.kind == "classic":
        return classic_text(giveaway)
    if giveaway.kind == "darts":
        return darts_text(giveaway)
    if giveaway.kind == "bowling":
        return bowling_text(giveaway)
    if giveaway.kind == "football":
        return football_text(giveaway)
    return duel_text(giveaway)


async def publish_giveaway(giveaway: Giveaway) -> None:
    message = await bot.send_message(
        chat_id=CHANNEL_ID,
        text=current_text(giveaway),
        reply_markup=public_keyboard(giveaway.kind, active=True),
        disable_web_page_preview=True,
    )
    giveaway.message_id = message.message_id
    active_giveaways[giveaway.kind] = giveaway


async def refresh_giveaway(giveaway: Giveaway, active: bool = True) -> None:
    if giveaway.message_id is None:
        return

    await bot.edit_message_text(
        chat_id=CHANNEL_ID,
        message_id=giveaway.message_id,
        text=current_text(giveaway),
        reply_markup=public_keyboard(giveaway.kind, active=active),
        disable_web_page_preview=True,
    )


async def delete_giveaway(kind: str) -> str:
    giveaway = active_giveaways.get(kind)
    if not giveaway:
        return "Активного поста такого типа нет."

    if giveaway.message_id is not None:
        try:
            await bot.delete_message(chat_id=CHANNEL_ID, message_id=giveaway.message_id)
        except Exception:
            await bot.edit_message_text(
                chat_id=CHANNEL_ID,
                message_id=giveaway.message_id,
                text=f"🗑 <b>{KIND_TITLES[kind]} удалён администратором</b>\n\n🔖 {signature_line()}",
                reply_markup=public_keyboard(kind, active=False),
                disable_web_page_preview=True,
            )

    active_giveaways.pop(kind, None)
    return f"{KIND_TITLES[kind]} удалён."


def schedule_message_cleanup(chat_id: int | str, message_ids: List[int], delay: int = ROLL_DELETE_DELAY_SECONDS) -> None:
    cleanup_ids = list(dict.fromkeys(message_ids))
    if not cleanup_ids:
        return

    async def cleanup() -> None:
        await asyncio.sleep(delay)
        for message_id in cleanup_ids:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
            except Exception:
                logging.debug("Could not delete temporary game message %s", message_id)

    asyncio.create_task(cleanup())


async def roll_contest(participants: List[dict], emoji: str, start_text: str) -> tuple[dict, int, List[int]]:
    await bot.send_message(CHANNEL_ID, start_text)
    active_players = list(participants)
    dice_message_ids: List[int] = []

    while True:
        round_scores: List[tuple[dict, int]] = []
        for player in active_players:
            dice_message = await bot.send_dice(chat_id=CHANNEL_ID, emoji=emoji)
            dice_message_ids.append(dice_message.message_id)
            round_scores.append((player, dice_message.dice.value))

        best_score = max(score for _, score in round_scores)
        leaders = [player for player, score in round_scores if score == best_score]

        if len(leaders) == 1:
            return leaders[0], best_score, dice_message_ids

        names = ", ".join(user_label(player) for player in leaders)
        await bot.send_message(CHANNEL_ID, f"{emoji} Ничья между {names}. Перекидываем ещё раз...")
        active_players = leaders


async def finish_mini(giveaway: Giveaway) -> str:
    giveaway.finished = True
    winner, winner_score, dice_message_ids = await roll_contest(
        giveaway.participants,
        "🎲",
        "🎲 Определяем победителя мини-розыгрыша реальными кубиками...",
    )
    await bot.edit_message_text(
        chat_id=CHANNEL_ID,
        message_id=giveaway.message_id,
        text="\n".join(
            [
                "✅ <b>Мини-розыгрыш завершён</b>",
                "",
                f"🎁 <b>Приз:</b> {escape(giveaway.prize)}",
                f"🎲 <b>Победный бросок:</b> {winner_score}",
                "",
                f"🏆 <b>Победитель:</b> {user_label(winner)}",
                "",
                f"🔖 {signature_line()}",
            ]
        ),
        reply_markup=public_keyboard("mini", active=False),
        disable_web_page_preview=True,
    )
    completed_giveaways["mini"] = CompletedGiveaway(
        kind="mini",
        prize=giveaway.prize,
        participants=list(giveaway.participants),
        winners=[winner],
        winners_count=1,
        message_id=giveaway.message_id,
    )
    active_giveaways.pop("mini", None)
    schedule_message_cleanup(CHANNEL_ID, dice_message_ids)
    return f"Победитель мини: {user_label(winner)}"


async def send_mini_money2_check_to_winner(completed: CompletedGiveaway) -> None:
    winner = completed.winners[0]
    check_url = completed.meta.get("claim_check_url")
    if not check_url:
        return

    bound_line = (
        f"Чек привязан к @{escape(winner['username'])}."
        if winner.get("username")
        else "Чек привязан к твоему Telegram-профилю."
    )

    await bot.send_message(
        winner["id"],
        "\n".join(
            [
                "🎉 <b>Ты выиграл Mini Babki 2</b>",
                "",
                f"💵 <b>Сумма:</b> ${escape(str(completed.meta.get('prize_amount_usd', completed.prize)))}",
                bound_line,
                "Нажми кнопку ниже, чтобы открыть чек CryptoBot.",
            ]
        ),
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🎁 Открыть чек", url=check_url)]]
        ),
        disable_web_page_preview=True,
    )


async def ensure_mini_money2_check(completed: CompletedGiveaway) -> str:
    if completed.meta.get("claim_check_url"):
        return str(completed.meta["claim_check_url"])

    check = await create_crypto_check(str(completed.meta["prize_amount_usd"]), completed.winners[0])
    completed.meta["claim_check_url"] = check["url"]
    completed.meta["claim_check_id"] = check["check_id"]
    return check["url"]


async def finish_mini_money2(giveaway: Giveaway) -> str:
    giveaway.finished = True
    winner, winner_score, dice_message_ids = await roll_contest(
        giveaway.participants,
        "🎲",
        "🎲 Определяем победителя Mini Babki 2 реальными кубиками...",
    )

    completed = CompletedGiveaway(
        kind="mini_money2",
        prize=giveaway.prize,
        participants=list(giveaway.participants),
        winners=[winner],
        winners_count=1,
        message_id=giveaway.message_id,
        meta=dict(giveaway.meta),
    )

    await bot.edit_message_text(
        chat_id=CHANNEL_ID,
        message_id=giveaway.message_id,
        text=mini_money2_result_text(completed, winner_score),
        reply_markup=mini_money2_claim_keyboard(),
        disable_web_page_preview=True,
    )
    completed_giveaways["mini_money2"] = completed
    active_giveaways.pop("mini_money2", None)
    schedule_message_cleanup(CHANNEL_ID, dice_message_ids)

    try:
        check_url = await ensure_mini_money2_check(completed)
        await bot.edit_message_reply_markup(
            chat_id=CHANNEL_ID,
            message_id=giveaway.message_id,
            reply_markup=mini_money2_claim_keyboard(check_url),
        )
    except Exception as exc:
        logging.exception("Could not create Mini Babki 2 check")
        await notify_admins(f"Mini Babki 2: не удалось выдать чек автоматически: {escape(str(exc))}")
    else:
        try:
            await send_mini_money2_check_to_winner(completed)
        except Exception as exc:
            logging.exception("Could not deliver Mini Babki 2 check to winner")
            await notify_admins(
                f"Mini Babki 2: чек создан, но не отправился в личку победителю: {escape(str(exc))}"
            )

    return f"Победитель Mini Babki 2: {user_label(winner)}"


async def finish_classic(giveaway: Giveaway) -> str:
    giveaway.finished = True
    winners_count = min(giveaway.winners_count, len(giveaway.participants))
    winners = random.sample(giveaway.participants, winners_count)
    await bot.edit_message_text(
        chat_id=CHANNEL_ID,
        message_id=giveaway.message_id,
        text=result_text("Розыгрыш завершён", giveaway.prize, winners),
        reply_markup=public_keyboard("classic", active=False),
        disable_web_page_preview=True,
    )
    completed_giveaways["classic"] = CompletedGiveaway(
        kind="classic",
        prize=giveaway.prize,
        participants=list(giveaway.participants),
        winners=list(winners),
        winners_count=winners_count,
        message_id=giveaway.message_id,
    )
    active_giveaways.pop("classic", None)
    return "Розыгрыш завершён."


async def finish_duel(giveaway: Giveaway) -> str:
    first, second = giveaway.participants
    await bot.send_message(CHANNEL_ID, "🎲 Дуэль начинается, кидаем реальные кубики...")
    first_roll_message = await bot.send_dice(chat_id=CHANNEL_ID, emoji="🎲")
    second_roll_message = await bot.send_dice(chat_id=CHANNEL_ID, emoji="🎲")
    first_roll = first_roll_message.dice.value
    second_roll = second_roll_message.dice.value

    while first_roll == second_roll:
        await bot.send_message(CHANNEL_ID, "🎲 Ничья на кубиках, кидаем ещё раз...")
        first_roll_message = await bot.send_dice(chat_id=CHANNEL_ID, emoji="🎲")
        second_roll_message = await bot.send_dice(chat_id=CHANNEL_ID, emoji="🎲")
        first_roll = first_roll_message.dice.value
        second_roll = second_roll_message.dice.value

    winner, loser = (first, second) if first_roll > second_roll else (second, first)
    await bot.edit_message_text(
        chat_id=CHANNEL_ID,
        message_id=giveaway.message_id,
        text=duel_result_text(giveaway, first, second, first_roll, second_roll, winner, loser),
        reply_markup=public_keyboard("duel", active=False),
        disable_web_page_preview=True,
    )
    completed_giveaways["duel"] = CompletedGiveaway(
        kind="duel",
        prize=giveaway.prize,
        participants=list(giveaway.participants),
        winners=[winner],
        winners_count=1,
        message_id=giveaway.message_id,
    )
    active_giveaways.pop("duel", None)
    return f"Победитель дуэли: {user_label(winner)}"


async def finish_darts(giveaway: Giveaway, reroll: bool = False) -> str:
    first, second = giveaway.participants
    first_dart = await bot.send_dice(chat_id=CHANNEL_ID, emoji="🎯")
    second_dart = await bot.send_dice(chat_id=CHANNEL_ID, emoji="🎯")

    first_score = first_dart.dice.value
    second_score = second_dart.dice.value
    while first_score == second_score:
        tie_break = await bot.send_message(CHANNEL_ID, "🎯 Ничья в дартсе, кидаем ещё раз...")
        first_dart = await bot.send_dice(chat_id=CHANNEL_ID, emoji="🎯")
        second_dart = await bot.send_dice(chat_id=CHANNEL_ID, emoji="🎯")
        first_score = first_dart.dice.value
        second_score = second_dart.dice.value
        await asyncio.sleep(0.5)

    winner, loser = (first, second) if first_score > second_score else (second, first)
    title = "РЕРОЛ ДАРТСА" if reroll else "ДАРТС ЗАВЕРШЁН"
    await bot.edit_message_text(
        chat_id=CHANNEL_ID,
        message_id=giveaway.message_id,
        text=darts_result_text(giveaway, first, second, first_score, second_score, winner, loser, title=title),
        reply_markup=public_keyboard("darts", active=False),
        disable_web_page_preview=True,
    )
    completed_giveaways["darts"] = CompletedGiveaway(
        kind="darts",
        prize=giveaway.prize,
        participants=list(giveaway.participants),
        winners=[winner],
        winners_count=1,
        message_id=giveaway.message_id,
    )
    active_giveaways.pop("darts", None)
    return f"Победитель дартса: {user_label(winner)}"


async def finish_bowling(giveaway: Giveaway, reroll: bool = False) -> str:
    first, second = giveaway.participants
    first_ball = await bot.send_dice(chat_id=CHANNEL_ID, emoji="🎳")
    second_ball = await bot.send_dice(chat_id=CHANNEL_ID, emoji="🎳")

    first_score = first_ball.dice.value
    second_score = second_ball.dice.value
    while first_score == second_score:
        await bot.send_message(CHANNEL_ID, "🎳 Ничья в боулинге, кидаем ещё раз...")
        first_ball = await bot.send_dice(chat_id=CHANNEL_ID, emoji="🎳")
        second_ball = await bot.send_dice(chat_id=CHANNEL_ID, emoji="🎳")
        first_score = first_ball.dice.value
        second_score = second_ball.dice.value
        await asyncio.sleep(0.5)

    winner, loser = (first, second) if first_score > second_score else (second, first)
    title = "РЕРОЛ БОУЛИНГА" if reroll else "БОУЛИНГ ЗАВЕРШЁН"
    await bot.edit_message_text(
        chat_id=CHANNEL_ID,
        message_id=giveaway.message_id,
        text=bowling_result_text(giveaway, first, second, first_score, second_score, winner, loser, title=title),
        reply_markup=public_keyboard("bowling", active=False),
        disable_web_page_preview=True,
    )
    completed_giveaways["bowling"] = CompletedGiveaway(
        kind="bowling",
        prize=giveaway.prize,
        participants=list(giveaway.participants),
        winners=[winner],
        winners_count=1,
        message_id=giveaway.message_id,
    )
    active_giveaways.pop("bowling", None)
    return f"Победитель боулинга: {user_label(winner)}"


async def finish_football(giveaway: Giveaway, reroll: bool = False) -> str:
    first, second = giveaway.participants
    first_ball = await bot.send_dice(chat_id=CHANNEL_ID, emoji="⚽")
    second_ball = await bot.send_dice(chat_id=CHANNEL_ID, emoji="⚽")

    first_score = first_ball.dice.value
    second_score = second_ball.dice.value
    while first_score == second_score:
        await bot.send_message(CHANNEL_ID, "⚽ Ничья в футболе, бьём ещё раз...")
        first_ball = await bot.send_dice(chat_id=CHANNEL_ID, emoji="⚽")
        second_ball = await bot.send_dice(chat_id=CHANNEL_ID, emoji="⚽")
        first_score = first_ball.dice.value
        second_score = second_ball.dice.value
        await asyncio.sleep(0.5)

    winner, loser = (first, second) if first_score > second_score else (second, first)
    title = "РЕРОЛ ФУТБОЛА" if reroll else "ФУТБОЛ ЗАВЕРШЁН"
    await bot.edit_message_text(
        chat_id=CHANNEL_ID,
        message_id=giveaway.message_id,
        text=football_result_text(giveaway, first, second, first_score, second_score, winner, loser, title=title),
        reply_markup=public_keyboard("football", active=False),
        disable_web_page_preview=True,
    )
    completed_giveaways["football"] = CompletedGiveaway(
        kind="football",
        prize=giveaway.prize,
        participants=list(giveaway.participants),
        winners=[winner],
        winners_count=1,
        message_id=giveaway.message_id,
    )
    active_giveaways.pop("football", None)
    return f"Победитель футбола: {user_label(winner)}"


def participants_text(kind: str) -> str:
    giveaway = active_giveaways.get(kind)
    if not giveaway:
        return "Активного розыгрыша такого типа сейчас нет."

    lines = [f"👥 <b>Участники: {KIND_TITLES[kind]}</b>", ""]
    if giveaway.participants:
        lines.extend(f"{index}. {user_label(user)}" for index, user in enumerate(giveaway.participants, start=1))
    else:
        lines.append("Пока участников нет.")
    return "\n".join(lines)


async def reroll_giveaway(kind: str) -> str:
    completed = completed_giveaways.get(kind)
    if not completed:
        return "Для этого типа ещё нет завершённого розыгрыша для рерола."

    if not completed.participants:
        return "Нет участников для рерола."

    winners_count = min(completed.winners_count, len(completed.participants))
    new_winners = random.sample(completed.participants, winners_count)
    completed.winners = list(new_winners)

    if completed.message_id is not None:
        if kind == "duel":
            giveaway = Giveaway(kind="duel", prize=completed.prize, max_players=2, message_id=completed.message_id, participants=list(completed.participants))
            return await finish_duel(giveaway)
        elif kind == "darts":
            giveaway = Giveaway(kind="darts", prize=completed.prize, max_players=2, message_id=completed.message_id, participants=list(completed.participants))
            return await finish_darts(giveaway, reroll=True)
        elif kind == "bowling":
            giveaway = Giveaway(kind="bowling", prize=completed.prize, max_players=2, message_id=completed.message_id, participants=list(completed.participants))
            return await finish_bowling(giveaway, reroll=True)
        elif kind == "football":
            giveaway = Giveaway(kind="football", prize=completed.prize, max_players=2, message_id=completed.message_id, participants=list(completed.participants))
            return await finish_football(giveaway, reroll=True)
        elif kind == "mini":
            giveaway = Giveaway(kind="mini", prize=completed.prize, max_players=len(completed.participants), message_id=completed.message_id, participants=list(completed.participants))
            return await finish_mini(giveaway)
        elif kind == "mini_money2":
            giveaway = Giveaway(
                kind="mini_money2",
                prize=completed.prize,
                max_players=len(completed.participants),
                message_id=completed.message_id,
                participants=list(completed.participants),
                meta=dict(completed.meta),
            )
            return await finish_mini_money2(giveaway)
        else:
            title = "Рерол розыгрыша"
            text = result_text(title, completed.prize, new_winners)

        await bot.edit_message_text(
            chat_id=CHANNEL_ID,
            message_id=completed.message_id,
            text=text,
            reply_markup=public_keyboard(kind, active=False),
            disable_web_page_preview=True,
        )

    winners_line = ", ".join(user_label(user) for user in new_winners)
    return f"Рерол выполнен. Новый результат: {winners_line}"


async def finish_giveaway_by_kind(kind: str) -> str:
    giveaway = active_giveaways.get(kind)
    if not giveaway:
        return "Активного поста такого типа нет."

    if giveaway.finished:
        return "Итоги уже формируются, подожди пару секунд."

    if not giveaway.participants:
        return "Нельзя завершить без участников."

    if kind == "mini":
        return await finish_mini(giveaway)
    if kind == "mini_money2":
        return await finish_mini_money2(giveaway)
    if kind == "classic":
        return await finish_classic(giveaway)
    if kind == "darts":
        if len(giveaway.participants) < 2:
            return "Для дартса нужно 2 игрока."
        return await finish_darts(giveaway)
    if kind == "bowling":
        if len(giveaway.participants) < 2:
            return "Для боулинга нужно 2 игрока."
        return await finish_bowling(giveaway)
    if kind == "football":
        if len(giveaway.participants) < 2:
            return "Для футбола нужно 2 игрока."
        return await finish_football(giveaway)
    if len(giveaway.participants) < 2:
        return "Для дуэли нужно 2 игрока."
    return await finish_duel(giveaway)


def status_text() -> str:
    lines = ["📊 <b>Текущий статус бота</b>", ""]
    for kind in ("mini_money2", "mini", "classic", "duel", "darts", "bowling", "football"):
        giveaway = active_giveaways.get(kind)
        if giveaway:
            lines.append(f"• <b>{KIND_TITLES[kind]}</b>: активен, участников {len(giveaway.participants)}")
        else:
            lines.append(f"• <b>{KIND_TITLES[kind]}</b>: не создан")
    lines.extend(
        [
            f"• <b>Пользователей для рассылки</b>: {len(known_users)}",
            f"• <b>Всего админов</b>: {len(all_admin_ids())}",
            "",
            "Управление:",
            "• вход в админку кнопкой из /start",
            "• всё остальное делается кнопками",
            "• для активных розыгрышей есть участники, завершение и удаление",
            "• для завершённых есть рерол",
        ]
    )
    return "\n".join(lines)


def active_giveaways_text() -> str:
    lines = ["🗂 <b>Активные розыгрыши</b>", ""]

    found = False
    for kind in ("mini_money2", "mini", "classic", "duel", "darts", "bowling", "football"):
        giveaway = active_giveaways.get(kind)
        if not giveaway:
            continue

        found = True
        lines.extend(
            [
                f"🎯 <b>{KIND_TITLES[kind]}</b>",
                f"🎁 Приз: {escape(giveaway.prize)}",
                f"👥 Участников: {len(giveaway.participants)}",
                "📌 Доступно в админке: участники, завершение, удаление",
                "",
            ]
        )

    if not found:
        lines.append("Сейчас активных розыгрышей нет.")

    if completed_giveaways:
        lines.extend(
            [
                "🎲 <b>Для завершённых доступен рерол:</b>",
                ", ".join(KIND_TITLES[kind] for kind in completed_giveaways),
            ]
        )

    return "\n".join(lines)


@dp.message(Command("start"))
async def start_handler(message: Message) -> None:
    remember_user(message.from_user.id)
    if not is_admin(message.from_user.id):
        await message.answer("Бот активен. Участвуй через кнопки под постами в канале.", reply_markup=start_keyboard(message.from_user.id))
        return
    await message.answer("Нажми кнопку ниже, чтобы открыть админку.", reply_markup=start_keyboard(message.from_user.id))


@dp.callback_query(F.data == "open_admin")
async def open_admin_handler(call: CallbackQuery) -> None:
    remember_user(call.from_user.id)
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return
    await call.message.answer("Панель управления открыта.", reply_markup=admin_keyboard())
    await call.answer()


@dp.callback_query(F.data == "closed")
async def closed_handler(call: CallbackQuery) -> None:
    await call.answer("Набор уже закрыт", show_alert=True)


@dp.callback_query(F.data.in_({"status", "reset", "manage", "back"}))
async def simple_admin_actions(call: CallbackQuery) -> None:
    remember_user(call.from_user.id)
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return

    if call.data == "reset":
        admin_state.pop(call.from_user.id, None)
        await call.message.answer("Черновик сброшен.", reply_markup=admin_keyboard())
    elif call.data == "status":
        await call.message.answer(status_text(), reply_markup=admin_keyboard())
    elif call.data == "manage":
        await call.message.answer(active_giveaways_text(), reply_markup=manage_keyboard())
    else:
        await call.message.answer("Возвращаю панель.", reply_markup=admin_keyboard())

    await call.answer()


@dp.callback_query(F.data == "admins:menu")
async def admins_menu(call: CallbackQuery) -> None:
    remember_user(call.from_user.id)
    if not is_owner(call.from_user.id):
        await call.answer("Только главный админ может управлять админами", show_alert=True)
        return

    await call.message.answer("Управление админами.", reply_markup=admins_keyboard())
    await call.answer()


@dp.callback_query(F.data == "admins:list")
async def admins_list_handler(call: CallbackQuery) -> None:
    remember_user(call.from_user.id)
    if not is_owner(call.from_user.id):
        await call.answer("Только главный админ может смотреть этот список", show_alert=True)
        return

    await call.message.answer(admin_list_text(), reply_markup=admins_keyboard())
    await call.answer()


@dp.callback_query(F.data == "admins:add")
async def admins_add_handler(call: CallbackQuery) -> None:
    remember_user(call.from_user.id)
    if not is_owner(call.from_user.id):
        await call.answer("Только главный админ может выдавать права", show_alert=True)
        return

    admin_state[call.from_user.id] = {"kind": "admin_add", "step": "id"}
    await call.message.answer("Пришли Telegram ID пользователя, которому нужно выдать админку.")
    await call.answer()


@dp.callback_query(F.data == "admins:remove_menu")
async def admins_remove_menu_handler(call: CallbackQuery) -> None:
    remember_user(call.from_user.id)
    if not is_owner(call.from_user.id):
        await call.answer("Только главный админ может удалять админов", show_alert=True)
        return

    if not extra_admin_ids:
        await call.message.answer("Дополнительных админов сейчас нет.", reply_markup=admins_keyboard())
        await call.answer()
        return

    await call.message.answer("Выбери админа для удаления.", reply_markup=remove_admin_keyboard())
    await call.answer()


@dp.callback_query(F.data.startswith("admins:remove:"))
async def admins_remove_handler(call: CallbackQuery) -> None:
    remember_user(call.from_user.id)
    if not is_owner(call.from_user.id):
        await call.answer("Только главный админ может удалять админов", show_alert=True)
        return

    admin_id = int(call.data.split(":")[-1])
    if admin_id not in extra_admin_ids:
        await call.answer("Такого дополнительного админа уже нет", show_alert=True)
        return

    extra_admin_ids.discard(admin_id)
    save_extra_admins()
    await call.message.answer(f"Админ <code>{admin_id}</code> удалён.", reply_markup=admins_keyboard())
    await call.answer("Удалено")


@dp.callback_query(F.data == "broadcast:start")
async def broadcast_start(call: CallbackQuery) -> None:
    remember_user(call.from_user.id)
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return

    admin_state[call.from_user.id] = {"kind": "broadcast", "step": "text"}
    await call.message.answer(
        "Пришли текст для рассылки.\n\nЕго получат все пользователи, которые уже взаимодействовали с ботом."
    )
    await call.answer()


@dp.callback_query(F.data.startswith("admin:"))
async def manage_actions(call: CallbackQuery) -> None:
    remember_user(call.from_user.id)
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return

    _, action, kind = call.data.split(":")
    if action == "members":
        text = participants_text(kind)
    elif action == "finish":
        text = await finish_giveaway_by_kind(kind)
    elif action == "reroll":
        text = await reroll_giveaway(kind)
    else:
        text = await delete_giveaway(kind)

    await call.message.answer(text, reply_markup=admin_keyboard())
    await call.answer("Готово")


@dp.callback_query(F.data.startswith("create:"))
async def create_handler(call: CallbackQuery) -> None:
    remember_user(call.from_user.id)
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return

    kind = call.data.split(":", 1)[1]
    admin_state[call.from_user.id] = {"kind": kind, "step": "prize"}
    prompts = {
        "mini_money2": "Пришли сумму приза в долларах для Mini Babki 2. Например: 25",
        "mini": "Пришли приз для мини-розыгрыша.",
        "classic": "Пришли приз для обычного розыгрыша.",
        "duel": "Пришли приз для дуэли.",
        "darts": "Пришли приз для дартс-дуэли.",
        "bowling": "Пришли приз для боулинг-дуэли.",
        "football": "Пришли приз для футбол-дуэли.",
    }
    await call.message.answer(prompts[kind])
    await call.answer()


@dp.message(F.text)
async def admin_flow(message: Message) -> None:
    remember_user(message.from_user.id)
    if not is_admin(message.from_user.id):
        return

    state = admin_state.get(message.from_user.id)
    if not state or not message.text:
        return

    kind = state["kind"]
    step = state["step"]
    text = message.text.strip()

    if kind == "admin_add" and step == "id":
        if not is_owner(message.from_user.id):
            admin_state.pop(message.from_user.id, None)
            return

        if not text.isdigit():
            await message.answer("Пришли именно числовой Telegram ID.")
            return

        new_admin_id = int(text)
        if new_admin_id == ADMIN_ID or new_admin_id in extra_admin_ids:
            await message.answer("Этот пользователь уже есть в списке админов.", reply_markup=admins_keyboard())
            admin_state.pop(message.from_user.id, None)
            return

        extra_admin_ids.add(new_admin_id)
        save_extra_admins()
        admin_state.pop(message.from_user.id, None)
        await message.answer(
            f"Админка выдана пользователю <code>{new_admin_id}</code>.",
            reply_markup=admins_keyboard(),
        )
        return

    if kind == "broadcast" and step == "text":
        if not text:
            await message.answer("Текст рассылки не должен быть пустым.")
            return

        sent = 0
        failed = 0
        for user_id in sorted(known_users):
            try:
                await bot.send_message(user_id, text, disable_web_page_preview=True)
                sent += 1
            except Exception:
                failed += 1

        admin_state.pop(message.from_user.id, None)
        await message.answer(
            f"Рассылка завершена.\n\nУспешно: {sent}\nНе доставлено: {failed}",
            reply_markup=admin_keyboard(),
        )
        return

    if step == "prize":
        if not text:
            await message.answer("Приз не должен быть пустым.")
            return

        if kind == "mini_money2":
            try:
                prize_amount = format_usd(text)
            except InvalidOperation:
                await message.answer("Пришли корректную сумму в USD. Например: 10 или 10.50")
                return

            try:
                invoice = await create_crypto_invoice(prize_amount, f"Mini Babki 2 prize fund ${prize_amount}")
            except Exception as exc:
                logging.exception("Could not create Mini Babki 2 invoice")
                await message.answer(f"Не удалось создать счёт CryptoBot: {escape(str(exc))}")
                return

            await create_and_publish(
                message,
                kind,
                f"${prize_amount}",
                1,
                meta={
                    "prize_amount_usd": prize_amount,
                    "crypto_invoice_url": invoice["url"],
                    "crypto_invoice_id": invoice["invoice_id"],
                },
            )
            return

        state["prize"] = text
        if kind == "classic":
            state["step"] = "winners"
            await message.answer("Сколько победителей нужно выбрать?")
            return

        await create_and_publish(message, kind, text, 1)
        return

    if step == "winners":
        if not text.isdigit() or int(text) < 1:
            await message.answer("Пришли число от 1 и выше.")
            return

        await create_and_publish(message, kind, state["prize"], int(text))


async def create_and_publish(message: Message, kind: str, prize: str, winners_count: int, meta: Optional[Dict[str, Any]] = None) -> None:
    if kind in active_giveaways:
        await message.answer(f"Сначала заверши текущий пост типа: {KIND_TITLES[kind]}.")
        return

    giveaway = Giveaway(
        kind=kind,
        prize=prize,
        winners_count=winners_count,
        max_players=MAX_MINI_PLAYERS if kind in {"mini", "mini_money2"} else 2 if kind in {"duel", "darts", "bowling", "football"} else None,
        meta=meta or {},
    )
    await publish_giveaway(giveaway)
    admin_state.pop(message.from_user.id, None)
    await message.answer("Пост опубликован в канал.", reply_markup=admin_keyboard())
    if kind == "mini_money2":
        await message.answer(
            "\n".join(
                [
                    "Mini Babki 2 опубликован в канале.",
                    f"Сумма приза: ${escape(str(giveaway.meta['prize_amount_usd']))}",
                    "Счёт на оплату:",
                    giveaway.meta["crypto_invoice_url"],
                ]
            ),
            reply_markup=admin_keyboard(),
            disable_web_page_preview=True,
        )
        return


@dp.callback_query(F.data.startswith("join:"))
async def join_handler(call: CallbackQuery) -> None:
    remember_user(call.from_user.id)
    kind = call.data.split(":", 1)[1]
    giveaway = active_giveaways.get(kind)

    if not giveaway or giveaway.finished:
        await call.answer("Этот набор уже закрыт", show_alert=True)
        return

    if any(user["id"] == call.from_user.id for user in giveaway.participants):
        await call.answer("Ты уже участвуешь")
        return

    if kind in {"mini", "mini_money2"}:
        now = asyncio.get_running_loop().time()
        last_join_time = mini_join_cooldowns.get(call.from_user.id, 0.0)
        remaining = MINI_JOIN_COOLDOWN_SECONDS - (now - last_join_time)
        if remaining > 0:
            await call.answer(f"Подожди {int(remaining) + 1} сек. перед новым нажатием", show_alert=True)
            return

    should_finish = False
    answer_text = ""
    giveaway_to_finish: Optional[Giveaway] = None

    async with giveaway_join_locks[kind]:
        giveaway = active_giveaways.get(kind)
        if not giveaway or giveaway.finished:
            await call.answer("Этот набор уже закрыт", show_alert=True)
            return

        if any(user["id"] == call.from_user.id for user in giveaway.participants):
            await call.answer("Ты уже участвуешь")
            return

        if giveaway.max_players and len(giveaway.participants) >= giveaway.max_players:
            await call.answer("Свободных мест уже нет", show_alert=True)
            return

        giveaway.participants.append(
            {
                "id": call.from_user.id,
                "username": call.from_user.username,
                "name": call.from_user.first_name,
            }
        )
        if kind in {"mini", "mini_money2"}:
            mini_join_cooldowns[call.from_user.id] = asyncio.get_running_loop().time()

        participants_count = len(giveaway.participants)
        should_finish = (
            kind in {"mini", "mini_money2"} and giveaway.max_players and participants_count >= giveaway.max_players
        ) or (kind in {"duel", "darts", "bowling", "football"} and participants_count >= 2)

        if should_finish:
            giveaway.finished = True
            giveaway_to_finish = giveaway
        else:
            await refresh_giveaway(giveaway, active=True)
            answer_text = f"Готово. Сейчас участников: {participants_count}"

    if not should_finish or not giveaway_to_finish:
        await call.answer(answer_text)
        return

    if kind in {"mini", "mini_money2"}:
        await asyncio.sleep(0.7)

    if kind == "mini":
        result = await finish_mini(giveaway_to_finish)
        answer_text = "Ты успел в мини, победитель уже определён."
    elif kind == "mini_money2":
        result = await finish_mini_money2(giveaway_to_finish)
        answer_text = "Ты успел в Mini Babki 2, победитель уже определён."
    elif kind == "duel":
        result = await finish_duel(giveaway_to_finish)
        answer_text = "Второй игрок зашёл, дуэль уже сыграна."
    elif kind == "darts":
        result = await finish_darts(giveaway_to_finish)
        answer_text = "Второй игрок зашёл, дартс уже сыгран."
    elif kind == "bowling":
        result = await finish_bowling(giveaway_to_finish)
        answer_text = "Второй игрок зашёл, боулинг уже сыгран."
    else:
        result = await finish_football(giveaway_to_finish)
        answer_text = "Второй игрок зашёл, футбол уже сыгран."

    await notify_admins(result)
    await call.answer(answer_text)


@dp.callback_query(F.data == "claim:mini_money2")
async def claim_mini_money2(call: CallbackQuery) -> None:
    completed = completed_giveaways.get("mini_money2")
    if not completed or not completed.winners:
        await call.answer("Сейчас нечего забирать", show_alert=True)
        return

    winner = completed.winners[0]
    if call.from_user.id != winner["id"]:
        await call.answer("Забрать приз может только победитель", show_alert=True)
        return

    try:
        check_url = await ensure_mini_money2_check(completed)
        if completed.message_id is not None:
            await bot.edit_message_reply_markup(
                chat_id=CHANNEL_ID,
                message_id=completed.message_id,
                reply_markup=mini_money2_claim_keyboard(check_url),
            )
    except Exception as exc:
        logging.exception("Could not resend Mini Babki 2 check")
        await notify_admins(f"Mini Babki 2: ошибка повторной выдачи чека: {escape(str(exc))}")
        await call.answer("Не удалось выдать чек, админы уже получили уведомление", show_alert=True)
        return

    try:
        await bot.send_message(
            call.from_user.id,
            f"🎁 Твой чек на ${escape(str(completed.meta.get('prize_amount_usd', completed.prize)))}",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🎁 Открыть чек", url=check_url)]]
            ),
            disable_web_page_preview=True,
        )
        await call.answer("Чек отправлен тебе в личку", show_alert=True)
    except Exception:
        logging.exception("Could not send Mini Babki 2 check to DM")
        await call.answer("Чек готов. Кнопка в посте уже открывает его напрямую.", show_alert=True)


async def on_startup() -> None:
    logging.info("Bot started")
    await bot.send_message(
        ADMIN_ID,
        "Бот запущен.\n\n"
        "Что можно делать:\n"
        "• открыть /start и зайти в админку кнопкой\n"
        "• создать мини, розыгрыш, дуэль, дартс, боулинг или футбол\n"
        "• смотреть участников, завершать, удалять и делать рерол кнопками\n"
        "• выдавать и удалять админку через раздел админов\n"
        "• менять бренд одной строкой: BRAND_USERNAME, BRAND_AUTHOR",
    )


async def main() -> None:
    await on_startup()
    await dp.start_polling(bot)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
