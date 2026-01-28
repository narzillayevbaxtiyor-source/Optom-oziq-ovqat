import os
import sqlite3
import logging
import threading
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple

from flask import Flask

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
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

# ===================== CONFIG =====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("shopbot")

BOT_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
ADMIN_IDS_RAW = (os.getenv("ADMIN_IDS") or "").strip()   # "123,456"
SHOP_NAME = (os.getenv("SHOP_NAME") or "🛒 Online Oziq-ovqat").strip()

PORT = int(os.getenv("PORT", "10000"))

# Render Disk ishlatsangiz shuni bering: DB_PATH=/var/data/data.db
DB_PATH = (os.getenv("DB_PATH") or "data.db").strip()

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN yo‘q (Render Environment ga qo‘ying).")

ADMIN_IDS = set()
if ADMIN_IDS_RAW:
    for x in ADMIN_IDS_RAW.split(","):
        x = x.strip()
        if x.isdigit():
            ADMIN_IDS.add(int(x))

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def money(x: float) -> str:
    return f"{x:.2f} SAR"

def now_iso() -> str:
    return datetime.utcnow().isoformat()

# ===================== DB =====================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS categories (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )""")

    # products: asosiy mahsulot
    cur.execute("""
    CREATE TABLE IF NOT EXISTS products (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        photo_file_id TEXT DEFAULT '',
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )""")

    # product_variants: Kg/Lt/Dona bo'yicha narx + step
    cur.execute("""
    CREATE TABLE IF NOT EXISTS product_variants (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        unit TEXT NOT NULL,                 -- "KG" / "LT" / "PC"
        price_per_unit REAL NOT NULL DEFAULT 0,
        step REAL NOT NULL DEFAULT 1,       -- 1kg, 0.5kg, 1 dona ...
        min_qty REAL NOT NULL DEFAULT 1,
        max_qty REAL NOT NULL DEFAULT 999999,
        UNIQUE(product_id, unit),
        FOREIGN KEY(product_id) REFERENCES products(id)
    )""")

    # product_categories: mahsulotlarni kategoriya ichida ko'rsatish
    cur.execute("""
    CREATE TABLE IF NOT EXISTS product_categories (
        product_id INTEGER NOT NULL,
        category_id INTEGER NOT NULL,
        PRIMARY KEY(product_id, category_id),
        FOREIGN KEY(product_id) REFERENCES products(id),
        FOREIGN KEY(category_id) REFERENCES categories(id)
    )""")

    # carts: user savatchasi (variant bo'yicha)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS carts (
        user_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        unit TEXT NOT NULL,
        qty REAL NOT NULL,
        PRIMARY KEY(user_id, product_id, unit)
    )""")

    # orders
    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        phone TEXT DEFAULT '',
        address TEXT DEFAULT '',
        location_lat REAL,
        location_lon REAL,
        note TEXT DEFAULT '',
        total_sar REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL,              -- NEW/ACCEPTED/REJECTED/COLLECT/ONWAY/DONE
        created_at TEXT NOT NULL
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS order_items (
        order_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        unit TEXT NOT NULL,
        price_per_unit REAL NOT NULL,
        qty REAL NOT NULL,
        line_total REAL NOT NULL,
        FOREIGN KEY(order_id) REFERENCES orders(id)
    )""")

    # seed categories (bo'sh bo'lsa)
    cur.execute("SELECT COUNT(*) AS c FROM categories")
    if cur.fetchone()["c"] == 0:
        base = [
            "🥬 Sabzavot", "🍎 Meva", "🍗 Go‘sht", "🐟 Baliq",
            "🥛 Sut", "🥫 Konserva", "🥖 Non", "🍚 Don",
            "🍫 Shirinlik", "🧴 Uy-ro‘zg‘or"
        ]
        for n in base:
            cur.execute("INSERT OR IGNORE INTO categories(name, is_active, created_at) VALUES(?,?,?)", (n, 1, now_iso()))
        conn.commit()

    conn.commit()
    conn.close()

# ===================== DB helpers =====================
def get_categories(active_only=True) -> List[sqlite3.Row]:
    conn = db()
    if active_only:
        rows = conn.execute("SELECT * FROM categories WHERE is_active=1 ORDER BY name").fetchall()
    else:
        rows = conn.execute("SELECT * FROM categories ORDER BY name").fetchall()
    conn.close()
    return rows

