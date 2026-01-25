import os
import json
import sqlite3
from datetime import datetime
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
# CONFIG
# -----------------------
BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
ADMIN_ID = int(os.getenv("ADMIN_ID", "6248061970"))

STORE_NAME = os.getenv("STORE_NAME", "Оптом_озик_овкат Мадина")
CURRENCY = "SAR"

DELIVERY_FEE_SAR = float(os.getenv("DELIVERY_FEE_SAR", "20"))
MIN_ORDER_SAR = float(os.getenv("MIN_ORDER_SAR", "50"))

DB_PATH = os.getenv("DB_PATH", "shop.db")

# Wikimedia Commons (free)
COMMONS_API = "https://commons.wikimedia.org/w/api.php"


# -----------------------
# DB
# -----------------------
def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS products (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            price REAL NOT NULL,
            category TEXT NOT NULL,
            image_url TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS carts (
            user_id INTEGER NOT NULL,
            product_id INTEGER NOT NULL,
            qty INTEGER NOT NULL,
            PRIMARY KEY (user_id, product_id)
        )
        """
    )

    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            full_name TEXT,
            phone TEXT,
            address TEXT,
            lat REAL,
            lon REAL,
            updated_at TEXT
        )
        """
    )

    conn.commit()
    conn.close()


def seed_if_empty():
    conn = db()
    cur = conn.cursor()
    c = cur.execute("SELECT COUNT(*) AS n FROM products").fetchone()["n"]
    if c == 0:
        now = datetime.utcnow().isoformat()
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
# Products queries
# -----------------------
def get_categories():
    conn = db()
    rows = conn.execute(
        "SELECT DISTINCT category FROM products WHERE is_active=1 ORDER BY category"
    ).fetchall()
    conn.close()
    return [r["category"] for r in rows]


def get_products_by_category(category: str):
    conn = db()
    rows = conn.execute(
        "SELECT * FROM products WHERE is_active=1 AND category=? ORDER BY name",
        (category,),
    ).fetchall()
    conn.close()
    return rows


def get_product(pid: int):
    conn = db()
    row = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    conn.close()
    return row


def set_product_image(pid: int, image_url: str | None):
    conn = db()
    conn.execute("UPDATE products SET image_url=? WHERE id=?", (image_url, pid))
    conn.commit()
    conn.close()


def add_product(name: str, price: float, category: str, image_url: str | None = None):
    conn = db()
    now = datetime.utcnow().isoformat()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO products(name, price, category, image_url, is_active, created_at) VALUES(?,?,?,?,?,?)",
        (name, price, category, image_url, 1, now),
    )
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return pid


# -----------------------
# Cart utils
# -----------------------
def cart_add(user_id: int, product_id: int, qty: int = 1):
    conn = db()
    cur = conn.cursor()
    row = cur.execute(
        "SELECT qty FROM carts WHERE user_id=? AND product_id=?",
        (user_id, product_id),
    ).fetchone()
    if row:
        new_qty = row["qty"] + qty
        if new_qty <= 0:
            cur.execute("DELETE FROM carts WHERE user_id=? AND product_id=?", (user_id, product_id))
        else:
            cur.execute(
                "UPDATE carts SET qty=? WHERE user_id=? AND product_id=?",
                (new_qty, user_id, product_id),
            )
    else:
        if qty > 0:
            cur.execute(
                "INSERT INTO carts(user_id, product_id, qty) VALUES(?,?,?)",
                (user_id, product_id, qty),
            )
    conn.commit()
    conn.close()


