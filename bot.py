# bot.py
# AIra (ИИра) — Telegram sales bot for franchise leads
# Safe-by-default: minimal moving parts, clear handlers, no fragile magic.
#
# Requirements (examples):
# aiogram==3.*
# asyncpg==0.29.*
# python-dotenv==1.*   (optional)
#
# ENV needed:
# BOT_TOKEN=...
# DATABASE_URL=postgresql://user:pass@host:port/dbname
#
# Optional ENV:
# ADMIN_CHAT_ID=123456789   (to forward "book_call" / "calc_model" requests)
# AMO_BASE_URL=https://...  (optional, if you later add amoCRM)
# AMO_ACCESS_TOKEN=...      (optional, if you later add amoCRM)

import os
import asyncio
import logging
from dataclasses import dataclass
from typing import Optional, Dict, Any

import asyncpg
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    FSInputFile,
)
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

# -----------------------------
# Config
# -----------------------------

@dataclass
class Config:
    bot_token: str
    database_url: str
    admin_chat_id: Optional[int] = None

    # assets (relative paths inside repo)
    deck_path: str = "knowledge/assets/deck_main.pdf"
    fin_model_path: str = "knowledge/assets/financial_model.xlsx"


def load_config() -> Config:
    token = os.getenv("BOT_TOKEN", "").strip()
    db = os.getenv("DATABASE_URL", "").strip()
    admin = os.getenv("ADMIN_CHAT_ID", "").strip()

    if not token:
        raise RuntimeError("BOT_TOKEN is not set")
    if not db:
        raise RuntimeError("DATABASE_URL is not set")

    admin_chat_id = int(admin) if admin.isdigit() else None
    return Config(bot_token=token, database_url=db, admin_chat_id=admin_chat_id)


# -----------------------------
# Logging
# -----------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("aiira-bot")


# -----------------------------
# DB
# -----------------------------

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS leads (
    id BIGSERIAL PRIMARY KEY,
    tg_user_id BIGINT NOT NULL,
    tg_username TEXT,
    first_name TEXT,
    last_name TEXT,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    last_action TEXT DEFAULT NULL,
    payload JSONB DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_leads_user_id ON leads (tg_user_id);
"""

UPSERT_LEAD_SQL = """
INSERT INTO leads (tg_user_id, tg_username, first_name, last_name, last_action, payload)
VALUES ($1, $2, $3, $4, $5, COALESCE($6, '{}'::jsonb))
ON CONFLICT (tg_user_id) DO UPDATE SET
  tg_username = EXCLUDED.tg_username,
  first_name = EXCLUDED.first_name,
  last_name = EXCLUDED.last_name,
  last_action = EXCLUDED.last_action,
  payload = leads.payload || EXCLUDED.payload;
"""

# NOTE: For ON CONFLICT(tg_user_id) we need UNIQUE constraint on tg_user_id.
# We'll ensure it in init below.

ENSURE_UNIQUE_SQL = """
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'leads_tg_user_id_unique'
    ) THEN
        ALTER TABLE leads ADD CONSTRAINT leads_tg_user_id_unique UNIQUE (tg_user_id);
    END IF;
