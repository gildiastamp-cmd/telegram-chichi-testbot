import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Optional, Any, Dict

import asyncpg
from aiogram import Bot, Dispatcher, F, Router, types
from aiogram.filters import CommandStart, Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage


# -------------------------
# Config
# -------------------------
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")


@dataclass
class Config:
    bot_token: str
    database_url: str
    assets_dir: str = "assets"
    deck_filename: str = "deck_main.pdf"
    finmodel_filename: str = "financial_model.xlsx"
    call_link: str = ""
    # amoCRM (optional; stub-safe)
    amo_base_url: str = ""
    amo_access_token: str = ""
    amo_pipeline_id: str = ""
    amo_status_id: str = ""


def load_config() -> Config:
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    database_url = os.getenv("DATABASE_URL", "").strip()
    if not bot_token:
        raise RuntimeError("BOT_TOKEN is missing")
    if not database_url:
        raise RuntimeError("DATABASE_URL is missing")

    return Config(
        bot_token=bot_token,
        database_url=database_url,
        assets_dir=os.getenv("ASSETS_DIR", "assets").strip() or "assets",
        deck_filename=os.getenv("DECK_FILE", "deck_main.pdf").strip() or "deck_main.pdf",
        finmodel_filename=os.getenv("FINMODEL_FILE", "financial_model.xlsx").strip() or "financial_model.xlsx",
        call_link=os.getenv("CALL_LINK", "").strip(),
        amo_base_url=os.getenv("AMO_BASE_URL", "").strip(),
        amo_access_token=os.getenv("AMO_ACCESS_TOKEN", "").strip(),
        amo_pipeline_id=os.getenv("AMO_PIPELINE_ID", "").strip(),
        amo_status_id=os.getenv("AMO_STATUS_ID", "").strip(),
    )


# -------------------------
# DB
# -------------------------
class DB:
    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    @classmethod
    async def create(cls, dsn: str) -> "DB":
        pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=5)
        db = cls(pool)
        await db.ensure_schema()
        return db

    async def ensure_schema(self) -> None:
        """
        'Не навреди': если таблица уже существует - НЕ трогаем данные,
        только добавляем недостающие колонки и индексы.
        """
        async with self.pool.acquire() as conn:
            # 1) create table if not exists (minimal)
            await conn.execute(
                """
                CREATE TABLE IF NOT EXISTS leads (
                    id BIGSERIAL PRIMARY KEY
                );
                """
            )

            # 2) add columns if missing
            # NOTE: ADD COLUMN IF NOT EXISTS works on modern Postgres (Railway обычно ок)
            await conn.execute("""ALTER TABLE leads ADD COLUMN IF NOT EXISTS tg_user_id BIGINT;""")
            await conn.execute("""ALTER TABLE leads ADD COLUMN IF NOT EXISTS username TEXT;""")
            await conn.execute("""ALTER TABLE leads ADD COLUMN IF NOT EXISTS first_name TEXT;""")
            await conn.execute("""ALTER TABLE leads ADD COLUMN IF NOT EXISTS last_name TEXT;""")
            await conn.execute("""ALTER TABLE leads ADD COLUMN IF NOT EXISTS language_code TEXT;""")
            await conn.execute("""ALTER TABLE leads ADD COLUMN IF NOT EXISTS city TEXT;""")
            await conn.execute("""ALTER TABLE leads ADD COLUMN IF NOT EXISTS phone TEXT;""")
            await conn.execute("""ALTER TABLE leads ADD COLUMN IF NOT EXISTS source TEXT;""")
            await conn.execute("""ALTER TABLE leads ADD COLUMN IF NOT EXISTS stage TEXT;""")
            await conn.execute("""ALTER TABLE leads ADD COLUMN IF NOT EXISTS state JSONB;""")
            await conn.execute("""ALTER TABLE leads ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();""")
            await conn.execute("""ALTER TABLE leads ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ DEFAULT NOW();""")

            # 3) indexes (safe)await conn.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS leads_tg_user_id_uidx ON leads(tg_user_id);"""
            )
            await conn.execute(
                """CREATE INDEX IF NOT EXISTS leads_last_seen_idx ON leads(last_seen_at);"""
            )

        log.info("DB schema ensured (self-healing migrations applied).")

    async def upsert_lead(self, user: types.User, source: str = "telegram") -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO leads (tg_user_id, username, first_name, last_name, language_code, source, last_seen_at)
                VALUES ($1, $2, $3, $4, $5, $6, NOW())
                ON CONFLICT (tg_user_id)
                DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name,
                    last_name = EXCLUDED.last_name,
                    language_code = EXCLUDED.language_code,
                    source = EXCLUDED.source,
                    last_seen_at = NOW();
                """,
                user.id,
                user.username,
                user.first_name,
                user.last_name,
                user.language_code,
                source,
            )

    async def set_stage(self, tg_user_id: int, stage: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE leads
                SET stage = $2, last_seen_at = NOW()
                WHERE tg_user_id = $1;
                """,
                tg_user_id,
                stage,
            )

    async def set_city(self, tg_user_id: int, city: str) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """UPDATE leads SET city = $2, last_seen_at = NOW() WHERE tg_user_id = $1;""",
                tg_user_id, city
            )

    async def set_state(self, tg_user_id: int, data: Dict[str, Any]) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute(
                """UPDATE leads SET state = $2::jsonb, last_seen_at = NOW() WHERE tg_user_id = $1;""",
                tg_user_id, json.dumps(data, ensure_ascii=False)
            )