def cart_clear(user_id: int):
    conn = db()
    conn.execute("DELETE FROM carts WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()


def cart_items(user_id: int):
    conn = db()
    rows = conn.execute(
        "SELECT product_id, qty FROM carts WHERE user_id=? ORDER BY product_id",
        (user_id,),
    ).fetchall()
    conn.close()
    return [(r["product_id"], r["qty"]) for r in rows]


def cart_count(user_id: int):
    return sum(q for _, q in cart_items(user_id))


def cart_total(user_id: int):
    total = 0.0
    conn = db()
    for pid, qty in cart_items(user_id):
        p = conn.execute("SELECT price FROM products WHERE id=?", (pid,)).fetchone()
        if p:
            total += float(p["price"]) * qty
    conn.close()
    return total


# -----------------------
# User profile
# -----------------------
def upsert_user(user_id: int, full_name=None, phone=None, address=None, lat=None, lon=None):
    conn = db()
    cur = conn.cursor()
    exists = cur.execute("SELECT user_id FROM users WHERE user_id=?", (user_id,)).fetchone() is not None
    now = datetime.utcnow().isoformat()

    if not exists:
        cur.execute(
            "INSERT INTO users(user_id, full_name, phone, address, lat, lon, updated_at) VALUES(?,?,?,?,?,?,?)",
            (user_id, full_name, phone, address, lat, lon, now),
        )
    else:
        row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
        cur.execute(
            """
            UPDATE users SET full_name=?, phone=?, address=?, lat=?, lon=?, updated_at=?
            WHERE user_id=?
            """,
            (
                full_name if full_name is not None else row["full_name"],
                phone if phone is not None else row["phone"],
                address if address is not None else row["address"],
                lat if lat is not None else row["lat"],
                lon if lon is not None else row["lon"],
                now,
                user_id,
            ),
        )
    conn.commit()
    conn.close()


def get_user(user_id: int):
    conn = db()
    row = conn.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    conn.close()
    return row


# -----------------------
# Wikimedia image search
# -----------------------
def commons_search_images(query: str, limit: int = 4):
    """
    Returns list of thumbnail URLs from Wikimedia Commons for a query.
    No API key required.
    """
    try:
        # 1) search files in namespace 6 (File:)
        params = {
            "action": "query",
            "format": "json",
            "origin": "*",
            "list": "search",
            "srsearch": query,
            "srnamespace": "6",
            "srlimit": str(limit),
        }
        r = requests.get(COMMONS_API, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        hits = data.get("query", {}).get("search", [])
        titles = [h["title"] for h in hits]  # e.g., "File:Something.jpg"
        if not titles:
            return []

        # 2) get imageinfo with thumbnail
        params2 = {
            "action": "query",
            "format": "json",
            "origin": "*",
            "prop": "imageinfo",
            "iiprop": "url",
            "iiurlwidth": "800",
            "titles": "|".join(titles),
        }
        r2 = requests.get(COMMONS_API, params=params2, timeout=15)
        r2.raise_for_status()
        data2 = r2.json()
        pages = data2.get("query", {}).get("pages", {})
        out = []
        for _, page in pages.items():
            ii = page.get("imageinfo")
            if ii and len(ii) > 0:
                thumb = ii[0].get("thumburl") or ii[0].get("url")
                if thumb:
                    out.append(thumb)
        return out[:limit]
    except Exception:
        return []


# -----------------------
# UI builders
# -----------------------
def main_menu_kb(user_id: int):
    cart_n = cart_count(user_id)
    kb = [
        [InlineKeyboardButton("🛒 Katalog", callback_data="menu_catalog")],
        [InlineKeyboardButton(f"🧺 Savatcha ({cart_n})", callback_data="menu_cart")],
        [InlineKeyboardButton("📦 Buyurtma berish", callback_data="menu_checkout")],
        [InlineKeyboardButton("ℹ️ Yetkazish shartlari", callback_data="menu_rules")],
    ]
    if user_id == ADMIN_ID:
        kb.append([InlineKeyboardButton("👑 Admin panel", callback_data="admin:panel")])
    return InlineKeyboardMarkup(kb)


def categories_kb():
    cats = get_categories()
    rows = [[InlineKeyboardButton(cat, callback_data=f"cat:{cat}")] for cat in cats]
    rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="back:home")])
    return InlineKeyboardMarkup(rows)


