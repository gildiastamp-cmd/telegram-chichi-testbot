FSInputFile(str(DECK_PATH)),
        caption="📎 Презентация франшизы (deck_main.pdf)",
    )

    # XLSX finmodel
    await message.answer_document(
        FSInputFile(str(FINMODEL_PATH)),
        caption="📎 Финмодель (financial_model.xlsx)",
    )

    await set_sent_assets(pool, user_id, True)


# -----------------------
# HANDLERS
# -----------------------
@router.message(CommandStart())
async def cmd_start(message: Message, bot: Bot) -> None:
    await message.answer(
        "Привет! Я помогу разобраться с франшизой CHI-CHI.\n"
        "Можешь написать город/бюджет/сроки — я подскажу по формату и шагам запуска."
    )


@router.message(Command("reset"))
async def cmd_reset(message: Message, pool: asyncpg.Pool) -> None:
    # Сброс флага, чтобы снова отправить материалы
    await set_sent_assets(pool, message.from_user.id, False)
    await message.answer("Сбросил. Теперь при следующем сообщении снова отправлю материалы.")


@router.message(F.text)
async def handle_text(message: Message, pool: asyncpg.Pool, openai_client: Optional["AsyncOpenAI"]) -> None:
    # 1) отправляем материалы один раз
    await send_assets_if_needed(message, pool)

    text = (message.text or "").strip()
    if not text:
        return

    # 2) если OpenAI не подключен — хотя бы не молчим
    if openai_client is None:
        await message.answer(
            "Принял вопрос. (OpenAI сейчас не подключён по ключу/пакету.)\n"
            "Напиши: город, бюджет на старт и какой формат интересен — я отвечу по структуре."
        )
        return

    # 3) отвечаем через LLM
    try:
        ans = await ai_answer(openai_client, text)
        if not ans:
            ans = "Понял. Уточни, пожалуйста: город и бюджет на запуск — тогда отвечу точнее."
        await message.answer(ans)
    except Exception as e:
        log.exception("OpenAI error: %s", e)
        await message.answer("Сейчас не смог сформировать ответ (ошибка AI). Попробуй ещё раз через минуту.")


# -----------------------
# MAIN
# -----------------------
async def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is empty")

    # DB pool (optional but recommended)
    pool: Optional[asyncpg.Pool] = None
    if DATABASE_URL:
        pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)
        await init_db(pool)
        log.info("DB connected ✅")
    else:
        log.warning("DATABASE_URL is empty — DB features disabled (assets will re-send each time).")

    bot = Bot(
        token=TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # IMPORTANT: remove webhook to avoid getUpdates конфликтов
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        log.info("Webhook deleted ✅")
    except Exception as e:
        log.warning("delete_webhook failed: %s", e)

    dp = Dispatcher()
    dp.include_router(router)

    # OpenAI client (optional)
    openai_client = build_openai_client()
    if openai_client:
        log.info("OpenAI client ready ✅ (%s)", OPENAI_MODEL)
    else:
        log.warning("OpenAI disabled (no OPENAI_API_KEY or package missing).")

    # inject dependencies
    dp["pool"] = pool  # type: ignore
    dp["openai_client"] = openai_client  # type: ignore

    # wrappers to pass deps into handlers
    # aiogram v3 gets them from dp context by parameter names
    log.info("BOT STARTED ✅")
    await dp.start_polling(bot, pool=pool, openai_client=openai_client)

    # cleanup
    if pool:
        await pool.close()


if name == "__main__":
    asyncio.run(main())
