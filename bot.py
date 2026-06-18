import asyncio
import json
import logging
import os
import random
import re
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
PROFILES_FILE = Path(__file__).with_name("user_profiles.json")

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
pending_mini_money2_invoices: Dict[int, dict] = {}
pending_mini_money2_watchers: Dict[int, asyncio.Task] = {}


def usd_decimal(value: str | Decimal) -> Decimal:
    amount = Decimal(str(value).replace("$", "").replace(",", ".").strip())
    if amount <= 0:
        raise InvalidOperation
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def format_usd(value: str | Decimal) -> str:
    return f"{usd_decimal(value):.2f}"


def safe_usd_decimal(value: str | Decimal) -> Decimal:
    amount = Decimal(str(value).replace("$", "").replace(",", ".").strip() or "0")
    if amount < 0:
        raise InvalidOperation
    return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def invoice_amount_with_fee(value: str | Decimal) -> str:
    total = (usd_decimal(value) * Decimal("1.10")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return f"{total:.2f}"


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


async def get_crypto_app() -> dict:
    result = await crypto_pay_request("getMe", {})
    return result if isinstance(result, dict) else {}


async def get_crypto_checks(
    *,
    status: Optional[str] = None,
    check_ids: Optional[List[int]] = None,
    count: int = 100,
    offset: int = 0,
) -> List[dict]:
    payload: Dict[str, Any] = {"count": count, "offset": offset}
    if status:
        payload["status"] = status
    if check_ids:
        payload["check_ids"] = ",".join(str(check_id) for check_id in check_ids)

    result = await crypto_pay_request("getChecks", payload)
    return result if isinstance(result, list) else []


async def get_crypto_invoices(
    *,
    status: Optional[str] = None,
    invoice_ids: Optional[List[int]] = None,
    count: int = 100,
    offset: int = 0,
) -> List[dict]:
    payload: Dict[str, Any] = {"count": count, "offset": offset}
    if status:
        payload["status"] = status
    if invoice_ids:
        payload["invoice_ids"] = ",".join(str(invoice_id) for invoice_id in invoice_ids)

    result = await crypto_pay_request("getInvoices", payload)
    return result if isinstance(result, list) else []


async def delete_crypto_check(check_id: int) -> None:
    await crypto_pay_request("deleteCheck", {"check_id": check_id})


async def get_crypto_balances() -> List[dict]:
    result = await crypto_pay_request("getBalance", {})
    return result if isinstance(result, list) else []


async def get_all_crypto_checks(status: Optional[str] = None) -> List[dict]:
    checks: List[dict] = []
    offset = 0

    while True:
        batch = await get_crypto_checks(status=status, count=1000, offset=offset)
        if not batch:
            break
        checks.extend(batch)
        if len(batch) < 1000:
            break
        offset += len(batch)

    return checks


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


def load_user_profiles() -> Dict[str, dict]:
    if not PROFILES_FILE.exists():
        return {}

    try:
        raw = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
    except Exception:
        logging.exception("Could not load user profiles")
        return {}

    if not isinstance(raw, dict):
        return {}

    profiles: Dict[str, dict] = {}
    for key, value in raw.items():
        if not isinstance(value, dict):
            continue
        profile = dict(value)
        profile.setdefault("balance_usd", "0.00")
        profile.setdefault("hold_usd", "0.00")
        profile.setdefault("id", int(key))
        profiles[str(key)] = profile
    return profiles


def save_user_profiles() -> None:
    try:
        payload = json.dumps(user_profiles, ensure_ascii=False, indent=2)
        PROFILES_FILE.write_text(payload, encoding="utf-8")
    except Exception:
        logging.exception("Could not save user profiles")


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


def profile_key(user_id: int) -> str:
    return str(user_id)


def ensure_user_profile(user_id: int, username: Optional[str] = None, name: Optional[str] = None) -> dict:
    key = profile_key(user_id)
    profile = user_profiles.get(key)
    if not profile:
        profile = {
            "id": user_id,
            "username": username,
            "name": name or "Без имени",
            "balance_usd": "0.00",
            "hold_usd": "0.00",
        }
        user_profiles[key] = profile
        save_user_profiles()
        return profile

    changed = False
    if username is not None and profile.get("username") != username:
        profile["username"] = username
        changed = True
    if name and profile.get("name") != name:
        profile["name"] = name
        changed = True
    if changed:
        save_user_profiles()
    return profile


def remember_user(user_id: int, username: Optional[str] = None, name: Optional[str] = None) -> None:
    if user_id in known_users:
        ensure_user_profile(user_id, username, name)
        return
    known_users.add(user_id)
    save_known_users()
    ensure_user_profile(user_id, username, name)


known_users = load_known_users()
extra_admin_ids = load_extra_admins()
user_profiles = load_user_profiles()


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


def normalize_username(value: str) -> str:
    return value.strip().lstrip("@").lower()


def profile_by_username(value: str) -> Optional[dict]:
    username = normalize_username(value)
    if not username:
        return None
    for profile in user_profiles.values():
        if normalize_username(str(profile.get("username") or "")) == username:
            return profile
    return None


def profile_balance_decimal(profile: dict) -> Decimal:
    return safe_usd_decimal(profile.get("balance_usd", "0.00") or "0.00")


def profile_hold_decimal(profile: dict) -> Decimal:
    return safe_usd_decimal(profile.get("hold_usd", "0.00") or "0.00")


def set_profile_balance(profile: dict, amount: Decimal) -> None:
    profile["balance_usd"] = f"{amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"


def set_profile_hold(profile: dict, amount: Decimal) -> None:
    profile["hold_usd"] = f"{amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):.2f}"


def pending_profile_by_check_id(check_id: Any) -> Optional[dict]:
    check_id_str = str(check_id)
    for profile in user_profiles.values():
        if str(profile.get("pending_check_id", "")) == check_id_str:
            return profile
    return None


def completed_by_check_id(check_id: Any) -> Optional[CompletedGiveaway]:
    check_id_str = str(check_id)
    for completed in completed_giveaways.values():
        if str(completed.meta.get("claim_check_id", "")) == check_id_str:
            return completed
    return None


def check_owner_label(check_id: Any) -> str:
    completed = completed_by_check_id(check_id)
    if completed and completed.winners:
        return user_label(completed.winners[0])

    profile = pending_profile_by_check_id(check_id)
    if profile:
        return user_label(profile)

    return "не найден в локальных розыгрышах"


def parse_check_id(value: str) -> Optional[int]:
    match = re.search(r"\d+", value)
    if not match:
        return None
    try:
        return int(match.group())
    except ValueError:
        return None


def credit_profile_balance(user_data: dict, amount: str | Decimal) -> dict:
    profile = ensure_user_profile(user_data["id"], user_data.get("username"), user_data.get("name"))
    new_balance = profile_balance_decimal(profile) + usd_decimal(amount)
    set_profile_balance(profile, new_balance)
    save_user_profiles()
    return profile


def change_profile_balance(profile: dict, amount: Decimal) -> None:
    set_profile_balance(profile, profile_balance_decimal(profile) + amount)
    save_user_profiles()


def clear_profile_pending_check(profile: dict, refund: bool = False) -> None:
    pending_amount = safe_usd_decimal(profile.get("pending_check_amount_usd", "0.00") or "0.00")
    current_balance = profile_balance_decimal(profile)
    current_hold = profile_hold_decimal(profile)

    if refund and pending_amount > 0:
        set_profile_balance(profile, current_balance + pending_amount)
        set_profile_hold(profile, max(Decimal("0.00"), current_hold - pending_amount))
    elif pending_amount > 0 and current_hold > 0:
        set_profile_hold(profile, max(Decimal("0.00"), current_hold - pending_amount))

    profile.pop("pending_check_id", None)
    profile.pop("pending_check_url", None)
    profile.pop("pending_check_amount_usd", None)
    save_user_profiles()


def register_profile_pending_check(profile: dict, amount: Decimal, check: dict) -> None:
    current_balance = profile_balance_decimal(profile)
    current_hold = profile_hold_decimal(profile)
    if current_balance < amount:
        raise RuntimeError("Недостаточно средств на балансе для вывода")

    set_profile_balance(profile, current_balance - amount)
    set_profile_hold(profile, current_hold + amount)
    profile["pending_check_id"] = check["check_id"]
    profile["pending_check_url"] = check["url"]
    profile["pending_check_amount_usd"] = f"{amount:.2f}"
    save_user_profiles()


async def sync_profile_pending_check(profile: dict) -> Optional[str]:
    check_id = profile.get("pending_check_id")
    if not check_id:
        return None

    active_checks = await get_crypto_checks(status="active", check_ids=[int(check_id)], count=1)
    if active_checks:
        active_check = active_checks[0]
        check_url = crypto_check_url(active_check) or profile.get("pending_check_url")
        if not check_url:
            raise RuntimeError("Crypto Pay не вернул ссылку на активный чек")
        if check_url:
            profile["pending_check_url"] = check_url
        profile["pending_check_id"] = active_check.get("check_id") or active_check.get("id") or check_id
        save_user_profiles()
        return str(check_url) if check_url else None

    checks = await get_crypto_checks(check_ids=[int(check_id)], count=1)
    if checks:
        status = str(checks[0].get("status", "")).lower()
        if status in {"activated", "paid", "completed", "used"}:
            clear_profile_pending_check(profile, refund=False)
        else:
            clear_profile_pending_check(profile, refund=True)
        return None

    clear_profile_pending_check(profile, refund=True)
    return None


async def create_profile_withdraw_check(profile: dict) -> str:
    amount = profile_balance_decimal(profile)
    if amount <= 0:
        raise RuntimeError("Баланс пустой")

    check = await create_crypto_check(f"{amount:.2f}", profile)
    register_profile_pending_check(profile, amount, check)
    return str(check["url"])


async def ensure_profile_withdraw_check(profile: dict) -> str:
    active_url = await sync_profile_pending_check(profile)
    if active_url:
        return active_url
    return await create_profile_withdraw_check(profile)


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
        "🎲 <b>Механика:</b> у каждого свой номер в списке, бот кидает один кубик.",
        "🏆 <b>Побеждает номер, который выпадет на кубике.</b>",
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


def mini_money2_result_text(completed: CompletedGiveaway, winner_number: int) -> str:
    winner = completed.winners[0]
    lines = [
        f"🤑 <b>{branded_title('MINI BABKI 2')}</b>",
        "",
        f"💵 <b>Приз:</b> ${escape(str(completed.meta.get('prize_amount_usd', completed.prize)))}",
        f"🎲 <b>Выпал номер:</b> {winner_number}",
        "",
        f"🏆 <b>Победитель:</b> {user_label(winner)}",
        f"🔢 <b>Номер победителя в списке:</b> {winner_number}",
        "💼 <b>Приз зачислен на баланс победителя.</b>",
        "🎁 <b>Вывод:</b> через профиль победителя в боте.",
        "",
        f"🔖 {signature_line()}",
    ]
    return "\n".join(lines)


def crypto_checks_text(checks: List[dict]) -> str:
    lines = ["💳 <b>Активные чеки Crypto Pay</b>", ""]
    if not checks:
        lines.append("Сейчас активных чеков нет.")
        return "\n".join(lines)

    for check in checks[:20]:
        check_id = check.get("check_id") or check.get("id") or "?"
        amount = check.get("amount") or "0"
        asset = check.get("asset") or "USDT"
        created_at = escape(str(check.get("created_at") or "неизвестно"))
        owner = escape(check_owner_label(check_id))
        lines.extend(
            [
                f"• <b>Чек #{escape(str(check_id))}</b> — {escape(str(amount))} {escape(str(asset))}",
                f"  Победитель: {owner}",
                f"  Создан: {created_at}",
            ]
        )

    hidden_count = max(len(checks) - 20, 0)
    if hidden_count:
        lines.extend(["", f"…и ещё {hidden_count} активных чеков."])

    return "\n".join(lines)


def crypto_balance_text(app_info: dict, balances: List[dict], active_checks_count: int) -> str:
    lines = ["💰 <b>Crypto Pay баланс</b>", ""]
    app_name = escape(str(app_info.get("name") or app_info.get("app_name") or "неизвестное приложение"))
    app_id = escape(str(app_info.get("app_id") or app_info.get("id") or "?"))
    lines.append(f"🤖 <b>Приложение:</b> {app_name} (ID: <code>{app_id}</code>)")
    lines.append("")

    total_onhold = Decimal("0")
    if not balances:
        lines.append("Баланс не вернулся из API.")
    else:
        for balance in balances:
            currency = escape(str(balance.get("currency_code") or balance.get("currency") or "?"))
            available = escape(str(balance.get("available") or "0"))
            onhold = escape(str(balance.get("onhold") or "0"))
            try:
                total_onhold += Decimal(str(balance.get("onhold") or "0"))
            except Exception:
                pass
            lines.append(f"• <b>{currency}</b>: доступно {available}, в удержании {onhold}")

    lines.extend(
        [
            "",
            f"🧾 <b>Активных чеков:</b> {active_checks_count}",
            "Удаление активных чеков освобождает сумму из удержания.",
        ]
    )
    if total_onhold > 0 and active_checks_count == 0:
        lines.extend(
            [
                "",
                "⚠️ <b>Чеки не найдены, но onhold есть.</b>",
                "Скорее всего в CRYPTO_PAY_TOKEN стоит токен не от того Crypto Pay приложения.",
                "Сверь название и ID приложения выше с тем приложением, где ты видишь удержание в CryptoBot.",
            ]
        )
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


def profile_keyboard(profile: dict) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    if profile.get("pending_check_url"):
        rows.append([InlineKeyboardButton(text="Open Active Check", url=str(profile["pending_check_url"]))])
    elif profile_balance_decimal(profile) > 0:
        rows.append([InlineKeyboardButton(text="Withdraw To CryptoBot", callback_data="profile:withdraw")])
    rows.append([InlineKeyboardButton(text="Refresh Profile", callback_data="profile:open")])
    rows.append([InlineKeyboardButton(text="Open Channel", url=f"https://t.me/{BRAND_USERNAME.lstrip('@')}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def start_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="Profile", callback_data="profile:open")]]
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
        [InlineKeyboardButton(text="💳 Crypto Pay", callback_data="crypto:menu")],
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


def crypto_menu_keyboard(confirm_delete_all: bool = False, has_checks: bool = True) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="💰 Баланс и удержание", callback_data="crypto:status")],
        [InlineKeyboardButton(text="🧾 Активные чеки", callback_data="crypto:checks")],
        [InlineKeyboardButton(text="✍️ Удалить чек по ID", callback_data="crypto:delete_manual")],
    ]
    if has_checks:
        if confirm_delete_all:
            rows.append([InlineKeyboardButton(text="⚠️ Подтвердить удаление всех чеков", callback_data="crypto:delete_all")])
            rows.append([InlineKeyboardButton(text="↩️ Не удалять", callback_data="crypto:checks")])
        else:
            rows.append([InlineKeyboardButton(text="🗑 Удалить все активные чеки", callback_data="crypto:delete_all:confirm")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="back")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def crypto_checks_keyboard(checks: List[dict], confirm_delete_all: bool = False) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    for check in checks[:20]:
        check_id = check.get("check_id") or check.get("id")
        amount = check.get("amount") or "0"
        asset = check.get("asset") or "USDT"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"🗑 Чек #{check_id} • {amount} {asset}",
                    callback_data=f"crypto:delete:{check_id}",
                )
            ]
        )

    rows.append([InlineKeyboardButton(text="🔄 Обновить список", callback_data="crypto:checks")])
    rows.append([InlineKeyboardButton(text="✍️ Удалить чек по ID", callback_data="crypto:delete_manual")])
    if checks:
        if confirm_delete_all:
            rows.append([InlineKeyboardButton(text="⚠️ Подтвердить удаление всех чеков", callback_data="crypto:delete_all")])
            rows.append([InlineKeyboardButton(text="↩️ Не удалять", callback_data="crypto:checks")])
        else:
            rows.append([InlineKeyboardButton(text="🗑 Удалить все активные чеки", callback_data="crypto:delete_all:confirm")])
    rows.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="crypto:menu")])
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


