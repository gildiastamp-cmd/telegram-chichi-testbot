import os
import asyncio
import logging
from pathlib import Path
from typing import Optional

import asyncpg
from aiogram import Bot, Dispatcher, Router, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile

# ----------------------------
# LOGGING
# ----------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot")

# ----------------------------
# ENV
# ----------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()  # Railway обычно даёт DATABASE_URL

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")

# Пути к файлам (как у тебя на скрине: knowledge/assets)
BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "knowledge" / "assets"
DECK_PATH = ASSETS_DIR / "deck_main.pdf"
FINMODEL_PATH = ASSETS_DIR / "financial_model.xlsx"

# ----------------------------
# DB
# ----------------------------
_pool: Optional[asyncpg.Pool] = None

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS leads (
    id BIGSERIAL PRIMARY KEY,
    tg_user_id BIGINT UNIQUE,
    username TEXT,
    first_name TEXT,
    last_name TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_action TEXT,
    stage TEXT DEFAULT 'new'
);
"""

# Добавляем колонки безопасно, если таблица уже была создана раньше без них
ALTERS_SQL = [
    "ALTER TABLE leads ADD COLUMN IF NOT EXISTS tg_user_id BIGINT;",
    "ALTER TABLE leads ADD COLUMN IF NOT EXISTS username TEXT;",
    "ALTER TABLE leads ADD COLUMN IF NOT EXISTS first_name TEXT;",
    "ALTER TABLE leads ADD COLUMN IF NOT EXISTS last_name TEXT;",
    "ALTER TABLE leads ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();",
    "ALTER TABLE leads ADD COLUMN IF NOT EXISTS last_action TEXT;",
    "ALTER TABLE leads ADD COLUMN IF NOT EXISTS stage TEXT DEFAULT 'new';",
]

CREATE_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS leads_tg_user_id_uidx ON leads(tg_user_id);
"""

UPSERT_LEAD_SQL = """
INSERT INTO leads(tg_user_id, username, first_name, last_name, last_action, stage)
VALUES($1, $2, $3, $4, $5, $6)
ON CONFLICT (tg_user_id) DO UPDATE SET
    username = EXCLUDED.username,
    first_name = EXCLUDED.first_name,
    last_name = EXCLUDED.last_name,
    last_action = EXCLUDED.last_action,
    stage = EXCLUDED.stage;
"""

async def db_init() -> None:
    global _pool
    if not DATABASE_URL:
        logger.warning("DATABASE_URL is not set. Bot will run WITHOUT DB.")
        _pool = None
        return

    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    async with _pool.acquire() as conn:
        # 1) гарантируем таблицу
        await conn.execute(CREATE_TABLE_SQL)
        # 2) гарантируем колонки (исправляет твою текущую ошибку tg_user_id does not exist)
        for sql in ALTERS_SQL:
            await conn.execute(sql)
        # 3) индекс
        await conn.execute(CREATE_INDEX_SQL)

    logger.info("DB ready")

async def save_lead(user, last_action: str, stage: str = "new") -> None:
    if not _pool:
        return
    try:
        async with _pool.acquire() as conn:
            await conn.execute(
                UPSERT_LEAD_SQL,
                user.id,
                user.username,
                user.first_name,
                user.last_name,
                last_action,
                stage,
            )
    except Exception:
        logger.exception("Failed to save lead")

# ----------------------------
# UI (Buttons)
# ----------------------------
CB_DECK = "get_deck"
CB_FINMODEL = "get_finmodel"
CB_CALC = "calc_finmodel"
CB_CALL = "book_call"

def main_menu_kb():
    kb = InlineKeyboardBuilder()
    kb.button(text="📄 Получить презентацию", callback_data=CB_DECK)
    kb.button(text="📊 Получить фин. модель", callback_data=CB_FINMODEL)
    kb.button(text="🧮 Рассчитать перс. фин. модель", callback_data=CB_CALC)
    kb.button(text="📞 Назначить созвон", callback_data=CB_CALL)
    kb.adjust(1)
    return kb.as_markup()

# ----------------------------
# BOT LOGIC
# ----------------------------
router = Router()

WELCOME_TEXT = (
    "Привет! Я ИИра — ассистент Дмитрия Родионова 🤝\n"
    "Могу сразу прислать материалы и помочь подготовиться к созвону.\n\n"
    "Выбирай кнопку ниже:"
)

