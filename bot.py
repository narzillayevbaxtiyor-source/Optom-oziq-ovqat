# =========================
# OPTOM OZIK OVQAT MADINA BOT
# =========================

import os
import sqlite3
import requests
from datetime import datetime

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
    ConversationHandler,
    ContextTypes,
    filters,
)

# =========================
# CONFIG
# =========================
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN")
ADMIN_ID = int(os.getenv("ADMIN_ID", "6248061970"))
STORE_NAME = os.getenv("STORE_NAME", "Оптом_озик_овкат Мадина")
CURRENCY = "SAR"

DELIVERY_FEE = float(os.getenv("DELIVERY_FEE_SAR", "20"))
MIN_ORDER = float(os.getenv("MIN_ORDER_SAR", "50"))

DB = "shop.db"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

# =========================
# DB
# =========================
def db():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    con = db()
    cur = con.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        price REAL,
        category TEXT,
        image TEXT,
        active INTEGER DEFAULT 1
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS carts(
        user_id INTEGER,
        product_id INTEGER,
        qty INTEGER,
        PRIMARY KEY(user_id, product_id)
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS users(
        user_id INTEGER PRIMARY KEY,
        name TEXT,
        phone TEXT
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders(
        id TEXT,
        user_id INTEGER,
        total REAL,
        status TEXT,
        created TEXT
    )
    """)

    con.commit()
    con.close()


# =========================
# UTILS
# =========================
def safe_edit(q, text, kb=None):
    try:
        return q.edit_message_text(text, reply_markup=kb, parse_mode="Markdown")
    except:
        pass


def safe_send(bot, chat_id, text, kb=None):
    try:
        return bot.send_message(chat_id, text, reply_markup=kb, parse_mode="Markdown")
    except:
        pass


# =========================
# IMAGE SEARCH
# =========================
def search_image(query):
    try:
        r = requests.get(COMMONS_API, params={
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srnamespace": 6,
            "format": "json"
        }, timeout=10).json()

        title = r["query"]["search"][0]["title"]
        r2 = requests.get(COMMONS_API, params={
            "action": "query",
            "titles": title,
            "prop": "imageinfo",
            "iiprop": "url",
            "format": "json"
        }, timeout=10).json()

        page = next(iter(r2["query"]["pages"].values()))
        return page["imageinfo"][0]["url"]
    except:
        return None


# =========================
# MENUS
# =========================
def main_menu(uid):
    kb = [
        [InlineKeyboardButton("🛒 Каталог", callback_data="catalog")],
        [InlineKeyboardButton("🔍 Қидирув", callback_data="search")],
        [InlineKeyboardButton("🧺 Саватча", callback_data="cart")],
        [InlineKeyboardButton("📦 Буюртма бериш", callback_data="checkout")]
    ]
    if uid == ADMIN_ID:
        kb.append([InlineKeyboardButton("👑 Админ", callback_data="admin")])
    return InlineKeyboardMarkup(kb)


# =========================
# START
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"👋 Ассалому алайкум!\n*{STORE_NAME}*",
        reply_markup=main_menu(update.effective_user.id),
        parse_mode="Markdown"
    )


# =========================
# ADMIN PANEL
# =========================
A_CAT, A_NAME, A_PRICE = range(3)

async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Маҳсулот қўшиш", callback_data="add")],
        [InlineKeyboardButton("📣 Broadcast", callback_data="bc")],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
        [InlineKeyboardButton("⬅️ Орқага", callback_data="home")]
    ])
    await safe_edit(q, "👑 Админ панел", kb)


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("Категория номини ёзинг:")
    return A_CAT


async def add_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["cat"] = update.message.text
    await update.message.reply_text("Маҳсулот номини ёзинг:")
    return A_NAME


async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text
    await update.message.reply_text("Нархни киритинг:")
    return A_PRICE


async def add_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    price = float(update.message.text)
    name = context.user_data["name"]
    cat = context.user_data["cat"]
    img = search_image(name)

    con = db()
    con.execute(
        "INSERT INTO products(name, price, category, image) VALUES(?,?,?,?)",
        (name, price, cat, img)
    )
    con.commit()
    con.close()

    await update.message.reply_text("✅ Маҳсулот қўшилди", reply_markup=main_menu(ADMIN_ID))
    return ConversationHandler.END


# =========================
# CART & CATALOG
# =========================
async def show_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    con = db()
    rows = con.execute("SELECT DISTINCT category FROM products WHERE active=1").fetchall()
    con.close()

    kb = [[InlineKeyboardButton(r["category"], callback_data=f"cat:{r['category']}")] for r in rows]
    kb.append([InlineKeyboardButton("⬅️ Орқага", callback_data="home")])
    await safe_edit(q, "Категориялар:", InlineKeyboardMarkup(kb))


async def show_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cat = q.data.split(":")[1]

    con = db()
    items = con.execute(
        "SELECT * FROM products WHERE category=? AND active=1",
        (cat,)
    ).fetchall()
    con.close()

    for p in items:
        try:
            await context.bot.send_photo(
                chat_id=q.message.chat_id,
                photo=p["image"],
                caption=f"*{p['name']}*\n{p['price']} {CURRENCY}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Қўшиш", callback_data=f"addcart:{p['id']}")]
                ]),
                parse_mode="Markdown"
            )
        except:
            pass


async def add_to_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    pid = int(q.data.split(":")[1])
    uid = q.from_user.id

    con = db()
    cur = con.cursor()
    row = cur.execute(
        "SELECT qty FROM carts WHERE user_id=? AND product_id=?",
        (uid, pid)
    ).fetchone()

    if row:
        cur.execute(
            "UPDATE carts SET qty=qty+1 WHERE user_id=? AND product_id=?",
            (uid, pid)
        )
    else:
        cur.execute(
            "INSERT INTO carts VALUES(?,?,1)",
            (uid, pid)
        )

    con.commit()
    con.close()

    await q.answer("Қўшилди ✅", show_alert=False)


# =========================
# SEARCH
# =========================
SEARCH = 10

async def search_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()
    await update.callback_query.edit_message_text("Қидирилаётган маҳсулотни ёзинг:")
    return SEARCH


async def search_do(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = f"%{update.message.text.lower()}%"
    con = db()
    rows = con.execute(
        "SELECT id,name,price FROM products WHERE lower(name) LIKE ?",
        (text,)
    ).fetchall()
    con.close()

    if not rows:
        await update.message.reply_text("Топилмади")
        return ConversationHandler.END

    kb = [[InlineKeyboardButton(f"{r['name']} — {r['price']} {CURRENCY}", callback_data=f"addcart:{r['id']}")] for r in rows]
    await update.message.reply_text("Натижалар:", reply_markup=InlineKeyboardMarkup(kb))
    return ConversationHandler.END


# =========================
# MAIN
# =========================
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(show_catalog, pattern="^catalog$"))
    app.add_handler(CallbackQueryHandler(show_category, pattern="^cat:"))
    app.add_handler(CallbackQueryHandler(add_to_cart, pattern="^addcart:"))
    app.add_handler(CallbackQueryHandler(admin_panel, pattern="^admin$"))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(add_start, pattern="^add$")],
        states={
            A_CAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_cat)],
            A_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
            A_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_price)],
        },
        fallbacks=[]
    ))

    app.add_handler(ConversationHandler(
        entry_points=[CallbackQueryHandler(search_start, pattern="^search$")],
        states={SEARCH: [MessageHandler(filters.TEXT & ~filters.COMMAND, search_do)]},
        fallbacks=[]
    ))

    app.run_polling()


if __name__ == "__main__":
    main()
