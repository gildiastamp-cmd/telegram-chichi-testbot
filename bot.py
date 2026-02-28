import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Optional

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.enums import ParseMode
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

# ---------------------------
# Logging
# ---------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("iira-bot")


# ---------------------------
# Env
# ---------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()  # Railway Postgres URL
ADMIN_CHAT_ID = os.getenv("ADMIN_CHAT_ID", "").strip()  # optional (your TG id)


# ---------------------------
# Files
# ---------------------------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

DECK_PATH = os.path.join(BASE_DIR, "knowledge", "deck_main.pdf")
FINMODEL_PATH = os.path.join(BASE_DIR, "knowledge", "financial_model.xlsx")

# fallback if you store files in root
if not os.path.exists(DECK_PATH):
    DECK_PATH = os.path.join(BASE_DIR, "deck_main.pdf")
if not os.path.exists(FINMODEL_PATH):
    FINMODEL_PATH = os.path.join(BASE_DIR, "financial_model.xlsx")


# ---------------------------
# Personality / copy (ИИра)
# ---------------------------
IIRA_NAME = "ИИра"

HELLO_TEXT = (
    f"Привет! Я {IIRA_NAME} — личный ИИ-ассистент Дмитрия Родионова.\n\n"
    "Я могу быстро дать материалы по франшизе и помочь прикинуть цифры под вашу ситуацию.\n"
    "А если будет интересно — аккуратно организую созвон с Дмитрием 🙂"
)

AFTER_FILES_TEXT = (
    "Держи материалы 👇\n\n"
    "Если хочешь, могу:\n"
    "• коротко объяснить, чем формат сильнее альтернатив,\n"
    "• задать 3–4 вопроса и прикинуть персональные цифры,\n"
    "• или сразу записать на созвон."
)

SOFT_STYLE_NOTE = (
    "PS: Я не навязчивая. Сначала — польза. Созвон предложу только если вижу интерес 😉"
)


# ---------------------------
# UI
# ---------------------------
def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📕 Получить презентацию", callback_data="get_deck")],
            [InlineKeyboardButton(text="📊 Получить фин. модель", callback_data="get_fin")],
            [InlineKeyboardButton(text="🧮 Рассчитать персональную фин. модель", callback_data="calc")],
            [InlineKeyboardButton(text="📞 Назначить созвон с Дмитрием", callback_data="call")],
        ]
    )


# ---------------------------
# FSM
# ---------------------------
class CalcFlow(StatesGroup):
    city = State()
    investment = State()
    rent = State()
    contact = State()


class CallFlow(StatesGroup):
    contact = State()
    time = State()


# ---------------------------
# DB
# ---------------------------
_pool: Optional[asyncpg.Pool] = None


async def db_connect() -> None:
    global _pool
    if not DATABASE_URL:
        log.warning("DATABASE_URL is empty. Bot will run WITHOUT DB.")
        _pool = None
        return

    _pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
    log.info("DB pool created")
    await db_init_schema()


