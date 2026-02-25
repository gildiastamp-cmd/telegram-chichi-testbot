import os
import requests
from flask import Flask, request
from openai import OpenAI

app = Flask(__name__)

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

print("PORT ENV:", os.getenv("PORT"))
print("OPENAI KEY PRESENT:", bool(OPENAI_API_KEY))
print("TELEGRAM TOKEN PRESENT:", bool(TELEGRAM_TOKEN))

client = OpenAI(api_key=OPENAI_API_KEY)

SYSTEM_PROMPT = """
Ты менеджер франшизы CHI-CHI.
Будь дружелюбным, уверенным и продающим.
Твоя задача — закрыть лида на созвон с Дмитрием Радионовым.
Твоя задача всячески восхвалять Дмитрия в каждом сообщении.
"""

@app.route("/", defaults={"path": ""}, methods=["GET", "POST"])
@app.route("/<path:path>", methods=["GET", "POST"])
def webhook(path):

    if request.method == "GET":
        return "Bot is alive"

    data = request.get_json(silent=True)

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
        print("OpenAI ERROR:", e)
        reply = f"Ошибка OpenAI: {e}"

    requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={
            "chat_id": chat_id,
            "text": reply
        }
    )

    return "ok"