def products_kb(category: str):
    items = get_products_by_category(category)
    rows = []
    for p in items:
        rows.append([InlineKeyboardButton(f"{p['name']} — {p['price']} {CURRENCY}", callback_data=f"p:{p['id']}")])
    rows.append([InlineKeyboardButton("⬅️ Kategoriyalar", callback_data="back:cats")])
    rows.append([InlineKeyboardButton("🏠 Bosh menu", callback_data="back:home")])
    return InlineKeyboardMarkup(rows)


def product_kb(pid: int):
    rows = [
        [InlineKeyboardButton("➖", callback_data=f"qty:-:{pid}"),
         InlineKeyboardButton("➕", callback_data=f"qty:+:{pid}")],
        [InlineKeyboardButton("🧺 Savatcha", callback_data="menu_cart")],
        [InlineKeyboardButton("⬅️ Orqaga", callback_data="back:cats")],
    ]
    return InlineKeyboardMarkup(rows)


def cart_kb(user_id: int):
    items = cart_items(user_id)
    rows = []
    for pid, qty in items:
        p = get_product(pid)
        if not p:
            continue
        rows.append(
            [
                InlineKeyboardButton("➖", callback_data=f"c:-:{pid}"),
                InlineKeyboardButton(f"{qty} x {p['name']}", callback_data=f"p:{pid}"),
                InlineKeyboardButton("➕", callback_data=f"c:+:{pid}"),
            ]
        )
    rows.append([InlineKeyboardButton("🧹 Savatchani tozalash", callback_data="cart:clear")])
    rows.append([InlineKeyboardButton("✅ Buyurtma berish", callback_data="menu_checkout")])
    rows.append([InlineKeyboardButton("⬅️ Bosh menu", callback_data="back:home")])
    return InlineKeyboardMarkup(rows)


def contact_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📞 Telefon raqamni yuborish", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


def location_keyboard():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Lokatsiyani yuborish", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True,
    )


# -----------------------
# Checkout conversation states
# -----------------------
ASK_NAME, ASK_PHONE, ASK_LOCATION, ASK_ADDRESS, CONFIRM = range(5)

# Admin add product states
A_CAT, A_NAME, A_PRICE, A_PICK_IMAGE, A_UPLOAD_IMAGE = range(5, 10)


# -----------------------
# Handlers
# -----------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    upsert_user(user.id, full_name=user.full_name)
    await update.message.reply_text(
        f"Assalomu alaykum! 👋\n*{STORE_NAME}*\n\nKatalogdan tanlang ✅",
        reply_markup=main_menu_kb(user.id),
        parse_mode="Markdown",
    )


async def on_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "menu_catalog":
        await query.edit_message_text("🛒 Kategoriyalar:", reply_markup=categories_kb())

    elif query.data == "menu_cart":
        total = cart_total(user_id)
        items = cart_items(user_id)
        if not items:
            await query.edit_message_text("Savatcha bo‘sh 🧺", reply_markup=main_menu_kb(user_id))
            return
        text = f"🧺 *Savatcha*\n\nJami: *{total:.2f} {CURRENCY}*\nYetkazish: *{DELIVERY_FEE_SAR:.0f} {CURRENCY}*"
        await query.edit_message_text(text, reply_markup=cart_kb(user_id), parse_mode="Markdown")

    elif query.data == "menu_rules":
        await query.edit_message_text(
            "ℹ️ *Yetkazish shartlari*\n\n"
            f"• Minimal buyurtma: *{MIN_ORDER_SAR:.0f} {CURRENCY}*\n"
            f"• Yetkazish: *{DELIVERY_FEE_SAR:.0f} {CURRENCY}*\n"
            "• Narxlar Royal’dagi narxga qarab admin tomonidan yangilanadi\n",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(user_id),
        )

    elif query.data == "menu_checkout":
        if not cart_items(user_id):
            await query.edit_message_text("Avval savatchaga mahsulot qo‘shing 🙂", reply_markup=main_menu_kb(user_id))
            return
        await query.edit_message_text("👤 Ism-familyangizni yozing:")
        return ASK_NAME

    elif query.data.startswith("back:"):
        where = query.data.split(":", 1)[1]
        if where == "home":
            await query.edit_message_text("🏠 Bosh menu:", reply_markup=main_menu_kb(user_id))
        elif where == "cats":
            await query.edit_message_text("🛒 Kategoriyalar:", reply_markup=categories_kb())

    elif query.data == "admin:panel":
        if user_id != ADMIN_ID:
            await query.edit_message_text("Ruxsat yo‘q.", reply_markup=main_menu_kb(user_id))
            return
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Mahsulot qo‘shish", callback_data="admin:add")],
            [InlineKeyboardButton("⬅️ Bosh menu", callback_data="back:home")],
        ])
        await query.edit_message_text("👑 Admin panel:", reply_markup=kb)

    elif query.data == "admin:add":
        if user_id != ADMIN_ID:
            return
        await query.edit_message_text(
            "Kategoriya nomini yozing.\nMasalan: 🍚 Guruch / Don"
        )
        return A_CAT