async def roll_slot_winner(participants: List[dict], emoji: str, start_text: str) -> tuple[dict, int, List[int]]:
    intro_message = await bot.send_message(CHANNEL_ID, start_text)
    temp_message_ids: List[int] = [intro_message.message_id]

    while True:
        dice_message = await bot.send_dice(chat_id=CHANNEL_ID, emoji=emoji)
        rolled_number = dice_message.dice.value
        temp_message_ids.append(dice_message.message_id)

        if 1 <= rolled_number <= len(participants):
            return participants[rolled_number - 1], rolled_number, temp_message_ids

        reroll_message = await bot.send_message(
            CHANNEL_ID,
            f"{emoji} Выпал номер {rolled_number}, а участников только {len(participants)}. Кидаем ещё раз...",
        )
        temp_message_ids.append(reroll_message.message_id)


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


async def reset_completed_check_state(completed: CompletedGiveaway) -> None:
    completed.meta.pop("claim_check_url", None)
    completed.meta.pop("claim_check_id", None)
    if completed.kind == "mini_money2" and completed.message_id is not None:
        try:
            await bot.edit_message_reply_markup(
                chat_id=CHANNEL_ID,
                message_id=completed.message_id,
                reply_markup=public_keyboard("mini_money2", active=False),
            )
        except Exception:
            logging.exception("Could not reset Mini Babki 2 claim button")


