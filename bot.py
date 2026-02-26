import os
import asyncio
from pathlib import Path
from typing import Dict, Any, List
import psycopg2
from psycopg2.extras import RealDictCursor

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message
from aiogram.enums import ParseMode
from openai import OpenAI

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini")
DATABASE_URL = os.getenv("DATABASE_URL")

BASE_DIR = Path(__file__).parent
KNOWLEDGE_DIR = BASE_DIR / "knowledge"
ASSETS_DIR = KNOWLEDGE_DIR / "assets"

client = OpenAI(api_key=OPENAI_API_KEY)

# ------------------------
# Database
# ------------------------
def get_conn():
    return psycopg2.connect(DATABASE_URL)

def init_db():
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS leads (
                    user_id TEXT PRIMARY KEY,
                    brief TEXT
                );
            """)
            conn.commit()

def get_brief(user_id: str) -> str:
    with get_conn() as conn:
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("SELECT brief FROM leads WHERE user_id = %s", (user_id,))
            row = cur.fetchone()
            return row["brief"] if row else ""

def save_brief(user_id: str, brief: str):
    with get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO leads (user_id, brief)
                VALUES (%s, %s)
                ON CONFLICT (user_id)
                DO UPDATE SET brief = EXCLUDED.brief;
            """, (user_id, brief))
            conn.commit()

# ------------------------
# Knowledge
# ------------------------
def load_md_chunks() -> List[str]:
    chunks = []
    for p in KNOWLEDGE_DIR.glob("*.md"):
        text = p.read_text(encoding="utf-8")
        chunks.extend(text.split("\n\n"))
    return chunks

KB_CHUNKS = load_md_chunks()

def retrieve_kb(query: str, k: int = 4) -> str:
    q = query.lower()
    scored = []
    for ch in KB_CHUNKS:
        score = sum(1 for w in q.split() if w in ch.lower())
        if score:
            scored.append((score, ch))
    scored.sort(reverse=True, key=lambda x: x[0])
    return "\n\n".join([c[1] for c in scored[:k]])

SYSTEM_PROMPT = (KNOWLEDGE_DIR / "system_prompt.txt").read_text(encoding="utf-8")

def update_brief(user_text: str, assistant_text: str, current_brief: str) -> str:
    messages = [
        {"role": "system", "content":
         "Обнови краткий бриф лида. Формат: Город, Бюджет, Срок, Интерес, Возражения, Следующий шаг."},
        {"role": "user", "content":
         f"Текущий бриф:\n{current_brief}\n\nСообщение лида:\n{user_text}\n\nОтвет:\n{assistant_text}\n\nОбнови бриф:"}
    ]
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        temperature=0.2,
    )
    return resp.choices[0].message.content.strip()

def answer_ai(user_text: str, brief: str) -> str:
    kb = retrieve_kb(user_text)

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "system", "content": f"Бриф лида:\n{brief}"},
        {"role": "system", "content": f"Материалы:\n{kb}"},
        {"role": "user", "content": user_text}
    ]

    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        messages=messages,
        temperature=0.6,
    )

    return resp.choices[0].message.content.strip()

# ------------------------
# Telegram
# ------------------------
dp = Dispatcher()

@dp.message(F.text == "/start")
async def start(message: Message):
    await message.answer("Привет! 😊 В каком городе рассматриваете запуск?")

@dp.message(F.text)
async def handle(message: Message):
    user_id = str(message.from_user.id)
    user_text = message.text.strip()

    brief = get_brief(user_id)

    answer = answer_ai(user_text, brief)
    await message.answer(answer)

    new_brief = update_brief(user_text, answer, brief)
    save_brief(user_id, new_brief)

async def main():init_db():
    bot = Bot(token=TELEGRAM_TOKEN, parse_mode=ParseMode.HTML)
    await dp.start_polling(bot)

if name == "__main__":
    asyncio.run(main())
