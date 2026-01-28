import os
import sqlite3
import logging
import threading
from datetime import datetime
from typing import Optional, List

from flask import Flask

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InputMediaPhoto,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.error import BadRequest

# ===================== CONFIG =====================
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("grocery_bot")

BOT_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
ADMIN_IDS_RAW = (os.getenv("ADMIN_IDS") or "").strip()   # "123,456"
SHOP_NAME = (os.getenv("SHOP_NAME") or "🛒 Online Oziq-ovqat").strip()

PORT = int(os.getenv("PORT", "10000"))
DB_PATH = (os.getenv("DB_PATH") or "data.db").strip()    # Render Disk bo'lsa: /var/data/data.db

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN env yo‘q. Render Environment ga qo‘ying.")

ADMIN_IDS = set()
if ADMIN_IDS_RAW:
    for x in ADMIN_IDS_RAW.split(","):
        x = x.strip()
        if x.isdigit():
            ADMIN_IDS.add(int(x))

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def now_iso() -> str:
    return datetime.utcnow().isoformat()

def money(x: float) -> str:
    return f"{x:.2f} SAR"

def unit_label(u: str) -> str:
    return {"KG": "Kg", "LT": "Lt", "PC": "Dona"}.get(u, u)

def unit_icon(u: str) -> str:
    return {"KG": "⚖️", "LT": "🧴", "PC": "📦"}.get(u, "🔹")

# ===================== DB =====================
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn

def init_db() -> None:
    conn = db()
    cur = conn.cursor()

    cur.execute("""
    CREATE TABLE IF NOT EXISTS categories(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL UNIQUE,
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        description TEXT DEFAULT '',
        photo_file_id TEXT DEFAULT '',
        is_active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )
    """)

    # Variant = unit (KG/LT/PC), price_per_unit, step, min, max
    cur.execute("""
    CREATE TABLE IF NOT EXISTS product_variants(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        unit TEXT NOT NULL,
        price_per_unit REAL NOT NULL DEFAULT 0,
        step REAL NOT NULL DEFAULT 1,
        min_qty REAL NOT NULL DEFAULT 1,
        max_qty REAL NOT NULL DEFAULT 999999,
        UNIQUE(product_id, unit),
        FOREIGN KEY(product_id) REFERENCES products(id)
    )
    """)

    # Mahsulotlarni kategoriya ichida ko'rsatish
    cur.execute("""
    CREATE TABLE IF NOT EXISTS product_categories(
        product_id INTEGER NOT NULL,
        category_id INTEGER NOT NULL,
        PRIMARY KEY(product_id, category_id),
        FOREIGN KEY(product_id) REFERENCES products(id),
        FOREIGN KEY(category_id) REFERENCES categories(id)
    )
    """)

    # Savatcha: variant bilan
    cur.execute("""
    CREATE TABLE IF NOT EXISTS carts(
        user_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        unit TEXT NOT NULL,
        qty REAL NOT NULL,
        PRIMARY KEY(user_id, product_id, unit)
    )
    """)

    # Orders
    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        phone TEXT DEFAULT '',
        address TEXT DEFAULT '',
        location_lat REAL,
        location_lon REAL,
        note TEXT DEFAULT '',
        total_sar REAL NOT NULL DEFAULT 0,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL
    )
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS order_items(
        order_id INTEGER NOT NULL,
        product_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        unit TEXT NOT NULL,
        price_per_unit REAL NOT NULL,
        qty REAL NOT NULL,
        line_total REAL NOT NULL,
        FOREIGN KEY(order_id) REFERENCES orders(id)
    )
    """)

    conn.commit()

    # Seed categories if empty
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

    conn.close()

# ===================== DB HELPERS =====================
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
    cur.execute("INSERT OR IGNORE INTO categories(name,is_active,created_at) VALUES(?,?,?)", (name, 1, now_iso()))
    conn.commit()
    cur.execute("SELECT id FROM categories WHERE name=?", (name,))
    cid = cur.fetchone()["id"]
    conn.close()
    return cid

def create_product(name: str, desc: str, photo_file_id: str) -> int:
    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO products(name, description, photo_file_id, is_active, created_at)
        VALUES(?,?,?,?,?)
    """, (name, desc, photo_file_id, 1, now_iso()))
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return pid