async def get_active_check_url(completed: CompletedGiveaway) -> Optional[str]:
    check_id = completed.meta.get("claim_check_id")
    if not check_id:
        return None

    checks = await get_crypto_checks(status="active", check_ids=[int(check_id)], count=1)
    if not checks:
        await reset_completed_check_state(completed)
        return None

    check = checks[0]
    check_url = crypto_check_url(check) or completed.meta.get("claim_check_url")
    if check_url:
        completed.meta["claim_check_url"] = check_url
    completed.meta["claim_check_id"] = check.get("check_id") or check.get("id") or check_id
    return str(check_url) if check_url else None


async def ensure_mini_money2_check(completed: CompletedGiveaway) -> str:
    if completed.meta.get("claim_check_url") and completed.meta.get("claim_check_id"):
        try:
            active_check_url = await get_active_check_url(completed)
        except Exception:
            logging.exception("Could not verify existing Mini Babki 2 check")
            return str(completed.meta["claim_check_url"])
        if active_check_url:
            return active_check_url

    check = await create_crypto_check(str(completed.meta["prize_amount_usd"]), completed.winners[0])
    completed.meta["claim_check_url"] = check["url"]
    completed.meta["claim_check_id"] = check["check_id"]
    return check["url"]


async def finish_mini_money2(giveaway: Giveaway) -> str:
    giveaway.finished = True
    winner, winner_number, dice_message_ids = await roll_slot_winner(
        giveaway.participants,
        "🎲",
        "🎲 Кидаем один кубик для Mini Babki 2. Побеждает участник с номером, который выпадет на кубике...",
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
        text=mini_money2_result_text(completed, winner_number),
        reply_markup=public_keyboard("mini_money2", active=False),
        disable_web_page_preview=True,
    )
    completed_giveaways["mini_money2"] = completed
    active_giveaways.pop("mini_money2", None)
    schedule_message_cleanup(CHANNEL_ID, dice_message_ids)

    try:
        profile = credit_profile_balance(winner, str(completed.meta["prize_amount_usd"]))
    except Exception as exc:
        logging.exception("Could not credit Mini Babki 2 balance")
        await notify_admins(f"Mini Babki 2: не удалось зачислить баланс победителю: {escape(str(exc))}")
        return f"Победитель Mini Babki 2: {user_label(winner)}"

    auto_check_url: Optional[str] = None
    try:
        auto_check_url = await ensure_profile_withdraw_check(profile)
    except Exception as exc:
        logging.exception("Could not auto-withdraw Mini Babki 2 balance")
        await notify_admins(
            f"Mini Babki 2: баланс зачислен, но автовывод чеком не создался: {escape(str(exc))}"
        )

    try:
        lines = [
            "🎉 <b>Ты выиграл Mini Babki 2</b>",
            "",
            f"💵 <b>На баланс зачислено:</b> ${escape(str(completed.meta['prize_amount_usd']))}",
        ]
        if auto_check_url:
            lines.extend(
                [
                    "✅ <b>Автовывод создан.</b>",
                    f"💸 <b>Сумма чека:</b> ${escape(str(profile.get('pending_check_amount_usd', '0.00')))}",
                ]
            )
        else:
            lines.extend(
                [
                    f"💰 <b>Доступно в профиле:</b> ${profile['balance_usd']}",
                    "Открой профиль в боте и выведи баланс вручную кнопкой ниже.",
                ]
            )
        await bot.send_message(
            winner["id"],
            "\n".join(lines),
            reply_markup=profile_keyboard(profile),
            disable_web_page_preview=True,
        )
    except Exception as exc:
        logging.exception("Could not notify winner about Mini Babki 2 payout")
        await notify_admins(
            f"Mini Babki 2: баланс зачислен, но победителю не отправилось сообщение: {escape(str(exc))}"
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
    dart_message_ids: List[int] = []
    first_dart = await bot.send_dice(chat_id=CHANNEL_ID, emoji="🎯")
    second_dart = await bot.send_dice(chat_id=CHANNEL_ID, emoji="🎯")
    dart_message_ids.extend([first_dart.message_id, second_dart.message_id])

    first_score = first_dart.dice.value
    second_score = second_dart.dice.value
    while first_score == second_score:
        tie_break = await bot.send_message(CHANNEL_ID, "🎯 Ничья в дартсе, кидаем ещё раз...")
        dart_message_ids.append(tie_break.message_id)
        first_dart = await bot.send_dice(chat_id=CHANNEL_ID, emoji="🎯")
        second_dart = await bot.send_dice(chat_id=CHANNEL_ID, emoji="🎯")
        dart_message_ids.extend([first_dart.message_id, second_dart.message_id])
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
    schedule_message_cleanup(CHANNEL_ID, dart_message_ids)
    return f"Победитель дартса: {user_label(winner)}"


async def finish_bowling(giveaway: Giveaway, reroll: bool = False) -> str:
    first, second = giveaway.participants
    bowl_message_ids: List[int] = []
    first_ball = await bot.send_dice(chat_id=CHANNEL_ID, emoji="🎳")
    second_ball = await bot.send_dice(chat_id=CHANNEL_ID, emoji="🎳")
    bowl_message_ids.extend([first_ball.message_id, second_ball.message_id])

    first_score = first_ball.dice.value
    second_score = second_ball.dice.value
    while first_score == second_score:
        tie_break = await bot.send_message(CHANNEL_ID, "🎳 Ничья в боулинге, кидаем ещё раз...")
        bowl_message_ids.append(tie_break.message_id)
        first_ball = await bot.send_dice(chat_id=CHANNEL_ID, emoji="🎳")
        second_ball = await bot.send_dice(chat_id=CHANNEL_ID, emoji="🎳")
        bowl_message_ids.extend([first_ball.message_id, second_ball.message_id])
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
    schedule_message_cleanup(CHANNEL_ID, bowl_message_ids)
    return f"Победитель боулинга: {user_label(winner)}"


async def finish_football(giveaway: Giveaway, reroll: bool = False) -> str:
    first, second = giveaway.participants
    football_message_ids: List[int] = []
    first_ball = await bot.send_dice(chat_id=CHANNEL_ID, emoji="⚽")
    second_ball = await bot.send_dice(chat_id=CHANNEL_ID, emoji="⚽")
    football_message_ids.extend([first_ball.message_id, second_ball.message_id])

    first_score = first_ball.dice.value
    second_score = second_ball.dice.value
    while first_score == second_score:
        tie_break = await bot.send_message(CHANNEL_ID, "⚽ Ничья в футболе, бьём ещё раз...")
        football_message_ids.append(tie_break.message_id)
        first_ball = await bot.send_dice(chat_id=CHANNEL_ID, emoji="⚽")
        second_ball = await bot.send_dice(chat_id=CHANNEL_ID, emoji="⚽")
        football_message_ids.extend([first_ball.message_id, second_ball.message_id])
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
    schedule_message_cleanup(CHANNEL_ID, football_message_ids)
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


async def active_crypto_checks() -> List[dict]:
    return await get_all_crypto_checks(status="active")


async def delete_and_unbind_check(check_id: int) -> Optional[CompletedGiveaway]:
    await delete_crypto_check(check_id)
    completed = completed_by_check_id(check_id)
    if completed:
        await reset_completed_check_state(completed)
        return completed

    profile = pending_profile_by_check_id(check_id)
    if profile:
        clear_profile_pending_check(profile, refund=True)
    return completed


async def send_crypto_status_message(message: Message) -> None:
    app_info = await get_crypto_app()
    balances = await get_crypto_balances()
    checks = await active_crypto_checks()
    await message.answer(
        crypto_balance_text(app_info, balances, len(checks)),
        reply_markup=crypto_menu_keyboard(has_checks=bool(checks)),
    )


async def send_crypto_checks_message(message: Message, confirm_delete_all: bool = False) -> None:
    checks = await active_crypto_checks()
    await message.answer(
        crypto_checks_text(checks),
        reply_markup=crypto_checks_keyboard(checks, confirm_delete_all=confirm_delete_all),
        disable_web_page_preview=True,
    )


def profile_text(profile: dict) -> str:
    available = profile_balance_decimal(profile)
    hold = profile_hold_decimal(profile)
    username_line = (
        f"@{escape(str(profile['username']))}"
        if profile.get("username")
        else escape(str(profile.get("name") or profile.get("id")))
    )
    lines = [
        "👤 <b>Профиль</b>",
        "",
        f"🆔 <b>Пользователь:</b> {username_line}",
        f"💰 <b>Баланс:</b> ${available:.2f}",
    ]
    if hold > 0:
        lines.append(f"⏳ <b>В удержании:</b> ${hold:.2f}")
    if profile.get("pending_check_id"):
        lines.append(f"🧾 <b>Активный чек:</b> <code>{escape(str(profile['pending_check_id']))}</code>")
    lines.extend(["", f"🔖 {signature_line()}"])
    return "\n".join(lines)


def profile_keyboard(profile: dict) -> InlineKeyboardMarkup:
    rows: List[List[InlineKeyboardButton]] = []
    if profile.get("pending_check_url"):
        rows.append([InlineKeyboardButton(text="🎁 Открыть активный чек", url=str(profile["pending_check_url"]))])
    elif profile_balance_decimal(profile) > 0:
        rows.append([InlineKeyboardButton(text="💸 Вывести баланс", callback_data="profile:withdraw")])
    rows.append([InlineKeyboardButton(text="🔄 Обновить профиль", callback_data="profile:open")])
    rows.append([InlineKeyboardButton(text="📣 Открыть канал", url=f"https://t.me/{BRAND_USERNAME.lstrip('@')}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def start_keyboard(user_id: int) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text="👤 Профиль", callback_data="profile:open")]]
    if is_admin(user_id):
        rows.append([InlineKeyboardButton(text="🛠 Открыть админку", callback_data="open_admin")])
    rows.append([InlineKeyboardButton(text="📣 Открыть канал", url=f"https://t.me/{BRAND_USERNAME.lstrip('@')}")])
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
        [InlineKeyboardButton(text="💼 Балансы", callback_data="balances:menu")],
        [InlineKeyboardButton(text="💳 Crypto Pay", callback_data="crypto:menu")],
        [InlineKeyboardButton(text="🗂 Активные посты", callback_data="manage")],
        [InlineKeyboardButton(text="📣 Рассылка", callback_data="broadcast:start")],
        [InlineKeyboardButton(text="📊 Статус", callback_data="status")],
    ]
    rows.append([InlineKeyboardButton(text="👑 Админы", callback_data="admins:menu")])
    rows.append([InlineKeyboardButton(text="🧹 Сбросить ввод", callback_data="reset")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def balances_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="➕ Начислить по @username", callback_data="balances:add")],
            [InlineKeyboardButton(text="➖ Снять по @username", callback_data="balances:subtract")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="back")],
        ]
    )


