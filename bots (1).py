import logging
import asyncio
import os
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.utils import executor

# --- ENV VARIABLES ---
TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID"))
CHANNEL_ID = os.getenv("CHANNEL_ID")
MAX_PARTICIPANTS = 6
GIVEAWAY_PHOTO = "AgACAgIAAxkBAANSaiOILtbjI9uXPclOjby3azTEWqQAAqodaxu6QRlJLp2T9fXeiH0BAAMCAAN5AAM7BA"

if not TOKEN or not ADMIN_ID or not CHANNEL_ID:
    raise ValueError("Set BOT_TOKEN, ADMIN_ID, CHANNEL_ID environment variables")

bot = Bot(token=TOKEN)
dp = Dispatcher(bot)

participants = []
message_id = None
giveaway_title = ""
waiting_for_title = False

def admin_keyboard():
    kb = InlineKeyboardMarkup()
    kb.add(InlineKeyboardButton("🎁 Создать МИНИ РОЗЫГРЫШ", callback_data="create"))
    return kb

def join_keyboard(active=True):
    kb = InlineKeyboardMarkup()
    if active:
        kb.add(InlineKeyboardButton("🎉 Участвовать", callback_data="join"))
    else:
        kb.add(InlineKeyboardButton("❌ Набор закрыт", callback_data="closed"))
    return kb

@dp.message_handler(commands=['start'])
async def start(message: types.Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer("Панель управления:", reply_markup=admin_keyboard())
    else:
        await message.answer("Этот бот для розыгрышей @brazers_promo 🎁 СОЗДАТЬ ТАКОГО ЖЕ @tipo_privet67")
            
@dp.callback_query_handler(lambda c: c.data == "create")
async def create_giveaway(callback: types.CallbackQuery):
    global waiting_for_title

    if callback.from_user.id != ADMIN_ID:
        return

    waiting_for_title = True
    await callback.message.answer("✏️ Пришлите ПРИЗ розыгрыша:")
    await callback.answer()

@dp.message_handler(lambda message: message.from_user.id == ADMIN_ID)
async def process_giveaway_title(message: types.Message):
    global participants, message_id, giveaway_title, waiting_for_title

    if not waiting_for_title:
        return

    giveaway_title = message.text
    waiting_for_title = False
    participants = []

    msg = await bot.send_photo(
    CHANNEL_ID,
    photo=GIVEAWAY_PHOTO,
    caption=(
        f"🎁 МИНИ-ИГРА 6 ИГРОКОВ ОТ ИЛЮШКИ\n\n"
        f"🏆 ПРИЗ: {giveaway_title}\n\n"
        f"УЧАСТВОВАТЬ ТУТ @brazers_promo\n\n"
        f"МИНИ-ИЛЮШКИ (0/{MAX_PARTICIPANTS}):\n"
        f"(пусто)"
    ),
    reply_markup=join_keyboard(True)
)
    
    message_id = msg.message_id
    await message.answer("✅ Розыгрыш создан и опубликован!")

async def update_message():
    text = f"🎁 МИНИ-ИГРА 6 ИГРОКОВ ОТ ИЛЮШКИ ПРИЗ:{giveaway_title}\nУЧАСТВОВАТЬ ТУТ @brazers_promo\nМИНИ-ИЛЮШКИ ({len(participants)}/{MAX_PARTICIPANTS}):\n"

    if not participants:
        text += "(пусто)"
    else:
        for p in participants:
            name = p['username'] or p['name']
            if p['username']:
                text += f"{p['number']}. @{name}\n"
            else:
                text += f"{p['number']}. {name}\n"

    await bot.edit_message_caption(
    chat_id=CHANNEL_ID,
    message_id=message_id,
    caption=text,
    reply_markup=join_keyboard(len(participants) < MAX_PARTICIPANTS)
)

@dp.callback_query_handler(lambda c: c.data == "closed")
async def closed(callback: types.CallbackQuery):
    await callback.answer("Набор участников уже закрыт")
    
@dp.callback_query_handler(lambda c: c.data == "join")
async def join(callback: types.CallbackQuery):
    global participants

    user = callback.from_user

    if len(participants) >= MAX_PARTICIPANTS:
        await callback.answer("Лимит участников достигнут")
        return

    if user.id in [p['id'] for p in participants]:
        await callback.answer("Ты уже участвуешь")
        return

    participants.append({
        "id": user.id,
        "username": user.username,
        "name": user.first_name,
        "number": len(participants) + 1
    })

    await callback.answer(f"Ты участник №{len(participants)}")
    await update_message()

    if len(participants) == MAX_PARTICIPANTS:
        await bot.send_message(CHANNEL_ID, "🎲 Набрано 6 МИНИ-ИЛЮШОК! Определяем победителя...")

        dice_msg = await bot.send_dice(CHANNEL_ID)
        await asyncio.sleep(4)

        dice_value = dice_msg.dice.value
        winner_index = min(dice_value, MAX_PARTICIPANTS) - 1
        winner = participants[winner_index]

        name = winner['username'] or winner['name']
        winner_tag = f"@{name}" if winner['username'] else name

        await bot.send_message(
            CHANNEL_ID,
            f"🎁 МИНИ-ИГРА ОТ ИЛЮШКИ ЗАВЕРШЕНА!\n\n"
            f"🎲 Выпало число: {dice_value}\n\n"
            f"🏆 Победитель:\n{winner_tag}"
            f"💰 ПРИЗ:{giveaway_title}"
        )

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    executor.start_polling(dp, skip_updates=True)