def get_product(pid: int) -> Optional[sqlite3.Row]:
    conn = db()
    r = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    conn.close()
    return r

def list_products(active_only=True) -> List[sqlite3.Row]:
    conn = db()
    if active_only:
        rows = conn.execute("SELECT * FROM products WHERE is_active=1 ORDER BY id DESC").fetchall()
    else:
        rows = conn.execute("SELECT * FROM products ORDER BY id DESC").fetchall()
    conn.close()
    return rows

def set_variant(pid: int, unit: str, price: float, step: float, mn: float, mx: float):
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
    """, (pid, unit, price, step, mn, mx))
    conn.commit()
    conn.close()

def get_variant(pid: int, unit: str) -> Optional[sqlite3.Row]:
    conn = db()
    r = conn.execute("SELECT * FROM product_variants WHERE product_id=? AND unit=?", (pid, unit)).fetchone()
    conn.close()
    return r

def get_variants(pid: int) -> List[sqlite3.Row]:
    conn = db()
    rows = conn.execute("SELECT * FROM product_variants WHERE product_id=? ORDER BY unit", (pid,)).fetchall()
    conn.close()
    return rows

def attach_product_to_category(pid: int, cid: int):
    conn = db()
    conn.execute("INSERT OR IGNORE INTO product_categories(product_id, category_id) VALUES(?,?)", (pid, cid))
    conn.commit()
    conn.close()

def get_products_in_category(cid: int) -> List[sqlite3.Row]:
    conn = db()
    rows = conn.execute("""
        SELECT p.*
        FROM products p
        JOIN product_categories pc ON pc.product_id=p.id
        WHERE pc.category_id=? AND p.is_active=1
        ORDER BY p.id DESC
    """, (cid,)).fetchall()
    conn.close()
    return rows

# ---- CART ----
def cart_items(uid: int) -> List[sqlite3.Row]:
    conn = db()
    rows = conn.execute("""
        SELECT c.user_id, c.product_id, c.unit, c.qty,
               p.name, p.photo_file_id,
               v.price_per_unit, v.step, v.min_qty, v.max_qty
        FROM carts c
        JOIN products p ON p.id=c.product_id
        JOIN product_variants v ON v.product_id=c.product_id AND v.unit=c.unit
        WHERE c.user_id=?
        ORDER BY p.name
    """, (uid,)).fetchall()
    conn.close()
    return rows

def cart_set(uid: int, pid: int, unit: str, qty: float):
    conn = db()
    cur = conn.cursor()
    if qty <= 0:
        cur.execute("DELETE FROM carts WHERE user_id=? AND product_id=? AND unit=?", (uid, pid, unit))
    else:
        cur.execute("""
            INSERT OR REPLACE INTO carts(user_id, product_id, unit, qty)
            VALUES(?,?,?,?)
        """, (uid, pid, unit, qty))
    conn.commit()
    conn.close()

def cart_clear(uid: int):
    conn = db()
    conn.execute("DELETE FROM carts WHERE user_id=?", (uid,))
    conn.commit()
    conn.close()

def cart_total(uid: int) -> float:
    items = cart_items(uid)
    return float(sum(float(i["price_per_unit"]) * float(i["qty"]) for i in items))

# ---- ORDERS ----
def order_create(uid: int, phone: str, address: str, lat: Optional[float], lon: Optional[float], note: str) -> int:
    items = cart_items(uid)
    if not items:
        return -1

    total = cart_total(uid)
    conn = db()
    cur = conn.cursor()

    cur.execute("""
        INSERT INTO orders(user_id, phone, address, location_lat, location_lon, note, total_sar, status, created_at)
        VALUES(?,?,?,?,?,?,?,?,?)
    """, (uid, phone, address, lat, lon, note, total, "NEW", now_iso()))
    oid = cur.lastrowid

    for it in items:
        line_total = float(it["price_per_unit"]) * float(it["qty"])
        cur.execute("""
            INSERT INTO order_items(order_id, product_id, name, unit, price_per_unit, qty, line_total)
            VALUES(?,?,?,?,?,?,?)
        """, (oid, int(it["product_id"]), it["name"], it["unit"], float(it["price_per_unit"]), float(it["qty"]), float(line_total)))

    conn.commit()
    conn.close()
    cart_clear(uid)
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

def set_order_status(oid: int, status: str):
    conn = db()
    conn.execute("UPDATE orders SET status=? WHERE id=?", (status, oid))
    conn.commit()
    conn.close()

def list_orders(limit=10) -> List[sqlite3.Row]:
    conn = db()
    rows = conn.execute("SELECT * FROM orders ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return rows

# ===================== UI HELPERS =====================
async def safe_edit_text(q, text: str, reply_markup=None, parse_mode=None):
    try:
        await q.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        raise

def kb_home(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🛒 Kategoriyalar", callback_data="CAT")],
        [InlineKeyboardButton("🧺 Savatcha", callback_data="CART")],
    ]
    if is_admin(uid):
        rows.append([InlineKeyboardButton("🛠 Admin", callback_data="ADMIN")])
    return InlineKeyboardMarkup(rows)

def kb_categories() -> InlineKeyboardMarkup:
    rows = []
    for c in get_categories(True):
        rows.append([InlineKeyboardButton(c["name"], callback_data=f"CAT:{c['id']}")])
    rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="HOME")])
    return InlineKeyboardMarkup(rows)

def kb_products(cid: int) -> InlineKeyboardMarkup:
    rows = []
    for p in get_products_in_category(cid)[:30]:
        rows.append([InlineKeyboardButton(p["name"], callback_data=f"P:{p['id']}")])
    rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="CAT")])
    return InlineKeyboardMarkup(rows)

def kb_product_units(pid: int) -> InlineKeyboardMarkup:
    rows = []
    vars_ = get_variants(pid)
    for v in vars_:
        u = v["unit"]
        rows.append([InlineKeyboardButton(
            f"{unit_icon(u)} {unit_label(u)} — {money(float(v['price_per_unit']))}/{unit_label(u)}",
            callback_data=f"U:{pid}:{u}"
        )])
    rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="CAT")])
    return InlineKeyboardMarkup(rows)

def kb_qty(pid: int, unit: str, qty: float) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➖", callback_data=f"Q:-:{pid}:{unit}"),
            InlineKeyboardButton(f"{qty:g} {unit_label(unit)}", callback_data="NOOP"),
            InlineKeyboardButton("➕", callback_data=f"Q:+:{pid}:{unit}"),
        ],
        [InlineKeyboardButton("🧺 Savatchaga qo‘shish", callback_data=f"ADD:{pid}:{unit}:{qty:g}")],
        [
            InlineKeyboardButton("🧺 Savatcha", callback_data="CART"),
            InlineKeyboardButton("🛒 Yana mahsulot", callback_data="CAT"),
        ]
    ])

def kb_cart(uid: int) -> InlineKeyboardMarkup:
    items = cart_items(uid)
    rows = []
    # Har bir item uchun: - / o‘chirish / +
    for it in items[:10]:
        pid = int(it["product_id"])
        unit = it["unit"]
        rows.append([
            InlineKeyboardButton("➖", callback_data=f"CQ:-:{pid}:{unit}"),
            InlineKeyboardButton("❌", callback_data=f"CDEL:{pid}:{unit}"),
            InlineKeyboardButton("➕", callback_data=f"CQ:+:{pid}:{unit}"),
        ])
    if items:
        rows.append([InlineKeyboardButton("➡️ Davom etish", callback_data="CHECKOUT")])
        rows.append([InlineKeyboardButton("🛒 Yana mahsulot qo‘shish", callback_data="CAT")])
        rows.append([InlineKeyboardButton("🧹 Savatchani tozalash", callback_data="CLEARCART")])
    rows.append([InlineKeyboardButton("⬅️ Bosh menyu", callback_data="HOME")])
    return InlineKeyboardMarkup(rows)

def kb_admin() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Mahsulot qo‘shish (rasm bilan)", callback_data="A:ADD")],
        [InlineKeyboardButton("✍️ Variant narx/step sozlash", callback_data="A:VHELP")],
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
        [
            InlineKeyboardButton("⬅️ Orqaga", callback_data="A:ORDERS"),
        ]
    ])

# ===================== STATES =====================
S_A_WAIT_PHOTO = "A_WAIT_PHOTO"
S_A_WAIT_META = "A_WAIT_META"
S_A_CATNEW = "A_CATNEW"
S_A_ATTACH_PICKP = "A_ATTACH_PICKP"
S_A_ATTACH_PICKC = "A_ATTACH_PICKC"

S_CHECK_PHONE = "CHECK_PHONE"
S_CHECK_LOC = "CHECK_LOC"
S_CHECK_ADDR = "CHECK_ADDR"
S_CHECK_NOTE = "CHECK_NOTE"

# ===================== BOT HANDLERS =====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (
        f"<b>{SHOP_NAME}</b>\n\n"
        "🛒 Kategoriyalar orqali mahsulot tanlang.\n"
        "🧺 Savatchada miqdorni o‘zgartirib davom etishingiz mumkin.\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_home(uid))

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("Admin emassiz.")
        return
    await update.message.reply_text("🛠 Admin panel", reply_markup=kb_admin())

async def show_cart_screen(q, uid: int):
    items = cart_items(uid)
    if not items:
        await safe_edit_text(q, "🧺 Savatcha bo‘sh.", reply_markup=kb_cart(uid))
        return

    lines = ["🧺 <b>Savatcha</b>\n"]
    for it in items:
        lt = float(it["price_per_unit"]) * float(it["qty"])
        lines.append(f"• {it['name']} — <b>{it['qty']:g}</b> {unit_label(it['unit'])} = <b>{money(lt)}</b>")
    total = cart_total(uid)

    lines.append(f"\n<b>Jami:</b> {money(total)}")
    lines.append("\n⬇️ Pastdagi tugmalar: miqdorni o‘zgartirish / o‘chirish / davom etish")
    await safe_edit_text(q, "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=kb_cart(uid))

async def show_product_with_photo(q, context: ContextTypes.DEFAULT_TYPE, pid: int):
    p = get_product(pid)
    if not p or int(p["is_active"]) != 1:
        await q.answer("Mahsulot topilmadi.")
        return

    desc = (p["description"] or "").strip()
    photo_id = (p["photo_file_id"] or "").strip()
    caption = f"🧾 <b>{p['name']}</b>\n\n{desc}\n\nO‘lchovni tanlang:"
    caption = caption.strip()

    # Mahsulot sahifasida rasm ko‘rsatamiz
    if photo_id:
        try:
            await q.edit_message_media(
                media=InputMediaPhoto(media=photo_id, caption=caption, parse_mode=ParseMode.HTML),
                reply_markup=kb_product_units(pid),
            )
            return
        except Exception:
            # edit bo'lmasa, yangi xabar yuboramiz
            try:
                await context.bot.send_photo(
                    chat_id=q.message.chat_id,
                    photo=photo_id,
                    caption=caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=kb_product_units(pid),
                )
                return
            except Exception:
                pass

    # fallback: rasm bo'lmasa text
    await safe_edit_text(q, caption, parse_mode=ParseMode.HTML, reply_markup=kb_product_units(pid))

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = update.effective_user.id
    data = q.data

    if data == "NOOP":
        return

    # HOME
    if data == "HOME":
        await safe_edit_text(q, f"<b>{SHOP_NAME}</b>\n\nKerakli bo‘limni tanlang:", parse_mode=ParseMode.HTML, reply_markup=kb_home(uid))
        return

    # CATEGORIES
    if data == "CAT":
        await safe_edit_text(q, "🛒 Kategoriyalar:", reply_markup=kb_categories())
        return

    if data.startswith("CAT:"):
        cid = int(data.split(":")[1])
        await safe_edit_text(q, "🛍 Mahsulotlar:", reply_markup=kb_products(cid))
        return

    # PRODUCT OPEN (with photo)
    if data.startswith("P:"):
        pid = int(data.split(":")[1])
        await show_product_with_photo(q, context, pid)
        return

    # UNIT select -> qty screen
    if data.startswith("U:"):
        _, pid_s, unit = data.split(":")
        pid = int(pid_s)
        v = get_variant(pid, unit)
        if not v:
            await q.answer("Bu mahsulotda bu o‘lchov yo‘q.")
            return
        qty = float(v["min_qty"])
        context.user_data["cur_pid"] = pid
        context.user_data["cur_unit"] = unit
        context.user_data["cur_qty"] = qty

        price = float(v["price_per_unit"]) * qty
        text = (
            f"{unit_icon(unit)} <b>{unit_label(unit)}</b>\n"
            f"Miqdor: <b>{qty:g}</b> {unit_label(unit)}\n"
            f"Narx: <b>{money(price)}</b>\n\n"
            "➕/➖ bilan miqdorni o‘zgartiring."
        )
        await safe_edit_text(q, text, parse_mode=ParseMode.HTML, reply_markup=kb_qty(pid, unit, qty))
        return

    # QTY adjust (product page)
    if data.startswith("Q:"):
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
        text = (
            f"{unit_icon(unit)} <b>{unit_label(unit)}</b>\n"
            f"Miqdor: <b>{qty:g}</b> {unit_label(unit)}\n"
            f"Narx: <b>{money(price)}</b>\n\n"
            "➕/➖ bilan miqdorni o‘zgartiring."
        )
        await safe_edit_text(q, text, parse_mode=ParseMode.HTML, reply_markup=kb_qty(pid, unit, qty))
        return

    # ADD to cart
    if data.startswith("ADD:"):
        _, pid_s, unit, qty_s = data.split(":")
        pid = int(pid_s)
        qty = float(qty_s)
        cart_set(uid, pid, unit, qty)
        await q.answer("Savatchaga qo‘shildi ✅")
        await show_cart_screen(q, uid)
        return

    # CART open
    if data == "CART":
        await show_cart_screen(q, uid)
        return

    # CART qty +/- by step
    if data.startswith("CQ:"):
        _, op, pid_s, unit = data.split(":")
        pid = int(pid_s)
        v = get_variant(pid, unit)
        if not v:
            return
        step = float(v["step"])
        mn = float(v["min_qty"])
        mx = float(v["max_qty"])

        # current qty from cart
        items = cart_items(uid)
        cur_qty = 0.0
        for it in items:
            if int(it["product_id"]) == pid and it["unit"] == unit:
                cur_qty = float(it["qty"])
                break
        if cur_qty <= 0:
            cur_qty = mn

        if op == "+":
            newq = min(mx, cur_qty + step)
        else:
            newq = cur_qty - step
            if newq < mn:
                newq = 0  # remove item

        cart_set(uid, pid, unit, newq)
        await show_cart_screen(q, uid)
        return

    # CART delete item
    if data.startswith("CDEL:"):
        _, pid_s, unit = data.split(":")
        pid = int(pid_s)
        cart_set(uid, pid, unit, 0)
        await q.answer("O‘chirildi ✅")
        await show_cart_screen(q, uid)
        return

    if data == "CLEARCART":
        cart_clear(uid)
        await safe_edit_text(q, "🧹 Savatcha tozalandi.", reply_markup=kb_home(uid))
        return

    # CHECKOUT
    if data == "CHECKOUT":
        if not cart_items(uid):
            await q.answer("Savatcha bo‘sh.")
            return
        context.user_data["state"] = S_CHECK_PHONE
        kb = ReplyKeyboardMarkup(
            [[KeyboardButton("📞 Telefon raqamni yuborish", request_contact=True)]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
        await q.message.reply_text("📞 Telefon raqamingizni yuboring (tugma orqali).", reply_markup=kb)
        return

    # ADMIN PANEL
    if data == "ADMIN":
        if not is_admin(uid):
            await q.answer("Admin emassiz.")
            return
        await safe_edit_text(q, "🛠 Admin panel:", reply_markup=kb_admin())
        return

    if data == "A:ADD":
        if not is_admin(uid):
            return
        context.user_data["state"] = S_A_WAIT_PHOTO
        await safe_edit_text(
            q,
            "➕ Mahsulot qo‘shish:\n\n"
            "1) Avval <b>rasm yuboring</b> (galereyadan).\n"
            "2) Keyin bot sizdan: <code>Nomi | Tavsif</code> so‘raydi.\n"
            "3) So‘ng variantlarni (KG/LT/PC) sozlaysiz.\n",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_admin()
        )
        return

    if data == "A:VHELP":
        if not is_admin(uid):
            return
        await safe_edit_text(
            q,
            "✍️ Variant sozlash (admin):\n\n"
            "Har qanday payt shu formatda yuborasiz:\n"
            "<code>ID | KG | narx | step | min | max</code>\n"
            "<code>ID | LT | narx | step | min | max</code>\n"
            "<code>ID | PC | narx | step | min | max</code>\n\n"
            "Misol:\n"
            "<code>1 | KG | 8.5 | 0.5 | 0.5 | 50</code>\n"
            "<code>1 | PC | 2 | 1 | 1 | 200</code>\n",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_admin()
        )
        return

    if data == "A:CATNEW":
        if not is_admin(uid):
            return
        context.user_data["state"] = S_A_CATNEW
        await safe_edit_text(q, "📁 Yangi kategoriya nomini yuboring:", reply_markup=kb_admin())
        return

    if data == "A:ATTACH":
        if not is_admin(uid):
            return
        prods = list_products(True)[:30]
        rows = [[InlineKeyboardButton(f"{p['id']}. {p['name']}", callback_data=f"A:PICKP:{p['id']}")] for p in prods]
        rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="ADMIN")])
        context.user_data["state"] = S_A_ATTACH_PICKP
        await safe_edit_text(q, "🔗 Qaysi mahsulotni kategoriya ichiga qo‘shamiz?", reply_markup=InlineKeyboardMarkup(rows))
        return

    if data.startswith("A:PICKP:"):
        if not is_admin(uid):
            return
        pid = int(data.split(":")[2])
        context.user_data["attach_pid"] = pid
        context.user_data["state"] = S_A_ATTACH_PICKC
        cats = get_categories(True)
        rows = [[InlineKeyboardButton(f"{c['id']}. {c['name']}", callback_data=f"A:PICKC:{c['id']}")] for c in cats]
        rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="ADMIN")])
        await safe_edit_text(q, "📌 Qaysi kategoriya?", reply_markup=InlineKeyboardMarkup(rows))
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
        await safe_edit_text(q, "✅ Mahsulot kategoriya ichiga qo‘shildi.", reply_markup=kb_admin())
        return

    if data == "A:ORDERS":
        if not is_admin(uid):
            return
        orders = list_orders(10)
        if not orders:
            await safe_edit_text(q, "Buyurtmalar yo‘q.", reply_markup=kb_admin())
            return
        lines = ["🧾 Oxirgi buyurtmalar:\n"]
        rows = []
        for o in orders:
            lines.append(f"• #{o['id']} | user {o['user_id']} | {money(float(o['total_sar']))} | {o['status']}")
            rows.append([InlineKeyboardButton(f"📦 Buyurtma #{o['id']}", callback_data=f"A:ORD:{o['id']}")])
        rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="ADMIN")])
        await safe_edit_text(q, "\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))
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
            txt.append(f"• {it['name']} — {it['qty']:g} {unit_label(it['unit'])} = {money(float(it['line_total']))}")
        await safe_edit_text(q, "\n".join(txt), parse_mode=ParseMode.HTML, reply_markup=kb_orders_admin(oid))
        return

    # ORDER status buttons
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
            "COLLECT": ("COLLECTING", "📦 Buyurtmangiz yig‘ilyapti."),
            "ONWAY": ("ONWAY", "🚚 Buyurtmangiz yo‘lda."),
            "DONE": ("DELIVERED", "🏁 Buyurtmangiz yetkazildi. Rahmat!"),
        }
        if action not in status_map:
            return

        new_status, user_msg = status_map[action]
        set_order_status(oid, new_status)

        # userga xabar
        try:
            await context.bot.send_message(chat_id=user_id, text=f"📦 Buyurtma #{oid}\n{user_msg}")
        except Exception:
            pass

        # admin xabarini yangilash
        await safe_edit_text(
            q,
            f"🧾 Buyurtma #{oid}\n"
            f"👤 User ID: {user_id}\n"
            f"📌 Status: {new_status}",
            reply_markup=kb_orders_admin(oid)
        )
        return

    await q.answer("Noma'lum buyruq.")

# ADMIN: photo capture
async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return

    if context.user_data.get("state") != S_A_WAIT_PHOTO:
        return

    photo = update.message.photo[-1]
    context.user_data["new_photo_file_id"] = photo.file_id
    context.user_data["state"] = S_A_WAIT_META
    await update.message.reply_text(
        "✅ Rasm saqlandi.\n\nEndi yuboring:\n<code>Nomi | Tavsif</code>\n\nMisol:\n<code>Olma | Yangi olma</code>",
        parse_mode=ParseMode.HTML
    )

# TEXT handler (admin variant set + admin meta + checkout)
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = context.user_data.get("state")
    txt = (update.message.text or "").strip()

    # ADMIN: category create
    if state == S_A_CATNEW and is_admin(uid):
        if len(txt) < 2:
            await update.message.reply_text("Kategoriya nomi juda qisqa.")
            return
        cid = create_category(txt)
        context.user_data["state"] = None
        await update.message.reply_text(f"✅ Kategoriya yaratildi (ID={cid}).", reply_markup=kb_admin())
        return

    # ADMIN: after photo -> meta
    if state == S_A_WAIT_META and is_admin(uid):
        if "|" not in txt:
            await update.message.reply_text("Format xato. Misol:\n<code>Olma | Yangi olma</code>", parse_mode=ParseMode.HTML)
            return
        name, desc = [x.strip() for x in txt.split("|", 1)]
        photo_id = (context.user_data.get("new_photo_file_id") or "").strip()
        pid = create_product(name, desc, photo_id)
        context.user_data["state"] = None
        context.user_data.pop("new_photo_file_id", None)

        await update.message.reply_text(
            f"✅ Mahsulot qo‘shildi: {name} (ID={pid})\n\n"
            "Endi variantlarni sozlang (admin):\n"
            "<code>ID | KG | narx | step | min | max</code>\n"
            "<code>ID | LT | narx | step | min | max</code>\n"
            "<code>ID | PC | narx | step | min | max</code>\n\n"
            "Misol:\n"
            f"<code>{pid} | KG | 8.5 | 0.5 | 0.5 | 50</code>\n"
            f"<code>{pid} | PC | 2 | 1 | 1 | 200</code>\n\n"
            "So‘ng /admin → 🔗 Mahsulotni kategoriya bog‘lash qilib, kategoriya ichiga qo‘shasiz.",
            parse_mode=ParseMode.HTML,
            reply_markup=kb_admin()
        )
        return

    # ADMIN: set variants ANYTIME
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
                await update.message.reply_text("Bunday mahsulot ID yo‘q.")
                return
            set_variant(pid, unit, price, step, mn, mx)
            await update.message.reply_text(f"✅ Variant saqlandi: ID={pid}, {unit} — {money(price)}/{unit_label(unit)}, step={step:g}")
            return

    # CHECKOUT FLOW
    if state == S_CHECK_PHONE:
        phone = ""
        if update.message.contact:
            phone = update.message.contact.phone_number or ""
        else:
            phone = txt
        context.user_data["phone"] = phone
        context.user_data["state"] = S_CHECK_LOC

        kb = ReplyKeyboardMarkup(
            [[KeyboardButton("📍 Lokatsiya yuborish", request_location=True)]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
        await update.message.reply_text("📍 Lokatsiya yuboring (tugma bilan). Xohlamasangiz 'o‘tib ket' deb yozing.", reply_markup=kb)
        return

    if state == S_CHECK_ADDR:
        context.user_data["address"] = txt
        context.user_data["state"] = S_CHECK_NOTE
        await update.message.reply_text("📝 Izoh (ixtiyoriy). Izoh yo‘q bo‘lsa 'yo‘q' deb yozing:")
        return

    if state == S_CHECK_NOTE:
        note = txt
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

        # Adminlarga xabar
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
            msg = "\n".join(lines)

            for aid in ADMIN_IDS:
                try:
                    await context.bot.send_message(
                        chat_id=aid,
                        text=msg,
                        parse_mode=ParseMode.HTML,
                        reply_markup=kb_orders_admin(oid)
                    )
                except Exception:
                    pass

        await update.message.reply_text("Bosh menyu: /start")
        return

    # Default
    await update.message.reply_text("Menyu: /start")

# LOCATION handler (checkout)
async def on_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = context.user_data.get("state")
    if state != S_CHECK_LOC:
        return

    if update.message.location:
        context.user_data["lat"] = float(update.message.location.latitude)
        context.user_data["lon"] = float(update.message.location.longitude)
    else:
        context.user_data["lat"] = None
        context.user_data["lon"] = None

    context.user_data["state"] = S_CHECK_ADDR
    await update.message.reply_text("🏠 Manzilni qo‘lda yozib yuboring:")

# CONTACT handler (checkout)
async def on_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = context.user_data.get("state")
    if state != S_CHECK_PHONE:
        return
    # Contact keldi -> on_textdagi S_CHECK_PHONEga o'xshash
    phone = update.message.contact.phone_number or ""
    context.user_data["phone"] = phone
    context.user_data["state"] = S_CHECK_LOC
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Lokatsiya yuborish", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await update.message.reply_text("📍 Lokatsiya yuboring (tugma bilan). Xohlamasangiz 'o‘tib ket' deb yozing.", reply_markup=kb)

# ===================== FLASK health (Render Web Service uchun) =====================
flask_app = Flask(__name__)

@flask_app.get("/")
def health():
    return "OK", 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=PORT)

# ===================== MAIN =====================
def main():
    init_db()

    # Flask health thread (Render web service health check uchun)
    t = threading.Thread(target=run_flask, daemon=True)
    t.start()

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("admin", cmd_admin))
    app.add_handler(CallbackQueryHandler(on_callback))

    # Admin photo
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))

    # Checkout contact/location
    app.add_handler(MessageHandler(filters.CONTACT, on_contact))
    app.add_handler(MessageHandler(filters.LOCATION, on_location))

    # Text (admin meta/variant + checkout)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("Bot ishga tushdi (polling).")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
