import os
import logging
from flask import Flask, request
from telegram import Update, Bot
from telegram.ext import Application, CommandHandler, ContextTypes

# ================= CONFIG =================
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEBHOOK_URL = os.getenv("WEBHOOK_URL")
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN or not WEBHOOK_URL:
    raise RuntimeError("Environment variables yo‘q")

logging.basicConfig(level=logging.INFO)

# ================= TELEGRAM =================
application = Application.builder().token(BOT_TOKEN).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot ishlayapti!")

application.add_handler(CommandHandler("start", start))

# ================= FLASK =================
app = Flask(__name__)

@app.get("/")
def health():
    return "OK", 200

@app.route("/webhook", methods=["GET", "POST"])
def webhook():
    # Health check GET
    if request.method == "GET":
        return "OK", 200

    # Telegram POST
    update = Update.de_json(request.get_json(force=True), application.bot)
    application.update_queue.put_nowait(update)
    return "OK", 200
# ================= MAIN =================
if __name__ == "__main__":
    application.bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)
    app.run(host="0.0.0.0", port=PORT)