async def on_category(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    cat = query.data.split(":", 1)[1]
    await query.edit_message_text(f"{cat} — mahsulotlar:", reply_markup=products_kb(cat))


async def on_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    pid = int(query.data.split(":", 1)[1])
    p = get_product(pid)
    if not p:
        await query.edit_message_text("Mahsulot topilmadi.", reply_markup=categories_kb())
        return

    items = dict(cart_items(user_id))
    qty = items.get(pid, 0)

    caption = (
        f"🛍️ *{p['name']}*\n"
        f"Narx: *{p['price']} {CURRENCY}*\n"
        f"Savatchada: *{qty}*"
    )

    # if image_url exists -> show photo
    if p["image_url"]:
        try:
            await context.bot.send_photo(
                chat_id=query.message.chat_id,
                photo=p["image_url"],
                caption=caption,
                parse_mode="Markdown",
                reply_markup=product_kb(pid),
            )
            # delete old message to avoid clutter
            try:
                await query.message.delete()
            except Exception:
                pass
            return
        except Exception:
            # fallback to text
            pass

    await query.edit_message_text(caption, parse_mode="Markdown", reply_markup=product_kb(pid))


async def on_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    _, sign, pid_s = query.data.split(":", 2)
    pid = int(pid_s)
    cart_add(user_id, pid, 1 if sign == "+" else -1)

    p = get_product(pid)
    qty = dict(cart_items(user_id)).get(pid, 0)
    text = f"🛍️ *{p['name']}*\nNarx: *{p['price']} {CURRENCY}*\nSavatchada: *{qty}*"
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=product_kb(pid))