# -------------------------
# Persona / UI
# -------------------------
IRA_NAME = "ИИра"

IRA_PERSONA = (
    f"Я {IRA_NAME} — личная ИИ-ассистентка Дмитрия Родионова.\n"
    "Я люблю ясность, быстрые решения и честные цифры. "
    "Из моих странностей — коллекционирую фотки луны над водой и умею вежливо дожимать до созвона 😌"
)

def main_menu_kb() -> types.InlineKeyboardMarkup:
    kb = [
        [types.InlineKeyboardButton(text="📄 Получить презентацию", callback_data="get_deck")],
        [types.InlineKeyboardButton(text="📊 Получить фин. модель", callback_data="get_finmodel")],
        [types.InlineKeyboardButton(text="🧮 Рассчитать персональную фин. модель", callback_data="calc_model")],
        [types.InlineKeyboardButton(text="📞 Назначить созвон с Дмитрием", callback_data="book_call")],
        [types.InlineKeyboardButton(text="✨ Чем вы лучше? (кратко)", callback_data="about")],
    ]
    return types.InlineKeyboardMarkup(inline_keyboard=kb)


def back_to_menu_kb() -> types.InlineKeyboardMarkup:
    return types.InlineKeyboardMarkup(
        inline_keyboard=[[types.InlineKeyboardButton(text="↩️ В меню", callback_data="menu")]]
    )


# -------------------------
# FSM (calc)
# -------------------------
class CalcFSM(StatesGroup):
    city = State()
    investment = State()
    format = State()


# -------------------------
# amoCRM (safe stub)
# -------------------------
async def send_to_amocrm_stub(cfg: Config, lead: Dict[str, Any]) -> None:
    """
    Безопасно: если amo env не заданы — просто логируем и выходим.
    Реальную интеграцию включим позже, не ломая работающий бот.
    """
    if not (cfg.amo_base_url and cfg.amo_access_token):
        log.info("amoCRM env not set -> skip integration (stub).")
        return# Здесь будет реальный запрос (httpx/aiohttp) — добавим, когда ты скажешь.
    # Сейчас intentionally no-op to avoid breaking production.
    log.info("amoCRM stub: would send lead=%s", lead)


# -------------------------
# Bot handlers
# -------------------------
router = Router()

def assets_path(cfg: Config, filename: str) -> str:
    return os.path.join(cfg.assets_dir, filename)

async def send_file_if_exists(message_or_cb: Any, cfg: Config, filename: str, caption: str) -> None:
    path = assets_path(cfg, filename)
    if not os.path.exists(path):
        text = (
            f"Упс. Файл не найден на сервере: {path}.\n"
            f"Проверь, что он лежит в репо и деплоится в Railway."
        )
        if isinstance(message_or_cb, types.CallbackQuery):
            await message_or_cb.message.answer(text, parse_mode="Markdown", reply_markup=back_to_menu_kb())
            await message_or_cb.answer()
        else:
            await message_or_cb.answer(text, parse_mode="Markdown", reply_markup=back_to_menu_kb())
        return

    doc = types.FSInputFile(path)
    if isinstance(message_or_cb, types.CallbackQuery):
        await message_or_cb.message.answer_document(document=doc, caption=caption)
        await message_or_cb.answer()
    else:
        await message_or_cb.answer_document(document=doc, caption=caption)


