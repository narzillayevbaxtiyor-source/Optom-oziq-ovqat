import os
import sqlite3
import logging
import asyncio
import threading
from datetime import datetime
from typing import Optional, Tuple, List

from flask import Flask, request

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================== CONFIG ==================
logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "").strip()
WEBHOOK_URL = os.getenv("WEBHOOK_URL", "").strip()  # https://xxx.onrender.com/webhook
PORT = int(os.getenv("PORT", "10000"))

# Admin(lar) ID sini Render Environment ga qo'ying: ADMIN_IDS="123,456"
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", "").strip()
ADMIN_IDS = set()
if ADMIN_IDS_RAW:
    for x in ADMIN_IDS_RAW.split(","):
        x = x.strip()
        if x.isdigit():
            ADMIN_IDS.add(int(x))

SHOP_NAME = os.getenv("SHOP_NAME", "Оптом_озик_овкат Мадина").strip()

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN environment yo‘q")
if not WEBHOOK_URL or not WEBHOOK_URL.startswith("https://") or not WEBHOOK_URL.endswith("/webhook"):
    raise RuntimeError("WEBHOOK_URL noto‘g‘ri. Masalan: https://<service>.onrender.com/webhook")

DB_PATH = os.getenv("DB_PATH", "data.db")

# ================== SAFE EDIT HELPERS ==================
def _same_markup(a, b) -> bool:
    try:
        return (a.to_dict() if a else None) == (b.to_dict() if b else None)
    except Exception:
        return False

async def safe_edit_text(query, text: str, reply_markup=None, parse_mode=None):
    """
    Telegram "Message is not modified" xatosini oldini oladi.
    Agar text/markup o'zgarmagan bo'lsa edit qilmaydi.
    """
    try:
        current_text = ""
        if query.message:
            current_text = (query.message.text or query.message.caption or "").strip()
        new_text = (text or "").strip()

        if current_text == new_text and _same_markup(query.message.reply_markup if query.message else None, reply_markup):
            return

        await query.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        raise

async def safe_edit_media(query, photo_url: str, caption: str, reply_markup=None, parse_mode=None):
    """
    Media edit paytida ham "not modified"ni yutadi.
    """
    try:
        current_caption = ""
        if query.message:
            current_caption = (query.message.caption or "").strip()
        new_caption = (caption or "").strip()

        # Agar faqat caption ham o'zgarmasa, edit qilmaslikka harakat qilamiz
        if current_caption == new_caption and _same_markup(query.message.reply_markup if query.message else None, reply_markup):
            return

        await query.edit_message_media(
            media=InputMediaPhoto(media=photo_url, caption=caption, parse_mode=parse_mode),
            reply_markup=reply_markup,
        )
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        # Ba'zan "message can't be edited" bo'lishi mumkin — unda textga qaytamiz
        raise

