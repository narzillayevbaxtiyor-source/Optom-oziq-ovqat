import os
import logging
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN yo‘q")
if not WEBHOOK_URL:
    raise RuntimeError("WEBHOOK_URL yo‘q")

logging.basicConfig(level=logging.INFO)

application = Application.builder().token(BOT_TOKEN).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot ishlayapti!")

application.add_handler(CommandHandler("start", start))

app = Flask(__name__)

@app.get("/")
def home():
    return "OK", 200

# Render health check GET bilan uradi, Telegram esa POST qiladi
@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return "OK", 200

    update = Update.de_json(request.get_json(force=True), application.bot)
    application.update_queue.put_nowait(update)
    return "OK", 200

if __name__ == "__main__":
    # webhookni o‘rnatib olamiz
    application.bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)

    # Flask serverni Render portida ochamiz
    app.run(host="0.0.0.0", port=PORT)