def create_category(name: str) -> int:
    conn = db()
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO categories(name, is_active, created_at) VALUES(?,?,?)", (name, 1, now_iso()))
    conn.commit()
    cur.execute("SELECT id FROM categories WHERE name=?", (name,))
    cid = cur.fetchone()["id"]
    conn.close()
    return cid

def list_products(active_only=True) -> List[sqlite3.Row]:
    conn = db()
    if active_only:
        rows = conn.execute("SELECT * FROM products WHERE is_active=1 ORDER BY id DESC").fetchall()
    else:
        rows = conn.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
    conn.close()
    return rows

def get_product(pid: int) -> Optional[sqlite3.Row]:
    conn = db()
    r = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    conn.close()
    return r

def upsert_product(name: str, description: str, photo_file_id: str) -> int:
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO products(name, description, photo_file_id, is_active, created_at)
        VALUES(?,?,?,?,?)
    """, (name, description, photo_file_id, 1, now_iso()))
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return pid

def set_variant(product_id: int, unit: str, price_per_unit: float, step: float, min_qty: float, max_qty: float):
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO product_variants(product_id, unit, price_per_unit, step, min_qty, max_qty)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(product_id, unit) DO UPDATE SET
            price_per_unit=excluded.price_per_unit,
            step=excluded.step,
            min_qty=excluded.min_qty,
            max_qty=excluded.max_qty
    """, (product_id, unit, price_per_unit, step, min_qty, max_qty))
    conn.commit()
    conn.close()

def get_variants(product_id: int) -> List[sqlite3.Row]:
    conn = db()
    rows = conn.execute("SELECT * FROM product_variants WHERE product_id=? ORDER BY unit", (product_id,)).fetchall()
    conn.close()
    return rows

def get_variant(product_id: int, unit: str) -> Optional[sqlite3.Row]:
    conn = db()
    r = conn.execute("SELECT * FROM product_variants WHERE product_id=? AND unit=?", (product_id, unit)).fetchone()
    conn.close()
    return r

def attach_product_to_category(product_id: int, category_id: int):
    conn = db()
    conn.execute("INSERT OR IGNORE INTO product_categories(product_id, category_id) VALUES(?,?)", (product_id, category_id))
    conn.commit()
    conn.close()

def get_products_in_category(category_id: int) -> List[sqlite3.Row]:
    conn = db()
    rows = conn.execute("""
        SELECT p.*
        FROM products p
        JOIN product_categories pc ON pc.product_id=p.id
        WHERE pc.category_id=? AND p.is_active=1
        ORDER BY p.id DESC
    """, (category_id,)).fetchall()
    conn.close()
    return rows

# ----- Cart -----
def cart_get(user_id: int) -> List[sqlite3.Row]:
    conn = db()
    rows = conn.execute("""
        SELECT c.user_id, c.product_id, c.unit, c.qty,
               p.name,
               v.price_per_unit, v.step
        FROM carts c
        JOIN products p ON p.id=c.product_id
        JOIN product_variants v ON v.product_id=c.product_id AND v.unit=c.unit
        WHERE c.user_id=?
        ORDER BY p.name
    """, (user_id,)).fetchall()
    conn.close()
    return rows

def cart_set(user_id: int, product_id: int, unit: str, qty: float):
    conn = db()
    cur = conn.cursor()
    if qty <= 0:
        cur.execute("DELETE FROM carts WHERE user_id=? AND product_id=? AND unit=?", (user_id, product_id, unit))
    else:
        cur.execute("""
            INSERT OR REPLACE INTO carts(user_id, product_id, unit, qty)
            VALUES(?,?,?,?)
        """, (user_id, product_id, unit, qty))
    conn.commit()
    conn.close()

