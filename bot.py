import asyncio
import logging
import os
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlparse, parse_qs

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, FSInputFile


# =======================
# CONFIG
# =======================
BOT_TOKEN = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
DATABASE_URL = (os.getenv("DATABASE_URL") or "").strip()

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "knowledge" / "assets"
DECK_PATH = ASSETS_DIR / "deck_main.pdf"
MODEL_PATH = ASSETS_DIR / "financial_model.xlsx"

LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").upper()


# =======================
# LOGGING
# =======================
logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("salesbot")


# =======================
# DB
# =======================
def _needs_ssl(db_url: str) -> bool:
    # Railway Postgres часто требует SSL. В DATABASE_URL может быть sslmode=require.
    try:
        q = parse_qs(urlparse(db_url).query)
        sslmode = (q.get("sslmode", [""])[0] or "").lower()
        return sslmode in {"require", "verify-ca", "verify-full"}
    except Exception:
        return False


async def create_pool() -> asyncpg.Pool:
    if not DATABASE_URL:
        raise RuntimeError("ENV DATABASE_URL is empty")

    ssl = "require" if _needs_ssl(DATABASE_URL) else None
    # Pool более живучий, чем одно соединение.
    pool = await asyncpg.create_pool(
        DATABASE_URL,
        ssl=ssl,
        min_size=1,
        max_size=5,
        command_timeout=30,
    )
    return pool


async def init_db(pool: asyncpg.Pool) -> None:
    """
    1) Создаёт таблицу leads, если её нет
    2) Если таблица есть, добавляет недостающие колонки (IF NOT EXISTS)
    """
    async with pool.acquire() as conn:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS leads (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                source TEXT,
                created_at TIMESTAMPTZ DEFAULT now(),
                updated_at TIMESTAMPTZ DEFAULT now()
            );
            """
        )
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS username TEXT;")
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS first_name TEXT;")
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS last_name TEXT;")
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS source TEXT;")
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now();")
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();")


async def upsert_lead(pool: asyncpg.Pool, message: Message, source: str = "telegram") -> None:
    user = message.from_user
    if not user:
        return

    now = datetime.now(timezone.utc)

    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO leads (user_id, username, first_name, last_name, source, created_at, updated_at)
            VALUES ($1, $2, $3, $4, $5, $6, $6)
            ON CONFLICT (user_id) DO UPDATE SET
                username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                last_name = EXCLUDED.last_name,
                source = EXCLUDED.source,
                updated_at = EXCLUDED.updated_at;
            """,
            user.id,
            user.username,
            user.first_name,
            user.last_name,
            source,
            now,
        )


# =======================
# ASSETS
# =======================
def assets_status_text() -> str:
    lines = [
        f"ASSETS_DIR: {ASSETS_DIR}",
        f"deck_main.pdf exists: {DECK_PATH.exists()} ({DECK_PATH})",
        f"financial_model.xlsx exists: {MODEL_PATH.exists()} ({MODEL_PATH})",
    ]
    return "\n".join(lines)


async def send_assets(message: Message) -> None:
    missing = []
    if not DECK_PATH.exists():
        missing.append(str(DECK_PATH))
    if not MODEL_PATH.exists():
        missing.append(str(MODEL_PATH))

    if missing:
        await message.answer(
            "Я запустился, но не нашёл файлы:\n"
            + "\n".join(f"• {p}" for p in missing)
            + "\n\nПроверь, что они реально закоммичены в репо и путь такой же."
        )
        return

    await message.answer("Держи материалы 👇")

    await message.answer_document(
        FSInputFile(str(DECK_PATH)),
        caption="Презентация франшизы (deck_main.pdf)",
    )
    await message.answer_document(
        FSInputFile(str(MODEL_PATH)),
        caption="Финансовая модель (financial_model.xlsx)",
    )


# =======================
# BOT
# =======================
async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("ENV TELEGRAM_BOT_TOKEN is empty")

    logger.info("Starting bot...")
    logger.info("\n" + assets_status_text())

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    pool: asyncpg.Pool | None = None

    # Поднимаем БД, но если что-то не так — бот всё равно отвечает
    try:
        pool = await create_pool()
        await init_db(pool)
        logger.info("DB ready")
    except Exception:
        logger.exception("DB init failed (bot will still respond, but lead saving may fail)")

    @dp.message(CommandStart())
    async def cmd_start(message: Message) -> None:
        # 1) Сохраняем лид (если БД ок)
        if pool:
            try:
                await upsert_lead(pool, message, source="telegram")
            except Exception:
                logger.exception("DB upsert failed on /start")

        # 2) Отвечаем ВСЕГДА
        await message.answer("Привет! Сейчас пришлю презентацию и финмодель.")
        await send_assets(message)

    @dp.message(F.text)
    async def on_text(message: Message) -> None:
        txt = (message.text or "").strip().lower()

        if pool:
            try:
                await upsert_lead(pool, message, source="telegram")
            except Exception:
                logger.exception("DB upsert failed on text")

        if txt in {"преза", "презентация", "материалы", "файл", "файлы", "финмодель", "модель"}:
            await send_assets(message)
            return

        await message.answer("Напиши «презентация» или «финмодель» — пришлю файлы. Или нажми /start.")

    try:
        logger.info("Polling started")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        if pool:
            await pool.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
