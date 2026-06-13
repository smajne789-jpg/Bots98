import asyncio
import json
import logging
import os
import random
import re
from html import escape
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils import executor


TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID_RAW = os.getenv("ADMIN_ID")
CHANNEL_ID = os.getenv("CHANNEL_ID")
REFERRAL_CHAT_ID = os.getenv("REFERRAL_CHAT_ID") or CHANNEL_ID

MAX_PARTICIPANTS = 6
GIVEAWAY_PHOTO = "AgACAgIAAxkBAANSaiOILtbjI9uXPclOjby3azTEWqQAAqodaxu6QRlJLp2T9fXeiH0BAAMCAAN5AAM7BA"
REFERRAL_DATA_FILE = Path(__file__).with_name("referrals.json")
REFERRAL_TOP_LIMIT = 10
PREMIUM_EMOJI_IDS = {
    "gift": os.getenv("PREMIUM_EMOJI_GIFT_ID"),
    "trophy": os.getenv("PREMIUM_EMOJI_TROPHY_ID"),
    "pointer": os.getenv("PREMIUM_EMOJI_POINTER_ID"),
    "players": os.getenv("PREMIUM_EMOJI_PLAYERS_ID"),
    "clover": os.getenv("PREMIUM_EMOJI_CLOVER_ID"),
    "swords": os.getenv("PREMIUM_EMOJI_SWORDS_ID"),
    "dice": os.getenv("PREMIUM_EMOJI_DICE_ID"),
    "sparkles": os.getenv("PREMIUM_EMOJI_SPARKLES_ID"),
    "money": os.getenv("PREMIUM_EMOJI_MONEY_ID"),
    "broken_heart": os.getenv("PREMIUM_EMOJI_BROKEN_HEART_ID"),
    "handshake": os.getenv("PREMIUM_EMOJI_HANDSHAKE_ID"),
}

if not TOKEN or not ADMIN_ID_RAW or not CHANNEL_ID:
    raise ValueError("Set BOT_TOKEN, ADMIN_ID, CHANNEL_ID environment variables")

try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError as exc:
    raise ValueError("ADMIN_ID must be a number") from exc

bot = Bot(token=TOKEN, parse_mode=types.ParseMode.HTML)
dp = Dispatcher(bot)

participants = []
message_id = None
giveaway_title = ""
waiting_for_title = False
mini_finished = False

classic_step = None
classic_prize = ""
classic_winners_count = 1
classic_message_id = None
classic_participants = []
last_classic_message_id = None
last_classic_prize = ""
last_classic_participants = []
last_classic_winners = []

duel_step = None
duel_prize = ""
duel_message_id = None
duel_participants = []

admin_input_state = None


def target_to_public_link(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None

    text = str(value).strip()
    if not text:
        return None

    if text.startswith("http://") or text.startswith("https://"):
        return text

    if text.startswith("@") and len(text) > 1:
        return f"https://t.me/{text[1:]}"

    return None


def parse_chat_target(value: str) -> Optional[str]:
    text = (value or "").strip()
    if not text:
        return None

    if text.startswith("@"):
        return text

    match = re.match(r"https?://t\.me/([A-Za-z0-9_]{4,})/?$", text)
    if match:
        return f"@{match.group(1)}"

    if re.fullmatch(r"-?\d+", text):
        return str(int(text))

    return None


def normalize_join_link(value: str) -> Optional[str]:
    text = (value or "").strip()
    if not text:
        return None

    if text.startswith("@") and len(text) > 1:
        return f"https://t.me/{text[1:]}"

    if text.startswith("t.me/"):
        return f"https://{text}"

    if text.startswith("http://") or text.startswith("https://"):
        return text

    return None


def default_settings() -> dict:
    return {
        "subscription_required": True,
        "required_channel_id": str(CHANNEL_ID),
        "required_channel_link": target_to_public_link(CHANNEL_ID),
        "required_chat_id": str(REFERRAL_CHAT_ID),
        "required_chat_link": target_to_public_link(REFERRAL_CHAT_ID),
    }


def empty_referral_data() -> dict:
    return {
        "users": {},
        "invite_links": {},
        "joined": {},
        "pending_joined": {},
        "access": {},
        "settings": default_settings(),
    }


def load_referral_data() -> dict:
    if not REFERRAL_DATA_FILE.exists():
        return empty_referral_data()

    try:
        data = json.loads(REFERRAL_DATA_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logging.exception("Could not load referral data")
        return empty_referral_data()

    defaults = empty_referral_data()
    for key, default_value in defaults.items():
        if not isinstance(data.get(key), dict):
            data[key] = default_value.copy()

    settings = data.setdefault("settings", {})
    for key, default_value in default_settings().items():
        if key not in settings:
            settings[key] = default_value

    return data


def save_referral_data() -> None:
    temp_file = REFERRAL_DATA_FILE.with_suffix(".json.tmp")
    temp_file.write_text(
        json.dumps(referral_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temp_file.replace(REFERRAL_DATA_FILE)


referral_data = load_referral_data()


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def user_display(user: dict) -> str:
    username = user.get("username")
    name = user.get("name") or "Без имени"

    if username:
        return f"@{escape(username)}"
    return escape(name)


def access_settings() -> dict:
    settings = referral_data.setdefault("settings", {})
    for key, default_value in default_settings().items():
        settings.setdefault(key, default_value)
    return settings


def current_referral_chat_target() -> str:
    settings = access_settings()
    return settings.get("required_chat_id") or str(REFERRAL_CHAT_ID)


def access_state(user_id: int) -> dict:
    state = referral_data.setdefault("access", {}).setdefault(
        str(user_id),
        {
            "access_granted": False,
        },
    )
    state.setdefault("access_granted", False)
    return state


def remember_referral_user(user: types.User) -> dict:
    users = referral_data.setdefault("users", {})
    user_id = str(user.id)
    profile = users.setdefault(
        user_id,
        {
            "username": None,
            "name": "Без имени",
            "invite_link": None,
            "invite_target": None,
            "invites": 0,
        },
    )

    profile["username"] = user.username
    profile["name"] = user.first_name or "Без имени"
    profile.setdefault("invite_link", None)
    profile.setdefault("invite_target", None)
    profile.setdefault("invites", 0)
    return profile


def referral_invites_count(user_id: int) -> int:
    profile = referral_data.setdefault("users", {}).get(str(user_id), {})
    return int(profile.get("invites") or 0)


def referral_chat_matches(chat: types.Chat) -> bool:
    target_chat = str(current_referral_chat_target())

    if target_chat.startswith("@"):
        username = getattr(chat, "username", None)
        return username is not None and f"@{username}".lower() == target_chat.lower()

    return str(chat.id) == target_chat


def referral_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("🔗 Моя ссылка", callback_data="ref_link"),
        InlineKeyboardButton("🏆 Топ игроков", callback_data="ref_top"),
    )
    return kb


def referral_top_text() -> str:
    leaders = []
    for user_id, profile in referral_data.setdefault("users", {}).items():
        invites = int(profile.get("invites") or 0)
        if invites > 0:
            leaders.append((invites, user_id, profile))

    if not leaders:
        return (
            "🏆 <b>Топ игроков</b>\n\n"
            "Пока никто никого не пригласил.\n"
            "Забирай свою ссылку через /ref и приглашай людей в чат."
        )

    leaders.sort(key=lambda item: (-item[0], item[1]))
    lines = ["🏆 <b>Топ игроков по приглашениям</b>\n"]

    for place, (invites, _, profile) in enumerate(leaders[:REFERRAL_TOP_LIMIT], start=1):
        lines.append(f"{place}. {user_display(profile)} - <b>{invites}</b>")

    return "\n".join(lines)


def subscription_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    settings = access_settings()

    channel_link = settings.get("required_channel_link")
    chat_link = settings.get("required_chat_link")

    if channel_link:
        kb.add(InlineKeyboardButton("📢 Подписаться на канал", url=channel_link))
    if chat_link:
        kb.add(InlineKeyboardButton("💬 Вступить в чат", url=chat_link))

    kb.add(InlineKeyboardButton("✅ Проверить подписку", callback_data="check_subscriptions"))
    return kb


def access_settings_keyboard() -> InlineKeyboardMarkup:
    settings = access_settings()
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton(
            f"📌 Подписка: {'ОБЯЗАТЕЛЬНА' if settings.get('subscription_required') else 'ВЫКЛ'}",
            callback_data="toggle_subscription",
        ),
        InlineKeyboardButton("📢 Задать канал (@username / id)", callback_data="set_channel_target"),
        InlineKeyboardButton("🔗 Задать ссылку на канал", callback_data="set_channel_link"),
        InlineKeyboardButton("💬 Задать чат (@username / id)", callback_data="set_chat_target"),
        InlineKeyboardButton("🔗 Задать ссылку на чат", callback_data="set_chat_link"),
        InlineKeyboardButton("⬅️ Назад", callback_data="access_back"),
    )
    return kb


def access_settings_text() -> str:
    settings = access_settings()
    channel_target = settings.get("required_channel_id") or "не задан"
    chat_target = settings.get("required_chat_id") or "не задан"
    channel_link = settings.get("required_channel_link") or "не задана"
    chat_link = settings.get("required_chat_link") or "не задана"

    warning_lines = []
    if settings.get("subscription_required") and not settings.get("required_channel_link"):
        warning_lines.append("• Для канала лучше добавить ссылку, иначе пользователю будет нечего нажимать.")
    if settings.get("subscription_required") and not settings.get("required_chat_link"):
        warning_lines.append("• Для чата лучше добавить ссылку, иначе пользователю будет нечего нажимать.")

    text = (
        "⚙️ <b>Настройки доступа</b>\n\n"
        f"Обязательная подписка: <b>{'да' if settings.get('subscription_required') else 'нет'}</b>\n\n"
        f"Канал для проверки: <code>{escape(str(channel_target))}</code>\n"
        f"Ссылка на канал: {escape(channel_link)}\n\n"
        f"Чат для проверки и рефералок: <code>{escape(str(chat_target))}</code>\n"
        f"Ссылка на чат: {escape(chat_link)}"
    )

    if warning_lines:
        text += "\n\n" + "\n".join(warning_lines)

    return text


def admin_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("🎁 Создать МИНИ-РОЗЫГРЫШ", callback_data="create"),
        InlineKeyboardButton("☘️ Создать обычный розыгрыш", callback_data="classic_create"),
        InlineKeyboardButton("🎛 Активные розыгрыши", callback_data="active_giveaways"),
        InlineKeyboardButton("📣 Рассылка", callback_data="broadcast_start"),
        InlineKeyboardButton("⚙️ Настройки доступа", callback_data="access_settings"),
        InlineKeyboardButton("📊 Статус", callback_data="status"),
        InlineKeyboardButton("🧹 Сбросить черновик", callback_data="cancel"),
    )
    return kb


def join_keyboard(active: bool = True) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    if active:
        kb.add(
            InlineKeyboardButton(
                "Участвовать",
                callback_data="join",
                icon_custom_emoji_id="5890971177484029249",
            )
        )
    else:
        kb.add(InlineKeyboardButton("❌ Набор закрыт", callback_data="closed"))
    return kb


def classic_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.add(
        InlineKeyboardButton(
            "Участвовать",
            callback_data="classic_join",
            icon_custom_emoji_id="5217822164362739968",
        )
    )
    return kb


def finish_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("🏆 Завершить розыгрыш", callback_data="finish_classic"),
        InlineKeyboardButton("🗑 Удалить розыгрыш", callback_data="delete_classic"),
        InlineKeyboardButton("📋 Участники", callback_data="classic_members"),
    )
    return kb


