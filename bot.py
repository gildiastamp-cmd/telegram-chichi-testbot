import asyncio
import logging
import os
from pathlib import Path
from typing import Optional, Dict, Any

import asyncpg
from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
)
from aiogram.types.input_file import FSInputFile

# ---------------------------
# CONFIG
# ---------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

# ВАЖНО: пути под твою структуру репо
DECK_PATH = os.getenv("DECK_PATH", "knowledge/assets/deck_main.pdf").strip()
MODEL_PATH = os.getenv("MODEL_PATH", "knowledge/assets/financial_model.xlsx").strip()

# Имя и тон "ИИры" (сдержанно-Apple, но живо)
IRA_NAME = "ИИра"
OWNER_NAME = "Дмитрий Радионов"

WELCOME_TEXT = (
    f"Привет! Я {IRA_NAME} — личный ИИ-ассистент {OWNER_NAME} 🤝\n\n"
    "Я могу:\n"
    "• прислать презентацию\n"
    "• прислать финансовую модель\n"
    "• помочь рассчитать персональную финмодель (пока в режиме вопросов)\n"
    "• записать на созвон с Дмитрием\n\n"
    "Выбирай кнопку ниже 👇"
)

HELP_TEXT = (
    "Команды:\n"
    "/start — меню\n"
    "/help — помощь\n\n"
    "Можно просто нажимать кнопки."
)

# ---------------------------
# LOGGING
# ---------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("bot")

# ---------------------------
# DB (optional)
# ---------------------------
_pool: Optional[asyncpg.Pool] = None


async def db_get_pool() -> Optional[asyncpg.Pool]:
    global _pool
    if not DATABASE_URL:
        return None
    if _pool is None:
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        log.info("Postgres pool created")
    return _pool


async def db_init() -> None:
    pool = await db_get_pool()
    if not pool:
        log.info("DATABASE_URL not set — running without DB")
        return

    create_table_sql = """
    CREATE TABLE IF NOT EXISTS leads (
        id SERIAL PRIMARY KEY,
        tg_user_id BIGINT UNIQUE NOT NULL,
        username TEXT,
        first_name TEXT,
        last_name TEXT,
        phone TEXT,
        source TEXT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """

    # Индекс можно не городить (unique уже есть), но оставим аккуратно:
    create_index_sql = """
    CREATE INDEX IF NOT EXISTS leads_tg_user_id_idx ON leads(tg_user_id);
    """

    async with pool.acquire() as conn:
        await conn.execute(create_table_sql)
        await conn.execute(create_index_sql)

    log.info("DB initialized (tables/indexes ensured)")


async def db_upsert_lead(
    user_id: int,
    username: Optional[str],
    first_name: Optional[str],
    last_name: Optional[str],
    phone: Optional[str] = None,
    source: Optional[str] = None,
) -> None:
    pool = await db_get_pool()
    if not pool:
        return

    sql = """
    INSERT INTO leads (tg_user_id, username, first_name, last_name, phone, source)
    VALUES ($1, $2, $3, $4, $5, $6)
    ON CONFLICT (tg_user_id) DO UPDATE SET
        username = EXCLUDED.username,
        first_name = EXCLUDED.first_name,
        last_name = EXCLUDED.last_name,
        phone = COALESCE(EXCLUDED.phone, leads.phone),
        source = COALESCE(EXCLUDED.source, leads.source),
        updated_at = NOW();
    """
    async with pool.acquire() as conn:
        await conn.execute(sql, user_id, username, first_name, last_name, phone, source)


# ---------------------------
# UI (keyboards)
# ---------------------------
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📄 Получить презентацию", callback_data="get_deck"),
            ],
            [
                InlineKeyboardButton(text="📊 Получить фин. модель", callback_data="get_model"),
            ],
            [
                InlineKeyboardButton(text="🧮 Рассчитать персональную финмодель", callback_data="calc_model"),
            ],
            [
                InlineKeyboardButton(text="📞 Назначить созвон", callback_data="book_call"),
            ],
        ]
    )


def contact_request_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📱 Отправить мой номер", request_contact=True)],
            [KeyboardButton(text="↩️ Отмена")],
        ],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


# ---------------------------
# Helpers
# ---------------------------
def file_exists(path_str: str) -> bool:
    p = Path(path_str)
    return p.exists() and p.is_file()


async def send_file_safe(message: Message, path_str: str, caption: str) -> None:
    if not file_exists(path_str):
        await message.answer(
            "Упс. Файл сейчас недоступен на сервере 😕\n"
            "Дмитрий уже чинит. Можешь пока нажать /start и выбрать другой пункт."
        )
        log.error("File not found: %s", path_str)
        return

    try:
        file = FSInputFile(path_str)
        await message.answer_document(file, caption=caption)
    except Exception:
        log.exception("Failed to send file: %s", path_str)
        await message.answer(
            "Не получилось отправить файл из-за ошибки на сервере 😕\n"
            "Попробуй ещё раз через минуту или нажми /start."
        )


# ---------------------------
# Bot
# ---------------------------
router = Router()


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    # Сохраняем лида (если БД есть)
    await db_upsert_lead(
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        source="start",
    )
    await message.answer(WELCOME_TEXT, reply_markup=main_menu_kb())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=main_menu_kb())


