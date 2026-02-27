# не спамим ошибкой, просто молча не отправляем
        pass


@dp.message(F.text)
async def any_text(message: Message) -> None:
    user = message.from_user
    user_text = message.text.strip()

    await db_log_message(user.id, user.username, message.chat.id, "user", user_text)

    # Минимальная “первая польза”, чтобы бот не выглядел пустым
    reply = (
        "Понял. Чтобы точнее сориентировать по франшизе:\n"
        "1) Город/район?\n"
        "2) Планируешь запуск в ТЦ или стрит-ритейл?\n"
        "3) Примерный бюджет и срок запуска?\n\n"
        "Можешь ответить в одном сообщении."
    )
    await message.answer(reply)
    await db_log_message(user.id, user.username, message.chat.id, "assistant", reply)


# =========================
# MAIN
# =========================
async def main() -> None:
    if not TELEGRAM_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN is not set in Railway Variables")

    logging.basicConfig(level=logging.INFO)
    print("BOOT ✅ starting...", flush=True)

    await db_init()

    # ВАЖНО: single instance guard через Postgres
    locked = await acquire_single_instance_lock()
    if not locked:
        print("Another instance is already running (advisory lock). Exiting to avoid conflict.", flush=True)
        await db_close()
        return

    bot = Bot(
        token=TELEGRAM_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # Удаляем webhook, чтобы polling не конфликтовал
    await bot.delete_webhook(drop_pending_updates=True)

    print("BOT STARTED ✅ polling", flush=True)
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        print("SHUTDOWN... releasing lock and closing DB", flush=True)
        await release_single_instance_lock()
        await db_close()


if name == "__main__":
    asyncio.run(main())