@router.message(CommandStart())
async def cmd_start(message: types.Message, state: FSMContext) -> None:
    cfg: Config = message.bot["cfg"]
    db: DB = message.bot["db"]

    await state.clear()
    await db.upsert_lead(message.from_user)
    await db.set_stage(message.from_user.id, "start")

    text = (
        f"Привет! Я {IRA_NAME} 👋\n\n"
        f"{IRA_PERSONA}\n\n"
        "Чтобы не тратить время — выбери, что прислать или что сделать:"
    )
    await message.answer(text, reply_markup=main_menu_kb())


@router.callback_query(F.data == "menu")
async def cb_menu(cb: types.CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.message.answer("Меню 👇", reply_markup=main_menu_kb())
    await cb.answer()


@router.callback_query(F.data == "get_deck")
async def cb_deck(cb: types.CallbackQuery) -> None:
    cfg: Config = cb.bot["cfg"]
    db: DB = cb.bot["db"]

    await db.upsert_lead(cb.from_user)
    await db.set_stage(cb.from_user.id, "sent_deck")

    await cb.message.answer("Держи материалы 👇")
    await send_file_if_exists(cb, cfg, cfg.deck_filename, f"Презентация франшизы ({cfg.deck_filename})")


@router.callback_query(F.data == "get_finmodel")
async def cb_finmodel(cb: types.CallbackQuery) -> None:
    cfg: Config = cb.bot["cfg"]
    db: DB = cb.bot["db"]

    await db.upsert_lead(cb.from_user)
    await db.set_stage(cb.from_user.id, "sent_finmodel")

    await cb.message.answer("Лови 👇")
    await send_file_if_exists(cb, cfg, cfg.finmodel_filename, f"Финансовая модель ({cfg.finmodel_filename})")


@router.callback_query(F.data == "about")
async def cb_about(cb: types.CallbackQuery) -> None:
    db: DB = cb.bot["db"]
    await db.upsert_lead(cb.from_user)
    await db.set_stage(cb.from_user.id, "about")

    text = (
        "Коротко и по делу:\n"
        "• Помогаем быстро выйти на понятную окупаемость (цифры — в финмодели).\n"
        "• Даем проверенные процессы: от запуска до стабильных продаж.\n"
        "• Сильная поддержка и контроль качества.\n\n"
        "Хочешь — скажи город и бюджет, я прикину реалистичный сценарий и предложу лучший следующий шаг."
    )
    await cb.message.answer(text, reply_markup=main_menu_kb())
    await cb.answer()


@router.callback_query(F.data == "book_call")
async def cb_book_call(cb: types.CallbackQuery) -> None:
    cfg: Config = cb.bot["cfg"]
    db: DB = cb.bot["db"]

    await db.upsert_lead(cb.from_user)
    await db.set_stage(cb.from_user.id, "book_call")

    if cfg.call_link:
        await cb.message.answer(
            f"Записаться на созвон с Дмитрием можно тут:\n{cfg.call_link}\n\n"
            "Если удобнее — напиши 2–3 окна по времени, я подстроюсь.",
            reply_markup=back_to_menu_kb(),
        )else:
        await cb.message.answer(
            "Ок, давай назначим созвон 👍\n"
            "Напиши, пожалуйста, 2–3 удобных окна по времени (и часовой пояс).",
            reply_markup=back_to_menu_kb(),
        )
    await cb.answer()


@router.callback_query(F.data == "calc_model")
async def cb_calc(cb: types.CallbackQuery, state: FSMContext) -> None:
    db: DB = cb.bot["db"]
    await db.upsert_lead(cb.from_user)
    await db.set_stage(cb.from_user.id, "calc_started")

    await state.set_state(CalcFSM.city)
    await cb.message.answer(
        "Супер. Сделаем быстрый расчет.\n\n"
        "1/3 — В каком ты городе (или стране)?",
        reply_markup=back_to_menu_kb(),
    )
    await cb.answer()


@router.message(CalcFSM.city)
async def fsm_city(message: types.Message, state: FSMContext) -> None:
    db: DB = message.bot["db"]
    city = (message.text or "").strip()
    await state.update_data(city=city)
    await db.set_city(message.from_user.id, city)
    await db.set_stage(message.from_user.id, "calc_city")

    await state.set_state(CalcFSM.investment)
    await message.answer("2/3 — Какой ориентир по инвестициям? (можно диапазон)")


@router.message(CalcFSM.investment)
async def fsm_investment(message: types.Message, state: FSMContext) -> None:
    db: DB = message.bot["db"]
    inv = (message.text or "").strip()
    await state.update_data(investment=inv)
    await db.set_stage(message.from_user.id, "calc_investment")

    await state.set_state(CalcFSM.format)
    await message.answer("3/3 — Какой формат хочешь? (например: 'с нуля', 'в партнёрстве', 'есть помещение')")


@router.message(CalcFSM.format)
async def fsm_format(message: types.Message, state: FSMContext) -> None:
    cfg: Config = message.bot["cfg"]
    db: DB = message.bot["db"]

    fmt = (message.text or "").strip()
    data = await state.get_data()
    city = data.get("city", "")
    inv = data.get("investment", "")

    await state.clear()
    await db.set_stage(message.from_user.id, "calc_done")
    await db.set_state(message.from_user.id, {"calc": {"city": city, "investment": inv, "format": fmt}})

    # safe amo stub (does not break)
    await send_to_amocrm_stub(cfg, {
        "tg_user_id": message.from_user.id,
        "username": message.from_user.username,
        "first_name": message.from_user.first_name,
        "city": city,
        "investment": inv,
        "format": fmt,
        "stage": "calc_done",
    })

    reply = (
        f"Принято ✅\n"
        f"Город: {city}\n"
        f"Инвестиции: {inv}\n"
        f"Формат: {fmt}\n\n"
        "Дальше честно: лучший рост конверсии дает короткий созвон с Дмитрием — "
        "он за 10–15 минут скажет, реалистично ли это по цифрам и какой сценарий лучше.\n"
    )

    if cfg.call_link:
        reply += f"\nСсылка на запись: {cfg.call_link}"
    else:
        reply += "\nНапиши 2–3 удобных окна по времени — я передам Дмитрию."

    await message.answer(reply, reply_markup=main_menu_kb())


# -------------------------
# Text triggers (non-button)
# -------------------------
@router.message(Command("help"))
async def cmd_help(message: types.Message) -> None:
    await message.answer("Жми кнопки в меню 👇", reply_markup=main_menu_kb())


@router.message(F.text)
async def any_text(message: types.Message) -> None:
    cfg: Config = message.bot["cfg"]
    db: DB = message.bot["db"]

    await db.upsert_lead(message.from_user)

    t = (message.text or "").strip().lower()

    if "презентац" in t:
        await db.set_stage(message.from_user.id, "sent_deck_text")
        await message.answer("Держи материалы 👇")
        await send_file_if_exists(message, cfg, cfg.deck_filename, f"Презентация франшизы ({cfg.deck_filename})")
        await message.answer("Если хочешь — могу прикинуть цифры под тебя. Жми «Рассчитать…» 👇", reply_markup=main_menu_kb())
        return

    if "финмодел" in t or "фин модель" in t or "фин. модель" in t:
        await db.set_stage(message.from_user.id, "sent_finmodel_text")
        await message.answer("Лови 👇")await send_file_if_exists(message, cfg, cfg.finmodel_filename, f"Финансовая модель ({cfg.finmodel_filename})")
        await message.answer("Хочешь персональный расчет? Жми «Рассчитать…» 👇", reply_markup=main_menu_kb())
        return

    if "созвон" in t or "звон" in t or "call" in t:
        await db.set_stage(message.from_user.id, "book_call_text")
        if cfg.call_link:
            await message.answer(f"Запись на созвон с Дмитрием:\n{cfg.call_link}", reply_markup=main_menu_kb())
        else:
            await message.answer("Ок. Напиши 2–3 удобных окна по времени (и часовой пояс).", reply_markup=main_menu_kb())
        return

    # default: keep bot "alive" and guide
    await db.set_stage(message.from_user.id, "chat")
    await message.answer(
        f"Я на связи 😊\n"
        f"Проще всего — выбрать действие кнопкой ниже.\n"
        f"Если хочешь, напиши: город + бюджет — и я предложу самый реалистичный следующий шаг.",
        reply_markup=main_menu_kb(),
    )


# -------------------------
# App entry
# -------------------------
async def main() -> None:
    cfg = load_config()

    bot = Bot(token=cfg.bot_token)
    dp = Dispatcher(storage=MemoryStorage())

    db = await DB.create(cfg.database_url)

    bot["cfg"] = cfg
    bot["db"] = db

    dp.include_router(router)

    log.info("Bot starting polling...")
    await dp.start_polling(bot)


if name == "__main__":
    asyncio.run(main())