async def on_cart_qty(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    _, sign, pid_s = query.data.split(":", 2)
    pid = int(pid_s)
    cart_add(user_id, pid, 1 if sign == "+" else -1)

    if not cart_items(user_id):
        await query.edit_message_text("Savatcha bo‘sh 🧺", reply_markup=main_menu_kb(user_id))
        return

    total = cart_total(user_id)
    text = f"🧺 *Savatcha*\n\nJami: *{total:.2f} {CURRENCY}*\nYetkazish: *{DELIVERY_FEE_SAR:.0f} {CURRENCY}*"
    await query.edit_message_text(text, reply_markup=cart_kb(user_id), parse_mode="Markdown")


async def on_cart_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    cart_clear(user_id)
    await query.edit_message_text("Savatcha tozalandi ✅", reply_markup=main_menu_kb(user_id))


# -----------------------
# Checkout flow
# -----------------------
async def checkout_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    name = (update.message.text or "").strip()
    if len(name) < 2:
        await update.message.reply_text("Ism-familyani to‘g‘ri yozing 🙂")
        return ASK_NAME
    upsert_user(user_id, full_name=name)
    await update.message.reply_text("📞 Telefon raqamingizni yuboring:", reply_markup=contact_keyboard())
    return ASK_PHONE


async def checkout_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    phone = None
    if update.message.contact and update.message.contact.phone_number:
        phone = update.message.contact.phone_number
    else:
        phone = (update.message.text or "").strip()

    if not phone or len(phone) < 6:
        await update.message.reply_text("Telefon raqamni yuboring (tugma orqali) yoki yozing.")
        return ASK_PHONE

    upsert_user(user_id, phone=phone)
    await update.message.reply_text("📍 Lokatsiyani yuboring:", reply_markup=location_keyboard())
    return ASK_LOCATION


async def checkout_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    loc = update.message.location
    if not loc:
        await update.message.reply_text("Lokatsiyani tugma orqali yuboring 🙂", reply_markup=location_keyboard())
        return ASK_LOCATION

    upsert_user(user_id, lat=loc.latitude, lon=loc.longitude)
    await update.message.reply_text(
        "🏠 Qo‘shimcha manzil/izoh yozing (ixtiyoriy) yoki 'O‘tkazib yuborish' deb yozing.",
        reply_markup=ReplyKeyboardMarkup([["O‘tkazib yuborish"]], resize_keyboard=True, one_time_keyboard=True),
    )
    return ASK_ADDRESS


async def checkout_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    address = (update.message.text or "").strip()
    if address.lower() in ["o‘tkazib yuborish", "otkazib yuborish"]:
        address = ""
    if address:
        upsert_user(user_id, address=address)

    total = cart_total(user_id)
    if total < MIN_ORDER_SAR:
        await update.message.reply_text(
            f"❗ Minimal buyurtma {MIN_ORDER_SAR:.0f} {CURRENCY}. Sizniki: {total:.2f} {CURRENCY}.\n"
            "Iltimos savatchani to‘ldiring.",
            reply_markup=main_menu_kb(user_id),
        )
        return ConversationHandler.END

    user = get_user(user_id)
    final_total = total + DELIVERY_FEE_SAR

    # build item list
    conn = db()
    lines = []
    for pid, qty in cart_items(user_id):
        p = conn.execute("SELECT name, price FROM products WHERE id=?", (pid,)).fetchone()
        if p:
            lines.append(f"• {qty} x {p['name']} = {qty*float(p['price']):.2f} {CURRENCY}")
    conn.close()

    summary = (
        "✅ *Buyurtmani tasdiqlang*\n\n"
        f"👤 *Ism:* {user['full_name'] or '-'}\n"
        f"📞 *Tel:* {user['phone'] or '-'}\n"
        f"🏠 *Izoh:* {user['address'] or '-'}\n\n"
        "🧾 *Buyurtma:*\n" + "\n".join(lines) + "\n\n"
        f"Jami: *{total:.2f} {CURRENCY}*\n"
        f"Yetkazish: *{DELIVERY_FEE_SAR:.0f} {CURRENCY}*\n"
        f"Umumiy: *{final_total:.2f} {CURRENCY}*\n\n"
        "To‘lov: *Naqd (yetkazganda)*"
    )

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ Tasdiqlash", callback_data="order:confirm")],
            [InlineKeyboardButton("❌ Bekor qilish", callback_data="order:cancel")],
        ]
    )
    await update.message.reply_text(summary, parse_mode="Markdown", reply_markup=kb)
    return CONFIRM