def active_giveaways_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)

    if message_id:
        kb.add(
            InlineKeyboardButton("🎁 Завершить мини", callback_data="finish_mini"),
            InlineKeyboardButton("🗑 Удалить мини", callback_data="delete_mini"),
        )

    if classic_message_id:
        kb.add(
            InlineKeyboardButton("☘️ Завершить обычный", callback_data="finish_classic"),
            InlineKeyboardButton("🗑 Удалить обычный", callback_data="delete_classic"),
        )

    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="active_back"))
    return kb


def reset_draft() -> None:
    global waiting_for_title, classic_step, classic_prize, classic_winners_count, admin_input_state

    waiting_for_title = False
    classic_step = None
    classic_prize = ""
    classic_winners_count = 1
    admin_input_state = None


def mini_caption() -> str:
    gift = premium_emoji_id("5384108682290152083", "🎁")
    trophy = premium_emoji_id("5291914649481007565", "🏆")
    pointer = premium_emoji_id("5220049530107475342", "👉")
    players_icon = premium_emoji_id("5253539825360843975", "😀")
    text = (
        f"{gift} <b>МИНИ-ИГРА НА 6 ИГРОКОВ ОТ ИЛЮШКИ</b>\n\n"
        f"{trophy} <b>ПРИЗ:</b> {escape(giveaway_title)}\n\n"
        f"{pointer} <b>УЧАСТВОВАТЬ ТУТ</b> @brazers_promo\n\n"
        f"{players_icon} <b>МИНИ-ИЛЮШКИ</b> ({len(participants)}/{MAX_PARTICIPANTS}):\n"
    )

    if not participants:
        return text + "(пусто)"

    for participant in participants:
        text += f"{participant['number']}. {user_display(participant)}\n"

    return text


def premium_emoji(name: str, fallback: str) -> str:
    emoji_id = (PREMIUM_EMOJI_IDS.get(name) or "").strip()
    if not re.fullmatch(r"\d+", emoji_id):
        return fallback
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def premium_emoji_id(emoji_id: str, fallback: str) -> str:
    if not re.fullmatch(r"\d+", (emoji_id or "").strip()):
        return fallback
    return f'<tg-emoji emoji-id="{emoji_id}">{fallback}</tg-emoji>'


def admin_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("🎁 Создать МИНИ-РОЗЫГРЫШ", callback_data="create"),
        InlineKeyboardButton("☘️ Создать обычный розыгрыш", callback_data="classic_create"),
        InlineKeyboardButton("⚔️ Создать дуэль на 2", callback_data="duel_create"),
        InlineKeyboardButton("🎛 Активные розыгрыши", callback_data="active_giveaways"),
        InlineKeyboardButton("📣 Рассылка", callback_data="broadcast_start"),
        InlineKeyboardButton("⚙️ Настройки доступа", callback_data="access_settings"),
        InlineKeyboardButton("📊 Статус", callback_data="status"),
        InlineKeyboardButton("🧹 Сбросить черновик", callback_data="cancel"),
    )
    if last_classic_message_id and last_classic_winners:
        kb.add(InlineKeyboardButton("🎲 Рерол большого розыгрыша", callback_data="classic_reroll_menu"))
    return kb


def duel_keyboard(active: bool = True) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    if active:
        kb.add(
            InlineKeyboardButton(
                "Войти в дуэль",
                callback_data="duel_join",
                icon_custom_emoji_id="5454109785857205810",
            )
        )
    else:
        kb.add(InlineKeyboardButton("❌ Дуэль закрыта", callback_data="duel_closed"))
    return kb


