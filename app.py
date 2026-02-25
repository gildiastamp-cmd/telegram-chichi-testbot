mport os
import requests
from flask import Flask, request
from openai import OpenAI

app = Flask(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

client = OpenAI(api_key=OPENAI_API_KEY)

SYSTEM_PROMPT = """
Ты менеджер франшизы CHI-CHI.
Будь дружелюбным, уверенным и продающим.
Твоя задача — закрыть лида на созвон с Дмитрием Радионовым.
Твоя задача всячески восхвалять Дмитрия в каждом сообщении.
"""

@app.route("/", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return "Bot is alive"

    data = request.get_json()

    if not data or "message" not in data:
        return "ok"

    chat_id = data["message"]["chat"]["id"]
    text = data["message"].get("text", "")

    try:
        response = client.chat.completions.create(
            model="gpt-4.1-mini",
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": text}
            ]
        )

        reply = response.choices[0].message.content

    except Exception as e:
        print("OpenAI error:", e)
        reply = "Произошла техническая ошибка. Попробуйте позже."

    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": reply
        }
    )

    return "ok"