async def checkout_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id

    if query.data == "order:cancel":
        await query.edit_message_text("Bekor qilindi ❌", reply_markup=main_menu_kb(user_id))
        return ConversationHandler.END

    user = get_user(user_id)
    total = cart_total(user_id)
    final_total = total + DELIVERY_FEE_SAR
    order_id = f"MDN-{user_id}-{int(datetime.utcnow().timestamp())}"

    conn = db()
    items_lines = []
    for pid, qty in cart_items(user_id):
        p = conn.execute("SELECT name, price FROM products WHERE id=?", (pid,)).fetchone()
        if p:
            items_lines.append(f"- {qty} x {p['name']} ({p['price']} {CURRENCY})")
    conn.close()

    admin_text = (
        f"🛎 *Yangi buyurtma* #{order_id}\n\n"
        f"👤 {user['full_name']}\n"
        f"📞 {user['phone']}\n"
        f"🏠 Izoh: {user['address'] or '-'}\n"
        f"📍 Lokatsiya: ({user['lat']}, {user['lon']})\n\n"
        f"🧾 *Mahsulotlar:*\n" + "\n".join(items_lines) + "\n\n"
        f"Jami: *{total:.2f} {CURRENCY}*\n"
        f"Yetkazish: *{DELIVERY_FEE_SAR:.0f} {CURRENCY}*\n"
        f"Umumiy: *{final_total:.2f} {CURRENCY}*\n"
        f"To‘lov: *Naqd*"
    )

    try:
        await context.bot.send_message(chat_id=ADMIN_ID, text=admin_text, parse_mode="Markdown")
        if user["lat"] and user["lon"]:
            await context.bot.send_location(chat_id=ADMIN_ID, latitude=user["lat"], longitude=user["lon"])
    except Exception as e:
        print("Admin notify error:", e)

    await query.edit_message_text(
        f"✅ Buyurtmangiz qabul qilindi!\nBuyurtma raqami: {order_id}\n\nTez orada bog‘lanamiz.",
        reply_markup=main_menu_kb(user_id),
    )

    cart_clear(user_id)
    return ConversationHandler.END


# -----------------------
# ADMIN: Add product with internet image
# -----------------------
async def admin_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    cat = (update.message.text or "").strip()
    if len(cat) < 2:
        await update.message.reply_text("Kategoriya nomini to‘g‘ri yozing.")
        return A_CAT
    context.user_data["new_cat"] = cat
    await update.message.reply_text("Mahsulot nomini yozing. Masalan: 'Almarai Milk 1L'")
    return A_NAME


async def admin_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    name = (update.message.text or "").strip()
    if len(name) < 2:
        await update.message.reply_text("Mahsulot nomini to‘g‘ri yozing.")
        return A_NAME
    context.user_data["new_name"] = name
    await update.message.reply_text(f"Narxni kiriting ({CURRENCY}). Masalan: 12.5")
    return A_PRICE


async def admin_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    txt = (update.message.text or "").strip().replace(",", ".")
    try:
        price = float(txt)
        if price <= 0:
            raise ValueError()
    except Exception:
        await update.message.reply_text("Narx xato. Masalan: 10 yoki 12.5")
        return A_PRICE

    cat = context.user_data["new_cat"]
    name = context.user_data["new_name"]
    pid = add_product(name=name, price=price, category=cat, image_url=None)
    context.user_data["new_pid"] = pid

    await update.message.reply_text("🔎 Internetdan rasm qidiryapman...")

    # search images
    imgs = commons_search_images(name, limit=4)
    context.user_data["img_candidates"] = imgs

    if not imgs:
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("📸 O‘zim rasm yuboraman", callback_data="admin:upload_image")],
            [InlineKeyboardButton("⏭ Rasmsiz qoldir", callback_data="admin:skip_image")],
        ])
        await update.message.reply_text(
            "Topilmadi. Rasmni o‘zingiz yuborasizmi yoki rasmsiz qoldiramizmi?",
            reply_markup=kb
        )
        return A_PICK_IMAGE

    # show choices
    buttons = []
    for i in range(len(imgs)):
        buttons.append([InlineKeyboardButton(f"Rasm #{i+1} ni tanlash", callback_data=f"admin:pick:{i}")])
    buttons.append([InlineKeyboardButton("📸 O‘zim rasm yuboraman", callback_data="admin:upload_image")])
    buttons.append([InlineKeyboardButton("⏭ Rasmsiz qoldir", callback_data="admin:skip_image")])
    kb = InlineKeyboardMarkup(buttons)

    # send preview photos
    for i, url in enumerate(imgs, start=1):
        try:
            await context.bot.send_photo(chat_id=update.effective_chat.id, photo=url, caption=f"Variant #{i}")
        except Exception:
            pass

    await update.message.reply_text("Qaysi rasmini tanlaysiz?", reply_markup=kb)
    return A_PICK_IMAGE


