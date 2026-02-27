import os
import asyncio
from pathlib import Path
from typing import List, Tuple

import psycopg2
from psycopg2.extras import RealDictCursor

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.enums import ParseMode

from openai import OpenAI


# =====================
# ENV / CONFIG
# =====================
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
DATABASE_URL = os.getenv("DATABASE_URL")

BASE_DIR = Path(__file__).parent
KNOWLEDGE_DIR = BASE_DIR / "knowledge"
ASSETS_DIR = KNOWLEDGE_DIR / "assets"

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError("Missing TELEGRAM_BOT_TOKEN in Railway Variables")
if not OPENAI_API_KEY:
    raise RuntimeError("Missing OPENAI_API_KEY in Railway Variables")
if not DATABASE_URL:
    raise RuntimeError("Missing DATABASE_URL in Railway Variables (add Variable Reference from Postgres service)")

client = OpenAI(api_key=OPENAI_API_KEY)

dp = Dispatcher()


# =====================
# DB
# =====================
def db_conn():
    return psycopg2.connect(DATABASE_URL)


def init_db():
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS leads (
                    user_id TEXT PRIMARY KEY,
                    brief TEXT NOT NULL DEFAULT '',
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                );
                """
            )
            conn.commit()


def get_brief(user_id: str) -> str:
    with db_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT brief FROM leads WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            return row["brief"] if row else ""


def save_brief(user_id: str, brief: str):
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO leads (user_id, brief, updated_at)
                VALUES (%s, %s, NOW())
                ON CONFLICT (user_id)
                DO UPDATE SET brief = EXCLUDED.brief, updated_at = NOW();
                """,
                (user_id, brief),
            )
            conn.commit()


# =====================
# KNOWLEDGE (simple retrieval)
# =====================
def read_text(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


SYSTEM_PROMPT = read_text(KNOWLEDGE_DIR / "system_prompt.txt")


def load_md_chunks() -> List[str]:
    chunks: List[str] = []
    for p in KNOWLEDGE_DIR.glob("*.md"):
        text = read_text(p)
        if not text:
            continue
        # простая нарезка по пустым строкам
        parts = [x.strip() for x in text.split("\n\n") if x.strip()]
        chunks.extend(parts)
    return chunks


KB_CHUNKS = load_md_chunks()


def retrieve_kb(query: str, k: int = 5) -> str:
    q = query.lower()
    words = [w for w in q.split() if len(w) > 3]
    scored: List[Tuple[int, str]] = []

    for ch in KB_CHUNKS:
        low = ch.lower()
        score = sum(1 for w in words if w in low)
        if score > 0:
            scored.append((score, ch))

    scored.sort(key=lambda x: x[0], reverse=True)
    top = [c for _, c in scored[:k]]
    return "\n\n".join(top).strip()


# =====================
# AI
# =====================
def ai_answer(user_text: str, brief: str) -> str:
    kb = retrieve_kb(user_text, k=5)

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT
            or (
                "Ты — тёплый и уверенный ассистент по продаже франшизы. "
                "Цель — довести до созвона. Пиши коротко и по делу. "
                "Не выдумывай факты и цифры."
            ),
        },
    ]

    if brief:
        messages.append({"role": "system", "content": f"Бриф лида (память):\n{brief}"})

    if kb:
        messages.append({"role": "system", "content": f"Фрагменты материалов (используй их):\n{kb}"})

    messages.append({"role": "user", "content": user_text})

    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        temperature=0.6,
    )
    return resp.choices[0].message.content.strip()


def ai_update_brief(user_text: str, assistant_text: str, current_brief: str) -> str:
    # коротко и дёшево: обновляем сводку лида
    messages = [
        {
            "role": "system",
            "content": (
                "Ты — помощник CRM. Обнови бриф лида по новому сообщению.\n"
                "Формат строго 6 строк:\n"
                "1) Город/регион:\n"
                "2) Срок запуска:\n"
                "3) Бюджет:\n"
                "4) Интерес/этап:\n"
                "5) Возражения/риски:\n"
                "6) Следующий шаг:\n"
                "Если данных нет — пиши 'не указано'. Не добавляй лишнего текста."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Текущий бриф:\n{current_brief or 'пусто'}\n\n"
                f"Сообщение лида:\n{user_text}\n\n"
                f"Ответ ассистента:\n{assistant_text}\n\n"
                "Обнови бриф:"
            ),
        },
    ]

    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        temperature=0.2,
    )
    return resp.choices[0].message.content.strip()


# =====================
# TELEGRAM HANDLERS
# =====================
@dp.message(F.text == "/start")
async def start(message: Message):
    await message.answer("Привет! 😊 В каком городе/стране рассматриваете запуск франшизы CHI-CHI?")


@dp.message(F.text == "/materials")
async def materials(message: Message):
    sent = False

    deck = ASSETS_DIR / "deck_main.pdf"
    if deck.exists():
        await message.answer_document(deck.open("rb"), caption="Презентация франшизы (PDF)")
        sent = True

    model = ASSETS_DIR / "financial_model.xlsx"
    if model.exists():
        await message.answer_document(model.open("rb"), caption="Финансовая модель (Excel)")
        sent = True

    if not sent:
        await message.answer("Материалы пока не загружены в knowledge/assets 🙂")


@dp.message(F.text == "/reset")
async def reset(message: Message):
    user_id = str(message.from_user.id)
    save_brief(user_id, "")
    await message.answer("Ок, сбросила бриф. С чего начнём? 🙂")


@dp.message(F.text)
async def handle_text(message: Message):
    user_id = str(message.from_user.id)
    user_text = message.text.strip()

    brief = get_brief(user_id)

    # 1) ответ
    try:
        answer = ai_answer(user_text, brief)
    except Exception:
        await message.answer("Техническая заминка. Напишите, пожалуйста, ещё раз через минуту 🙏")
        return

    await message.answer(answer)

    # 2) обновление брифа
    try:
        new_brief = ai_update_brief(user_text, answer, brief)
        save_brief(user_id, new_brief)
    except Exception:
        # если обновление брифа упало — не ломаем диалог
        pass

# ======================
# MAIN
# ======================

async def main():
    init_db()
    print("BOT STARTED ✅", flush=True)

    # если у бота когда-то был webhook — удаляем, чтобы polling работал
    await bot.delete_webhook(drop_pending_updates=True)

    await dp.start_polling(bot)


if __name__ ==__"__main__":
    import asyncio
    asyncio.run(main())
