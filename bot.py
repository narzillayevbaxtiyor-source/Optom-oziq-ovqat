import os
import json
import sqlite3
from datetime import datetime, timezone
import requests
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# -----------------------
# CONFIG - TO'G'RILANGAN
# -----------------------
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()

# Admin ID ni xavfsiz o'qish
admin_id_str = os.getenv("ADMIN_ID")
ADMIN_ID = int(admin_id_str) if admin_id_str and admin_id_str.isdigit() else 6248061970

STORE_NAME = os.getenv("STORE_NAME", "Оптом_озик_овкат Мадина")
CURRENCY = "SAR"

DELIVERY_FEE_SAR = float(os.getenv("DELIVERY_FEE_SAR", "20"))
MIN_ORDER_SAR = float(os.getenv("MIN_ORDER_SAR", "50"))

DB_PATH = os.getenv("DB_PATH", "shop.db")
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

# -----------------------
# DB FUNCTIONS - TO'G'RILANGAN
# -----------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            category TEXT NOT NULL,
            image_url TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS carts (
            user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            qty INTEGER NOT NULL,
            PRIMARY KEY (user_id, product_id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            phone TEXT,
            address TEXT,
            lat REAL,
            lon REAL,
            updated_at TEXT
        )
    """)

    conn.commit()
    conn.close()

def seed_if_empty():
    conn = db()
    cur = conn.cursor()
    c = cur.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"]
    if c == 0:
        # datetime.utcnow() -> datetime.now(timezone.utc)
        now = datetime.now(timezone.utc).isoformat()
        demo = [
            ("Guruch 5kg", 45.0, "🍚 Guruch / Don"),
            ("O'simlik moyi 1L", 18.0, "🫒 Yog' / Moy"),
            ("Qora choy (paket)", 15.0, "🍵 Choy / Ichimlik"),
            ("Shakar 2kg", 9.0, "🍬 Shakar / Shirinlik"),
        ]
        for n, p, cat in demo:
            cur.execute(
                "INSERT INTO products(name, price, category, image_url, is_active, created_at) VALUES(?,?,?,?,?,?)",
                (n, p, cat, None, 1, now),
            )
        conn.commit()
    conn.close()

# -----------------------
# WIKIMEDIA SEARCH - YANGILANGAN
# -----------------------
def commons_search_images(query: str, limit: int = 4):
    """Yangi Wikimedia Commons API uchun yangilangan funksiya"""
    try:
        # 1) Fayllarni qidirish
        params = {
            "action": "query",
            "format": "json",
            "list": "search",
            "srsearch": query,
            "srnamespace": "6",
            "srlimit": limit,
            "srwhat": "text",
        }
        
        r = requests.get(COMMONS_API, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        
        hits = data.get("query", {}).get("search", [])
        if not hits:
            return []
        
        # 2) Rasm ma'lumotlarini olish
        titles = [f"File:{h['title']}" for h in hits]
        
        params2 = {
            "action": "query",
            "format": "json",
            "prop": "imageinfo",
            "iiprop": "url",
            "iiurlwidth": "800",
            "titles": "|".join(titles[:limit]),
        }
        
        r2 = requests.get(COMMONS_API, params=params2, timeout=15)
        r2.raise_for_status()
        data2 = r2.json()
        
        pages = data2.get("query", {}).get("pages", {})
        out = []
        
        for page_id, page in pages.items():
            imageinfo = page.get("imageinfo", [])
            if imageinfo:
                thumb_url = imageinfo[0].get("thumburl")
                if thumb_url:
                    out.append(thumb_url)
        
        return out[:limit]
        
    except Exception as e:
        print(f"Rasm qidirish xatosi: {e}")
        return []

# -----------------------
# CONVERSATION STATES - TO'G'RILANGAN
# -----------------------
# Checkout conversation states
ASK_NAME, ASK_PHONE, ASK_LOCATION, ASK_ADDRESS, CONFIRM = range(5)

# Admin add product states (alohida range)
A_CAT, A_NAME, A_PRICE, A_PICK_IMAGE, A_UPLOAD_IMAGE = range(5, 10)

# -----------------------
# MAIN FUNCTION - TO'G'RILANGAN
# -----------------------
def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN environment variable o'rnatilmagan")
    
    init_db()
    seed_if_empty()
    
    app = Application.builder().token(BOT_TOKEN).build()
    
    # User handlers
    app.add_handler(CommandHandler("start", start))
    
    # Menu handler - patternlarni aniqroq qilish
    app.add_handler(CallbackQueryHandler(
        on_menu, 
        pattern="^(menu_catalog|menu_cart|menu_rules|menu_checkout|admin:panel|admin:add)$"
    ))
    
    # Back handler alohida
    app.add_handler(CallbackQueryHandler(
        on_back, 
        pattern="^back:"
    ))
    
    app.add_handler(CallbackQueryHandler(on_category, pattern="^cat:"))
    app.add_handler(CallbackQueryHandler(on_product, pattern="^p:"))
    app.add_handler(CallbackQueryHandler(on_qty, pattern="^qty:"))
    app.add_handler(CallbackQueryHandler(on_cart_qty, pattern="^c:"))
    app.add_handler(CallbackQueryHandler(on_cart_clear, pattern="^cart:clear$"))
    
    # Checkout conversation
    checkout_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(checkout_start, pattern="^menu_checkout$")],
        states={
            ASK_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, checkout_name)],
            ASK_PHONE: [
                MessageHandler(filters.CONTACT, checkout_phone),
                MessageHandler(filters.TEXT & ~filters.COMMAND, checkout_phone),
            ],
            ASK_LOCATION: [MessageHandler(filters.LOCATION, checkout_location)],
            ASK_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, checkout_address)],
            CONFIRM: [CallbackQueryHandler(checkout_confirm, pattern="^order:(confirm|cancel)$")],
        },
        fallbacks=[CommandHandler("cancel", cancel_checkout)],
        allow_reentry=True,
    )
    app.add_handler(checkout_conv)
    
    # Admin add product conversation
    admin_add_conv = ConversationHandler(
        entry_points=[CallbackQueryHandler(admin_add_start, pattern="^admin:add$")],
        states={
            A_CAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_cat)],
            A_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_name)],
            A_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_price)],
            A_PICK_IMAGE: [
                CallbackQueryHandler(admin_pick_image, pattern="^admin:(pick:|upload_image|skip_image)")
            ],
            A_UPLOAD_IMAGE: [MessageHandler(filters.PHOTO, admin_upload_image)],
        },
        fallbacks=[CommandHandler("cancel", cancel_admin)],
        allow_reentry=True,
    )
    app.add_handler(admin_add_conv)
    
    app.run_polling()

# -----------------------
# YANGI FUNKSIYALAR - TO'G'RILANGAN
# -----------------------
async def on_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Back tugmasi uchun handler"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    where = query.data.split(":", 1)[1]
    if where == "home":
        await query.edit_message_text(
            f"Assalomu alaykum! 👋\n*{STORE_NAME}*",
            reply_markup=main_menu_kb(user_id),
            parse_mode="Markdown"
        )
    elif where == "cats":
        await query.edit_message_text(
            "🛒 Kategoriyalar:",
            reply_markup=categories_kb()
        )

async def checkout_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Checkout boshlanishi"""
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if not cart_items(user_id):
        await query.edit_message_text(
            "Avval savatchaga mahsulot qo'shing 🙂",
            reply_markup=main_menu_kb(user_id)
        )
        return ConversationHandler.END
    
    await query.edit_message_text("👤 Ism-familyangizni yozing:")
    return ASK_NAME

async def admin_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin mahsulot qo'shishni boshlash"""
    query = update.callback_query
    await query.answer()
    
    if query.from_user.id != ADMIN_ID:
        await query.edit_message_text("Ruxsat yo'q.", reply_markup=main_menu_kb(query.from_user.id))
        return ConversationHandler.END
    
    await query.edit_message_text("Kategoriya nomini yozing.\nMasalan: 🍚 Guruch / Don")
    return A_CAT

async def cancel_checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Checkoutni bekor qilish"""
    await update.message.reply_text(
        "Buyurtma bekor qilindi.",
        reply_markup=main_menu_kb(update.effective_user.id)
    )
    return ConversationHandler.END

async def cancel_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin rejimini bekor qilish"""
    await update.message.reply_text(
        "Admin rejimi bekor qilindi.",
        reply_markup=main_menu_kb(update.effective_user.id)
    )
    return ConversationHandler.END

if __name__ == "__main__":
    main()
