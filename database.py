import os
import logging
import asyncpg
from dotenv import load_dotenv

# Загружаем переменные из .env
load_dotenv()

# Формируем словарь конфигурации из файла .env
DB_CONFIG = {
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
    "database": os.getenv("DB_NAME"),
    "host": os.getenv("DB_HOST"),
    "port": int(os.getenv("DB_PORT", 5432))
}

db_pool = None


async def init_db():
    """Инициализация таблиц и создание пула соединений"""
    global db_pool
    try:
        db_pool = await asyncpg.create_pool(**DB_CONFIG)

        async with db_pool.acquire() as conn:
            # Сносим старую таблицу, чтобы применилось правило UNIQUE
            await conn.execute('DROP TABLE IF EXISTS allowed_users CASCADE;')
            await conn.execute('DROP TABLE IF EXISTS authorized_users CASCADE;')
            await conn.execute('DROP TABLE IF EXISTS responses CASCADE;')
            # 1. Таблица со списком РАЗРЕШЕННЫХ юзернеймов
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS allowed_users (
                    ID INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    username TEXT UNIQUE
                )
            ''')

            # 2. Таблица АВТОРИЗОВАННЫХ пользователей
            await conn.execute('''
                CREATE TABLE IF NOT EXISTS authorized_users (
                    ID INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    telegram_id BIGINT UNIQUE,
                    username TEXT
                )
            ''')

            await conn.execute("""
            CREATE TABLE IF NOT EXISTS responses (
                    ID BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    telegram_id BIGINT,
                    link TEXT,
                    additional_info TEXT,
                    point INTEGER,
                    critical BOOLEAN,
                    comment TEXT
            )""")

            # Тестовый запуск: добавляем ваш никнейм для тестов (без @)
            test_user = 'shurygin_egor'
            await conn.execute('''
                INSERT INTO allowed_users (username) 
                VALUES ($1) 
                ON CONFLICT (username) DO NOTHING
            ''', test_user.lower())

            # Тестовый запуск: добавляем ваш никнейм для тестов (без @)
            test_user = 'aemss72'
            await conn.execute('''
                INSERT INTO allowed_users (username) 
                VALUES ($1) 
                ON CONFLICT (username) DO NOTHING
            ''', test_user.lower())

        logging.info("База данных успешно инициализирована.")
    except Exception as e:
        logging.error(f"Ошибка инициализации БД: {e}")
        raise e


async def close_db():
    """Закрытие пула соединений при выключении бота"""
    if db_pool:
        await db_pool.close()
        logging.info("Пул соединений с БД закрыт.")


async def check_user_allowed(username: str) -> bool:
    """Проверяет, есть ли юзернейм в белом списке"""
    if not db_pool:
        raise RuntimeError("Пул базы данных не инициализирован.")

    async with db_pool.acquire() as conn:
        allowed_user = await conn.fetchval(
            "SELECT username FROM allowed_users WHERE username = $1",
            username.lower()
        )
        return allowed_user is not None


async def register_authorized_user(telegram_id: int, username: str):
    """Связывает telegram_id с юзернеймом в базе авторизованных"""
    if not db_pool:
        raise RuntimeError("Пул базы данных не инициализирован.")

    async with db_pool.acquire() as conn:
        await conn.execute('''
            INSERT INTO authorized_users (telegram_id, username) 
            VALUES ($1, $2)
            ON CONFLICT (telegram_id) 
            DO UPDATE SET username = EXCLUDED.username
        ''', telegram_id, username.lower())


async def is_user_registered(telegram_id: int) -> bool:
    """Проверяет, существует ли пользователь с таким telegram_id в базе авторизованных"""
    if not db_pool:
        raise RuntimeError("Пул базы данных не инициализирован.")
    async with db_pool.acquire() as conn:
        result = await conn.fetchval(
            "SELECT telegram_id FROM authorized_users WHERE telegram_id = $1",
            telegram_id
        )
        return result is not None


async def add_allowed_user(username: str) -> None:
    if not db_pool:
        raise RuntimeError("Пул базы данных не инициализирован.")
    async with db_pool.acquire() as conn:
        await conn.execute(
            f"INSERT INTO allowed_users (username) VALUES ({username})"
        )
        return


async def del_user(username: str) -> None:
    if not db_pool:
        raise RuntimeError("Пул базы данных не инициализирован.")
    async with db_pool.acquire() as conn:
        await conn.execute(
            f"DELETE FROM allowed_users WHERE username = $1",
            username
        )
        await conn.execute(
            f"DELETE FROM authorized_users WHERE username = $1",
            username
        )
        return


async def write_result(id: int, point: int, is_critical: bool, ai_comment: str) -> None:
    if not db_pool:
        raise RuntimeError("Пул базы данных не инициализирован.")
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE responses "
            "SET point = $1, critical = $2, comment = $3 "
            "WHERE id = $4",
            point, is_critical, ai_comment, id
        )



async def write_response(tg_id: int, link: str, additional_info: str) -> int:
    if not db_pool:
        raise RuntimeError("Пул базы данных не инициализирован.")
    async with db_pool.acquire() as conn:
        response_id = await conn.fetchval(
            "INSERT INTO responses (telegram_id, link, additional_info) "
            "VALUES ($1, $2, $3) RETURNING ID",
            tg_id, link, additional_info
        )
        return response_id