# ================== DB ==================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    conn = db()
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        price_sar REAL DEFAULT 0,
        photo_url TEXT DEFAULT '',
        is_active INTEGER DEFAULT 1,
        created_at TEXT NOT NULL,
        FOREIGN KEY(category_id) REFERENCES categories(id)
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS carts (
        user_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        qty INTEGER NOT NULL,
        PRIMARY KEY(user_id, product_id)
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        phone TEXT,
        address TEXT,
        note TEXT,
        total_sar REAL NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS order_items (
        order_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        price_sar REAL NOT NULL,
        qty INTEGER NOT NULL
    )""")
    conn.commit()

    # Seed categories (agar bo‘sh bo‘lsa)
    cur.execute("SELECT COUNT(*) AS c FROM categories")
    if cur.fetchone()["c"] == 0:
        for cname in ["🥬 Sabzavot", "🍎 Meva", "🍗 Go‘sht", "🐟 Baliq", "🥛 Sut", "🥫 Konserva", "🥖 Non", "🍚 Don", "🍫 Shirinlik", "🧴 Uy-ro‘zg‘or"]:
            cur.execute("INSERT OR IGNORE INTO categories(name) VALUES(?)", (cname,))
        conn.commit()

    # Seed products (minimal demo)
    cur.execute("SELECT COUNT(*) AS c FROM products")
    if cur.fetchone()["c"] == 0:
        demo = [
            ("🥬 Sabzavot", "Pomidor", "Yangi pomidor. 1 kg.", 0, "https://source.unsplash.com/800x600/?tomatoes"),
            ("🥬 Sabzavot", "Bodring", "Yangi bodring. 1 kg.", 0, "https://source.unsplash.com/800x600/?cucumber"),
            ("🍗 Go‘sht", "Tovuq", "Butun tovuq (taxminiy 1.2-1.5kg).", 0, "https://source.unsplash.com/800x600/?chicken,food"),
            ("🥛 Sut", "Sut 1L", "1 litr sut.", 0, "https://source.unsplash.com/800x600/?milk,bottle"),
            ("🥖 Non", "Non", "Issiq non.", 0, "https://source.unsplash.com/800x600/?bread"),
        ]
        for catname, name, desc, price, photo in demo:
            cur.execute("SELECT id FROM categories WHERE name=?", (catname,))
            cid = cur.fetchone()["id"]
            cur.execute("""
                INSERT INTO products(category_id, name, description, price_sar, photo_url, is_active, created_at)
                VALUES(?,?,?,?,?,?,?)
            """, (cid, name, desc, float(price), photo, 1, datetime.utcnow().isoformat()))
        conn.commit()

    conn.close()

# ================== HELPERS ==================
def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS if ADMIN_IDS else False

def money(x: float) -> str:
    return f"{x:.2f} SAR"

def get_categories() -> List[sqlite3.Row]:
    conn = db()
    rows = conn.execute("SELECT id, name FROM categories ORDER BY name").fetchall()
    conn.close()
    return rows

def get_products(category_id: int, only_active: bool = True) -> List[sqlite3.Row]:
    conn = db()
    q = "SELECT * FROM products WHERE category_id=?"
    params = [category_id]
    if only_active:
        q += " AND is_active=1"
    q += " ORDER BY id DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return rows

def get_product(pid: int) -> Optional[sqlite3.Row]:
    conn = db()
    row = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    conn.close()
    return row

def cart_add(user_id: int, pid: int, qty: int = 1) -> None:
    conn = db()
    cur = conn.cursor()
    cur.execute("SELECT qty FROM carts WHERE user_id=? AND product_id=?", (user_id, pid))
    r = cur.fetchone()
    if r:
        cur.execute("UPDATE carts SET qty=? WHERE user_id=? AND product_id=?", (r["qty"] + qty, user_id, pid))
    else:
        cur.execute("INSERT INTO carts(user_id, product_id, qty) VALUES(?,?,?)", (user_id, pid, qty))
    conn.commit()
    conn.close()

def cart_set(user_id: int, pid: int, qty: int) -> None:
    conn = db()
    cur = conn.cursor()
    if qty <= 0:
        cur.execute("DELETE FROM carts WHERE user_id=? AND product_id=?", (user_id, pid))
    else:
        cur.execute("INSERT OR REPLACE INTO carts(user_id, product_id, qty) VALUES(?,?,?)", (user_id, pid, qty))
    conn.commit()
    conn.close()

def cart_items(user_id: int) -> List[sqlite3.Row]:
    conn = db()
    rows = conn.execute("""
        SELECT c.product_id, c.qty, p.name, p.price_sar, p.photo_url
        FROM carts c
        JOIN products p ON p.id=c.product_id
        WHERE c.user_id=?
        ORDER BY p.name
    """, (user_id,)).fetchall()
    conn.close()
    return rows

def cart_total(user_id: int) -> float:
    items = cart_items(user_id)
    return sum(float(i["price_sar"]) * int(i["qty"]) for i in items)

def cart_clear(user_id: int) -> None:
    conn = db()
    conn.execute("DELETE FROM carts WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def create_order(user_id: int, phone: str, address: str, note: str) -> int:
    items = cart_items(user_id)
    total = cart_total(user_id)
    if not items:
        return -1
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO orders(user_id, phone, address, note, total_sar, status, created_at)
        VALUES(?,?,?,?,?,?,?)
    """, (user_id, phone, address, note, float(total), "NEW", datetime.utcnow().isoformat()))
    oid = cur.lastrowid
    for it in items:
        cur.execute("""
            INSERT INTO order_items(order_id, product_id, name, price_sar, qty)
            VALUES(?,?,?,?,?)
        """, (oid, int(it["product_id"]), it["name"], float(it["price_sar"]), int(it["qty"])))
    conn.commit()
    conn.close()
    cart_clear(user_id)
    return oid