def mini_money2_invoice_keyboard(invoice_id: int, invoice_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оплатить счёт", url=invoice_url)],
            [InlineKeyboardButton(text="🔄 Проверить оплату", callback_data=f"mini2:invoice:check:{invoice_id}")],
            [InlineKeyboardButton(text="❌ Отменить ожидание", callback_data=f"mini2:invoice:cancel:{invoice_id}")],
        ]
    )


async def send_profile_overview(message: Message, user_data: Any) -> None:
    profile = ensure_user_profile(user_data.id, user_data.username, user_data.first_name)
    try:
        await sync_profile_pending_check(profile)
    except Exception:
        logging.exception("Could not sync profile pending check")
    await message.answer(profile_text(profile), reply_markup=profile_keyboard(profile), disable_web_page_preview=True)


async def publish_paid_mini_money2_invoice(invoice_id: int) -> str:
    pending = pending_mini_money2_invoices.get(invoice_id)
    if not pending:
        return "Счёт уже не ожидает оплаты."

    invoices = await get_crypto_invoices(invoice_ids=[invoice_id], count=1)
    if not invoices:
        return "Счёт не найден в Crypto Pay."

    invoice = invoices[0]
    status = str(invoice.get("status", "")).lower()
    if status != "paid":
        if status in {"expired", "cancelled", "deleted"}:
            pending_mini_money2_invoices.pop(invoice_id, None)
            return f"Счёт больше не активен. Статус: {status}."
        return f"Счёт ещё не оплачен. Текущий статус: {status or 'unknown'}."

    if "mini_money2" in active_giveaways:
        return "Оплата есть, но Mini Babki 2 уже активен. Сначала заверши текущий розыгрыш."

    giveaway = Giveaway(
        kind="mini_money2",
        prize=f"${pending['prize_amount_usd']}",
        winners_count=1,
        max_players=MAX_MINI_PLAYERS,
        meta={
            "prize_amount_usd": pending["prize_amount_usd"],
            "payment_amount_usd": pending["payment_amount_usd"],
            "crypto_invoice_url": pending["crypto_invoice_url"],
            "crypto_invoice_id": pending["crypto_invoice_id"],
        },
    )
    await publish_giveaway(giveaway)
    pending_mini_money2_invoices.pop(invoice_id, None)
    return (
        "Оплата подтверждена. Mini Babki 2 опубликован в канале.\n"
        f"Приз: ${pending['prize_amount_usd']}\n"
        f"Оплачено по счёту: ${pending['payment_amount_usd']}"
    )