def finish_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("🏆 Завершить розыгрыш", callback_data="finish_classic_v2"),
        InlineKeyboardButton("🗑 Удалить розыгрыш", callback_data="delete_classic_v2"),
        InlineKeyboardButton("📋 Участники", callback_data="classic_members_v2"),
    )
    if last_classic_message_id and last_classic_winners:
        kb.add(InlineKeyboardButton("🎲 Рерол", callback_data="classic_reroll_menu"))
    return kb


def classic_reroll_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    for index, winner in enumerate(last_classic_winners):
        kb.add(
            InlineKeyboardButton(
                f"🎲 {index + 1}. {user_display(winner)}",
                callback_data=f"classic_reroll_pick:{index}",
            )
        )
    return kb


def active_giveaways_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)

    if message_id:
        kb.add(
            InlineKeyboardButton("🎁 Завершить мини", callback_data="finish_mini"),
            InlineKeyboardButton("🗑 Удалить мини", callback_data="delete_mini"),
        )

    if classic_message_id:
        kb.add(
            InlineKeyboardButton("☘️ Завершить обычный", callback_data="finish_classic_v2"),
            InlineKeyboardButton("🗑 Удалить обычный", callback_data="delete_classic_v2"),
        )

    if duel_message_id:
        kb.add(
            InlineKeyboardButton("⚔️ Завершить дуэль", callback_data="finish_duel_v2"),
            InlineKeyboardButton("🗑 Удалить дуэль", callback_data="delete_duel_v2"),
        )

    kb.add(InlineKeyboardButton("⬅️ Назад", callback_data="active_back"))
    return kb


def reset_draft() -> None:
    global waiting_for_title, classic_step, classic_prize, classic_winners_count, duel_step, admin_input_state

    waiting_for_title = False
    classic_step = None
    classic_prize = ""
    classic_winners_count = 1
    duel_step = None
    admin_input_state = None


def classic_giveaway_caption(prize: str, winners_count: int) -> str:
    classic_title = premium_emoji_id("5442939099906325301", "☘️")
    classic_pointer = premium_emoji_id("5456140674028019486", "👉")
    trophy = premium_emoji("trophy", "🏆")
    return (
        f"{classic_title} <b>{escape(prize)} ОТ ИЛЮШКИ</b>\n\n"
        f"{classic_pointer} <b>УЧАСТВОВАТЬ ТУТ</b> @brazers_promo\n\n"
        f"{trophy} Победителей: {winners_count}"
    )


def classic_result_caption(prize: str, winners: list[dict]) -> str:
    classic_title = premium_emoji_id("5442939099906325301", "☘️")
    classic_pointer = premium_emoji_id("5456140674028019486", "👉")
    classic_winners = premium_emoji_id("5217822164362739968", "✨")
    winners_text = "\n".join(user_display(winner) for winner in winners)
    return (
        f"{classic_title} <b>{escape(prize)} ОТ ИЛЮШКИ</b>\n\n"
        f"{classic_pointer} <b>УЧАСТВОВАТЬ ТУТ</b> @brazers_promo\n\n"
        f"{classic_winners} <b>Победители:</b>\n{winners_text}"
    )


def duel_caption() -> str:
    swords = premium_emoji_id("5454014806950429357", "⚔️")
    trophy = premium_emoji_id("5276032951342088188", "🏆")
    dice = premium_emoji_id("5890924594268737731", "🎲")
    players_icon = premium_emoji_id("5206523956537865948", "👥")
    text = (
        f"{swords} <b>ДУЭЛЬ НА 2 ИГРОКОВ</b>\n\n"
        f"{trophy} <b>ПРИЗ:</b> {escape(duel_prize)}\n\n"
        f"{dice} Как только зайдут два игрока, бот сам бросит кубики.\n\n"
        f"{players_icon} <b>Дуэлянты</b> ({len(duel_participants)}/2):\n"
    )

    if not duel_participants:
        return text + "(пусто)"

    for index, participant in enumerate(duel_participants, start=1):
        text += f"{index}. {user_display(participant)}\n"

    return text


def duel_result_caption(winner: dict, loser: dict, round_lines: list[str]) -> str:
    swords = premium_emoji_id("5427168083074628963", "⚔️")
    sparkles = premium_emoji_id("5461151367559141950", "✨")
    broken_heart = premium_emoji_id("5249244862359812334", "💔")
    trophy = premium_emoji_id("5206523956537865948", "🏆")
    return (
        f"{swords} <b>ДУЭЛЬ ЗАВЕРШЕНА</b>\n\n"
        f"{sparkles} Победитель: {user_display(winner)}\n"
        f"{broken_heart} Не повезло: {user_display(loser)}\n\n"
        + "\n".join(round_lines)
        + f"\n\n{trophy} <b>ПРИЗ:</b> {escape(duel_prize)}"
    )


def copy_users(users: list[dict]) -> list[dict]:
    return [user.copy() for user in users]


def reset_last_classic_result() -> None:
    global last_classic_message_id, last_classic_prize, last_classic_participants, last_classic_winners

    last_classic_message_id = None
    last_classic_prize = ""
    last_classic_participants = []
    last_classic_winners = []


def save_last_classic_result(source_message_id: int, prize: str, participants_list: list[dict], winners: list[dict]) -> None:
    global last_classic_message_id, last_classic_prize, last_classic_participants, last_classic_winners

    last_classic_message_id = source_message_id
    last_classic_prize = prize
    last_classic_participants = copy_users(participants_list)
    last_classic_winners = copy_users(winners)


async def update_duel_message() -> None:
    if not duel_message_id:
        return

    await bot.edit_message_caption(
        chat_id=CHANNEL_ID,
        message_id=duel_message_id,
        caption=duel_caption(),
        reply_markup=duel_keyboard(len(duel_participants) < 2),
    )


async def delete_duel_giveaway() -> None:
    global duel_message_id, duel_participants, duel_prize, duel_step

    if duel_message_id:
        try:
            await bot.delete_message(chat_id=CHANNEL_ID, message_id=duel_message_id)
        except Exception:
            logging.exception("Could not delete duel giveaway message")

    duel_message_id = None
    duel_participants = []
    duel_prize = ""
    duel_step = None


async def finish_duel_giveaway() -> tuple[bool, str]:
    global duel_message_id, duel_participants, duel_prize, duel_step

    if not duel_message_id:
        return False, "Дуэль сейчас не активна."

    if len(duel_participants) < 2:
        return False, "Для дуэли нужно 2 участника."

    try:
        await bot.edit_message_reply_markup(
            chat_id=CHANNEL_ID,
            message_id=duel_message_id,
            reply_markup=duel_keyboard(False),
        )
    except Exception:
        logging.info("Could not disable duel buttons")

    rounds = []
    while True:
        first_roll_msg = await bot.send_dice(CHANNEL_ID)
        await asyncio.sleep(4)
        second_roll_msg = await bot.send_dice(CHANNEL_ID)
        await asyncio.sleep(4)

        first_roll = first_roll_msg.dice.value
        second_roll = second_roll_msg.dice.value
        rounds.append((first_roll, second_roll))

        if first_roll != second_roll:
            break

        await bot.send_message(
            CHANNEL_ID,
            f"{premium_emoji_id('5258420634785947640', '🤝')} Ничья в дуэли. Перебрасываем кубики...",
        )

    first_player = duel_participants[0]
    second_player = duel_participants[1]
    winner = first_player if rounds[-1][0] > rounds[-1][1] else second_player
    loser = second_player if winner["id"] == first_player["id"] else first_player

    round_lines = []
    for index, (first_roll, second_roll) in enumerate(rounds, start=1):
        title = f"Раунд {index}" if len(rounds) > 1 else "Броски"
        round_lines.append(
            f"{title}: {user_display(first_player)} <b>{first_roll}</b> vs {user_display(second_player)} <b>{second_roll}</b>"
        )

    await bot.edit_message_caption(
        chat_id=CHANNEL_ID,
        message_id=duel_message_id,
        caption=duel_result_caption(winner, loser, round_lines),
        reply_markup=None,
    )

    duel_message_id = None
    duel_participants = []
    duel_prize = ""
    duel_step = None
    return True, "Дуэль завершена."