def list_orders(limit: int = 20) -> List[sqlite3.Row]:
    conn = db()
    rows = conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return rows

def order_details(order_id: int) -> Tuple[Optional[sqlite3.Row], List[sqlite3.Row]]:
    conn = db()
    o = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    items = conn.execute("SELECT * FROM order_items WHERE order_id=?", (order_id,)).fetchall()
    conn.close()
    return o, items

# ================== UI BUILDERS ==================
def kb_main(is_admin_flag: bool) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🛒 Katalog", callback_data="CAT")],
        [InlineKeyboardButton("🧺 Savatcha", callback_data="CART")],
        [InlineKeyboardButton("☎️ Aloqa", callback_data="CONTACT")],
    ]
    if is_admin_flag:
        rows.append([InlineKeyboardButton("🛠 Admin", callback_data="ADMIN")])
    return InlineKeyboardMarkup(rows)

def kb_categories() -> InlineKeyboardMarkup:
    rows = []
    for c in get_categories():
        rows.append([InlineKeyboardButton(c["name"], callback_data=f"CAT:{c['id']}")])
    rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="HOME")])
    return InlineKeyboardMarkup(rows)

def kb_product(pid: int) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("➕ Qo‘shish", callback_data=f"ADD:{pid}"),
            InlineKeyboardButton("➖ Ayirish", callback_data=f"SUB:{pid}"),
        ],
        [InlineKeyboardButton("🧺 Savatcha", callback_data="CART")],
        [InlineKeyboardButton("⬅️ Orqaga", callback_data="CAT")],
    ]
    return InlineKeyboardMarkup(rows)

def kb_cart(user_id: int) -> InlineKeyboardMarkup:
    items = cart_items(user_id)
    rows = []
    for it in items[:8]:
        pid = int(it["product_id"])
        rows.append([
            InlineKeyboardButton("➖", callback_data=f"SUB:{pid}"),
            InlineKeyboardButton(f"{it['name']} x{it['qty']}", callback_data=f"SHOW:{pid}"),
            InlineKeyboardButton("➕", callback_data=f"ADD:{pid}"),
        ])
    if items:
        rows.append([InlineKeyboardButton("✅ Buyurtma berish", callback_data="CHECKOUT")])
        rows.append([InlineKeyboardButton("🧹 Savatchani tozalash", callback_data="CLEARCART")])
    rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="HOME")])
    return InlineKeyboardMarkup(rows)

def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Mahsulot qo‘shish", callback_data="A_ADD")],
        [InlineKeyboardButton("🧾 Buyurtmalar", callback_data="A_ORDERS")],
        [InlineKeyboardButton("📣 Broadcast", callback_data="A_BC")],
        [InlineKeyboardButton("⬅️ Orqaga", callback_data="HOME")],
    ])

# ================== BOT LOGIC ==================
STATE_ADD_PRODUCT = "ADD_PRODUCT"
STATE_SET_PRICE = "SET_PRICE"
STATE_CHECKOUT_PHONE = "CHECKOUT_PHONE"
STATE_CHECKOUT_ADDRESS = "CHECKOUT_ADDRESS"
STATE_CHECKOUT_NOTE = "CHECKOUT_NOTE"
STATE_BROADCAST = "BROADCAST"

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (
        f"🛍 <b>{SHOP_NAME}</b>\n\n"
        "Xush kelibsiz! Katalogdan mahsulot tanlang va savatchaga qo‘shing.\n\n"
        "✅ Buyurtma berish — savatchadan."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_main(is_admin(uid)))

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("Admin emasiz.")
        return
    await update.message.reply_text("🛠 Admin panel", reply_markup=kb_admin())

