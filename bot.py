exception("Failed to send finmodel: %s", e)

    if not sent_any:
        await message.answer(
            "Не нашла файлы в репозитории. Проверь пути:\n"
            "knowledge/assets/deck_main.pdf\n"
            "knowledge/assets/financial_model.xlsx"
        )


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await db_upsert_lead(message)
    await db_save_message(message.from_user.id, "user", "/start")

    # 1) сразу ценность — файлы
    await message.answer(
        "Привет! Я помогу быстро разобраться по франшизе.\n"
        "Сейчас отправлю презентацию и финмодель, а потом коротко уточню пару моментов."
    )
    await send_assets_if_exist(message)

    # 2) короткая первичка (без навязчивого созвона)
    text = (
        "Чтобы дать максимально полезный расклад под тебя, уточню 2 вещи:\n"
        "1) В каком городе планируешь запуск?\n"
        "2) Какой ориентир по стартовому бюджету (примерно)?\n\n"
        "Можешь ответить одной строкой 🙂"
    )
    await message.answer(text)
    await db_save_message(message.from_user.id, "assistant", text)


@router.message(Command("files"))
async def cmd_files(message: Message) -> None:
    await db_upsert_lead(message)
    await send_assets_if_exist(message)


@router.message(Command("ping"))
async def cmd_ping(message: Message) -> None:
    await message.answer("pong ✅")


@router.message(F.text)
async def handle_text(message: Message) -> None:
    await db_upsert_lead(message)

    user_text = (message.text or "").strip()
    await db_save_message(message.from_user.id, "user", user_text)

    # Быстрые команды по смыслу
    lowered = user_text.lower()
    if any(k in lowered for k in ["през", "презентац", "дек", "deck", "файл", "финмод", "фин модель", "excel", "xlsx"]):
        await message.answer("Отправляю файлы 👇")
        await send_assets_if_exist(message)
        await db_save_message(message.from_user.id, "assistant", "Отправляю файлы 👇")
        return

    # Если OpenAI ключа нет — не молчим, отвечаем шаблоном
    if not OPENAI_API_KEY:
        reply = (
            "Поняла. Чтобы не гадать и дать точный ответ — напиши, пожалуйста:\n"
            "• город запуска\n"
            "• бюджет (диапазон)\n"
            "• есть ли уже помещение/локация?\n\n"
            "Если удобнее — могу скинуть ещё раз файлы: /files"
        )
        await message.answer(reply)
        await db_save_message(message.from_user.id, "assistant", reply)
        return

    # OpenAI ответ с контекстом и историей
    history = await db_get_recent_messages(message.from_user.id, limit=12)

    messages = [{"role": "system", "content": SYSTEM_CONTEXT}]
    messages.extend(history)
    # (history уже содержит текущее сообщение user, но оставим как есть)

    ai_reply = await openai_chat(messages)
    if not ai_reply:
        ai_reply = (
            "Я вижу запрос, но сейчас не смогла получить ответ от модели (API/лимиты).\n"
            "Напиши город и бюджет — я продолжу в ручном режиме, и при необходимости пришлю файлы: /files"
        )

    await message.answer(ai_reply)
    await db_save_message(message.from_user.id, "assistant", ai_reply)


# =========================
# MAIN
# =========================
async def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    bot = Bot(
        token=TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # Удаляем webhook на всякий случай, чтобы polling работал
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Webhook deleted (if existed).")
    except Exception as e:
        logger.warning("delete_webhook failed (can ignore): %s", e)

    # DB + advisory lock (чтобы не было конфликтов getUpdates)
    await db_init()

    dp = Dispatcher()
    dp.include_router(router)

    logger.info("BOT STARTED ✅")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