@router.message(CommandStart())
async def cmd_start(message: Message):
    await save_lead(message.from_user, last_action="/start", stage="new")
    await message.answer(WELCOME_TEXT, reply_markup=main_menu_kb())

async def send_file_safe(message_or_cb, path: Path, caption: str):
    try:
        if not path.exists():
            text = (
                f"Файл не найден на сервере 😕\n\n"
                f"Ожидаемый путь:\n`{path}`\n\n"
                "Проверь, что файл реально лежит в репозитории в `knowledge/assets/` "
                "и закоммичен (а не только на твоём компе)."
            )
            if isinstance(message_or_cb, CallbackQuery):
                await message_or_cb.message.answer(text, parse_mode=ParseMode.MARKDOWN)
            else:
                await message_or_cb.answer(text, parse_mode=ParseMode.MARKDOWN)
            return

        doc = FSInputFile(str(path))
        if isinstance(message_or_cb, CallbackQuery):
            await message_or_cb.message.answer_document(doc, caption=caption)
        else:
            await message_or_cb.answer_document(doc, caption=caption)
    except Exception:
        logger.exception("Failed to send file")
        if isinstance(message_or_cb, CallbackQuery):
            await message_or_cb.message.answer("Не смогла отправить файл. Смотри логи Railway 🙏")
        else:
            await message_or_cb.answer("Не смогла отправить файл. Смотри логи Railway 🙏")

@router.callback_query(F.data == CB_DECK)
async def on_get_deck(cb: CallbackQuery):
    await cb.answer()
    await save_lead(cb.from_user, last_action="get_deck", stage="materials_sent")
    await cb.message.answer("Держи материалы 👇")
    await send_file_safe(cb, DECK_PATH, "Презентация франшизы (deck_main.pdf)")
    await cb.message.answer("Если хочешь — могу коротко (в 3 пункта) объяснить, чем модель сильнее конкурентов.")

@router.callback_query(F.data == CB_FINMODEL)
async def on_get_finmodel(cb: CallbackQuery):
    await cb.answer()
    await save_lead(cb.from_user, last_action="get_finmodel", stage="materials_sent")
    await cb.message.answer("Держи фин. модель 👇")
    await send_file_safe(cb, FINMODEL_PATH, "Финансовая модель (financial_model.xlsx)")
    await cb.message.answer("Хочешь — подскажи город и формат точки, и я скажу, какие цифры смотреть в первую очередь.")

@router.callback_query(F.data == CB_CALC)
async def on_calc(cb: CallbackQuery):
    await cb.answer()
    await save_lead(cb.from_user, last_action="calc_request", stage="calc")
    await cb.message.answer(
        "Ок, посчитаем персонально 🧮\n\n"
        "Напиши одним сообщением:\n"
        "1) город\n"
        "2) сколько денег готов(а) вложить\n"
        "3) есть ли помещение (да/нет)\n\n"
        "Пример: `Казань, 4 млн, нет`",
        parse_mode=ParseMode.MARKDOWN
    )

@router.callback_query(F.data == CB_CALL)
async def on_book_call(cb: CallbackQuery):
    await cb.answer()
    await save_lead(cb.from_user, last_action="book_call", stage="call")
    await cb.message.answer(
        "Назначим созвон с Дмитрием 📞\n\n"
        "Напиши, пожалуйста:\n"
        "1) твой номер телефона\n"
        "2) удобные 2–3 окна по времени (сегодня/завтра)\n\n"
        "Пример: `+7 999 123-45-67, завтра 12:00–14:00 или 19:00–20:00`"
    )

@router.message()
async def fallback(message: Message):
    # Любой текст — не молчим: даём меню
    await save_lead(message.from_user, last_action=f"text:{message.text[:50]}", stage="active")
    await message.answer("Я на связи. Вот что могу сделать 👇", reply_markup=main_menu_kb())

# ----------------------------
# MAIN
# ----------------------------
async def main():
    await db_init()
    bot = Bot(BOT_TOKEN, parse_mode=ParseMode.HTML)
    dp = Dispatcher()
    dp.include_router(router)

    logger.info("Bot started (polling)")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