async def delete_mini_giveaway() -> None:
    global message_id, participants, giveaway_title, mini_finished

    if message_id:
        try:
            await bot.delete_message(chat_id=CHANNEL_ID, message_id=message_id)
        except Exception:
            logging.exception("Could not delete mini giveaway message")

    message_id = None
    participants = []
    giveaway_title = ""
    mini_finished = False


async def finish_mini_giveaway() -> tuple[bool, str]:
    global message_id, mini_finished

    if not message_id:
        return False, "Мини-розыгрыш сейчас не активен."

    try:
        await bot.edit_message_reply_markup(
            chat_id=CHANNEL_ID,
            message_id=message_id,
            reply_markup=join_keyboard(False),
        )
    except Exception:
        logging.info("Could not disable mini giveaway buttons")

    if not participants:
        await bot.send_message(
            CHANNEL_ID,
            f"{premium_emoji('broken_heart', '❌')} Мини-розыгрыш завершен админом. Участников не было.",
        )
        message_id = None
        mini_finished = True
        return True, "Мини-розыгрыш завершен без участников."

    winner = random.choice(participants)
    await bot.send_message(
        CHANNEL_ID,
        (
            f"{premium_emoji_id('5240335244762038648', '🎁')} <b>МИНИ-ИГРА ОТ ИЛЮШКИ ЗАВЕРШЕНА АДМИНОМ!</b>\n\n"
            f"{premium_emoji_id('5217822164362739968', '🏆')} Победитель:\n{user_display(winner)}\n\n"
            f"{premium_emoji_id('5280602079285493593', '💰')} <b>ПРИЗ:</b> {escape(giveaway_title)}"
        ),
    )
    message_id = None
    mini_finished = True
    return True, "Мини-розыгрыш завершен."


async def delete_classic_giveaway() -> None:
    global classic_message_id, classic_participants, classic_prize, classic_winners_count, classic_step

    if classic_message_id:
        try:
            await bot.delete_message(chat_id=CHANNEL_ID, message_id=classic_message_id)
        except Exception:
            logging.exception("Could not delete classic giveaway message")

    classic_message_id = None
    classic_participants = []
    classic_prize = ""
    classic_winners_count = 1
    classic_step = None


def chat_id_for_api(value: Optional[str]):
    if value is None:
        return None

    text = str(value).strip()
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    return text


async def send_admin_panel(chat_id: int) -> None:
    await bot.send_message(chat_id, "Панель управления:", reply_markup=admin_keyboard())


async def send_status(chat_id: int) -> None:
    settings = access_settings()
    mini_status = "завершена" if mini_finished else "идет" if message_id else "не создана"
    classic_status = "идет" if classic_message_id else "не создан"

    await bot.send_message(
        chat_id,
        (
            "📊 <b>Статус</b>\n\n"
            f"Мини-игра: {mini_status}\n"
            f"Участников мини-игры: {len(participants)}/{MAX_PARTICIPANTS}\n"
            f"Обычный розыгрыш: {classic_status}\n"
            f"Участников обычного розыгрыша: {len(classic_participants)}\n\n"
            f"Подписка: {'обязательна' if settings.get('subscription_required') else 'выкл'}"
        ),
    )


async def send_access_settings(chat_id: int) -> None:
    await bot.send_message(chat_id, access_settings_text(), reply_markup=access_settings_keyboard())


async def send_active_giveaways(chat_id: int) -> None:
    mini_status = "активен" if message_id and not mini_finished else "завершен" if mini_finished else "не создан"
    classic_status = "активен" if classic_message_id else "не создан"

    await bot.send_message(
        chat_id,
        (
            "🎛 <b>Активные розыгрыши</b>\n\n"
            f"Мини-розыгрыш: {mini_status}\n"
            f"Участников мини: {len(participants)}\n\n"
            f"Обычный розыгрыш: {classic_status}\n"
            f"Участников обычного: {len(classic_participants)}"
        ),
        reply_markup=active_giveaways_keyboard(),
    )


async def send_status(chat_id: int) -> None:
    settings = access_settings()
    mini_status = "завершена" if mini_finished else "идет" if message_id else "не создана"
    classic_status = "идет" if classic_message_id else "завершен" if last_classic_message_id else "не создан"
    duel_status = "идет" if duel_message_id else "не создана"

    await bot.send_message(
        chat_id,
        (
            "📊 <b>Статус</b>\n\n"
            f"Мини-игра: {mini_status}\n"
            f"Участников мини-игры: {len(participants)}/{MAX_PARTICIPANTS}\n"
            f"Обычный розыгрыш: {classic_status}\n"
            f"Участников обычного розыгрыша: {len(classic_participants)}\n"
            f"Последних победителей для рерола: {len(last_classic_winners)}\n"
            f"Дуэль: {duel_status}\n"
            f"Участников дуэли: {len(duel_participants)}/2\n\n"
            f"Подписка: {'обязательна' if settings.get('subscription_required') else 'выкл'}"
        ),
    )


async def send_active_giveaways(chat_id: int) -> None:
    mini_status = "активен" if message_id and not mini_finished else "завершен" if mini_finished else "не создан"
    classic_status = "активен" if classic_message_id else "завершен" if last_classic_message_id else "не создан"
    duel_status = "активна" if duel_message_id else "не создана"

    await bot.send_message(
        chat_id,
        (
            "🎛 <b>Активные розыгрыши</b>\n\n"
            f"Мини-розыгрыш: {mini_status}\n"
            f"Участников мини: {len(participants)}\n\n"
            f"Обычный розыгрыш: {classic_status}\n"
            f"Участников обычного: {len(classic_participants)}\n"
            f"Рерол доступен: {'да' if last_classic_message_id and last_classic_winners else 'нет'}\n\n"
            f"Дуэль: {duel_status}\n"
            f"Участников дуэли: {len(duel_participants)}"
        ),
        reply_markup=active_giveaways_keyboard(),
    )


def get_broadcast_user_ids() -> list[int]:
    user_ids = set()

    for storage_key in ("users", "access"):
        for raw_user_id in referral_data.setdefault(storage_key, {}).keys():
            try:
                user_id = int(raw_user_id)
            except (TypeError, ValueError):
                continue

            if user_id != ADMIN_ID:
                user_ids.add(user_id)

    return sorted(user_ids)


async def send_subscription_prompt(chat_id: int, user: types.User, missing: Optional[list[str]] = None) -> None:
    remember_referral_user(user)
    settings = access_settings()

    if not settings.get("subscription_required"):
        await grant_user_access(user)
        await send_referral_profile(chat_id, user)
        return

    missing_text = ""
    if missing:
        missing_text = "\n\nНе хватает подписки на: <b>" + ", ".join(missing) + "</b>."

    await bot.send_message(
        chat_id,
        (
            "Подпишись на обязательные ресурсы и нажми кнопку проверки."
            + missing_text
        ),
        reply_markup=subscription_keyboard(),
    )


async def send_user_home(chat_id: int, user: types.User) -> None:
    remember_referral_user(user)
    save_referral_data()

    settings = access_settings()

    if settings.get("subscription_required"):
        missing = await get_missing_subscriptions(user.id)
        if missing:
            await send_subscription_prompt(chat_id, user, missing)
            return

    await grant_user_access(user)
    await send_referral_profile(chat_id, user)


