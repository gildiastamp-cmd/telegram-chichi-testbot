import os
import asyncio
import logging
from pathlib import Path
from typing import Optional

from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.types import Message
from aiogram.client.default import DefaultBotProperties
from aiogram.types import FSInputFile

import asyncpg

# Optional OpenAI (если ключ не задан — бот будет отвечать без ИИ)
try:
    from openai import AsyncOpenAI
except Exception:
    AsyncOpenAI = None  # type: ignore


# --------------------
# CONFIG
# --------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini").strip()

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "knowledge" / "assets"
DECK_PATH = ASSETS_DIR / "deck_main.pdf"
FINMODEL_PATH = ASSETS_DIR / "financial_model.xlsx"

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("bot")

router = Router()

_pool: Optional[asyncpg.Pool] = None
_openai_client: Optional["AsyncOpenAI"] = None


# --------------------
# DB
# --------------------
async def init_db() -> None:
    global _pool
    if not DATABASE_URL:
        logger.warning("DATABASE_URL is empty. DB features disabled.")
        return

    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

    async with _pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS leads (
                user_id BIGINT PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );

            CREATE TABLE IF NOT EXISTS messages (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                role TEXT NOT NULL,
                text TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            """
        )

    logger.info("DB initialized")


async def db_upsert_lead(message: Message) -> None:
    if not _pool:
        return
    user = message.from_user
    if not user:
        return
    async with _pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO leads (user_id, first_name, username)
            VALUES ($1, $2, $3)
            ON CONFLICT (user_id) DO UPDATE
            SET first_name = EXCLUDED.first_name,
                username = EXCLUDED.username,
                updated_at = NOW();
            """,
            user.id,
            user.first_name,
            user.username,
        )


async def db_save_message(user_id: int, role: str, text: str) -> None:
    if not _pool:
        return
    async with _pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO messages (user_id, role, text) VALUES ($1, $2, $3);",
            user_id,
            role,
            text,
        )


# --------------------
# FILES SENDER
# --------------------
async def send_assets_if_exist(message: Message) -> bool:
    sent_any = False

    # deck
    if DECK_PATH.exists():
        try:
            await message.answer_document(
                FSInputFile(str(DECK_PATH)),
                caption="📎 Презентация франшизы (deck_main.pdf)",
            )
            sent_any = True
        except Exception as e:
            logger.exception("Failed to send deck: %s", e)

    # finmodel
    if FINMODEL_PATH.exists():
        try:
            await message.answer_document(
                FSInputFile(str(FINMODEL_PATH)),
                caption="📎 Финмодель (financial_model.xlsx)",
            )
            sent_any = True
        except Exception as e:
            logger.exception("Failed to send finmodel: %s", e)

    if not sent_any:
        await message.answer(
            "Не нашёл файлы в репозитории. Проверь пути:\n"
            "knowledge/assets/deck_main.pdf\n""knowledge/assets/financial_model.xlsx"
        )

    return sent_any


# --------------------
# AI
# --------------------
def ensure_openai() -> None:
    global _openai_client
    if _openai_client is not None:
        return
    if not OPENAI_API_KEY or AsyncOpenAI is None:
        _openai_client = None
        return
    _openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)


async def ai_reply(user_text: str) -> str:
    # если OpenAI не настроен — простой ответ
    ensure_openai()
    if _openai_client is None:
        return (
            "Я на связи ✅\n"
            "Сейчас OpenAI не подключен (нет OPENAI_API_KEY), поэтому отвечаю без ИИ.\n"
            "Напиши: какой город/район рассматриваешь и какой бюджет на запуск?"
        )

    try:
        resp = await _openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты ассистент по продаже франшизы. "
                        "Твоя задача: вежливо, кратко, по делу, "
                        "дать первичную консультацию и мягко предложить созвон."
                    ),
                },
                {"role": "user", "content": user_text},
            ],
            temperature=0.6,
        )
        return (resp.choices[0].message.content or "").strip() or "Ок. Уточни, пожалуйста, город и бюджет запуска."
    except Exception as e:
        logger.exception("OpenAI error: %s", e)
        return "Поймал ошибку на стороне ИИ. Напиши город и бюджет — я продолжу без ИИ."


# --------------------
# HANDLERS
# --------------------
@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await db_upsert_lead(message)
    if message.from_user:
        await db_save_message(message.from_user.id, "user", "/start")

    await message.answer(
        "Привет! Я помогу быстро разобраться по франшизе.\n"
        "Сейчас отправлю презентацию и финмодель ✅"
    )

    await send_assets_if_exist(message)

    await message.answer(
        "Пара быстрых вопросов, чтобы подсказать точнее:\n"
        "1) В каком городе планируешь запуск?\n"
        "2) Что важнее сейчас: быстрее стартовать или уложиться в минимальный бюджет?"
    )


@router.message(F.text)
async def handle_text(message: Message) -> None:
    user = message.from_user
    if not user:
        return

    text = (message.text or "").strip()
    if not text:
        return

    await db_upsert_lead(message)
    await db_save_message(user.id, "user", text)

    # на всякий случай — команда "файлы"
    if text.lower() in {"файлы", "преза", "презентация", "финмодель", "фин модель"}:
        await send_assets_if_exist(message)
        return

    reply = await ai_reply(text)
    await db_save_message(user.id, "assistant", reply)
    await message.answer(reply)


# --------------------
# MAIN
# --------------------
async def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    await init_db()

    bot = Bot(
        token=TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    # если когда-то был webhook — удаляем, чтобы polling работал стабильно
    await bot.delete_webhook(drop_pending_updates=True)

    logger.info("BOT STARTED ✅")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