async def db_init_schema() -> None:
    """Creates tables/indexes. Safe to run on every boot."""
    if not _pool:
        return

    create_leads = """
    CREATE TABLE IF NOT EXISTS leads (
        id              BIGSERIAL PRIMARY KEY,
        tg_user_id      BIGINT NOT NULL,
        tg_username     TEXT,
        first_name      TEXT,
        last_name       TEXT,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        last_action     TEXT,
        phone_or_contact TEXT,
        calc_payload    JSONB
    );
    create_index = """
    CREATE UNIQUE INDEX IF NOT EXISTS leads_tg_user_id_uidx
    ON leads (tg_user_id);
    """

    async with _pool.acquire() as conn:
        await conn.execute(create_leads)
        await conn.execute(create_index)

    log.info("DB schema ensured")


async def upsert_lead(
    user_id: int,
    username: Optional[str],
    first_name: Optional[str],
    last_name: Optional[str],
    last_action: str = "",
    phone_or_contact: Optional[str] = None,
    calc_payload: Optional[dict] = None,
) -> None:
    if not _pool:
        return

    payload_json = json.dumps(calc_payload, ensure_ascii=False) if calc_payload else None

    query = """
    INSERT INTO leads (tg_user_id, tg_username, first_name, last_name, last_action, phone_or_contact, calc_payload)
    VALUES ($1, $2, $3, $4, $5, $6, COALESCE($7::jsonb, NULL))
    ON CONFLICT (tg_user_id) DO UPDATE SET
        tg_username = EXCLUDED.tg_username,
        first_name = EXCLUDED.first_name,
        last_name = EXCLUDED.last_name,
        last_action = EXCLUDED.last_action,
        phone_or_contact = COALESCE(EXCLUDED.phone_or_contact, leads.phone_or_contact),
        calc_payload = COALESCE(EXCLUDED.calc_payload, leads.calc_payload),
        updated_at = NOW();"""
    async with _pool.acquire() as conn:
        await conn.execute(
            query,
            user_id,
            username,
            first_name,
            last_name,
            last_action,
            phone_or_contact,
            payload_json,
        )


async def notify_admin(text: str) -> None:
    if not ADMIN_CHAT_ID:
        return
    try:
        await bot.send_message(chat_id=int(ADMIN_CHAT_ID), text=text)
    except Exception:
        log.exception("Failed to notify admin")


# ---------------------------
# Bot
# ---------------------------
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is empty. Set BOT_TOKEN env var.")

bot = Bot(token=BOT_TOKEN, parse_mode=ParseMode.HTML)
dp = Dispatcher(storage=MemoryStorage())


# ---------------------------
# Helpers
# ---------------------------
def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


async def safe_send_file(message: Message, path: str, caption: str) -> bool:
    if not os.path.exists(path):
        await message.answer(f"Не нашла файл на сервере 😕\nПуть: <code>{path}</code>\nСкажи Дмитрию — поправим.")
        return False
    try:
        await message.answer_document(FSInputFile(path), caption=caption)
        return True
    except Exception:
        log.exception("Failed to send file: %s", path)
        await message.answer("Упс, не удалось отправить файл из-за ошибки. Я уже записала лог.")
        return False


# ---------------------------
# Start / Menu
# ---------------------------
@dp.message(CommandStart())
async def on_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await upsert_lead(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name,
        last_action="start",
    )
    await message.answer(HELLO_TEXT, reply_markup=main_menu_kb())


@dp.message(F.text.lower().in_({"меню", "menu"}))
async def on_menu(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Вот что я умею:", reply_markup=main_menu_kb())


# ---------------------------
# Quick text triggers (compat)
# ---------------------------
@dp.message(F.text.lower().contains("презентац"))
async def text_deck(message: Message) -> None:
    await handle_get_deck(message)


@dp.message(F.text.lower().contains("финмод"))
async def text_fin(message: Message) -> None:
    await handle_get_fin(message)


# ---------------------------
# Callbacks
# ---------------------------
@dp.callback_query(F.data == "get_deck")
async def cb_get_deck(call: CallbackQuery) -> None:
    await call.answer()
    await handle_get_deck(call.message)


@dp.callback_query(F.data == "get_fin")
async def cb_get_fin(call: CallbackQuery) -> None:
    await call.answer()
    await handle_get_fin(call.message)


@dp.callback_query(F.data == "calc")
async def cb_calc(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.clear()
    await upsert_lead(
        call.from_user.id,
        call.from_user.username,
        call.from_user.first_name,
        call.from_user.last_name,
        last_action="calc_start",
    )
    await call.message.answer(
        "Ок! Давай быстро прикинем персонально.\n\n"
        "1/4: В каком городе планируете запуск?",
    )
    await state.set_state(CalcFlow.city)


@dp.callback_query(F.data == "call")
async def cb_call(call: CallbackQuery, state: FSMContext) -> None:
    await call.answer()
    await state.clear()
    await upsert_lead(
        call.from_user.id,
        call.from_user.username,
        call.from_user.first_name,
        call.from_user.last_name,
        last_action="call_start",
    )
    await call.message.answer(
        "Супер. Чтобы назначить созвон с Дмитрием:\n\n"
        "Напишите, пожалуйста, ваш номер телефона или удобный контакт (WhatsApp/Telegram)."
    )
    await state.set_state(CallFlow.contact)


# ---------------------------
# Handlers: send files
# ---------------------------
async def handle_get_deck(message: Message) -> None:
    await upsert_lead(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name,
        last_action="get_deck",
    )
    ok = await safe_send_file(
        message,
        DECK_PATH,
        caption="Презентация франшизы (deck_main.pdf)",
    )
    if ok:
        await message.answer(AFTER_FILES_TEXT, reply_markup=main_menu_kb())
        await message.answer(SOFT_STYLE_NOTE)


async def handle_get_fin(message: Message) -> None:
    await upsert_lead(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name,
        last_action="get_finmodel",
    )
    ok = await safe_send_file(
        message,
        FINMODEL_PATH,
        caption="Финансовая модель (financial_model.xlsx)",
    )
    if ok:
        await message.answer(
            "Если хотите — я могу помочь заполнить её под вашу ситуацию.\n"
            "Нажмите «🧮 Рассчитать персональную фин. модель» или просто напишите: <b>рассчитать</b>.",
            reply_markup=main_menu_kb(),
        )


@dp.message(F.text.lower().contains("рассчит"))
async def text_calc(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer("Запускаю персональный расчёт.", reply_markup=main_menu_kb())
    # имитируем нажатие кнопки
    await message.answer("1/4: В каком городе планируете запуск?")
    await state.set_state(CalcFlow.city)


# ---------------------------
# FSM: Calc flow
# ---------------------------
@dp.message(CalcFlow.city)
async def calc_city(message: Message, state: FSMContext) -> None:
    city = (message.text or "").strip()
    await state.update_data(city=city)
    await message.answer("2/4: Какой ориентировочно бюджет на старт (в ₽)? Одной цифрой, примерно.")
    await state.set_state(CalcFlow.investment)


@dp.message(CalcFlow.investment)
async def calc_investment(message: Message, state: FSMContext) -> None:
    inv = (message.text or "").strip()
    await state.update_data(investment=inv)
    await message.answer("3/4: Сколько готовы платить за аренду в месяц (в ₽), примерно?")
    await state.set_state(CalcFlow.rent)


@dp.message(CalcFlow.rent)
async def calc_rent(message: Message, state: FSMContext) -> None:
    rent = (message.text or "").strip()
    await state.update_data(rent=rent)
    await message.answer(
        "4/4: Оставьте контакт для уточнений (телефон/WhatsApp/Telegram).\n"
        "Можно просто номер."
    )
    await state.set_state(CalcFlow.contact)


@dp.message(CalcFlow.contact)
async def calc_contact(message: Message, state: FSMContext) -> None:
    contact = (message.text or "").strip()
    data = await state.get_data()
    payload = {
        "flow": "calc",


"city": data.get("city"),
        "investment": data.get("investment"),
        "rent": data.get("rent"),
        "contact": contact,
        "ts": now_utc_iso(),
    }

    await upsert_lead(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name,
        last_action="calc_done",
        phone_or_contact=contact,
        calc_payload=payload,
    )

    await notify_admin(
        "🧮 Запрос на персональный расчёт\n"
        f"User: {message.from_user.id} @{message.from_user.username}\n"
        f"Город: {payload['city']}\n"
        f"Бюджет: {payload['investment']}\n"
        f"Аренда: {payload['rent']}\n"
        f"Контакт: {payload['contact']}"
    )

    await state.clear()
    await message.answer(
        "Принято ✅\n\n"
        "Я передала вводные Дмитрию. Обычно он отвечает быстро.\n"
        "Если хотите — пока могу коротко рассказать, как устроена модель дохода и за счёт чего она работает.",
        reply_markup=main_menu_kb(),
    )


# ---------------------------
# FSM: Call flow
# ---------------------------
@dp.message(CallFlow.contact)
async def call_contact(message: Message, state: FSMContext) -> None:
    contact = (message.text or "").strip()
    await state.update_data(contact=contact)
    await message.answer("Отлично. Какое время созвона удобно? (например: сегодня 18:30 или завтра после 12)")
    await state.set_state(CallFlow.time)


@dp.message(CallFlow.time)
async def call_time(message: Message, state: FSMContext) -> None:
    pref_time = (message.text or "").strip()
    data = await state.get_data()
    contact = data.get("contact")

    payload = {
        "flow": "call",
        "contact": contact,
        "preferred_time": pref_time,
        "ts": now_utc_iso(),
    }

    await upsert_lead(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name,
        last_action="call_request",
        phone_or_contact=contact,
        calc_payload=payload,
    )

    await notify_admin(
        "📞 Запрос на созвон\n"
        f"User: {message.from_user.id} @{message.from_user.username}\n"
        f"Контакт: {contact}\n"
        f"Время: {pref_time}"
    )

    await state.clear()
    await message.answer(
        "Супер ✅ Я зафиксировала запрос и передала Дмитрию.\n"
        "Он напишет вам и подтвердит время.\n\n"
        "Пока могу отправить материалы или прикинуть цифры под вас:",
        reply_markup=main_menu_kb(),
    )


# ---------------------------
# Fallback: friendly talk
# ---------------------------
@dp.message()
async def fallback(message: Message) -> None:
    # минимальная “умность” без LLM: держим фокус на продаже и кнопках
    text = (message.text or "").strip()
    await upsert_lead(
        message.from_user.id,
        message.from_user.username,
        message.from_user.first_name,
        message.from_user.last_name,
        last_action="free_text",
    )

    if len(text) < 2:
        await message.answer("Я тут 🙂 Нажмите кнопку из меню:", reply_markup=main_menu_kb())
        return

    await message.answer(
        "Поняла. Чтобы не гонять вас по кругу — давайте выберем действие 👇",
        reply_markup=main_menu_kb(),
    )


# ---------------------------
# Main
# ---------------------------
async def main() -> None:
    await db_connect()
    log.info("Starting polling...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Bot stopped")