@router.callback_query(F.data == "get_deck")
async def cb_get_deck(call: CallbackQuery) -> None:
    await call.answer()
    await db_upsert_lead(
        user_id=call.from_user.id,
        username=call.from_user.username,
        first_name=call.from_user.first_name,
        last_name=call.from_user.last_name,
        source="deck",
    )
    await send_file_safe(
        call.message,
        DECK_PATH,
        "📄 Презентация франшизы (deck_main.pdf)",
    )
    await call.message.answer("Хочешь — расскажу кратко ключевые цифры/условия и подберу формат под твой город 👇", reply_markup=main_menu_kb())


@router.callback_query(F.data == "get_model")
async def cb_get_model(call: CallbackQuery) -> None:
    await call.answer()
    await db_upsert_lead(
        user_id=call.from_user.id,
        username=call.from_user.username,
        first_name=call.from_user.first_name,
        last_name=call.from_user.last_name,
        source="finmodel",
    )
    await send_file_safe(
        call.message,
        MODEL_PATH,
        "📊 Финансовая модель (financial_model.xlsx)",
    )
    await call.message.answer("Если скажешь город и формат точки — помогу прикинуть окупаемость 👇", reply_markup=main_menu_kb())


@router.callback_query(F.data == "calc_model")
async def cb_calc_model(call: CallbackQuery) -> None:
    await call.answer()
    await db_upsert_lead(
        user_id=call.from_user.id,
        username=call.from_user.username,
        first_name=call.from_user.first_name,
        last_name=call.from_user.last_name,
        source="calc_model",
    )
    await call.message.answer(
        "Окей, посчитаем персонально 🧮\n\n"
        "Ответь одним сообщением в таком формате:\n"
        "1) Город\n"
        "2) Есть ли помещение? (да/нет)\n"
        "3) Бюджет на запуск (примерно)\n"
        "4) Когда хочешь стартовать?\n\n"
        "Я соберу вводные и предложу сценарий.",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.callback_query(F.data == "book_call")
async def cb_book_call(call: CallbackQuery) -> None:
    await call.answer()
    await db_upsert_lead(
        user_id=call.from_user.id,
        username=call.from_user.username,
        first_name=call.from_user.first_name,
        last_name=call.from_user.last_name,
        source="book_call",
    )
    await call.message.answer(
        "Договорились 📞\n"
        "Чтобы Дмитрий быстро связался — отправь номер кнопкой ниже.\n"
        "И напиши удобное время (например: завтра 14:00–18:00).",
        reply_markup=contact_request_kb(),
    )


@router.message(F.contact)
async def on_contact(message: Message) -> None:
    phone = message.contact.phone_number if message.contact else None
    await db_upsert_lead(
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        phone=phone,
        source="contact_shared",
    )
    await message.answer(
        "Приняла ✅\n"
        "Теперь просто напиши удобные окна по времени для созвона (и часовой пояс).",
        reply_markup=ReplyKeyboardRemove(),
    )


@router.message(F.text == "↩️ Отмена")
async def on_cancel(message: Message) -> None:
    await message.answer("Ок, вернула меню 👇", reply_markup=main_menu_kb())


@router.message(F.text)
async def on_text(message: Message) -> None:
    # Любой текст: либо ответы на "персональную модель", либо время для созвона.
    txt = (message.text or "").strip()

    # Сохраняем факт активности лида (если БД есть)
    await db_upsert_lead(
        user_id=message.from_user.id,
        username=message.from_user.username,
        first_name=message.from_user.first_name,
        last_name=message.from_user.last_name,
        source="message",
    )

    # Очень мягкая логика: если в тексте есть время/дата — считаем что это про созвон
    has_time_hint = any(token in txt.lower() for token in ["завтра", "сегодня", "пон", "вт", "ср", "чт", "пт", "сб", "вс", ":", "00", "30", "мск", "gmt", "utc"])
    if has_time_hint:
        await message.answer(
            "Отлично, зафиксировала 📝\n"
            "Дмитрий вернётся с подтверждением слота.\n\n"
            "Пока можешь взять материалы 👇",
            reply_markup=main_menu_kb(),
        )
        return

    # Иначе — дружелюбно возвращаем к меню
    await message.answer(
        "Поняла 🙂\n"
        "Чтобы я была точнее — выбери, что нужно, кнопкой ниже 👇",
        reply_markup=main_menu_kb(),
    )


async def on_startup(bot: Bot) -> None:
    # Инициализация БД — безопасная (если нет DATABASE_URL, просто пропускаем)
    await db_init()

    # Быстрый лог путей (чтобы сразу видно было в Railway logs)
    log.info("DECK_PATH=%s exists=%s", DECK_PATH, file_exists(DECK_PATH))
    log.info("MODEL_PATH=%s exists=%s", MODEL_PATH, file_exists(MODEL_PATH))


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is not set")

    bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
    dp = Dispatcher()
    dp.include_router(router)

    await on_startup(bot)

    log.info("Bot starting (polling)...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped")
