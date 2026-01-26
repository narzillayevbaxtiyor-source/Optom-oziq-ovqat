import os
import logging
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()  # https://xxxx.onrender.com/webhook
PORT = int(os.getenv("PORT", "10000"))

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN yo‘q. Render -> Environment ga qo‘ying.")
if not WEBHOOK_URL:
    raise RuntimeError("WEBHOOK_URL yo‘q. Render -> Environment ga qo‘ying.")
if not WEBHOOK_URL.startswith("https://"):
    raise RuntimeError("WEBHOOK_URL https:// bilan boshlanishi shart.")
if not WEBHOOK_URL.endswith("/webhook"):
    raise RuntimeError("WEBHOOK_URL oxiri /webhook bo‘lishi kerak. Masalan: https://xxx.onrender.com/webhook")

logging.basicConfig(level=logging.INFO)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Bot ishlayapti! (/ping ham bor)")

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("pong ✅")

def main():
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))

    # Muhim: polling YO‘Q
    app.run_webhook(
        listen="0.0.0.0",
        port=PORT,
        url_path="webhook",
        webhook_url=WEBHOOK_URL,
        allowed_updates=Update.ALL_TYPES,
    )

if __name__ == "__main__":
    main()
