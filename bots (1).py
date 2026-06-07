import asyncio
import logging
import os
import random
from html import escape

from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils import executor


# --- ENV VARIABLES ---
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID_RAW = os.getenv("ADMIN_ID")
CHANNEL_ID = os.getenv("CHANNEL_ID")

MAX_PARTICIPANTS = 6
GIVEAWAY_PHOTO = "AgACAgIAAxkBAANSaiOILtbjI9uXPclOjby3azTEWqQAAqodaxu6QRlJLp2T9fXeiH0BAAMCAAN5AAM7BA"

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


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def user_display(user: dict) -> str:
    username = user.get("username")
    name = user.get("name") or "Без имени"

    if username:
        return f"@{escape(username)}"
    return escape(name)


def admin_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("🎁 Создать МИНИ-РОЗЫГРЫШ", callback_data="create"),
        InlineKeyboardButton("☘️ Создать обычный розыгрыш", callback_data="classic_create"),
        InlineKeyboardButton("📊 Статус", callback_data="status"),
        InlineKeyboardButton("🧹 Сбросить черновик", callback_data="cancel"),
    )
    return kb


def join_keyboard(active: bool = True) -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    if active:
        kb.add(InlineKeyboardButton("🎉 Участвовать", callback_data="join"))
    else:
        kb.add(InlineKeyboardButton("❌ Набор закрыт", callback_data="closed"))
    return kb


def classic_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🎉 Участвовать", callback_data="classic_join"))
    return kb


def finish_keyboard() -> InlineKeyboardMarkup:
    kb = InlineKeyboardMarkup(row_width=1)
    kb.add(
        InlineKeyboardButton("🏆 Завершить розыгрыш", callback_data="finish_classic"),
        InlineKeyboardButton("📋 Участники", callback_data="classic_members"),
    )
    return kb


def reset_draft() -> None:
    global waiting_for_title, classic_step, classic_prize, classic_winners_count

    waiting_for_title = False
    classic_step = None
    classic_prize = ""
    classic_winners_count = 1


def mini_caption() -> str:
    text = (
        "🎁 <b>МИНИ-ИГРА НА 6 ИГРОКОВ ОТ ИЛЮШКИ</b>\n\n"
        f"🏆 <b>ПРИЗ:</b> {escape(giveaway_title)}\n\n"
        "👉 <b>УЧАСТВОВАТЬ ТУТ</b> @brazers_promo\n\n"
        f"😈 <b>МИНИ-ИЛЮШКИ</b> ({len(participants)}/{MAX_PARTICIPANTS}):\n"
    )

    if not participants:
        return text + "(пусто)"

    for participant in participants:
        text += f"{participant['number']}. {user_display(participant)}\n"

    return text


async def send_admin_panel(chat_id: int) -> None:
    await bot.send_message(chat_id, "Панель управления:", reply_markup=admin_keyboard())


async def send_status(chat_id: int) -> None:
    mini_status = "завершена" if mini_finished else "идет" if message_id else "не создана"
    classic_status = "идет" if classic_message_id else "не создан"

    await bot.send_message(
        chat_id,
        (
            "📊 <b>Статус</b>\n\n"
            f"Мини-игра: {mini_status}\n"
            f"Участников мини-игры: {len(participants)}/{MAX_PARTICIPANTS}\n"
            f"Обычный розыгрыш: {classic_status}\n"
            f"Участников обычного розыгрыша: {len(classic_participants)}"
        ),
    )


@dp.message_handler(commands=["start", "panel"])
async def start(message: types.Message):
    if is_admin(message.from_user.id):
        await send_admin_panel(message.chat.id)
    else:
        await message.answer(
            "Это бот для розыгрышей @brazers_promo 🎁\n"
            "Создать такого бота: @tipo_privet67"
        )


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


@dp.message_handler(lambda message: is_admin(message.from_user.id))
async def process_admin_text(message: types.Message):
    global classic_message_id, classic_participants, classic_prize, classic_step
    global classic_winners_count, giveaway_title, message_id, mini_finished
    global participants, waiting_for_title

    if classic_step == "prize":
        classic_prize = message.text.strip()
        if not classic_prize:
            await message.answer("Приз не должен быть пустым.")
            return

        classic_step = "winners"
        await message.answer("👥 Сколько победителей?")
        return

    if classic_step == "winners":
        try:
            classic_winners_count = int(message.text)
        except ValueError:
            await message.answer("Введите число.")
            return

        if classic_winners_count < 1:
            await message.answer("Победителей должно быть минимум 1.")
            return

        classic_participants = []
        msg = await bot.send_photo(
            CHANNEL_ID,
            photo=GIVEAWAY_PHOTO,
            caption=(
                f"☘️ <b>{escape(classic_prize)} ОТ ИЛЮШКИ</b>\n\n"
                "👉 <b>УЧАСТВОВАТЬ ТУТ</b> @brazers_promo\n\n"
                f"🏆 Победителей: {classic_winners_count}"
            ),
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

    if not waiting_for_title:
        return

    giveaway_title = message.text.strip()
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
    global mini_finished, participants

    if mini_finished:
        await callback.answer("Мини-игра уже завершена")
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
        "🎲 Набрано 6 МИНИ-ИЛЮШЕК! Определяем победителя...",
    )

    dice_msg = await bot.send_dice(CHANNEL_ID)
    await asyncio.sleep(4)

    dice_value = dice_msg.dice.value
    winner_index = min(dice_value, MAX_PARTICIPANTS) - 1
    winner = participants[winner_index]

    await bot.send_message(
        CHANNEL_ID,
        (
            "🎁 <b>МИНИ-ИГРА ОТ ИЛЮШКИ ЗАВЕРШЕНА!</b>\n\n"
            f"🎲 Выпало число: <b>{dice_value}</b>\n\n"
            f"🏆 Победитель:\n{user_display(winner)}\n\n"
            f"💰 <b>ПРИЗ:</b> {escape(giveaway_title)}"
        ),
    )


@dp.callback_query_handler(lambda c: c.data == "finish_classic")
async def finish_classic(callback: types.CallbackQuery):
    global classic_message_id

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
    await callback.answer("Розыгрыш завершен")
    await callback.message.answer("✅ Обычный розыгрыш завершен.", reply_markup=admin_keyboard())


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    executor.start_polling(dp, skip_updates=True)