async def watch_mini_money2_invoice(invoice_id: int) -> None:
    try:
        while invoice_id in pending_mini_money2_invoices:
            await asyncio.sleep(15)
            result = await publish_paid_mini_money2_invoice(invoice_id)
            if result.startswith("Оплата подтверждена") or "больше не активен" in result or "не найден" in result:
                await notify_admins(result)
                break
    except Exception:
        logging.exception("Could not watch Mini Babki 2 invoice")
    finally:
        pending_mini_money2_watchers.pop(invoice_id, None)


@dp.message(Command("start"))
async def start_handler(message: Message) -> None:
    remember_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    if not is_admin(message.from_user.id):
        await message.answer("Бот активен. Участвуй через кнопки под постами в канале.", reply_markup=start_keyboard(message.from_user.id))
        return
    await message.answer("Нажми кнопку ниже, чтобы открыть админку.", reply_markup=start_keyboard(message.from_user.id))


@dp.callback_query(F.data == "profile:open")
async def profile_open_handler(call: CallbackQuery) -> None:
    remember_user(call.from_user.id, call.from_user.username, call.from_user.first_name)
    await send_profile_overview(call.message, call.from_user)
    await call.answer()


@dp.callback_query(F.data == "profile:withdraw")
async def profile_withdraw_handler(call: CallbackQuery) -> None:
    remember_user(call.from_user.id, call.from_user.username, call.from_user.first_name)
    profile = ensure_user_profile(call.from_user.id, call.from_user.username, call.from_user.first_name)

    try:
        check_url = await ensure_profile_withdraw_check(profile)
    except Exception as exc:
        logging.exception("Could not withdraw profile balance")
        await call.answer(f"Не удалось вывести баланс: {escape(str(exc))}", show_alert=True)
        return

    if profile.get("pending_check_url") != check_url:
        profile["pending_check_url"] = check_url
        save_user_profiles()

    await call.message.answer(
        profile_text(profile),
        reply_markup=profile_keyboard(profile),
        disable_web_page_preview=True,
    )
    try:
        await bot.send_message(
            call.from_user.id,
            f"🎁 Твой чек на ${escape(str(profile.get('pending_check_amount_usd', '0.00')))} готов.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[[InlineKeyboardButton(text="🎁 Открыть чек", url=check_url)]]
            ),
            disable_web_page_preview=True,
        )
    except Exception:
        logging.exception("Could not send profile withdrawal check to DM")
    await call.answer("Чек на вывод готов", show_alert=True)