END $$;
"""


async def init_db(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(CREATE_TABLE_SQL)
        await conn.execute(ENSURE_UNIQUE_SQL)
    log.info("DB initialized")


async def upsert_lead(
    pool: asyncpg.Pool,
    message: Message,
    last_action: str,
    payload: Optional[Dict[str, Any]] = None,
) -> None:
    user = message.from_user
    if not user:
        return

    data = payload or {}
    async with pool.acquire() as conn:
        await conn.execute(
            UPSERT_LEAD_SQL,
            user.id,
            user.username,
            user.first_name,
            user.last_name,
            last_action,
            data,
        )


# -----------------------------
# UI (Buttons)
# -----------------------------

def main_menu() -> InlineKeyboardMarkup:return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 Получить презентацию", callback_data="get_presentation")],
        [InlineKeyboardButton(text="📊 Получить фин. модель", callback_data="get_finmodel")],
        [InlineKeyboardButton(text="🧮 Рассчитать персональную фин. модель", callback_data="calc_model")],
        [InlineKeyboardButton(text="📞 Назначить созвон", callback_data="book_call")],
    ])


# -----------------------------
# FSM (for safe step-by-step)
# -----------------------------

class CalcModelFlow(StatesGroup):
    waiting_input = State()


class BookCallFlow(StatesGroup):
    waiting_input = State()


# -----------------------------
# Router
# -----------------------------

router = Router()

# Will be set in main()
DB_POOL: Optional[asyncpg.Pool] = None
CFG: Optional[Config] = None


# -----------------------------
# Helpers
# -----------------------------

async def safe_answer(callback: CallbackQuery) -> None:
    # Telegram sometimes complains if we don't answer callback quickly
    try:
        await callback.answer()
    except Exception:
        pass


def aiira_intro() -> str:
    return (
        "Привет 💫 Я ИИра — личный ИИ-ассистент Дмитрия Родионова.\n"
        "Помогаю быстро разобраться во франшизе и подобрать лучший путь к запуску.\n\n"
        "Выбери, с чего начнём 👇"
    )


def not_found_asset_text(path: str) -> str:
    return (
        "Похоже, файл сейчас недоступен 😕\n"
        f"Проверь, что он лежит по пути:\n{path}\n"
        "и задеплой заново."
    )


async def send_admin_notification(bot: Bot, text: str) -> None:
    if not CFG or not CFG.admin_chat_id:
        return
    try:
        await bot.send_message(CFG.admin_chat_id, text)
    except Exception as e:
        log.warning("Failed to notify admin: %s", e)


# -----------------------------
# Handlers
# -----------------------------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()

    if DB_POOL:
        await upsert_lead(DB_POOL, message, last_action="start", payload={"event": "start"})

    await message.answer(aiira_intro(), reply_markup=main_menu())


@router.callback_query(F.data == "get_presentation")
async def cb_presentation(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    if not callback.message:
        return

    msg = callback.message
    bot = msg.bot

    if DB_POOL:
        await upsert_lead(DB_POOL, msg, last_action="get_presentation", payload={"event": "get_presentation"})

    assert CFG is not None
    if not os.path.exists(CFG.deck_path):
        await msg.answer(not_found_asset_text(CFG.deck_path))
        return

    await msg.answer("Держи материалы 👇")
    await msg.answer_document(
        FSInputFile(CFG.deck_path),
        caption="📄 Презентация франшизы (deck_main.pdf)",
    )


@router.callback_query(F.data == "get_finmodel")
async def cb_finmodel(callback: CallbackQuery) -> None:
    await safe_answer(callback)
    if not callback.message:
        return

    msg = callback.message

    if DB_POOL:
        await upsert_lead(DB_POOL, msg, last_action="get_finmodel", payload={"event": "get_finmodel"})

    assert CFG is not None
    if not os.path.exists(CFG.fin_model_path):
        await msg.answer(not_found_asset_text(CFG.fin_model_path))
        return

    await msg.answer_document(
        FSInputFile(CFG.fin_model_path),
        caption="📊 Финансовая модель (financial_model.xlsx)",
    )


@router.callback_query(F.data == "calc_model")
async def cb_calc_model(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    if not callback.message:
        return

    msg = callback.message

    if DB_POOL:
        await upsert_lead(DB_POOL, msg, last_action="calc_model_clicked", payload={"event": "calc_model_clicked"})

    await state.set_state(CalcModelFlow.waiting_input)
    await msg.answer(
        "Ок, рассчитаю персонально 🧮\n\n"
        "Напиши одним сообщением:\n"
        "1) Город\n""2) Бюджет (примерно)\n"
        "3) Есть ли опыт в бизнесе (да/нет)\n"
        "4) Сколько времени готов уделять (часов в неделю)\n\n"
        "Можно в свободной форме — я пойму."
    )


@router.callback_query(F.data == "book_call")
async def cb_book_call(callback: CallbackQuery, state: FSMContext) -> None:
    await safe_answer(callback)
    if not callback.message:
        return

    msg = callback.message

    if DB_POOL:
        await upsert_lead(DB_POOL, msg, last_action="book_call_clicked", payload={"event": "book_call_clicked"})

    await state.set_state(BookCallFlow.waiting_input)
    await msg.answer(
        "Ок, организую созвон с Дмитрием 📞\n\n"
        "Напиши:\n"
        "1) Имя\n"
        "2) Телефон\n"
        "3) Удобное время (сегодня/завтра, диапазон)\n"
        "4) Город/часовой пояс\n\n"
        "Я передам Дмитрию и подтвержу."
    )


@router.message(CalcModelFlow.waiting_input)
async def calc_model_collect(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напиши текстом, пожалуйста 🙂")
        return

    await state.clear()

    if DB_POOL:
        await upsert_lead(DB_POOL, message, last_action="calc_model_submitted", payload={"calc_request": text})

    await message.answer(
        "Приняла ✅\n"
        "Соберу расчёт и вернусь с цифрами.\n\n"
        "Пока можешь выбрать ещё что-то из меню 👇",
        reply_markup=main_menu(),
    )

    # Notify admin (optional)
    await send_admin_notification(
        message.bot,
        f"🧮 Calc model request\n"
        f"User: {message.from_user.id} @{message.from_user.username}\n"
        f"Text:\n{text}",
    )


@router.message(BookCallFlow.waiting_input)
async def book_call_collect(message: Message, state: FSMContext) -> None:
    text = (message.text or "").strip()
    if not text:
        await message.answer("Напиши текстом, пожалуйста 🙂")
        return

    await state.clear()

    if DB_POOL:
        await upsert_lead(DB_POOL, message, last_action="book_call_submitted", payload={"call_request": text})

    await message.answer(
        "Приняла ✅\n"
        "Передаю Дмитрию. Он подтвердит время созвона.\n\n"
        "Если хочешь, могу параллельно скинуть материалы или финмодель 👇",
        reply_markup=main_menu(),
    )

    await send_admin_notification(
        message.bot,
        f"📞 Call request\n"
        f"User: {message.from_user.id} @{message.from_user.username}\n"
        f"Text:\n{text}",
    )


@router.message()
async def fallback_message(message: Message) -> None:
    """
    Safe fallback:
    - If user types keywords: "презентация" or "финмодель", send files.
    - Otherwise show menu + short helpful line.
    """
    txt = (message.text or "").strip().lower()

    if not txt:
        await message.answer("Выбери кнопку в меню 👇", reply_markup=main_menu())
        return

    # Keyword shortcuts (keep backwards compatibility)
    if "през" in txt:
        assert CFG is not None
        if os.path.exists(CFG.deck_path):
            if DB_POOL:
                await upsert_lead(DB_POOL, message, last_action="keyword_presentation", payload={"event": "keyword_presentation"})
            await message.answer_document(FSInputFile(CFG.deck_path), caption="📄 Презентация франшизы (deck_main.pdf)")
        else:
            await message.answer(not_found_asset_text(CFG.deck_path))
        return

    if "фин" in txt:
        assert CFG is not None
        if os.path.exists(CFG.fin_model_path):
            if DB_POOL:
                await upsert_lead(DB_POOL, message, last_action="keyword_finmodel", payload={"event": "keyword_finmodel"})
            await message.answer_document(FSInputFile(CFG.fin_model_path), caption="📊 Финансовая модель (financial_model.xlsx)")
        else:
            await message.answer(not_found_asset_text(CFG.fin_model_path))
        return

    # Light "Apple-style" assistant move: one clarifying question + menu
    if DB_POOL:await upsert_lead(DB_POOL, message, last_action="free_text", payload={"message": message.text})

    await message.answer(
        "Поняла. Чтобы помочь точнее — что сейчас важнее: быстрее стартовать или уложиться в бюджет?\n\n"
        "А ещё можно выбрать действие в меню 👇",
        reply_markup=main_menu(),
    )


# -----------------------------
# Main
# -----------------------------

async def main() -> None:
    global DB_POOL, CFG

    CFG = load_config()

    DB_POOL = await asyncpg.create_pool(CFG.database_url, min_size=1, max_size=5)
    await init_db(DB_POOL)

    bot = Bot(token=CFG.bot_token)
    dp = Dispatcher()
    dp.include_router(router)

    log.info("Bot started")
    await dp.start_polling(bot)


if name == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
