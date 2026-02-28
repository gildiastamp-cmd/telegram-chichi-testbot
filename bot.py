import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, Message

# -------------------- CONFIG --------------------
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

ASSETS_DIR = Path(__file__).parent / "knowledge" / "assets"
DECK_PATH = ASSETS_DIR / "deck_main.pdf"
FINMODEL_PATH = ASSETS_DIR / "financial_model.xlsx"

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# -------------------- LOGGING -------------------
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("bot")

# -------------------- DB ------------------------
_pool: Optional[asyncpg.Pool] = None


async def get_pool() -> asyncpg.Pool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is empty. Set it in Railway Variables.")
        _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    return _pool


async def ensure_schema() -> None:
    """
    Делает схему идемпотентной:
    - создаёт таблицу leads если её нет
    - добавляет недостающие колонки если таблица уже была создана старой версией
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        # 1) Таблица (если нет)
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS leads (
                id BIGSERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE NOT NULL,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                stage TEXT DEFAULT 'new',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        # 2) Колонки (если таблица старая)
        # В Postgres можно безопасно: ADD COLUMN IF NOT EXISTS
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS username TEXT;")
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS first_name TEXT;")
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS last_name TEXT;")
        await conn.execute("ALTER TABLE leads ADD COLUMN IF NOT EXISTS stage TEXT DEFAULT 'new';")
        await conn.execute(
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ NOT NULL DEFAULT NOW();"
        )
        await conn.execute(
            "ALTER TABLE leads ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();"
        )

        # 3) updated_at триггером не усложняем — обновляем вручную в UPSERT


async def upsert_lead(message: Message) -> None:
    pool = await get_pool()
    tg = message.from_user
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO leads (telegram_id, username, first_name, last_name, stage)
            VALUES ($1, $2, $3, $4, 'started')
            ON CONFLICT (telegram_id) DO UPDATE
            SET username = EXCLUDED.username,
                first_name = EXCLUDED.first_name,
                last_name = EXCLUDED.last_name,
                stage = 'started',
                updated_at = NOW();
            """,
            tg.id,
            tg.username,
            tg.first_name,
            tg.last_name,
        )


# -------------------- BOT ------------------------
dp = Dispatcher()


async def safe_send_assets(message: Message) -> None:
    # Отправка файлов — отдельно, чтобы видеть точную причину если файла нет
    if not DECK_PATH.exists():
        log.error("Missing file: %s", DECK_PATH)
        await message.answer("Не нашёл файл презентации на сервере 😕 (deck_main.pdf)")
    else:
        await message.answer_document(
            FSInputFile(str(DECK_PATH)),
            caption="📄 Презентация франшизы (deck_main.pdf)",
        )

    if not FINMODEL_PATH.exists():
        log.error("Missing file: %s", FINMODEL_PATH)
        await message.answer("Не нашёл файл финмодели на сервере 😕 (financial_model.xlsx)")
    else:
        await message.answer_document(
            FSInputFile(str(FINMODEL_PATH)),
            caption="📊 Финмодель (financial_model.xlsx)",
        )


@dp.message(CommandStart())
async def cmd_start(message: Message) -> None:
    # Важно: никогда не молчим — даже если БД упала
    await message.answer("Привет! Сейчас пришлю презентацию и финмодель 👇")

    try:
        await upsert_lead(message)
    except Exception:
        log.exception("DB error in /start (lead upsert). Continuing anyway...")
        await message.answer("⚠️ Технический момент: не смог записать данные в базу, но материалы пришлю.")

    try:
        await safe_send_assets(message)
    except Exception:
        log.exception("Asset sending failed")
        await message.answer("⚠️ Не смог отправить файлы. Проверь, что они есть в repo: knowledge/assets/.")


@dp.message(F.text)
async def any_text(message: Message) -> None:
    # Минимальная заглушка, чтобы бот не выглядел "мертвым"
    await message.answer("Я на связи. Напиши /start чтобы получить материалы 🙂")


async def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is empty. Set it in Railway Variables.")

    # 1) Чиним схему до старта polling
    await ensure_schema()
    log.info("DB schema ensured.")

    # 2) Стартуем бота
    bot = Bot(token=BOT_TOKEN)
    log.info("Starting polling...")
    await dp.start_polling(bot)


if name == "__main__":
    asyncio.run(main())
