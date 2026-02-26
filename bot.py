append({"role": "system", "content": f"Фрагменты материалов (используй их):\n{kb}"})

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


# =====================
# MAIN
# =====================
async def main():
    init_db()
    print("BOT STARTED ✅", flush=True)

    bot = Bot(token=TELEGRAM_BOT_TOKEN, parse_mode=ParseMode.HTML)
    await dp.start_polling(bot)


if name == "__main__":
    asyncio.run(main())
