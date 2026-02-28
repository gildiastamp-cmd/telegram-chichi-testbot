import asyncio
import logging
import os
from pathlib import Path

from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ParseMode
from aiogram.types import Message, FSInputFile
from aiogram.client.default import DefaultBotProperties

# ---------- LOGGING ----------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("bot")

# ---------- CONFIG ----------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("TELEGRAM_TOKEN")

# Путь к презентации в репозитории
PRESENTATION_PATH = os.getenv("PRESENTATION_PATH", "knowledge/assets/presentation.pdf")


# ---------- ROUTER ----------
router = Router()


@router.message(F.text.in_({"/start", "start"}))
async def cmd_start(message: Message):
    text = (
        "Привет! Я бот франшизы CHI-CHI.\n\n"
        "Могу:\n"
        "1) Отправить презентацию (напиши: <b>презентация</b>)\n"
        "2) Ответить на вопросы по франшизе (напиши вопрос текстом)\n"
    )
    await message.answer(text)


@router.message(F.text.lower().contains("презентац"))
async def send_presentation(message: Message):
    file_path = Path(PRESENTATION_PATH)
    if not file_path.exists():
        await message.answer(
            "Похоже, файл презентации не найден на сервере.\n"
            f"Я ищу тут: <code>{file_path.as_posix()}</code>\n\n"
            "Проверь, что ты закоммитил файл в репозиторий по этому пути "
            "или укажи переменную PRESENTATION_PATH."
        )
        return

    await message.answer("Ок, отправляю презентацию 👇")
    await message.answer_document(FSInputFile(file_path))


@router.message()
async def fallback(message: Message):
    # Пока простая заглушка: чтобы бот НЕ МОЛЧАЛ никогда
    await message.answer(
        "Принял. Напиши слово <b>презентация</b>, чтобы я отправил файл.\n"
        "Или задай вопрос — отвечу."
    )


# ---------- MAIN ----------
async def main():
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("ENV TELEGRAM_BOT_TOKEN is not set")

    bot = Bot(
        token=TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    log.info("BOT STARTED ✅")

    # На всякий случай убираем webhook, чтобы polling точно работал
    await bot.delete_webhook(drop_pending_updates=True)

    # ВАЖНО: allowed_updates можно оставить None (по умолчанию всё)
    await dp.start_polling(bot)


if name == "__main__":
    asyncio.run(main())