async def get_missing_subscriptions(user_id: int) -> list[str]:
    settings = access_settings()
    missing = []

    if not settings.get("subscription_required"):
        return missing

    checks = [
        ("канал", settings.get("required_channel_id")),
        ("чат", settings.get("required_chat_id")),
    ]

    for label, target in checks:
        if not target:
            continue

        try:
            member = await bot.get_chat_member(chat_id_for_api(target), user_id)
        except Exception:
            logging.exception("Could not check subscription for %s in %s", user_id, target)
            missing.append(label)
            continue

        status = getattr(member, "status", None)
        if status in {"left", "kicked"}:
            missing.append(label)

    return missing


async def grant_user_access(user: types.User) -> None:
    state = access_state(user.id)
    state["access_granted"] = True
    save_referral_data()
    await confirm_pending_referral(user)


async def ensure_callback_access(callback: types.CallbackQuery) -> bool:
    user = callback.from_user
    remember_referral_user(user)
    save_referral_data()

    settings = access_settings()

    if settings.get("subscription_required"):
        missing = await get_missing_subscriptions(user.id)
        if missing:
            await callback.answer("Сначала подпишись на канал и чат, потом нажми кнопку участия еще раз", show_alert=True)
            try:
                await send_subscription_prompt(user.id, user, missing)
            except Exception:
                logging.info("Could not send subscription prompt to %s", user.id)
            return False

    await grant_user_access(user)
    return True


async def get_personal_invite_link(user: types.User) -> str:
    profile = remember_referral_user(user)
    user_id = str(user.id)
    link = profile.get("invite_link")
    current_target = current_referral_chat_target()

    if link and profile.get("invite_target") == current_target:
        invite_links = referral_data.setdefault("invite_links", {})
        if invite_links.get(link) != user_id:
            invite_links[link] = user_id
            save_referral_data()
        return link

    old_link = profile.get("invite_link")
    if old_link:
        invite_links = referral_data.setdefault("invite_links", {})
        if invite_links.get(old_link) == user_id:
            invite_links.pop(old_link, None)

    try:
        invite = await bot.create_chat_invite_link(
            chat_id=chat_id_for_api(current_target),
            name=f"ref_{user.id}",
        )
    except TypeError:
        invite = await bot.create_chat_invite_link(chat_id=chat_id_for_api(current_target))
    link = invite.invite_link

    profile["invite_link"] = link
    profile["invite_target"] = current_target
    referral_data.setdefault("invite_links", {})[link] = user_id
    save_referral_data()

    return link


async def send_referral_profile(chat_id: int, user: types.User) -> None:
    try:
        link = await get_personal_invite_link(user)
    except Exception:
        logging.exception("Could not create referral link")
        await bot.send_message(
            chat_id,
            (
                "Не получилось создать персональную ссылку.\n"
                "Проверьте, что бот добавлен админом в чат и может приглашать пользователей."
            ),
        )
        return

    await bot.send_message(
        chat_id,
        (
            "🔗 <b>Твоя персональная ссылка для приглашения в чат:</b>\n"
            f"{escape(link)}\n\n"
            f"👥 Подтвержденных приглашений: <b>{referral_invites_count(user.id)}</b>\n\n"
            "Реферал засчитывается только после обязательной подписки приглашенного пользователя.\n"
            "Команды: /ref - моя ссылка, /top - топ игроков."
        ),
        reply_markup=referral_keyboard(),
    )


async def confirm_pending_referral(user: types.User) -> bool:
    remember_referral_user(user)
    user_id = str(user.id)

    joined = referral_data.setdefault("joined", {})
    if user_id in joined:
        save_referral_data()
        return False

    inviter_id = referral_data.setdefault("pending_joined", {}).pop(user_id, None)
    if not inviter_id or inviter_id == user_id:
        save_referral_data()
        return False

    inviter = referral_data.setdefault("users", {}).get(inviter_id)
    if not inviter:
        save_referral_data()
        return False

    inviter["invites"] = int(inviter.get("invites") or 0) + 1
    joined[user_id] = inviter_id
    save_referral_data()

    try:
        await bot.send_message(
            int(inviter_id),
            (
                f"🎉 Приглашение подтверждено: {user_display({'username': user.username, 'name': user.first_name})}.\n"
                f"Всего подтвержденных приглашений: <b>{inviter['invites']}</b>"
            ),
        )
    except Exception:
        logging.info("Could not notify inviter %s", inviter_id)

    return True


async def register_referral_join(user: types.User, invite_link) -> bool:
    remember_referral_user(user)

    link = getattr(invite_link, "invite_link", None) if invite_link else None
    user_id = str(user.id)
    inviter_id = referral_data.setdefault("invite_links", {}).get(link)

    if not link or not inviter_id or inviter_id == user_id:
        save_referral_data()
        return False

    if user_id in referral_data.setdefault("joined", {}):
        save_referral_data()
        return False

    pending_joined = referral_data.setdefault("pending_joined", {})
    if user_id in pending_joined:
        save_referral_data()
        return False

    inviter = referral_data.setdefault("users", {}).get(inviter_id)
    if not inviter:
        save_referral_data()
        return False

    pending_joined[user_id] = inviter_id
    save_referral_data()

    try:
        await bot.send_message(
            int(inviter_id),
            (
                f"👀 По твоей ссылке зашел {user_display({'username': user.username, 'name': user.first_name})}.\n"
                "Приглашение засчитается после обязательной подписки."
            ),
        )
    except Exception:
        logging.info("Could not notify inviter %s", inviter_id)

    return True


@dp.message_handler(commands=["start", "panel"])
async def start(message: types.Message):
    if is_admin(message.from_user.id):
        await send_admin_panel(message.chat.id)
        return

    await send_user_home(message.chat.id, message.from_user)


@dp.message_handler(commands=["ref", "link"])
async def referral_link_command(message: types.Message):
    if is_admin(message.from_user.id):
        await send_admin_panel(message.chat.id)
        return

    await send_user_home(message.chat.id, message.from_user)


@dp.message_handler(commands=["top", "leaders"])
async def referral_top_command(message: types.Message):
    if is_admin(message.from_user.id):
        await send_admin_panel(message.chat.id)
        return

    remember_referral_user(message.from_user)
    save_referral_data()

    if access_settings().get("subscription_required"):
        missing = await get_missing_subscriptions(message.from_user.id)
        if missing:
            await send_subscription_prompt(message.chat.id, message.from_user, missing)
            return

    await grant_user_access(message.from_user)
    await message.answer(referral_top_text(), reply_markup=referral_keyboard())


@dp.callback_query_handler(lambda c: c.data == "ref_link")
async def referral_link_button(callback: types.CallbackQuery):
    await send_user_home(callback.message.chat.id, callback.from_user)
    await callback.answer()


@dp.callback_query_handler(lambda c: c.data == "ref_top")
async def referral_top_button(callback: types.CallbackQuery):
    if access_settings().get("subscription_required"):
        missing = await get_missing_subscriptions(callback.from_user.id)
        if missing:
            await send_subscription_prompt(callback.message.chat.id, callback.from_user, missing)
            await callback.answer("Сначала подпишись на обязательные ресурсы", show_alert=True)
            return

    await grant_user_access(callback.from_user)
    remember_referral_user(callback.from_user)
    save_referral_data()
    await callback.message.answer(referral_top_text(), reply_markup=referral_keyboard())
    await callback.answer()


@dp.callback_query_handler(lambda c: c.data == "check_subscriptions")
async def check_subscriptions(callback: types.CallbackQuery):
    if is_admin(callback.from_user.id):
        await callback.answer("Админу проверка подписки не нужна")
        return

    missing = await get_missing_subscriptions(callback.from_user.id)
    if missing:
        await send_subscription_prompt(callback.message.chat.id, callback.from_user, missing)
        await callback.answer("Подписка еще не подтверждена", show_alert=True)
        return

    await grant_user_access(callback.from_user)
    await callback.message.answer("✅ Доступ открыт. Теперь можешь пользоваться реферальной системой.")
    await send_referral_profile(callback.message.chat.id, callback.from_user)
    await callback.answer("Подписка подтверждена")


