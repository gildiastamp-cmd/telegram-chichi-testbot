import asyncio
import logging
import os
from pathlib import Path
from datetime import datetime, timezone

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, FSInputFile


# -----------------------
# Config
# -----------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

BASE_DIR = Path(__file__).resolve().parent
ASSETS_DIR = BASE_DIR / "knowledge" / "assets"
DECK_PATH = ASSETS_DIR / "deck_main.pdf"
MODEL_PATH = ASSETS_DIR / "financial_model.xlsx"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("salesbot")


# -----------------------
# DB helpers
# -----------------------
async def init_db(conn: asyncpg.Connection) -> None:
    """
    1) Создаёт таблицу leads, если её нет
    2) Если таблица есть, но колонок не хватает — добавляет их
    (чтобы больше не ловить UndefinedColumnError)
    """
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

    # Если таблица была создана раньше в другой версии — мягко докидываем колонки
    await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS username TEXT;")
    await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS first_name TEXT;")
    await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS last_name TEXT;")
    await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS source TEXT;")
    await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT now();")
    await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ DEFAULT now();")


async def upsert_lead(conn: asyncpg.Connection, message: Message, source: str = "telegram") -> None:
    user = message.from_user
    if not user:
        return

    now = datetime.now(timezone.utc)
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


# -----------------------
# Bot logic
# -----------------------
async def send_assets(message: Message) -> None:
    missing = []
    if not DECK_PATH.exists():
        missing.append(str(DECK_PATH))
    if not MODEL_PATH.exists():
        missing.append(str(MODEL_PATH))

    if missing:
        await message.answer(
            "Я запустился, но не нашёл файлы в репозитории:\n"
            + "\n".join(f"• {p}" for p in missing)
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


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("ENV TELEGRAM_BOT_TOKEN is empty")
    if not DATABASE_URL:
        raise RuntimeError("ENV DATABASE_URL is empty")

    bot = Bot(token=BOT_TOKEN)
    dp = Dispatcher()

    # создаём одно соединение (для простоты и стабильности)
    conn = await asyncpg.connect(DATABASE_URL)
    await init_db(conn)
    logger.info("DB ready")

    @dp.message(CommandStart())
    async def cmd_start(message: Message) -> None:try:
            await upsert_lead(conn, message, source="telegram")
        except Exception:
            logger.exception("DB upsert failed on /start")
        await message.answer("Привет! Я бот по франшизе CHI-CHI. Сейчас пришлю презентацию и финмодель.")
        await send_assets(message)

    @dp.message(F.text)
    async def any_text(message: Message) -> None:
        # Чтобы бот не молчал вообще никогда
        try:
            await upsert_lead(conn, message, source="telegram")
        except Exception:
            logger.exception("DB upsert failed on text")

        txt = (message.text or "").strip().lower()
        if txt in {"преза", "презентация", "файл", "материалы", "финмодель", "модель"}:
            await send_assets(message)
            return

        await message.answer("Напиши «презентация» или «финмодель» — пришлю файлы. Или нажми /start.")

    try:
        logger.info("Bot polling started")
        await dp.start_polling(bot)
    finally:
        await conn.close()


if name == "__main__":
    asyncio.run(main())