@dp.callback_query(F.data == "open_admin")
async def open_admin_handler(call: CallbackQuery) -> None:
    remember_user(call.from_user.id, call.from_user.username, call.from_user.first_name)
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
    remember_user(call.from_user.id, call.from_user.username, call.from_user.first_name)
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


@dp.callback_query(F.data == "balances:menu")
async def balances_menu_handler(call: CallbackQuery) -> None:
    remember_user(call.from_user.id, call.from_user.username, call.from_user.first_name)
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return

    await call.message.answer(
        "Управление балансами.\nМожно начислить или снять сумму по @username.",
        reply_markup=balances_keyboard(),
    )
    await call.answer()


@dp.callback_query(F.data.in_({"balances:add", "balances:subtract"}))
async def balances_action_handler(call: CallbackQuery) -> None:
    remember_user(call.from_user.id, call.from_user.username, call.from_user.first_name)
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return

    action = "add" if call.data.endswith("add") else "subtract"
    admin_state[call.from_user.id] = {"kind": f"balance_{action}", "step": "username"}
    prompt = "Пришли @username пользователя." if action == "add" else "Пришли @username пользователя для списания."
    await call.message.answer(prompt, reply_markup=balances_keyboard())
    await call.answer()


@dp.callback_query(F.data.startswith("mini2:invoice:"))
async def mini_money2_invoice_actions(call: CallbackQuery) -> None:
    remember_user(call.from_user.id, call.from_user.username, call.from_user.first_name)
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return

    _, _, action, invoice_id_raw = call.data.split(":")
    invoice_id = int(invoice_id_raw)

    if action == "cancel":
        pending_mini_money2_invoices.pop(invoice_id, None)
        watcher = pending_mini_money2_watchers.pop(invoice_id, None)
        if watcher:
            watcher.cancel()
        await call.message.answer("Ожидание оплаты Mini Babki 2 остановлено.", reply_markup=admin_keyboard())
        await call.answer("Отменено")
        return

    try:
        result = await publish_paid_mini_money2_invoice(invoice_id)
    except Exception as exc:
        logging.exception("Could not check Mini Babki 2 invoice")
        await call.message.answer(f"Ошибка проверки оплаты: {escape(str(exc))}", reply_markup=admin_keyboard())
        await call.answer("Ошибка", show_alert=True)
        return

    await call.message.answer(result, reply_markup=admin_keyboard(), disable_web_page_preview=True)
    await call.answer("Проверено")