async def show_home(query_or_msg, uid: int):
    text = f"🛍 <b>{SHOP_NAME}</b>\n\nKerakli bo‘limni tanlang:"
    if hasattr(query_or_msg, "edit_message_text"):
        await safe_edit_text(query_or_msg, text, parse_mode=ParseMode.HTML, reply_markup=kb_main(is_admin(uid)))
    else:
        await query_or_msg.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_main(is_admin(uid)))

async def show_categories(query):
    await safe_edit_text(query, "🛒 Katalog bo‘limlari:", reply_markup=kb_categories())

async def show_products(query, category_id: int):
    prods = get_products(category_id, only_active=True)
    if not prods:
        await safe_edit_text(query, "Bu bo‘limda hozircha mahsulot yo‘q.", reply_markup=kb_categories())
        return

    rows = []
    for p in prods[:10]:
        rows.append([InlineKeyboardButton(f"{p['name']} — {money(float(p['price_sar']))}", callback_data=f"SHOW:{p['id']}")])
    rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="CAT")])
    await safe_edit_text(query, "Mahsulotlar:", reply_markup=InlineKeyboardMarkup(rows))

async def show_product(query, uid: int, pid: int):
    p = get_product(pid)
    if not p or int(p["is_active"]) != 1:
        await query.answer("Mahsulot topilmadi.")
        return

    price = float(p["price_sar"])
    desc = (p["description"] or "").strip()
    photo = (p["photo_url"] or "").strip()

    caption = (
        f"🧾 <b>{p['name']}</b>\n"
        f"💰 <b>{money(price)}</b>\n\n"
        f"{desc if desc else ''}"
    )

    try:
        if photo:
            await safe_edit_media(query, photo, caption, reply_markup=kb_product(pid), parse_mode=ParseMode.HTML)
        else:
            await safe_edit_text(query, caption, parse_mode=ParseMode.HTML, reply_markup=kb_product(pid))
    except Exception:
        await safe_edit_text(query, caption, parse_mode=ParseMode.HTML, reply_markup=kb_product(pid))

async def show_cart(query, uid: int):
    items = cart_items(uid)
    if not items:
        await safe_edit_text(query, "🧺 Savatcha bo‘sh.", reply_markup=kb_cart(uid))
        return

    lines = []
    for it in items:
        lines.append(f"• {it['name']} x{it['qty']} = {money(float(it['price_sar']) * int(it['qty']))}")
    total = cart_total(uid)
    text = "🧺 <b>Savatcha</b>\n\n" + "\n".join(lines) + f"\n\n<b>Jami:</b> {money(total)}"
    await safe_edit_text(query, text, parse_mode=ParseMode.HTML, reply_markup=kb_cart(uid))

