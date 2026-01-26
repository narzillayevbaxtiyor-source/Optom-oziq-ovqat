import os
import logging
from flask import Flask, request
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

# ================= CONFIG =================
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN yo‘q")
if not WEBHOOK_URL:
    raise RuntimeError("WEBHOOK_URL yo‘q")

logging.basicConfig(level=logging.INFO)

# ================= TELEGRAM APP =================
tg_app = Application.builder().token(BOT_TOKEN).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot ishlayapti. Webhook OK!")

tg_app.add_handler(CommandHandler("start", start))

# ================= FLASK SERVER =================
flask_app = Flask(__name__)

@flask_app.get("/")
def health():
    # Render health check shu yerga keladi
    return "OK", 200

@flask_app.post("/webhook")
def webhook():
    update = Update.de_json(request.get_json(force=True), tg_app.bot)
    tg_app.update_queue.put_nowait(update)
    return "OK", 200

# ================= MAIN =================
def main():
    # Webhook’ni Telegram’da o‘rnatamiz
    tg_app.bot.set_webhook(
        url=WEBHOOK_URL,
        drop_pending_updates=True
    )

    # Flask server
    flask_app.run(host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