@dp.callback_query(F.data == "crypto:menu")
async def crypto_menu_handler(call: CallbackQuery) -> None:
    remember_user(call.from_user.id)
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return

    try:
        checks = await active_crypto_checks()
    except Exception as exc:
        logging.exception("Could not open Crypto Pay menu")
        await call.message.answer(f"Не удалось открыть Crypto Pay меню: {escape(str(exc))}", reply_markup=admin_keyboard())
    else:
        await call.message.answer(
            "Управление Crypto Pay.\nЗдесь можно смотреть удержание и удалять активные чеки.",
            reply_markup=crypto_menu_keyboard(has_checks=bool(checks)),
        )
    await call.answer()


@dp.callback_query(F.data == "crypto:delete_manual")
async def crypto_delete_manual_prompt(call: CallbackQuery) -> None:
    remember_user(call.from_user.id)
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return

    admin_state[call.from_user.id] = {"kind": "crypto_delete_manual", "step": "check_id"}
    await call.message.answer(
        "Пришли check_id для удаления.\nМожно отправить просто число или текст вида #12345.",
        reply_markup=crypto_menu_keyboard(has_checks=True),
    )
    await call.answer()


@dp.callback_query(F.data.in_({"crypto:status", "crypto:checks", "crypto:delete_all:confirm", "crypto:delete_all"}))
async def crypto_actions_handler(call: CallbackQuery) -> None:
    remember_user(call.from_user.id)
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return

    try:
        if call.data == "crypto:status":
            await send_crypto_status_message(call.message)
        elif call.data == "crypto:checks":
            await send_crypto_checks_message(call.message)
        elif call.data == "crypto:delete_all:confirm":
            await send_crypto_checks_message(call.message, confirm_delete_all=True)
        else:
            checks = await active_crypto_checks()
            if not checks:
                await call.message.answer("Активных чеков уже нет.", reply_markup=crypto_menu_keyboard(has_checks=False))
                await call.answer("Пусто")
                return

            deleted_count = 0
            released_assets: Dict[str, Decimal] = {}
            for check in checks:
                check_id = check.get("check_id") or check.get("id")
                if not check_id:
                    continue
                await delete_and_unbind_check(int(check_id))
                deleted_count += 1
                asset = str(check.get("asset") or "USDT")
                released_assets.setdefault(asset, Decimal("0"))
                try:
                    released_assets[asset] += Decimal(str(check.get("amount") or "0"))
                except Exception:
                    pass

            released_text = ", ".join(
                f"{amount.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)} {asset}"
                for asset, amount in released_assets.items()
            ) or "сумма не определена"
            await call.message.answer(
                f"Удалил активные чеки: {deleted_count}.\nОсвобождено из удержания примерно: {released_text}.",
                reply_markup=crypto_menu_keyboard(has_checks=False),
            )
        await call.answer("Готово")
    except Exception as exc:
        logging.exception("Crypto Pay action failed")
        await call.message.answer(f"Crypto Pay ошибка: {escape(str(exc))}", reply_markup=crypto_menu_keyboard(has_checks=True))
        await call.answer("Ошибка", show_alert=True)