@dp.message_handler(commands=["cancel"])
async def cancel_command(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    reset_draft()
    await message.answer("Черновик сброшен.", reply_markup=admin_keyboard())


@dp.message_handler(commands=["status"])
async def status_command(message: types.Message):
    if not is_admin(message.from_user.id):
        return

    await send_status(message.chat.id)


@dp.callback_query_handler(lambda c: c.data == "status")
async def status_button(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await send_status(callback.message.chat.id)
    await callback.answer()


@dp.callback_query_handler(lambda c: c.data == "cancel")
async def cancel_button(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    reset_draft()
    await callback.message.answer("Черновик сброшен.", reply_markup=admin_keyboard())
    await callback.answer()


@dp.callback_query_handler(lambda c: c.data == "access_settings")
async def access_settings_button(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    reset_draft()
    await send_access_settings(callback.message.chat.id)
    await callback.answer()


@dp.callback_query_handler(lambda c: c.data == "active_giveaways")
async def active_giveaways_button(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    await send_active_giveaways(callback.message.chat.id)
    await callback.answer()


@dp.callback_query_handler(lambda c: c.data == "toggle_subscription")
async def toggle_subscription(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    settings = access_settings()
    settings["subscription_required"] = not settings.get("subscription_required")
    save_referral_data()
    await send_access_settings(callback.message.chat.id)
    await callback.answer("Настройка обновлена")


@dp.callback_query_handler(lambda c: c.data == "broadcast_start")
async def broadcast_start(callback: types.CallbackQuery):
    global admin_input_state

    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    reset_draft()
    admin_input_state = "broadcast_message"
    await callback.message.answer(
        "Пришли сообщение для рассылки.\n"
        "Можно отправить текст, фото, видео, стикер или пересланный пост."
    )
    await callback.answer()


@dp.callback_query_handler(lambda c: c.data == "set_channel_target")
async def set_channel_target(callback: types.CallbackQuery):
    global admin_input_state

    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    reset_draft()
    admin_input_state = "set_channel_target"
    await callback.message.answer("Пришли @username или id канала для обязательной подписки.")
    await callback.answer()


@dp.callback_query_handler(lambda c: c.data == "set_channel_link")
async def set_channel_link(callback: types.CallbackQuery):
    global admin_input_state

    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    reset_draft()
    admin_input_state = "set_channel_link"
    await callback.message.answer("Пришли ссылку на канал: https://t.me/... или @username")
    await callback.answer()


@dp.callback_query_handler(lambda c: c.data == "set_chat_target")
async def set_chat_target(callback: types.CallbackQuery):
    global admin_input_state

    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    reset_draft()
    admin_input_state = "set_chat_target"
    await callback.message.answer("Пришли @username или id чата для проверки и рефералок.")
    await callback.answer()


@dp.callback_query_handler(lambda c: c.data == "set_chat_link")
async def set_chat_link(callback: types.CallbackQuery):
    global admin_input_state

    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    reset_draft()
    admin_input_state = "set_chat_link"
    await callback.message.answer("Пришли ссылку на чат: https://t.me/... или @username")
    await callback.answer()


@dp.callback_query_handler(lambda c: c.data == "active_back")
async def active_back(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    reset_draft()
    await send_admin_panel(callback.message.chat.id)
    await callback.answer()


@dp.callback_query_handler(lambda c: c.data == "access_back")
async def access_back(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    reset_draft()
    await send_admin_panel(callback.message.chat.id)
    await callback.answer()


@dp.callback_query_handler(lambda c: c.data == "create")
async def create_giveaway(callback: types.CallbackQuery):
    global waiting_for_title

    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    reset_draft()
    waiting_for_title = True
    await callback.message.answer(
        "✏️ Пришлите приз мини-розыгрыша.\n"
        "Например: 💰 500 рублей или 🎮 игровая подписка"
    )
    await callback.answer()


@dp.callback_query_handler(lambda c: c.data == "classic_create")
async def classic_create(callback: types.CallbackQuery):
    global classic_step

    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    reset_draft()
    classic_step = "prize"
    await callback.message.answer("✏️ Введите приз обычного розыгрыша:")
    await callback.answer()


@dp.callback_query_handler(lambda c: c.data == "duel_create")
async def duel_create(callback: types.CallbackQuery):
    global duel_step

    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    reset_draft()
    duel_step = "prize"
    await callback.message.answer("⚔️ Введите приз для дуэли на 2 игроков:")
    await callback.answer()


@dp.message_handler(lambda message: is_admin(message.from_user.id) and admin_input_state == "broadcast_message", content_types=types.ContentTypes.ANY)
async def process_admin_broadcast(message: types.Message):
    global admin_input_state

    user_ids = get_broadcast_user_ids()
    if not user_ids:
        admin_input_state = None
        await message.answer("Некому отправлять рассылку. Пользователей в базе пока нет.", reply_markup=admin_keyboard())
        return

    sent_count = 0
    failed_count = 0

    for user_id in user_ids:
        try:
            await bot.copy_message(
                chat_id=user_id,
                from_chat_id=message.chat.id,
                message_id=message.message_id,
            )
            sent_count += 1
        except Exception:
            failed_count += 1

    admin_input_state = None
    await message.answer(
        f"✅ Рассылка завершена.\nУспешно: {sent_count}\nНе доставлено: {failed_count}",
        reply_markup=admin_keyboard(),
    )


@dp.message_handler(lambda message: is_admin(message.from_user.id), content_types=types.ContentTypes.ANY)
async def process_admin_text(message: types.Message):
    global admin_input_state, classic_message_id, classic_participants, classic_prize, classic_step
    global classic_winners_count, giveaway_title, message_id, mini_finished
    global participants, waiting_for_title, duel_step, duel_prize, duel_message_id, duel_participants

    settings = access_settings()

    if admin_input_state == "set_channel_target":
        target = parse_chat_target(message.text or "")
        if not target:
            await message.answer("Не смог понять канал. Пришли @username, ссылку вида https://t.me/name или id.")
            return

        settings["required_channel_id"] = target
        settings["required_channel_link"] = settings.get("required_channel_link") or target_to_public_link(target)
        admin_input_state = None
        save_referral_data()
        await message.answer("✅ Канал сохранен.", reply_markup=access_settings_keyboard())
        return

    if admin_input_state == "set_channel_link":
        link = normalize_join_link(message.text or "")
        if not link:
            await message.answer("Не смог понять ссылку. Пришли https://t.me/... или @username.")
            return

        settings["required_channel_link"] = link
        admin_input_state = None
        save_referral_data()
        await message.answer("✅ Ссылка на канал сохранена.", reply_markup=access_settings_keyboard())
        return

    if admin_input_state == "set_chat_target":
        target = parse_chat_target(message.text or "")
        if not target:
            await message.answer("Не смог понять чат. Пришли @username, ссылку вида https://t.me/name или id.")
            return

        settings["required_chat_id"] = target
        settings["required_chat_link"] = settings.get("required_chat_link") or target_to_public_link(target)
        admin_input_state = None
        save_referral_data()
        await message.answer("✅ Чат сохранен. Теперь новые реферальные ссылки будут вести туда.", reply_markup=access_settings_keyboard())
        return

    if admin_input_state == "set_chat_link":
        link = normalize_join_link(message.text or "")
        if not link:
            await message.answer("Не смог понять ссылку. Пришли https://t.me/... или @username.")
            return

        settings["required_chat_link"] = link
        admin_input_state = None
        save_referral_data()
        await message.answer("✅ Ссылка на чат сохранена.", reply_markup=access_settings_keyboard())
        return

    if classic_step == "prize":
        if not message.text:
            await message.answer("Пришлите приз текстом.")
            return

        classic_prize = (message.text or "").strip()
        if not classic_prize:
            await message.answer("Приз не должен быть пустым.")
            return

        classic_step = "winners"
        await message.answer("👥 Сколько победителей?")
        return

    if classic_step == "winners":
        if not message.text:
            await message.answer("Введите число текстом.")
            return

        try:
            classic_winners_count = int(message.text)
        except (TypeError, ValueError):
            await message.answer("Введите число.")
            return

        if classic_winners_count < 1:
            await message.answer("Победителей должно быть минимум 1.")
            return

        reset_last_classic_result()
        classic_participants = []
        msg = await bot.send_photo(
            CHANNEL_ID,
            photo=GIVEAWAY_PHOTO,
            caption=classic_giveaway_caption(classic_prize, classic_winners_count),
            reply_markup=classic_keyboard(),
        )

        classic_message_id = msg.message_id
        classic_step = None

        await bot.send_message(
            ADMIN_ID,
            "✅ Обычный розыгрыш создан.",
            reply_markup=finish_keyboard(),
        )
        return

    if duel_step == "prize":
        if not message.text:
            await message.answer("Пришлите приз текстом.")
            return

        duel_prize = (message.text or "").strip()
        if not duel_prize:
            await message.answer("Приз не должен быть пустым.")
            return

        duel_participants = []
        duel_step = None

        msg = await bot.send_photo(
            CHANNEL_ID,
            photo=GIVEAWAY_PHOTO,
            caption=duel_caption(),
            reply_markup=duel_keyboard(True),
        )
        duel_message_id = msg.message_id

        await message.answer("✅ Дуэль создана.", reply_markup=admin_keyboard())
        return

    if not waiting_for_title:
        return

    if not message.text:
        await message.answer("Пришлите приз текстом.")
        return

    giveaway_title = (message.text or "").strip()
    if not giveaway_title:
        await message.answer("Приз не должен быть пустым.")
        return

    participants = []
    mini_finished = False
    waiting_for_title = False

    msg = await bot.send_photo(
        CHANNEL_ID,
        photo=GIVEAWAY_PHOTO,
        caption=mini_caption(),
        reply_markup=join_keyboard(True),
    )
    message_id = msg.message_id

    await message.answer("✅ Мини-розыгрыш создан.", reply_markup=admin_keyboard())


@dp.callback_query_handler(lambda c: c.data == "classic_join")
async def classic_join(callback: types.CallbackQuery):
    global classic_participants

    if not classic_message_id:
        await callback.answer("Розыгрыш еще не создан")
        return

    if not await ensure_callback_access(callback):
        return

    user = callback.from_user

    if user.id in [p["id"] for p in classic_participants]:
        await callback.answer("Ты уже участвуешь")
        return

    classic_participants.append(
        {
            "id": user.id,
            "username": user.username,
            "name": user.first_name,
        }
    )

    await callback.answer(f"Ты участвуешь! Всего участников: {len(classic_participants)}")


@dp.callback_query_handler(lambda c: c.data == "classic_members")
async def classic_members(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if not classic_participants:
        await callback.message.answer("В обычном розыгрыше пока нет участников.")
        await callback.answer()
        return

    members = "\n".join(
        f"{index}. {user_display(participant)}"
        for index, participant in enumerate(classic_participants, start=1)
    )
    await callback.message.answer(f"📋 <b>Участники обычного розыгрыша:</b>\n{members}")
    await callback.answer()


@dp.callback_query_handler(lambda c: c.data == "finish_mini")
async def finish_mini(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    ok, text = await finish_mini_giveaway()
    await callback.answer(text, show_alert=not ok)
    await callback.message.answer(text, reply_markup=admin_keyboard())


@dp.callback_query_handler(lambda c: c.data == "delete_mini")
async def delete_mini(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if not message_id:
        await callback.answer("Мини-розыгрыш не активен", show_alert=True)
        return

    await delete_mini_giveaway()
    await callback.answer("Мини-розыгрыш удален")
    await callback.message.answer("✅ Мини-розыгрыш удален.", reply_markup=admin_keyboard())


async def update_message():
    if not message_id:
        return

    await bot.edit_message_caption(
        chat_id=CHANNEL_ID,
        message_id=message_id,
        caption=mini_caption(),
        reply_markup=join_keyboard(len(participants) < MAX_PARTICIPANTS),
    )


@dp.callback_query_handler(lambda c: c.data == "closed")
async def closed(callback: types.CallbackQuery):
    await callback.answer("Набор участников уже закрыт")


@dp.callback_query_handler(lambda c: c.data == "join")
async def join(callback: types.CallbackQuery):
    global message_id, mini_finished, participants

    if mini_finished:
        await callback.answer("Мини-игра уже завершена")
        return

    if not await ensure_callback_access(callback):
        return

    user = callback.from_user

    if len(participants) >= MAX_PARTICIPANTS:
        await callback.answer("Лимит участников достигнут")
        return

    if user.id in [p["id"] for p in participants]:
        await callback.answer("Ты уже участвуешь")
        return

    participants.append(
        {
            "id": user.id,
            "username": user.username,
            "name": user.first_name,
            "number": len(participants) + 1,
        }
    )

    await callback.answer(f"Ты участник №{len(participants)}")
    await update_message()

    if len(participants) != MAX_PARTICIPANTS:
        return

    mini_finished = True
    await bot.send_message(
        CHANNEL_ID,
        f"{premium_emoji_id('5890971177484029249', '🎲')} Набрано 6 МИНИ-ИЛЮШЕК! Определяем победителя...",
    )

    dice_msg = await bot.send_dice(CHANNEL_ID)
    await asyncio.sleep(4)

    dice_value = dice_msg.dice.value
    winner_index = min(dice_value, MAX_PARTICIPANTS) - 1
    winner = participants[winner_index]

    await bot.send_message(
        CHANNEL_ID,
        (
            f"{premium_emoji_id('5240335244762038648', '🎁')} <b>МИНИ-ИГРА ОТ ИЛЮШКИ ЗАВЕРШЕНА!</b>\n\n"
            f"{premium_emoji_id('5890971177484029249', '🎲')} Выпало число: <b>{dice_value}</b>\n\n"
            f"{premium_emoji_id('5217822164362739968', '🏆')} Победитель:\n{user_display(winner)}\n\n"
            f"{premium_emoji_id('5280602079285493593', '💰')} <b>ПРИЗ:</b> {escape(giveaway_title)}"
        ),
    )
    message_id = None


@dp.callback_query_handler(lambda c: c.data == "finish_classic")
async def finish_classic(callback: types.CallbackQuery):
    global classic_message_id, classic_participants

    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if not classic_message_id:
        await callback.answer("Обычный розыгрыш еще не создан")
        return

    if not classic_participants:
        await callback.answer("Нет участников")
        return

    winners = random.sample(
        classic_participants,
        min(classic_winners_count, len(classic_participants)),
    )
    winners_text = "\n".join(user_display(winner) for winner in winners)

    await bot.edit_message_caption(
        chat_id=CHANNEL_ID,
        message_id=classic_message_id,
        caption=(
            f"☘️ <b>{escape(classic_prize)} ОТ ИЛЮШКИ</b>\n\n"
            "👉 <b>УЧАСТВОВАТЬ ТУТ</b> @brazers_promo\n\n"
            f"✨ <b>Победители:</b>\n{winners_text}"
        ),
    )

    classic_message_id = None
    classic_participants = []
    await callback.answer("Розыгрыш завершен")
    await callback.message.answer("✅ Обычный розыгрыш завершен.", reply_markup=admin_keyboard())


@dp.callback_query_handler(lambda c: c.data == "delete_classic")
async def delete_classic(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if not classic_message_id:
        await callback.answer("Обычный розыгрыш не активен", show_alert=True)
        return

    await delete_classic_giveaway()
    await callback.answer("Обычный розыгрыш удален")
    await callback.message.answer("✅ Обычный розыгрыш удален.", reply_markup=admin_keyboard())


@dp.callback_query_handler(lambda c: c.data == "duel_closed")
async def duel_closed(callback: types.CallbackQuery):
    await callback.answer("Дуэль уже закрыта")


@dp.callback_query_handler(lambda c: c.data == "duel_join")
async def duel_join(callback: types.CallbackQuery):
    global duel_participants

    if not duel_message_id:
        await callback.answer("Дуэль еще не создана")
        return

    if not await ensure_callback_access(callback):
        return

    user = callback.from_user

    if len(duel_participants) >= 2:
        await callback.answer("Лимит игроков уже набран")
        return

    if user.id in [p["id"] for p in duel_participants]:
        await callback.answer("Ты уже в дуэли")
        return

    duel_participants.append(
        {
            "id": user.id,
            "username": user.username,
            "name": user.first_name,
        }
    )

    await callback.answer(f"Ты вошел в дуэль! Место: {len(duel_participants)}/2")
    await update_duel_message()

    if len(duel_participants) < 2:
        return

    await bot.send_message(
        CHANNEL_ID,
        f"{premium_emoji_id('5357593469760059395', '⚔️')} Дуэль собрана. Бросаем кубики...",
    )
    await finish_duel_giveaway()


@dp.callback_query_handler(lambda c: c.data == "classic_members_v2")
async def classic_members_v2(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    source = classic_participants or last_classic_participants
    title = "Участники обычного розыгрыша" if classic_participants else "Участники последнего завершенного розыгрыша"

    if not source:
        await callback.message.answer("В обычном розыгрыше пока нет участников.")
        await callback.answer()
        return

    members = "\n".join(
        f"{index}. {user_display(participant)}"
        for index, participant in enumerate(source, start=1)
    )
    await callback.message.answer(f"📋 <b>{title}:</b>\n{members}")
    await callback.answer()


@dp.callback_query_handler(lambda c: c.data == "finish_classic_v2")
async def finish_classic_v2(callback: types.CallbackQuery):
    global classic_message_id, classic_participants, classic_prize

    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if not classic_message_id:
        await callback.answer("Обычный розыгрыш еще не создан")
        return

    if not classic_participants:
        await callback.answer("Нет участников")
        return

    winners = random.sample(
        classic_participants,
        min(classic_winners_count, len(classic_participants)),
    )
    current_message_id = classic_message_id
    current_prize = classic_prize

    await bot.edit_message_caption(
        chat_id=CHANNEL_ID,
        message_id=current_message_id,
        caption=classic_result_caption(current_prize, winners),
        reply_markup=None,
    )

    save_last_classic_result(current_message_id, current_prize, classic_participants, winners)
    classic_message_id = None
    classic_participants = []
    classic_prize = ""

    await callback.answer("Розыгрыш завершен")
    await callback.message.answer("✅ Обычный розыгрыш завершен. Рерол доступен из админ-панели.", reply_markup=admin_keyboard())


@dp.callback_query_handler(lambda c: c.data == "delete_classic_v2")
async def delete_classic_v2(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if not classic_message_id:
        await callback.answer("Обычный розыгрыш не активен", show_alert=True)
        return

    await delete_classic_giveaway()
    await callback.answer("Обычный розыгрыш удален")
    await callback.message.answer("✅ Обычный розыгрыш удален.", reply_markup=admin_keyboard())


@dp.callback_query_handler(lambda c: c.data == "classic_reroll_menu")
async def classic_reroll_menu(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if not last_classic_message_id or not last_classic_winners:
        await callback.answer("Нет завершенного большого розыгрыша для рерола", show_alert=True)
        return

    await callback.message.answer(
        "🎲 Выбери победителя, которого нужно рерольнуть:",
        reply_markup=classic_reroll_keyboard(),
    )
    await callback.answer()


@dp.callback_query_handler(lambda c: c.data and c.data.startswith("classic_reroll_pick:"))
async def classic_reroll_pick(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if not last_classic_message_id or not last_classic_winners:
        await callback.answer("Рерол недоступен", show_alert=True)
        return

    try:
        winner_index = int(callback.data.split(":", 1)[1])
    except (IndexError, ValueError):
        await callback.answer("Не удалось определить победителя", show_alert=True)
        return

    if winner_index < 0 or winner_index >= len(last_classic_winners):
        await callback.answer("Такого победителя нет", show_alert=True)
        return

    current_winner = last_classic_winners[winner_index]
    busy_ids = {
        winner["id"]
        for index, winner in enumerate(last_classic_winners)
        if index != winner_index
    }
    candidate_pool = [
        participant
        for participant in last_classic_participants
        if participant["id"] not in busy_ids and participant["id"] != current_winner["id"]
    ]

    if not candidate_pool:
        await callback.answer("Нет свободных участников для замены", show_alert=True)
        return

    new_winner = random.choice(candidate_pool).copy()
    last_classic_winners[winner_index] = new_winner

    await bot.edit_message_caption(
        chat_id=CHANNEL_ID,
        message_id=last_classic_message_id,
        caption=classic_result_caption(last_classic_prize, last_classic_winners),
        reply_markup=None,
    )

    await callback.answer("Рерол выполнен")
    await callback.message.answer(
        f"🎲 Рерол выполнен.\nБыло: {user_display(current_winner)}\nСтало: {user_display(new_winner)}",
        reply_markup=admin_keyboard(),
    )


@dp.callback_query_handler(lambda c: c.data == "finish_duel_v2")
async def finish_duel_v2(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    ok, text = await finish_duel_giveaway()
    await callback.answer(text, show_alert=not ok)
    await callback.message.answer(text, reply_markup=admin_keyboard())


@dp.callback_query_handler(lambda c: c.data == "delete_duel_v2")
async def delete_duel_v2(callback: types.CallbackQuery):
    if not is_admin(callback.from_user.id):
        await callback.answer("Нет доступа", show_alert=True)
        return

    if not duel_message_id:
        await callback.answer("Дуэль не активна", show_alert=True)
        return

    await delete_duel_giveaway()
    await callback.answer("Дуэль удалена")
    await callback.message.answer("✅ Дуэль удалена.", reply_markup=admin_keyboard())


@dp.message_handler(content_types=types.ContentType.NEW_CHAT_MEMBERS)
async def track_referral_join_message(message: types.Message):
    if not referral_chat_matches(message.chat):
        return

    invite_link = getattr(message, "invite_link", None)
    for user in message.new_chat_members:
        await register_referral_join(user, invite_link)


if hasattr(dp, "chat_member_handler"):
    @dp.chat_member_handler()
    async def track_referral_chat_member(update):
        if not referral_chat_matches(update.chat):
            return

        old_status = getattr(update.old_chat_member, "status", None)
        new_status = getattr(update.new_chat_member, "status", None)
        joined_from_outside = old_status in {"left", "kicked"} and new_status in {
            "member",
            "restricted",
            "administrator",
            "creator",
        }

        if joined_from_outside:
            await register_referral_join(update.new_chat_member.user, getattr(update, "invite_link", None))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    executor.start_polling(
        dp,
        skip_updates=True,
        allowed_updates=["message", "callback_query", "chat_member"],
    )