async def checkout_start(query, context: ContextTypes.DEFAULT_TYPE, uid: int):
    if not cart_items(uid):
        await query.answer("Savatcha bo‘sh.")
        return
    context.user_data["state"] = STATE_CHECKOUT_PHONE
    await safe_edit_text(query, "📞 Telefon raqamingizni yuboring (masalan: +9665xxxxxxx):")

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = context.user_data.get("state")

    if state == STATE_CHECKOUT_PHONE:
        context.user_data["phone"] = update.message.text.strip()
        context.user_data["state"] = STATE_CHECKOUT_ADDRESS
        await update.message.reply_text("📍 Manzilni yozing (qayerga yetkazib beramiz?):")
        return

    if state == STATE_CHECKOUT_ADDRESS:
        context.user_data["address"] = update.message.text.strip()
        context.user_data["state"] = STATE_CHECKOUT_NOTE
        await update.message.reply_text("📝 Izoh (ixtiyoriy). Agar izoh yo‘q bo‘lsa 'yo‘q' deb yozing:")
        return

    if state == STATE_CHECKOUT_NOTE:
        note = update.message.text.strip()
        if note.lower() in ["yoq", "yo‘q", "yooq", "нет", "no"]:
            note = ""
        phone = context.user_data.get("phone", "")
        address = context.user_data.get("address", "")
        oid = create_order(uid, phone, address, note)
        context.user_data["state"] = None

        if oid == -1:
            await update.message.reply_text("Savatcha bo‘sh. Qaytadan urinib ko‘ring.")
            return

        await update.message.reply_text(
            f"✅ Buyurtma qabul qilindi! ID: <b>{oid}</b>\nTez orada aloqaga chiqamiz.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_main(is_admin(uid)),
        )

        if ADMIN_IDS:
            o, items = order_details(oid)
            lines = [
                f"🆕 <b>Yangi buyurtma</b> #{oid}",
                f"👤 User: <code>{uid}</code>",
                f"📞 {o['phone']}",
                f"📍 {o['address']}",
                f"💰 Jami: {money(float(o['total_sar']))}",
                "",
            ]
            for it in items:
                lines.append(f"• {it['name']} x{it['qty']} = {money(float(it['price_sar']) * int(it['qty']))}")
            msg = "\n".join(lines)
            for aid in ADMIN_IDS:
                try:
                    await context.bot.send_message(aid, msg, parse_mode=ParseMode.HTML)
                except Exception:
                    pass
        return

    if state == STATE_ADD_PRODUCT:
        txt = update.message.text.strip()
        parts = [p.strip() for p in txt.split("|")]
        if len(parts) < 2:
            await update.message.reply_text(
                "Format xato.\nMisol:\n<code>1 | Pomidor | 1 kg | https://...</code>",
                parse_mode=ParseMode.HTML,
            )
            return
        try:
            cat_id = int(parts[0])
        except Exception:
            await update.message.reply_text("category_id raqam bo‘lsin.")
            return

        name = parts[1]
        desc = parts[2] if len(parts) >= 3 else ""
        photo = parts[3] if len(parts) >= 4 else "https://source.unsplash.com/800x600/?groceries"

        conn = db()
        cur = conn.cursor()
        cur.execute("SELECT id FROM categories WHERE id=?", (cat_id,))
        if not cur.fetchone():
            conn.close()
            await update.message.reply_text("Bunday category_id yo‘q. /admin → bo‘limlardan oling.")
            return
        cur.execute("""
            INSERT INTO products(category_id, name, description, price_sar, photo_url, is_active, created_at)
            VALUES(?,?,?,?,?,?,?)
        """, (cat_id, name, desc, 0.0, photo, 1, datetime.utcnow().isoformat()))
        pid = cur.lastrowid
        conn.commit()
        conn.close()

        context.user_data["state"] = STATE_SET_PRICE
        context.user_data["price_pid"] = pid
        await update.message.reply_text(f"✅ Mahsulot qo‘shildi (ID={pid}). Endi narxini yuboring (faqat son):")
        return

    if state == STATE_SET_PRICE:
        pid = int(context.user_data.get("price_pid", 0) or 0)
        try:
            price = float(update.message.text.strip().replace(",", "."))
        except Exception:
            await update.message.reply_text("Narx xato. Misol: 12.5")
            return
        conn = db()
        conn.execute("UPDATE products SET price_sar=? WHERE id=?", (price, pid))
        conn.commit()
        conn.close()
        context.user_data["state"] = None
        await update.message.reply_text("✅ Narx saqlandi.", reply_markup=kb_admin())
        return

    if state == STATE_BROADCAST:
        if not is_admin(uid):
            context.user_data["state"] = None
            return
        context.user_data["state"] = None
        await update.message.reply_text(
            "📣 Broadcast matni tayyor. Uni o‘zingiz kanal/guruhga yuboring.\n(Avto-broadcast uchun user bazasi kengaytiriladi.)"
        )
        return

    await update.message.reply_text("Menyu: /start")

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    uid = update.effective_user.id
    data = query.data

    if data == "HOME":
        await show_home(query, uid)
        return

    if data == "CAT":
        await show_categories(query)
        return

    if data.startswith("CAT:"):
        cid = int(data.split(":")[1])
        await show_products(query, cid)
        return

    if data.startswith("SHOW:"):
        pid = int(data.split(":")[1])
        await show_product(query, uid, pid)
        return

    if data.startswith("ADD:"):
        pid = int(data.split(":")[1])
        cart_add(uid, pid, 1)
        await show_cart(query, uid)
        return

    if data.startswith("SUB:"):
        pid = int(data.split(":")[1])
        items = cart_items(uid)
        current = 0
        for it in items:
            if int(it["product_id"]) == pid:
                current = int(it["qty"])
                break
        cart_set(uid, pid, current - 1)
        await show_cart(query, uid)
        return

    if data == "CART":
        await show_cart(query, uid)
        return

    if data == "CLEARCART":
        cart_clear(uid)
        await safe_edit_text(query, "🧹 Savatcha tozalandi.", reply_markup=kb_main(is_admin(uid)))
        return

    if data == "CHECKOUT":
        await checkout_start(query, context, uid)
        return

    if data == "CONTACT":
        text = "☎️ Aloqa: admin bilan bog‘lanish.\n\nBuyurtma qilganingizdan so‘ng siz bilan aloqaga chiqamiz."
        await safe_edit_text(query, text, reply_markup=kb_main(is_admin(uid)))
        return

    if data == "ADMIN":
        if not is_admin(uid):
            await query.answer("Admin emassiz.")
            return
        await safe_edit_text(query, "🛠 Admin panel", reply_markup=kb_admin())
        return

    if data == "A_ADD":
        if not is_admin(uid):
            return
        cats = get_categories()
        lines = ["➕ Mahsulot qo‘shish", "", "Avval category_id ni tanlang (pastda):"]
        for c in cats:
            lines.append(f"• <b>{c['id']}</b> — {c['name']}")
        lines += [
            "",
            "Keyin shu formatda yuboring:",
            "<code>category_id | nomi | tavsif | foto_url</code>",
            "Misol:",
            "<code>1 | Pomidor | 1 kg | https://source.unsplash.com/800x600/?tomatoes</code>",
        ]
        context.user_data["state"] = STATE_ADD_PRODUCT
        await safe_edit_text(query, "\n".join(lines), parse_mode=ParseMode.HTML)
        return

    if data == "A_ORDERS":
        if not is_admin(uid):
            return
        orders = list_orders(10)
        if not orders:
            await safe_edit_text(query, "Buyurtmalar yo‘q.", reply_markup=kb_admin())
            return
        lines = ["🧾 Oxirgi buyurtmalar:"]
        for o in orders:
            lines.append(f"• #{o['id']} | user {o['user_id']} | {money(float(o['total_sar']))} | {o['status']}")
        await safe_edit_text(query, "\n".join(lines), reply_markup=kb_admin())
        return

    if data == "A_BC":
        if not is_admin(uid):
            return
        context.user_data["state"] = STATE_BROADCAST
        await safe_edit_text(query, "📣 Broadcast matnini yuboring:")
        return

# ================== WEBHOOK SERVER (Render-safe) ==================
tg_app = Application.builder().token(BOT_TOKEN).build()
tg_app.add_handler(CommandHandler("start", cmd_start))
tg_app.add_handler(CommandHandler("admin", cmd_admin))
tg_app.add_handler(CallbackQueryHandler(on_callback))
tg_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

loop = asyncio.new_event_loop()

def run_loop():
    asyncio.set_event_loop(loop)
    loop.run_forever()

threading.Thread(target=run_loop, daemon=True).start()

async def tg_bootstrap():
    await tg_app.initialize()
    await tg_app.start()
    await tg_app.bot.set_webhook(WEBHOOK_URL, drop_pending_updates=True)

asyncio.run_coroutine_threadsafe(tg_bootstrap(), loop)

flask_app = Flask(__name__)

@flask_app.get("/")
def health():
    return "OK", 200

@flask_app.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        return "OK", 200
    data = request.get_json(force=True, silent=True) or {}
    upd = Update.de_json(data, tg_app.bot)
    asyncio.run_coroutine_threadsafe(tg_app.process_update(upd), loop)
    return "OK", 200

if __name__ == "__main__":
    init_db()
    flask_app.run(host="0.0.0.0", port=PORT)
