import os
import asyncio
import logging
from aiogram import Bot, Dispatcher, F
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    CallbackQuery
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import StatesGroup, State
from dotenv import load_dotenv

import database
import fetch_talk
import ai

# Включаем логирование
logging.basicConfig(level=logging.INFO)
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# --- Чтение списка админов ---
raw_admins = os.getenv("ADMIN_USERNAMES", "")
ADMIN_USERNAMES = [name.strip().lower() for name in raw_admins.split(",") if name.strip()]


# --- Определение состояний FSM ---
class MeetStates(StatesGroup):
    wait_for_link = State()
    wait_for_additional_information = State()
    wait_for_info_confirmation = State()


# --- Клавиатуры ---
def get_auth_keyboard():
    button = KeyboardButton(text="🔐 Авторизоваться")
    return ReplyKeyboardMarkup(keyboard=[[button]], resize_keyboard=True)


def get_main_keyboard():
    button = KeyboardButton(text="📅 Проверить встречу")
    return ReplyKeyboardMarkup(keyboard=[[button]], resize_keyboard=True)


def get_meet_inline_keyboard():
    buttons = [
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data="meet_confirm"),
            InlineKeyboardButton(text="❌ Отменить", callback_data="meet_cancel")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_info_inline_keyboard():
    buttons = [
        [
            InlineKeyboardButton(text="📥 Подтвердить данные", callback_data="info_confirm"),
            InlineKeyboardButton(text="✍️ Изменить", callback_data="info_edit")
        ]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# --- Блок Администратора ---
@dp.message(Command("add"))
async def cmd_add_user(message: Message):
    username = message.from_user.username
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


@dp.message(Command("del"))
async def cmd_del_user(message: Message):
    username = message.from_user.username
    if not username or username.lower() not in ADMIN_USERNAMES:
        await message.answer("Пошел нахуй.")
        return
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("⚠️ Использование команды: `/del username`")
        return
    target_username = args[1].strip()
    success = await database.delete_user(target_username)
    if success:
        await message.answer(f"✅ Юзернейм `{target_username}` успешно удален из белого списка!")
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
            await message.answer("Вас нет в базе данных. Обратитесь к администратору для добавления вас.")
    except Exception as e:
        logging.error(f"Ошибка при обработке авторизации: {e}")
        await message.answer("❌ Произошла техническая ошибка при проверке доступа.")


@dp.message(F.text == "📅 Проверить встречу")
async def check_meet_start(message: Message, state: FSMContext):
    user_id = message.from_user.id
    is_registered = await database.is_user_registered(user_id)
    if not is_registered:
        await message.answer("Пошел нахуй. Сначала авторизуйся.", reply_markup=get_auth_keyboard())
        return
    await state.set_state(MeetStates.wait_for_link)
    await message.answer("Жду ссылку.", reply_markup=ReplyKeyboardRemove())


@dp.message(MeetStates.wait_for_link)
async def process_meet_link(message: Message, state: FSMContext):
    link = message.text.strip()

    meet_available = await fetch_talk.is_meet_available(link, fetch_talk.get_api_key())
    if len(meet_available) != 0:
        # Временно сохраняем ссылку в память FSM
        await state.update_data(saved_link=link)

        await message.answer(
            f"✅ Встреча доступна. Информаиция овстрече\ntitle: "
            f"{meet_available['title']}\nlogin: {meet_available['login']}\ndate: "
            f"{meet_available['date']}\nname: {meet_available['name']}\nsurname: "
            f"{meet_available['surname']}.\n Если это та встреча, подтвердите это.",
            reply_markup=get_meet_inline_keyboard()
        )
    else:
        await state.clear()
        await message.answer("Нет такой встречи.", reply_markup=get_main_keyboard())


# --- Обработка кнопок Первого этапа (Встреча) ---
@dp.callback_query(F.data == "meet_confirm")
async def process_confirm_meet(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    # Удаляем инлайн-кнопки под сообщением о встрече
    await callback.message.edit_reply_markup(reply_markup=None)

    await state.set_state(MeetStates.wait_for_additional_information)
    await callback.message.answer("Пожалуйста, предоставьте дополнительную информацию о встрече:")


@dp.callback_query(F.data == "meet_cancel")
async def process_cancel_meet(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    # Удаляем инлайн-кнопки под сообщением о встрече
    await callback.message.edit_reply_markup(reply_markup=None)

    await state.clear()
    await callback.message.answer("Процесс отменен. Возврат в главное меню.", reply_markup=get_main_keyboard())


# --- Обработка Ввода Дополнительной Информации ---
@dp.message(MeetStates.wait_for_additional_information)
async def process_additional_information(message: Message, state: FSMContext):
    text = message.text.strip()
    # Дописываем текст допинформации в память к уже сохраненной ссылке
    await state.update_data(saved_info=text)

    await state.set_state(MeetStates.wait_for_info_confirmation)
    await message.answer(
        f"Вы ввели следующую информацию:\n\n\"{text}\"\n\nПодтвердите её корректность или измените текст.",
        reply_markup=get_info_inline_keyboard()
    )


# --- Обработка кнопок Второго этапа (Подтверждение текста) ---
@dp.callback_query(F.data == "info_confirm", MeetStates.wait_for_info_confirmation)
async def process_confirm_info_data(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    # Удаляем инлайн-кнопки проверки допинформации
    await callback.message.edit_reply_markup(reply_markup=None)

    user_data = await state.get_data()
    link = user_data.get("saved_link")
    additional_info = user_data.get("saved_info")
    tg_id = callback.from_user.id

    try:
        # Запись в базу данных с помощью вашей функции из файла database
        response_id = await database.write_response(tg_id=tg_id, link=link, additional_info=additional_info)
        logging.info(f"Успешная запись в БД. ID строки: {response_id}")

        await callback.message.answer(
            f"Встреча успешно подтверждена и сохранена!\nВот ваша ссылка: {link}"
        )
        logging.info(f"Получаем артефакты лекции {link}")
        artifacts = await fetch_talk.download_all_artifacts_by_url(link, fetch_talk.get_api_key())
        if artifacts["video"] is None:
            await callback.message.answer(f"Встречу не удалось скачать, попробуйте еще раз",
                                          reply_markup=get_main_keyboard())
            return
        await callback.message.answer(f"Встреча отправлена на проверку ИИ")
        mark = await ai.process(artifacts)
        await fetch_talk.delete_files(artifacts)
        await callback.message.answer(f"Встреча проверена: {mark}")
        await database.write_result(tg_id, mark["point"], mark["is_critical"], mark["comment"])

    except Exception as e:
        logging.error(f"Ошибка: {e}")
        await callback.message.answer("❌ Произошла ошибка при сохранении данных в БД.",
                                      reply_markup=get_main_keyboard())
    finally:
        await state.clear()


@dp.callback_query(F.data == "info_edit", MeetStates.wait_for_info_confirmation)
async def process_edit_info_data(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    # Удаляем инлайн-кнопки проверки допинформации
    await callback.message.edit_reply_markup(reply_markup=None)

    # Возвращаем пользователя на шаг ввода текста (ссылка остается в памяти)
    await state.set_state(MeetStates.wait_for_additional_information)
    await callback.message.answer("Введите текст дополнительной информации заново:")


# --- Основной цикл запуска ---
async def main():
    await database.init_db()
    try:
        await dp.start_polling(bot, skip_updates=True)
    finally:
        await database.close_db()


if __name__ == '__main__':
    asyncio.run(main())
