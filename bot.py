"knowledge/assets/financial_model.xlsx"
        )

    return sent_any


# --------------------
# AI
# --------------------
def ensure_openai() -> None:
    global _openai_client
    if _openai_client is not None:
        return
    if not OPENAI_API_KEY or AsyncOpenAI is None:
        _openai_client = None
        return
    _openai_client = AsyncOpenAI(api_key=OPENAI_API_KEY)


async def ai_reply(user_text: str) -> str:
    # если OpenAI не настроен — простой ответ
    ensure_openai()
    if _openai_client is None:
        return (
            "Я на связи ✅\n"
            "Сейчас OpenAI не подключен (нет OPENAI_API_KEY), поэтому отвечаю без ИИ.\n"
            "Напиши: какой город/район рассматриваешь и какой бюджет на запуск?"
        )

    try:
        resp = await _openai_client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {
                    "role": "system",
                    "content": (
                        "Ты ассистент по продаже франшизы. "
                        "Твоя задача: вежливо, кратко, по делу, "
                        "дать первичную консультацию и мягко предложить созвон."
                    ),
                },
                {"role": "user", "content": user_text},
            ],
            temperature=0.6,
        )
        return (resp.choices[0].message.content or "").strip() or "Ок. Уточни, пожалуйста, город и бюджет запуска."
    except Exception as e:
        logger.exception("OpenAI error: %s", e)
        return "Поймал ошибку на стороне ИИ. Напиши город и бюджет — я продолжу без ИИ."


# --------------------
# HANDLERS
# --------------------
@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await db_upsert_lead(message)
    if message.from_user:
        await db_save_message(message.from_user.id, "user", "/start")

    await message.answer(
        "Привет! Я помогу быстро разобраться по франшизе.\n"
        "Сейчас отправлю презентацию и финмодель ✅"
    )

    await send_assets_if_exist(message)

    await message.answer(
        "Пара быстрых вопросов, чтобы подсказать точнее:\n"
        "1) В каком городе планируешь запуск?\n"
        "2) Что важнее сейчас: быстрее стартовать или уложиться в минимальный бюджет?"
    )


@router.message(F.text)
async def handle_text(message: Message) -> None:
    user = message.from_user
    if not user:
        return

    text = (message.text or "").strip()
    if not text:
        return

    await db_upsert_lead(message)
    await db_save_message(user.id, "user", text)

    # на всякий случай — команда "файлы"
    if text.lower() in {"файлы", "преза", "презентация", "финмодель", "фин модель"}:
        await send_assets_if_exist(message)
        return

    reply = await ai_reply(text)
    await db_save_message(user.id, "assistant", reply)
    await message.answer(reply)


# --------------------
# MAIN
# --------------------
async def main() -> None:
    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    await init_db()

    bot = Bot(
        token=TELEGRAM_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    # если когда-то был webhook — удаляем, чтобы polling работал стабильно
    await bot.delete_webhook(drop_pending_updates=True)

    logger.info("BOT STARTED ✅")
    await dp.start_polling(bot)


if name == "__main__":
    asyncio.run(main())