def cart_clear(user_id: int):
    conn = db()
    conn.execute("DELETE FROM carts WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def cart_total(user_id: int) -> float:
    items = cart_get(user_id)
    total = 0.0
    for it in items:
        total += float(it["price_per_unit"]) * float(it["qty"])
    return float(total)

# ----- Orders -----
def order_create(user_id: int, phone: str, address: str, lat: Optional[float], lon: Optional[float], note: str) -> int:
    items = cart_get(user_id)
    if not items:
        return -1

    total = cart_total(user_id)
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO orders(user_id, phone, address, location_lat, location_lon, note, total_sar, status, created_at)
        VALUES(?,?,?,?,?,?,?,?,?)
    """, (user_id, phone, address, lat, lon, note, total, "NEW", now_iso()))
    oid = cur.lastrowid

    for it in items:
        line_total = float(it["price_per_unit"]) * float(it["qty"])
        cur.execute("""
            INSERT INTO order_items(order_id, product_id, name, unit, price_per_unit, qty, line_total)
            VALUES(?,?,?,?,?,?,?)
        """, (oid, int(it["product_id"]), it["name"], it["unit"], float(it["price_per_unit"]), float(it["qty"]), float(line_total)))

    conn.commit()
    conn.close()
    cart_clear(user_id)
    return oid

def get_order(oid: int) -> Optional[sqlite3.Row]:
    conn = db()
    r = conn.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    conn.close()
    return r

def get_order_items(oid: int) -> List[sqlite3.Row]:
    conn = db()
    rows = conn.execute("SELECT * FROM order_items WHERE order_id=?", (oid,)).fetchall()
    conn.close()
    return rows

def set_order_status(oid: int, new_status: str):
    conn = db()
    conn.execute("UPDATE orders SET status=? WHERE id=?", (new_status, oid))
    conn.commit()
    conn.close()

# ===================== UI helpers =====================
def safe_edit(q, text: str, reply_markup=None, parse_mode=None):
    # "message is not modified" xatosini oldini oladi
    async def _do():
        try:
            await q.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
        except BadRequest as e:
            if "Message is not modified" in str(e):
                return
            raise
    return _do()

def unit_label(unit: str) -> str:
    return {"KG":"Kg", "LT":"Lt", "PC":"Dona"}.get(unit, unit)

def unit_emoji(unit: str) -> str:
    return {"KG":"⚖️", "LT":"🧴", "PC":"📦"}.get(unit, "🔹")

def kb_home(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🛒 Kategoriyalar", callback_data="CAT")],
        [InlineKeyboardButton("🧺 Savatcha", callback_data="CART")],
    ]
    if is_admin(uid):
        rows.append([InlineKeyboardButton("🛠 Admin panel", callback_data="ADMIN")])
    return InlineKeyboardMarkup(rows)

def kb_categories(uid: int) -> InlineKeyboardMarkup:
    rows = []
    for c in get_categories(True):
        rows.append([InlineKeyboardButton(c["name"], callback_data=f"CAT:{c['id']}")])
    rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="HOME")])
    return InlineKeyboardMarkup(rows)

def kb_products(category_id: int) -> InlineKeyboardMarkup:
    prods = get_products_in_category(category_id)
    rows = []
    for p in prods[:20]:
        rows.append([InlineKeyboardButton(f"{p['name']}", callback_data=f"P:{p['id']}")])
    rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="CAT")])
    return InlineKeyboardMarkup(rows)

def kb_product_units(pid: int) -> InlineKeyboardMarkup:
    vars_ = get_variants(pid)
    rows = []
    for v in vars_:
        u = v["unit"]
        rows.append([InlineKeyboardButton(f"{unit_emoji(u)} {unit_label(u)} — {money(float(v['price_per_unit']))}/{unit_label(u)}", callback_data=f"U:{pid}:{u}")])
    rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="CAT")])
    return InlineKeyboardMarkup(rows)

def kb_qty(pid: int, unit: str, qty: float) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➖", callback_data=f"Q:-:{pid}:{unit}"),
            InlineKeyboardButton(f"{qty:g} {unit_label(unit)}", callback_data="NOOP"),
            InlineKeyboardButton("➕", callback_data=f"Q:+:{pid}:{unit}"),
        ],
        [
            InlineKeyboardButton("🧺 Savatchaga qo‘shish", callback_data=f"ADD:{pid}:{unit}:{qty:g}")
        ],
        [
            InlineKeyboardButton("🧺 Savatcha", callback_data="CART"),
            InlineKeyboardButton("⬅️ Orqaga", callback_data=f"P:{pid}"),
        ]
    ])

def kb_cart(uid: int) -> InlineKeyboardMarkup:
    items = cart_get(uid)
    rows = []
    for it in items[:10]:
        pid = int(it["product_id"])
        unit = it["unit"]
        qty = float(it["qty"])
        rows.append([
            InlineKeyboardButton("➖", callback_data=f"CQ:-:{pid}:{unit}"),
            InlineKeyboardButton(f"{it['name']} ({qty:g} {unit_label(unit)})", callback_data=f"P:{pid}"),
            InlineKeyboardButton("➕", callback_data=f"CQ:+:{pid}:{unit}"),
        ])
    if items:
        rows.append([InlineKeyboardButton("➡️ Davom etish", callback_data="CHECKOUT")])
        rows.append([InlineKeyboardButton("❌ Bekor qilish", callback_data="HOME")])
        rows.append([InlineKeyboardButton("🧹 Savatchani tozalash", callback_data="CLEARCART")])
    else:
        rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="HOME")])
    return InlineKeyboardMarkup(rows)

def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Mahsulot qo‘shish (rasm bilan)", callback_data="A:ADD")],
        [InlineKeyboardButton("📁 Kategoriya yaratish", callback_data="A:CATNEW")],
        [InlineKeyboardButton("🔗 Mahsulotni kategoriya bog‘lash", callback_data="A:ATTACH")],
        [InlineKeyboardButton("🧾 Buyurtmalar", callback_data="A:ORDERS")],
        [InlineKeyboardButton("⬅️ Orqaga", callback_data="HOME")],
    ])

def kb_orders_admin(oid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Qabul", callback_data=f"O:ACCEPT:{oid}"),
            InlineKeyboardButton("❌ Rad", callback_data=f"O:REJECT:{oid}"),
        ],
        [
            InlineKeyboardButton("📦 Yig‘ilyapti", callback_data=f"O:COLLECT:{oid}"),
            InlineKeyboardButton("🚚 Yo‘lda", callback_data=f"O:ONWAY:{oid}"),
        ],
        [
            InlineKeyboardButton("🏁 Yetkazildi", callback_data=f"O:DONE:{oid}"),
        ],
    ])

# ===================== States (user_data) =====================
S_ADMIN_ADD_WAIT_PHOTO = "ADMIN_ADD_WAIT_PHOTO"
S_ADMIN_ADD_WAIT_INFO = "ADMIN_ADD_WAIT_INFO"
S_ADMIN_ATTACH_PICK_PRODUCT = "ADMIN_ATTACH_PICK_PRODUCT"
S_ADMIN_ATTACH_PICK_CAT = "ADMIN_ATTACH_PICK_CAT"
S_ADMIN_CAT_NEW = "ADMIN_CAT_NEW"

S_CHECK_PHONE = "CHECK_PHONE"
S_CHECK_LOCATION = "CHECK_LOCATION"
S_CHECK_ADDRESS = "CHECK_ADDRESS"
S_CHECK_NOTE = "CHECK_NOTE"

# ===================== Handlers =====================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (
        f"<b>{SHOP_NAME}</b>\n\n"
        "✅ Kategoriyalar orqali mahsulot tanlang.\n"
        "🧺 Savatchada jami narx ko‘rinadi.\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_home(uid))

async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("Admin emassiz.")
        return
    await update.message.reply_text("🛠 Admin panel", reply_markup=kb_admin())

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    data = q.data

    if data == "NOOP":
        return

    if data == "HOME":
        await safe_edit(q, f"<b>{SHOP_NAME}</b>\n\nKerakli bo‘limni tanlang:", reply_markup=kb_home(uid), parse_mode=ParseMode.HTML)
        return

    if data == "CAT":
        await safe_edit(q, "🛒 Kategoriyalar:", reply_markup=kb_categories(uid))
        return

    if data.startswith("CAT:"):
        cid = int(data.split(":")[1])
        await safe_edit(q, "🛍 Mahsulotlar:", reply_markup=kb_products(cid))
        return

    if data.startswith("P:"):
        pid = int(data.split(":")[1])
        p = get_product(pid)
        if not p:
            await q.answer("Mahsulot topilmadi.")
            return
        desc = (p["description"] or "").strip()
        text = f"🧾 <b>{p['name']}</b>\n{desc}\n\nO‘lchovni tanlang:"
        # Rasm bo'lsa, text edit; (oddiy qilish uchun) faqat text
        await safe_edit(q, text, reply_markup=kb_product_units(pid), parse_mode=ParseMode.HTML)
        return

    if data.startswith("U:"):
        _, pid_s, unit = data.split(":")
        pid = int(pid_s)
        v = get_variant(pid, unit)
        if not v:
            await q.answer("Variant topilmadi.")
            return
        qty = float(v["min_qty"])
        price = float(v["price_per_unit"]) * qty
        text = f"✅ Tanlandi: <b>{unit_label(unit)}</b>\nMiqdor: <b>{qty:g}</b> {unit_label(unit)}\nNarx: <b>{money(price)}</b>"
        await safe_edit(q, text, reply_markup=kb_qty(pid, unit, qty), parse_mode=ParseMode.HTML)
        # qtyni vaqtincha user_data ga ham qo'yamiz (editlar uchun)
        context.user_data["cur_qty"] = qty
        context.user_data["cur_pid"] = pid
        context.user_data["cur_unit"] = unit
        return

    if data.startswith("Q:"):
        # product qty adjust screen
        _, op, pid_s, unit = data.split(":")
        pid = int(pid_s)
        v = get_variant(pid, unit)
        if not v:
            return
        qty = float(context.user_data.get("cur_qty", float(v["min_qty"])))
        step = float(v["step"])
        mn = float(v["min_qty"])
        mx = float(v["max_qty"])
        if op == "+":
            qty = min(mx, qty + step)
        else:
            qty = max(mn, qty - step)
        context.user_data["cur_qty"] = qty
        price = float(v["price_per_unit"]) * qty
        text = f"✅ Tanlandi: <b>{unit_label(unit)}</b>\nMiqdor: <b>{qty:g}</b> {unit_label(unit)}\nNarx: <b>{money(price)}</b>"
        await safe_edit(q, text, reply_markup=kb_qty(pid, unit, qty), parse_mode=ParseMode.HTML)
        return

    if data.startswith("ADD:"):
        _, pid_s, unit, qty_s = data.split(":")
        pid = int(pid_s)
        qty = float(qty_s)
        cart_set(uid, pid, unit, qty)
        await q.answer("Savatchaga qo‘shildi ✅")
        # savatchani ko'rsatamiz
        await show_cart(q, uid)
        return

    if data.startswith("CQ:"):
        # cart qty +/- by step
        _, op, pid_s, unit = data.split(":")
        pid = int(pid_s)
        v = get_variant(pid, unit)
        if not v:
            return
        step = float(v["step"])
        mn = float(v["min_qty"])
        mx = float(v["max_qty"])

        # current qty from cart
        items = cart_get(uid)
        cur = 0.0
        for it in items:
            if int(it["product_id"]) == pid and it["unit"] == unit:
                cur = float(it["qty"])
                break
        if cur <= 0:
            cur = mn
        if op == "+":
            newq = min(mx, cur + step)
        else:
            newq = cur - step
            if newq < mn:
                newq = 0  # remove
        cart_set(uid, pid, unit, newq)
        await show_cart(q, uid)
        return

    if data == "CART":
        await show_cart(q, uid)
        return

    if data == "CLEARCART":
        cart_clear(uid)
        await safe_edit(q, "🧹 Savatcha tozalandi.", reply_markup=kb_home(uid))
        return

    if data == "CHECKOUT":
        if not cart_get(uid):
            await q.answer("Savatcha bo‘sh.")
            return
        context.user_data["state"] = S_CHECK_PHONE
        # contact keyboard
        kb = ReplyKeyboardMarkup(
            [[KeyboardButton("📞 Telefon raqamni yuborish", request_contact=True)]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
        await q.message.reply_text("📞 Telefon raqamingizni yuboring (contact tugma orqali):", reply_markup=kb)
        return

    # ============== ADMIN PANEL ==============
    if data == "ADMIN":
        if not is_admin(uid):
            await q.answer("Admin emassiz.")
            return
        await safe_edit(q, "🛠 Admin panel:", reply_markup=kb_admin())
        return

    if data == "A:ADD":
        if not is_admin(uid):
            return
        context.user_data["state"] = S_ADMIN_ADD_WAIT_PHOTO
        await safe_edit(q,
                        "➕ Mahsulot qo‘shish:\n\n"
                        "1) Avval <b>rasm yuboring</b>.\n"
                        "2) Keyin quyidagi formatda yozasiz:\n"
                        "<code>Nomi | Tavsif</code>\n\n"
                        "Masalan:\n<code>Pomidor | Yangi pomidor</code>",
                        parse_mode=ParseMode.HTML,
                        reply_markup=kb_admin())
        return

    if data == "A:CATNEW":
        if not is_admin(uid):
            return
        context.user_data["state"] = S_ADMIN_CAT_NEW
        await safe_edit(q, "📁 Yangi kategoriya nomini yuboring (masalan: 🥤 Ichimlik):", reply_markup=kb_admin())
        return

    if data == "A:ATTACH":
        if not is_admin(uid):
            return
        # mahsulotni tanlash ro'yxati
        prods = list_products(True)[:30]
        rows = []
        for p in prods:
            rows.append([InlineKeyboardButton(f"{p['id']}. {p['name']}", callback_data=f"A:PICKP:{p['id']}")])
        rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="ADMIN")])
        context.user_data["state"] = S_ADMIN_ATTACH_PICK_PRODUCT
        await safe_edit(q, "🔗 Qaysi mahsulotni kategoriya ichiga qo‘shamiz?", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("A:PICKP:"):
        if not is_admin(uid):
            return
        pid = int(data.split(":")[2])
        context.user_data["attach_pid"] = pid
        context.user_data["state"] = S_ADMIN_ATTACH_PICK_CAT

        cats = get_categories(True)
        rows = []
        for c in cats:
            rows.append([InlineKeyboardButton(f"{c['id']}. {c['name']}", callback_data=f"A:PICKC:{c['id']}")])
        rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="ADMIN")])
        await safe_edit(q, "📌 Qaysi kategoriya?", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("A:PICKC:"):
        if not is_admin(uid):
            return
        cid = int(data.split(":")[2])
        pid = int(context.user_data.get("attach_pid", 0) or 0)
        if not pid:
            await q.answer("Avval mahsulot tanlang.")
            return
        attach_product_to_category(pid, cid)
        await safe_edit(q, "✅ Mahsulot kategoriya ichiga qo‘shildi.", reply_markup=kb_admin())
        return

    if data == "A:ORDERS":
        if not is_admin(uid):
            return
        # oxirgi 10 buyurtma
        conn = db()
        orders = conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 10").fetchall()
        conn.close()
        if not orders:
            await safe_edit(q, "Buyurtmalar yo‘q.", reply_markup=kb_admin())
            return
        lines = ["🧾 Oxirgi buyurtmalar (tugmani bosib status o‘zgartiring):\n"]
        rows = []
        for o in orders:
            lines.append(f"• #{o['id']} | user {o['user_id']} | {money(float(o['total_sar']))} | {o['status']}")
            rows.append([InlineKeyboardButton(f"📦 Buyurtma #{o['id']}", callback_data=f"A:ORD:{o['id']}")])
        rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="ADMIN")])
        await safe_edit(q, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("A:ORD:"):
        if not is_admin(uid):
            return
        oid = int(data.split(":")[2])
        order = get_order(oid)
        if not order:
            await q.answer("Buyurtma topilmadi.")
            return
        items = get_order_items(oid)
        txt = [
            f"🧾 <b>Buyurtma #{oid}</b>",
            f"👤 User: <code>{order['user_id']}</code>",
            f"📞 {order['phone'] or '-'}",
            f"📍 {order['address'] or '-'}",
            f"💬 {order['note'] or '-'}",
            f"💰 Jami: <b>{money(float(order['total_sar']))}</b>",
            f"📌 Status: <b>{order['status']}</b>",
            "",
            "🧺 Items:"
        ]
        for it in items:
            txt.append(f"• {it['name']} — {it['qty']:g} {unit_label(it['unit'])} × {money(float(it['price_per_unit']))} = {money(float(it['line_total']))}")
        await safe_edit(q, "\n".join(txt), parse_mode=ParseMode.HTML, reply_markup=kb_orders_admin(oid))
        return

    # ORDER status buttons (admin)
    if data.startswith("O:"):
        if not is_admin(uid):
            return
        _, action, oid_s = data.split(":")
        oid = int(oid_s)
        order = get_order(oid)
        if not order:
            await q.answer("Buyurtma topilmadi.")
            return

        user_id = int(order["user_id"])
        status_map = {
            "ACCEPT": ("ACCEPTED", "✅ Buyurtmangiz qabul qilindi."),
            "REJECT": ("REJECTED", "❌ Buyurtmangiz rad etildi."),
            "COLLECT": ("COLLECT", "📦 Buyurtmangiz yig‘ilyapti."),
            "ONWAY": ("ONWAY", "🚚 Buyurtmangiz yo‘lda."),
            "DONE": ("DONE", "🏁 Buyurtmangiz yetkazildi. Rahmat!"),
        }
        if action not in status_map:
            return

        new_status, user_msg = status_map[action]
        set_order_status(oid, new_status)

        # Userga xabar
        try:
            await context.bot.send_message(chat_id=user_id, text=f"📦 Buyurtma #{oid}\n{user_msg}")
        except Exception:
            pass

        # Admin uchun yangilangan ko‘rinish
        await safe_edit(
            q,
            f"🧾 Buyurtma #{oid}\n"
            f"👤 User ID: {user_id}\n"
            f"📌 Status: {new_status}",
            reply_markup=kb_orders_admin(oid)
        )
        return

    # default
    await q.answer("Noma'lum buyruq.")

async def show_cart(q, uid: int):
    items = cart_get(uid)
    if not items:
        await safe_edit(q, "🧺 Savatcha bo‘sh.", reply_markup=kb_cart(uid))
        return
    lines = ["🧺 <b>Savatcha</b>\n"]
    for it in items:
        line_total = float(it["price_per_unit"]) * float(it["qty"])
        lines.append(f"• {it['name']} — <b>{it['qty']:g}</b> {unit_label(it['unit'])} = <b>{money(line_total)}</b>")
    total = cart_total(uid)
    lines.append(f"\n<b>Jami:</b> {money(total)}")
    lines.append("\n⬇️ Pastdan ❌ Bekor qilish yoki ➡️ Davom etish tanlang.")
    await safe_edit(q, "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=kb_cart(uid))

# ===================== TEXT / PHOTO / CONTACT / LOCATION =====================
async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = context.user_data.get("state")

    # ----- ADMIN: add product flow -----
    if state == S_ADMIN_ADD_WAIT_PHOTO and is_admin(uid):
        if not update.message.photo:
            await update.message.reply_text("Rasm yuboring (gallerydan).")
            return
        # eng yuqori sifat
        photo = update.message.photo[-1]
        context.user_data["new_photo_file_id"] = photo.file_id
        context.user_data["state"] = S_ADMIN_ADD_WAIT_INFO
        await update.message.reply_text(
            "✅ Rasm qabul qilindi.\nEndi yozing:\n<code>Nomi | Tavsif</code>",
            parse_mode=ParseMode.HTML
        )
        return

    if state == S_ADMIN_ADD_WAIT_INFO and is_admin(uid):
        txt = (update.message.text or "").strip()
        if "|" not in txt:
            await update.message.reply_text("Format xato. Misol:\n<code>Pomidor | Yangi pomidor</code>", parse_mode=ParseMode.HTML)
            return
        name, desc = [x.strip() for x in txt.split("|", 1)]
        photo_id = context.user_data.get("new_photo_file_id", "")
        pid = upsert_product(name, desc, photo_id)

        # default: 3 unitni ham so'rab qo'yamiz
        context.user_data["state"] = None
        await update.message.reply_text(
            f"✅ Mahsulot qo‘shildi: <b>{name}</b> (ID={pid})\n\n"
            "Endi variantlarni sozlash uchun shu formatda yuboring (3 ta qatorda):\n"
            "<code>ID | KG | narx | step | min | max</code>\n"
            "<code>ID | LT | narx | step | min | max</code>\n"
            "<code>ID | PC | narx | step | min | max</code>\n\n"
            "Masalan:\n"
            f"<code>{pid} | KG | 8.5 | 0.5 | 0.5 | 50</code>\n"
            f"<code>{pid} | PC | 2 | 1 | 1 | 200</code>",
            parse_mode=ParseMode.HTML
        )
        return

    # ----- ADMIN: category new -----
    if state == S_ADMIN_CAT_NEW and is_admin(uid):
        name = (update.message.text or "").strip()
        if len(name) < 2:
            await update.message.reply_text("Kategoriya nomi juda qisqa.")
            return
        cid = create_category(name)
        context.user_data["state"] = None
        await update.message.reply_text(f"✅ Kategoriya yaratildi: {name} (ID={cid})")
        return

    # ----- ADMIN: variant set (har qanday vaqtda) -----
    # Format: ID | KG | narx | step | min | max
    txt = (update.message.text or "").strip()
    if is_admin(uid) and "|" in txt:
        parts = [p.strip() for p in txt.split("|")]
        if len(parts) == 6 and parts[1].upper() in ("KG", "LT", "PC") and parts[0].isdigit():
            pid = int(parts[0])
            unit = parts[1].upper()
            try:
                price = float(parts[2].replace(",", "."))
                step = float(parts[3].replace(",", "."))
                mn = float(parts[4].replace(",", "."))
                mx = float(parts[5].replace(",", "."))
            except Exception:
                await update.message.reply_text("Sonlar xato. Misol: 8.5 | 0.5 | 0.5 | 50")
                return
            if step <= 0 or mn <= 0 or mx < mn:
                await update.message.reply_text("step/min/max noto‘g‘ri.")
                return
            if not get_product(pid):
                await update.message.reply_text("Bunday ID mahsulot yo‘q.")
                return
            set_variant(pid, unit, price, step, mn, mx)
            await update.message.reply_text(f"✅ Variant saqlandi: ID={pid}, {unit} — {money(price)}/{unit_label(unit)}, step={step:g}")
            return

    # ----- CHECKOUT flow -----
    if state == S_CHECK_PHONE:
        # contact bilan keladi
        if update.message.contact:
            phone = update.message.contact.phone_number or ""
        else:
            phone = (update.message.text or "").strip()
        context.user_data["phone"] = phone
        context.user_data["state"] = S_CHECK_LOCATION

        kb = ReplyKeyboardMarkup(
            [[KeyboardButton("📍 Lokatsiya yuborish", request_location=True)]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
        await update.message.reply_text("📍 Lokatsiya yuboring (tugma orqali). Agar xohlamasangiz 'o‘tib ket' deb yozing.", reply_markup=kb)
        return

    if state == S_CHECK_LOCATION:
        lat = lon = None
        if update.message.location:
            lat = float(update.message.location.latitude)
            lon = float(update.message.location.longitude)
        else:
            # user skip
            pass
        context.user_data["lat"] = lat
        context.user_data["lon"] = lon
        context.user_data["state"] = S_CHECK_ADDRESS
        await update.message.reply_text("🏠 Manzilni qo‘lda yozib yuboring:")
        return

    if state == S_CHECK_ADDRESS:
        address = (update.message.text or "").strip()
        context.user_data["address"] = address
        context.user_data["state"] = S_CHECK_NOTE
        await update.message.reply_text("📝 Izoh (ixtiyoriy). Izoh yo‘q bo‘lsa 'yo‘q' deb yozing:")
        return

    if state == S_CHECK_NOTE:
        note = (update.message.text or "").strip()
        if note.lower() in ("yoq", "yo‘q", "yo'q", "no", "нет"):
            note = ""
        phone = context.user_data.get("phone", "")
        address = context.user_data.get("address", "")
        lat = context.user_data.get("lat", None)
        lon = context.user_data.get("lon", None)

        oid = order_create(uid, phone, address, lat, lon, note)
        context.user_data["state"] = None
        if oid == -1:
            await update.message.reply_text("Savatcha bo‘sh. /start")
            return

        await update.message.reply_text(
            f"✅ Buyurtma qabul qilindi! ID: <b>{oid}</b>\nTez orada aloqaga chiqamiz.",
            parse_mode=ParseMode.HTML
        )

        # Adminlarga yuborish
        if ADMIN_IDS:
            order = get_order(oid)
            items = get_order_items(oid)
            lines = [
                f"🆕 <b>Yangi buyurtma #{oid}</b>",
                f"👤 User: <code>{uid}</code>",
                f"📞 {order['phone'] or '-'}",
                f"📍 {order['address'] or '-'}",
                f"💬 {order['note'] or '-'}",
                f"💰 Jami: <b>{money(float(order['total_sar']))}</b>",
                "",
                "🧺 Items:"
            ]
            for it in items:
                lines.append(f"• {it['name']} — {it['qty']:g} {unit_label(it['unit'])} = {money(float(it['line_total']))}")
            admin_msg = "\n".join(lines)

            for aid in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=aid,
                        text=admin_msg,
                        parse_mode=ParseMode.HTML,
                        reply_markup=kb_orders_admin(oid)
                    )
                except Exception:
                    pass

        await update.message.reply_text("Bosh menyu:", reply_markup=kb_home(uid))
        return

    # default fallback
    await update.message.reply_text("Menyu: /start")

# ===================== FLASK health (Render web service uchun) =====================
flask_app = Flask(__name__)

@flask_app.get("/")
def health():
    return "OK", 200

def run_flask():
    # Render health check uchun portga tinglaydi
    flask_app.run(host="0.0.0.0", port=PORT)

# ===================== MAIN =====================
def main():
    init_db()

    # Flask thread
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()

    # Telegram polling (eng barqaror)
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.ALL, on_message))

    log.info("Bot ishga tushdi (polling).")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