@dp.callback_query(F.data.startswith("crypto:delete:"))
async def crypto_delete_single_handler(call: CallbackQuery) -> None:
    remember_user(call.from_user.id)
    if not is_admin(call.from_user.id):
        await call.answer("Нет доступа", show_alert=True)
        return

    try:
        check_id = int(call.data.split(":")[-1])
        completed = await delete_and_unbind_check(check_id)
        suffix = ""
        if completed and completed.winners:
            suffix = f"\nПривязка к победителю сброшена: {user_label(completed.winners[0])}."
        await call.message.answer(
            f"Чек #{check_id} удалён.{suffix}",
            reply_markup=crypto_menu_keyboard(has_checks=True),
        )
        await call.answer("Удалено")
    except Exception as exc:
        logging.exception("Could not delete Crypto Pay check")
        await call.message.answer(f"Не удалось удалить чек: {escape(str(exc))}", reply_markup=crypto_menu_keyboard(has_checks=True))
        await call.answer("Ошибка", show_alert=True)


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
    remember_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
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

    if kind in {"balance_add", "balance_subtract"} and step == "username":
        profile = profile_by_username(text)
        if not profile:
            await message.answer("Пользователь с таким @username не найден в профилях бота.")
            return

        state["username"] = str(profile.get("username") or "")
        state["step"] = "amount"
        action_text = "начислить" if kind == "balance_add" else "снять"
        await message.answer(f"Пришли сумму в USD, которую нужно {action_text} для {user_label(profile)}.")
        return

    if kind in {"balance_add", "balance_subtract"} and step == "amount":
        profile = profile_by_username(state.get("username", ""))
        if not profile:
            admin_state.pop(message.from_user.id, None)
            await message.answer("Профиль пользователя больше не найден.", reply_markup=balances_keyboard())
            return

        try:
            amount = usd_decimal(text)
        except InvalidOperation:
            await message.answer("Пришли корректную сумму в USD. Например: 5 или 12.50")
            return

        current_balance = profile_balance_decimal(profile)
        if kind == "balance_subtract" and current_balance < amount:
            await message.answer(
                f"Недостаточно средств на балансе. Сейчас доступно: ${current_balance:.2f}",
                reply_markup=balances_keyboard(),
            )
            return

        change_profile_balance(profile, amount if kind == "balance_add" else -amount)
        admin_state.pop(message.from_user.id, None)
        action_text = "начислен" if kind == "balance_add" else "списан"
        await message.answer(
            "\n".join(
                [
                    f"Баланс {action_text} для {user_label(profile)}.",
                    f"Сумма: ${amount:.2f}",
                    f"Новый баланс: ${profile['balance_usd']}",
                ]
            ),
            reply_markup=balances_keyboard(),
        )
        return

    if kind == "crypto_delete_manual" and step == "check_id":
        check_id = parse_check_id(text)
        if not check_id:
            await message.answer("Пришли корректный check_id. Например: 12345 или #12345.")
            return

        try:
            completed = await delete_and_unbind_check(check_id)
            checks = await active_crypto_checks()
        except Exception as exc:
            logging.exception("Could not delete Crypto Pay check manually")
            await message.answer(
                f"Не удалось удалить чек <code>{check_id}</code>: {escape(str(exc))}",
                reply_markup=crypto_menu_keyboard(has_checks=True),
            )
            return

        suffix = ""
        if completed and completed.winners:
            suffix = f"\nПривязка к победителю сброшена: {user_label(completed.winners[0])}."

        admin_state.pop(message.from_user.id, None)
        await message.answer(
            f"Чек <code>{check_id}</code> удалён.{suffix}",
            reply_markup=crypto_menu_keyboard(has_checks=bool(checks)),
        )
        return

    if step == "prize":
        if not text:
            await message.answer("Приз не должен быть пустым.")
            return

        if kind == "mini_money2":
            if kind in active_giveaways:
                await message.answer("Сначала заверши текущий Mini Babki 2.")
                return
            if pending_mini_money2_invoices:
                await message.answer("Уже есть неоплаченный или ожидающий оплаты счёт для Mini Babki 2.")
                return

            try:
                prize_amount = format_usd(text)
                payment_amount = invoice_amount_with_fee(prize_amount)
            except InvalidOperation:
                await message.answer("Пришли корректную сумму в USD. Например: 10 или 10.50")
                return

            try:
                invoice = await create_crypto_invoice(payment_amount, f"Mini Babki 2 prize fund ${prize_amount}")
            except Exception as exc:
                logging.exception("Could not create Mini Babki 2 invoice")
                await message.answer(f"Не удалось создать счёт CryptoBot: {escape(str(exc))}")
                return

            invoice_id = int(invoice["invoice_id"])
            pending_mini_money2_invoices[invoice_id] = {
                "creator_id": message.from_user.id,
                "prize_amount_usd": prize_amount,
                "payment_amount_usd": payment_amount,
                "crypto_invoice_url": invoice["url"],
                "crypto_invoice_id": invoice_id,
            }
            watcher = pending_mini_money2_watchers.get(invoice_id)
            if watcher:
                watcher.cancel()
            pending_mini_money2_watchers[invoice_id] = asyncio.create_task(watch_mini_money2_invoice(invoice_id))

            admin_state.pop(message.from_user.id, None)
            await message.answer(
                "\n".join(
                    [
                        "🤑 <b>Счёт для Mini Babki 2 создан</b>",
                        "",
                        f"💵 <b>Приз розыгрыша:</b> ${prize_amount}",
                        f"💳 <b>К оплате с комиссией +10%:</b> ${payment_amount}",
                        "",
                        "После оплаты бот сам опубликует розыгрыш в канале.",
                    ]
                ),
                reply_markup=mini_money2_invoice_keyboard(invoice_id, invoice["url"]),
                disable_web_page_preview=True,
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
                    "🤑 <b>Mini Babki 2 опубликован</b>",
                    "",
                    f"💵 <b>Приз:</b> ${escape(str(giveaway.meta['prize_amount_usd']))}",
                    "💳 <b>Счёт на оплату:</b>",
                    giveaway.meta["crypto_invoice_url"],
                    "",
                    "🎁 После победы сумма зачислится на баланс пользователя.",
                ]
            ),
            reply_markup=admin_keyboard(),
            disable_web_page_preview=True,
        )
        return


@dp.callback_query(F.data.startswith("join:"))
async def join_handler(call: CallbackQuery) -> None:
    remember_user(call.from_user.id, call.from_user.username, call.from_user.first_name)
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
    remember_user(call.from_user.id, call.from_user.username, call.from_user.first_name)
    completed = completed_giveaways.get("mini_money2")
    if not completed or not completed.winners:
        await call.answer("Сейчас нечего забирать", show_alert=True)
        return

    winner = completed.winners[0]
    if call.from_user.id != winner["id"]:
        await call.answer("Забрать приз может только победитель", show_alert=True)
        return

    try:
        profile = ensure_user_profile(call.from_user.id, call.from_user.username, call.from_user.first_name)
        check_url = await ensure_profile_withdraw_check(profile)
    except Exception as exc:
        logging.exception("Could not withdraw Mini Babki 2 balance")
        await notify_admins(f"Mini Babki 2: ошибка вывода баланса победителя: {escape(str(exc))}")
        await call.answer("Не удалось вывести баланс, админы уже получили уведомление", show_alert=True)
        return

    try:
        await bot.send_message(
            call.from_user.id,
            "\n".join(
                [
                    "🎁 <b>Твой чек готов</b>",
                    f"💵 <b>Сумма чека:</b> ${escape(str(profile.get('pending_check_amount_usd', '0.00')))}",
                ]
            ),
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
