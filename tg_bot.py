import os
import asyncio
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.types import Message, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from dotenv import load_dotenv

import database
import fetch_talk

import asyncio
import logging
from aiogram import Bot, Dispatcher, types
from aiogram.filters import CommandStart
from aiohttp_socks import ProxyConnector

# Включаем логирование, чтобы видеть возможные ошибки подключения
logging.basicConfig(level=logging.INFO)

load_dotenv()
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- Чтение списка админов ---
raw_admins = os.getenv("ADMIN_USERNAMES", "")
# Превращаем строку "admin1,admin2" в список ['admin1', 'admin2']
ADMIN_USERNAMES = [name.strip().lower() for name in raw_admins.split(",") if name.strip()]


class MeetStates(StatesGroup):
    wait_for_link = State()
    wait_for_additional_information = State()


def get_auth_keyboard():
    button = KeyboardButton(text="🔐 Авторизоваться")
    return ReplyKeyboardMarkup(keyboard=[[button]], resize_keyboard=True)


def get_main_keyboard():
    button = KeyboardButton(text="📅 Проверить встречу")
    return ReplyKeyboardMarkup(keyboard=[[button]], resize_keyboard=True)


def get_approuve_keyboard():
    button = KeyboardButton(text="Подтверждаю")
    return ReplyKeyboardMarkup(keyboard=[[button]], resize_keyboard=True)


# --- Блок Администратора ---
@dp.message(Command("add"))
async def cmd_add_user(message: Message):
    username = message.from_user.username

    # Проверяем, есть ли у написавшего username и находится ли он в списке админов
    if not username or username.lower() not in ADMIN_USERNAMES:
        await message.answer("Пошел нахуй.")
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("⚠️ Использование команды: `/add username`")
        return

    target_username = args[1].strip()
    success = await database.add_allowed_user(target_username)

    if success:
        await message.answer(f"✅ Юзернейм `{target_username}` успешно добавлен в белый список!")
    else:
        await message.answer("❌ Произошла ошибка при сохранении в базу данных.")


# --- Блок Обычного Пользователя ---
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        f"Привет, {message.from_user.full_name}! Нажми кнопку ниже для проверки доступа.",
        reply_markup=get_auth_keyboard()
    )


@dp.message(F.text == "🔐 Авторизоваться")
async def process_authorization(message: Message):
    username = message.from_user.username
    user_id = message.from_user.id

    if not username:
        await message.answer("У тебя не установлен username в профиле Telegram. Доступ запрещен.")
        return

    try:
        is_allowed = await database.check_user_allowed(username)

        if is_allowed:
            await database.register_authorized_user(user_id, username)
            await message.answer(
                "✅ Доступ разрешен! Твой ID успешно привязан к базе данных.",
                reply_markup=get_main_keyboard()
            )
        else:
            await message.answer("Пошел нахуй.")

    except Exception as e:
        logging.error(f"Ошибка при обработке авторизации: {e}")
        await message.answer("❌ Произошла техническая ошибка при проверке доступа.")


@dp.message(F.text == "📅 Проверить встречу")
async def check_meet_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    is_registered = await database.is_user_registered(user_id)

    if not is_registered:
        await message.answer("Пошел нахуй. Сначала авторизууйся.", reply_markup=get_auth_keyboard())
        return

    await state.set_state(MeetStates.wait_for_link)
    await message.answer("Жду ссылку.", reply_markup=ReplyKeyboardRemove())


@dp.message(MeetStates.wait_for_link)
async def process_meet_link(message: Message, state: FSMContext):
    link = message.text.strip()
    await state.clear()

    meet_available = await fetch_talk.is_meet_available(link, fetch_talk.get_apik_key())

    if len(meet_available) != 0:
        await state.set_state(MeetStates.wait_for_additional_information)
        await message.answer(
            f"✅ Встреча доступна. Информаиция овстрече\ntitle: {meet_available["title"]}\nlogin: {meet_available["login"]}\ndate: {meet_available["date"]}\nname: {meet_available["name"]}\nsurname: {meet_available["surname"]}. Если это та встреча, подтвердите это.",
            reply_markup=get_approuve_keyboard())
    else:
        await message.answer("Нет такой встречи.", reply_markup=get_main_keyboard())


@dp.message(MeetStates.wait_for_additional_information)
async def process_additional_information(message: Message, state: FSMContext):
    text = message.text.strip()
    await state.clear()


async def main():
    await database.init_db()
    try:
        await dp.start_polling(bot, skip_updates=True)
    finally:
        await database.close_db()


if __name__ == '__main__':
    asyncio.run(main())