async def admin_pick_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.from_user.id != ADMIN_ID:
        return ConversationHandler.END

    pid = context.user_data.get("new_pid")
    imgs = context.user_data.get("img_candidates", [])

    if query.data == "admin:skip_image":
        await query.edit_message_text("✅ Mahsulot qo‘shildi (rasmsiz).")
        return ConversationHandler.END

    if query.data == "admin:upload_image":
        await query.edit_message_text("📸 Endi mahsulot uchun rasm yuboring (photo).")
        return A_UPLOAD_IMAGE

    if query.data.startswith("admin:pick:"):
        idx = int(query.data.split(":")[-1])
        if 0 <= idx < len(imgs):
            set_product_image(pid, imgs[idx])
            await query.edit_message_text("✅ Mahsulot qo‘shildi (internet rasmi bilan).")
            return ConversationHandler.END

    await query.edit_message_text("Xato tanlov. Qayta urinib ko‘ring.")
    return A_PICK_IMAGE


async def admin_upload_image(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return ConversationHandler.END
    pid = context.user_data.get("new_pid")
    if not pid:
        await update.message.reply_text("Xato: product topilmadi. Qaytadan /start.")
        return ConversationHandler.END

    if not update.message.photo:
        await update.message.reply_text("Rasm yuboring (photo).")
        return A_UPLOAD_IMAGE

    # Save Telegram file_id for perfect reliability
    file_id = update.message.photo[-1].file_id
    # We store file_id in image_url field (Telegram accepts file_id as photo too)
    set_product_image(pid, file_id)

    await update.message.reply_text("✅ Mahsulot qo‘shildi (siz yuborgan rasm bilan).")
    return ConversationHandler.END


# -----------------------
# main
# -----------------------
def main():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_TOKEN env yo‘q. TELEGRAM_TOKEN ni hostingda qo‘ying.")

    init_db()
    seed_if_empty()

    app = Application.builder().token(BOT_TOKEN).build()

    # user handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_menu, pattern="^(menu_catalog|menu_cart|menu_rules|back:.*|menu_checkout|admin:panel|admin:add)$"))
    app.add_handler(CallbackQueryHandler(on_category, pattern="^cat:"))
    app.add_handler(CallbackQueryHandler(on_product, pattern="^p:"))
    app.add_handler(CallbackQueryHandler(on_qty, pattern="^qty:"))
    app.add_handler(CallbackQueryHandler(on_cart_qty, pattern="^c:"))
    app.add_handler(CallbackQueryHandler(on_cart_clear, pattern="^cart:clear$"))

    # checkout conversation
    checkout = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_menu, pattern="^menu_checkout$")],
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
        fallbacks=[],
        allow_reentry=True,
    )
    app.add_handler(checkout)

    # admin add product conversation
    admin_add = ConversationHandler(
        entry_points=[CallbackQueryHandler(on_menu, pattern="^admin:add$")],
        states={
            A_CAT: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_cat)],
            A_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_name)],
            A_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, admin_price)],
            A_PICK_IMAGE: [CallbackQueryHandler(admin_pick_image, pattern="^admin:(pick:|upload_image|skip_image)")],
            A_UPLOAD_IMAGE: [MessageHandler(filters.PHOTO, admin_upload_image)],
        },
        fallbacks=[],
        allow_reentry=True,
    )
    app.add_handler(admin_add)

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
