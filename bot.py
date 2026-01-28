import os
import sqlite3
import logging
from datetime import datetime
from typing import Optional, List, Tuple

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
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

BOT_TOKEN = (os.getenv("TELEGRAM_TOKEN") or "").strip()
ADMIN_IDS_RAW = (os.getenv("ADMIN_IDS") or "").strip()  # "123,456"
SHOP_NAME = (os.getenv("SHOP_NAME") or "Оптом_озик_овкат Мадина").strip()
DB_PATH = (os.getenv("DB_PATH") or "data.db").strip()

if not BOT_TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN env yo‘q")

ADMIN_IDS = set()
if ADMIN_IDS_RAW:
    for x in ADMIN_IDS_RAW.split(","):
        x = x.strip()
        if x.isdigit():
            ADMIN_IDS.add(int(x))

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

# ================== DB ==================
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
        name TEXT NOT NULL UNIQUE
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS products(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        photo_file_id TEXT DEFAULT '',
        created_at TEXT NOT NULL
    )""")

    # Variant: product + unit (kg/lt/dona) + unit_price
    cur.execute("""
    CREATE TABLE IF NOT EXISTS variants(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        product_id INTEGER NOT NULL,
        unit TEXT NOT NULL,                 -- kg / lt / dona
        unit_price REAL NOT NULL DEFAULT 0, -- price per 1 unit
        UNIQUE(product_id, unit),
        FOREIGN KEY(product_id) REFERENCES products(id)
    )""")

    # product-category mapping (admin 2-bo'lim)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS product_categories(
        product_id INTEGER NOT NULL,
        category_id INTEGER NOT NULL,
        PRIMARY KEY(product_id, category_id),
        FOREIGN KEY(product_id) REFERENCES products(id),
        FOREIGN KEY(category_id) REFERENCES categories(id)
    )""")

    # cart: per user, per variant
    cur.execute("""
    CREATE TABLE IF NOT EXISTS carts(
        user_id INTEGER NOT NULL,
        variant_id INTEGER NOT NULL,
        qty INTEGER NOT NULL,
        PRIMARY KEY(user_id, variant_id),
        FOREIGN KEY(variant_id) REFERENCES variants(id)
    )""")

    # orders
    cur.execute("""
    CREATE TABLE IF NOT EXISTS orders(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        phone TEXT DEFAULT '',
        address TEXT DEFAULT '',
        lat REAL,
        lon REAL,
        status TEXT NOT NULL,           -- NEW/ACCEPTED/REJECTED/PACKING/ONTHEWAY/DELIVERED
        total REAL NOT NULL,
        created_at TEXT NOT NULL
    )""")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS order_items(
        order_id INTEGER NOT NULL,
        variant_id INTEGER NOT NULL,
        product_name TEXT NOT NULL,
        unit TEXT NOT NULL,
        unit_price REAL NOT NULL,
        qty INTEGER NOT NULL,
        line_total REAL NOT NULL
    )""")

    conn.commit()

    # seed categories if empty
    cur.execute("SELECT COUNT(*) AS c FROM categories")
    if cur.fetchone()["c"] == 0:
        for cname in ["🥬 Sabzavot", "🍎 Meva", "🍗 Go‘sht", "🐟 Baliq", "🥛 Sut", "🥫 Konserva", "🥖 Non", "🍚 Don", "🍫 Shirinlik", "🧴 Uy-ro‘zg‘or"]:
            cur.execute("INSERT OR IGNORE INTO categories(name) VALUES(?)", (cname,))
        conn.commit()

    conn.close()

# ================== SAFE EDIT ==================
def _same_markup(a, b) -> bool:
    try:
        return (a.to_dict() if a else None) == (b.to_dict() if b else None)
    except Exception:
        return False

async def safe_edit_text(q, text: str, reply_markup=None, parse_mode=None):
    try:
        current = ""
        if q.message:
            current = (q.message.text or q.message.caption or "").strip()
        new = (text or "").strip()
        if current == new and _same_markup(q.message.reply_markup if q.message else None, reply_markup):
            return
        await q.edit_message_text(text, reply_markup=reply_markup, parse_mode=parse_mode)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        raise

# ================== DATA ACCESS ==================
def get_categories() -> List[sqlite3.Row]:
    conn = db()
    rows = conn.execute("SELECT id, name FROM categories ORDER BY name").fetchall()
    conn.close()
    return rows

def get_category(cid: int) -> Optional[sqlite3.Row]:
    conn = db()
    row = conn.execute("SELECT id, name FROM categories WHERE id=?", (cid,)).fetchone()
    conn.close()
    return row

def get_products_in_category(cid: int) -> List[sqlite3.Row]:
    conn = db()
    rows = conn.execute("""
        SELECT p.id, p.name, p.photo_file_id
        FROM products p
        JOIN product_categories pc ON pc.product_id=p.id
        WHERE pc.category_id=?
        ORDER BY p.id DESC
    """, (cid,)).fetchall()
    conn.close()
    return rows

def get_product(pid: int) -> Optional[sqlite3.Row]:
    conn = db()
    row = conn.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    conn.close()
    return row

def get_variants(pid: int) -> List[sqlite3.Row]:
    conn = db()
    rows = conn.execute("SELECT * FROM variants WHERE product_id=? ORDER BY unit", (pid,)).fetchall()
    conn.close()
    return rows

def get_variant(vid: int) -> Optional[sqlite3.Row]:
    conn = db()
    row = conn.execute("""
        SELECT v.*, p.name as product_name, p.photo_file_id
        FROM variants v
        JOIN products p ON p.id=v.product_id
        WHERE v.id=?
    """, (vid,)).fetchone()
    conn.close()
    return row

def upsert_cart(user_id: int, variant_id: int, qty: int) -> None:
    conn = db()
    cur = conn.cursor()
    if qty <= 0:
        cur.execute("DELETE FROM carts WHERE user_id=? AND variant_id=?", (user_id, variant_id))
    else:
        cur.execute("INSERT OR REPLACE INTO carts(user_id, variant_id, qty) VALUES(?,?,?)", (user_id, variant_id, qty))
    conn.commit()
    conn.close()

def cart_items(user_id: int) -> List[sqlite3.Row]:
    conn = db()
    rows = conn.execute("""
        SELECT c.variant_id, c.qty, v.unit, v.unit_price, p.name as product_name
        FROM carts c
        JOIN variants v ON v.id=c.variant_id
        JOIN products p ON p.id=v.product_id
        WHERE c.user_id=?
        ORDER BY p.name
    """, (user_id,)).fetchall()
    conn.close()
    return rows

def cart_total(user_id: int) -> float:
    items = cart_items(user_id)
    return sum(float(r["unit_price"]) * int(r["qty"]) for r in items)

def cart_clear(user_id: int) -> None:
    conn = db()
    conn.execute("DELETE FROM carts WHERE user_id=?", (user_id,))
    conn.commit()
    conn.close()

def create_order_from_cart(user_id: int, phone: str, address: str, lat: Optional[float], lon: Optional[float]) -> int:
    items = cart_items(user_id)
    if not items:
        return -1

    total = cart_total(user_id)
    now = datetime.utcnow().isoformat()

    conn = db()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO orders(user_id, phone, address, lat, lon, status, total, created_at)
        VALUES(?,?,?,?,?,?,?,?)
    """, (user_id, phone, address, lat, lon, "NEW", float(total), now))
    oid = cur.lastrowid

    for it in items:
        line_total = float(it["unit_price"]) * int(it["qty"])
        cur.execute("""
            INSERT INTO order_items(order_id, variant_id, product_name, unit, unit_price, qty, line_total)
            VALUES(?,?,?,?,?,?,?)
        """, (oid, int(it["variant_id"]), it["product_name"], it["unit"], float(it["unit_price"]), int(it["qty"]), float(line_total)))

    conn.commit()
    conn.close()
    cart_clear(user_id)
    return oid

def get_order(order_id: int) -> Optional[sqlite3.Row]:
    conn = db()
    row = conn.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    conn.close()
    return row

def get_order_items(order_id: int) -> List[sqlite3.Row]:
    conn = db()
    rows = conn.execute("SELECT * FROM order_items WHERE order_id=?", (order_id,)).fetchall()
    conn.close()
    return rows

def set_order_status(order_id: int, status: str) -> None:
    conn = db()
    conn.execute("UPDATE orders SET status=? WHERE id=?", (status, order_id))
    conn.commit()
    conn.close()

# ================== UI ==================
def kb_home(uid: int) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("🛒 Kategoriyalar", callback_data="U:CATS")],
        [InlineKeyboardButton("🧺 Savatcha", callback_data="U:CART")],
    ]
    if is_admin(uid):
        rows.append([InlineKeyboardButton("🛠 Admin", callback_data="A:PANEL")])
    return InlineKeyboardMarkup(rows)

def kb_categories() -> InlineKeyboardMarkup:
    rows = []
    for c in get_categories():
        rows.append([InlineKeyboardButton(c["name"], callback_data=f"U:CAT:{c['id']}")])
    rows.append([InlineKeyboardButton("⬅️ Bosh menyu", callback_data="U:HOME")])
    return InlineKeyboardMarkup(rows)

def kb_products(cid: int) -> InlineKeyboardMarkup:
    prods = get_products_in_category(cid)
    rows = []
    for p in prods[:25]:
        rows.append([InlineKeyboardButton(p["name"], callback_data=f"U:PROD:{p['id']}")])
    rows.append([InlineKeyboardButton("⬅️ Kategoriyalar", callback_data="U:CATS")])
    return InlineKeyboardMarkup(rows)

def kb_product_variants(pid: int) -> InlineKeyboardMarkup:
    vars_ = get_variants(pid)
    rows = []
    for v in vars_:
        unit = v["unit"]
        price = float(v["unit_price"])
        rows.append([InlineKeyboardButton(f"{unit.upper()} — {price:.2f} SAR / 1", callback_data=f"U:VAR:{v['id']}")])
    rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="U:BACKCAT")])
    return InlineKeyboardMarkup(rows)

def kb_qty(vid: int, qty: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➖", callback_data=f"U:QTY:{vid}:{qty-1}"),
            InlineKeyboardButton(f"{qty}", callback_data="noop"),
            InlineKeyboardButton("➕", callback_data=f"U:QTY:{vid}:{qty+1}"),
        ],
        [InlineKeyboardButton("🧺 Savatchaga qo‘shish", callback_data=f"U:ADD:{vid}:{qty}")],
        [InlineKeyboardButton("⬅️ Variantlar", callback_data=f"U:PROD:{get_variant(vid)['product_id']}")],
    ])

def kb_cart(uid: int) -> InlineKeyboardMarkup:
    items = cart_items(uid)
    rows = []
    for it in items[:10]:
        vid = int(it["variant_id"])
        qty = int(it["qty"])
        rows.append([
            InlineKeyboardButton("➖", callback_data=f"U:CSET:{vid}:{qty-1}"),
            InlineKeyboardButton(f"{it['product_name']} ({it['unit']}) x{qty}", callback_data=f"U:VAR:{vid}"),
            InlineKeyboardButton("➕", callback_data=f"U:CSET:{vid}:{qty+1}"),
        ])
    if items:
        rows.append([InlineKeyboardButton("✅ Davom etish", callback_data="U:CHECKOUT")])
        rows.append([InlineKeyboardButton("❌ Bekor qilish", callback_data="U:CANCEL")])
        rows.append([InlineKeyboardButton("🧹 Savatchani tozalash", callback_data="U:CCLEAR")])
    rows.append([InlineKeyboardButton("⬅️ Bosh menyu", callback_data="U:HOME")])
    return InlineKeyboardMarkup(rows)

def kb_admin_panel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Mahsulot qo‘shish (rasm bilan)", callback_data="A:ADDPROD")],
        [InlineKeyboardButton("⚙️ Mahsulotni kategoriya(ga) qo‘shish", callback_data="A:ASSIGN")],
        [InlineKeyboardButton("➕ Kategoriya qo‘shish", callback_data="A:ADDCAT")],
        [InlineKeyboardButton("⬅️ Bosh menyu", callback_data="U:HOME")],
    ])

def kb_admin_choose_product(prefix: str) -> InlineKeyboardMarkup:
    conn = db()
    prods = conn.execute("SELECT id, name FROM products ORDER BY id DESC LIMIT 30").fetchall()
    conn.close()
    rows = []
    for p in prods:
        rows.append([InlineKeyboardButton(p["name"], callback_data=f"{prefix}:{p['id']}")])
    rows.append([InlineKeyboardButton("⬅️ Admin panel", callback_data="A:PANEL")])
    return InlineKeyboardMarkup(rows)

def kb_admin_choose_category(prefix: str, pid: int) -> InlineKeyboardMarkup:
    rows = []
    for c in get_categories():
        rows.append([InlineKeyboardButton(c["name"], callback_data=f"{prefix}:{pid}:{c['id']}")])
    rows.append([InlineKeyboardButton("⬅️ Orqaga", callback_data="A:ASSIGN")])
    return InlineKeyboardMarkup(rows)

def kb_order_admin(order_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Qabul", callback_data=f"O:ACCEPT:{order_id}"),
            InlineKeyboardButton("❌ Rad", callback_data=f"O:REJECT:{order_id}"),
        ],
        [
            InlineKeyboardButton("📦 Yig‘ilyapti", callback_data=f"O:PACKING:{order_id}"),
            InlineKeyboardButton("🚚 Yo‘lda", callback_data=f"O:ONTHEWAY:{order_id}"),
        ],
        [InlineKeyboardButton("🏁 Yetkazildi", callback_data=f"O:DELIVERED:{order_id}")],
    ])

# ================== STATES ==================
S_ADD_CAT = "ADD_CAT"
S_ADD_PROD_WAIT_PHOTO = "ADD_PROD_WAIT_PHOTO"
S_ADD_PROD_WAIT_NAME = "ADD_PROD_WAIT_NAME"
S_ADD_PROD_WAIT_UNITPRICES = "ADD_PROD_WAIT_UNITPRICES"  # "kg=12.5, lt=10, dona=2"

S_CHECKOUT_PHONE = "CHECKOUT_PHONE"
S_CHECKOUT_LOCATION = "CHECKOUT_LOCATION"
S_CHECKOUT_ADDRESS = "CHECKOUT_ADDRESS"

# ================== COMMANDS ==================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = f"🛍 <b>{SHOP_NAME}</b>\n\nKerakli bo‘limni tanlang:"
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=kb_home(uid))

async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        await update.message.reply_text("Admin emassiz.")
        return
    await update.message.reply_text("🛠 Admin panel", reply_markup=kb_admin_panel())

# ================== USER FLOWS ==================
async def show_cart_message(q, uid: int):
    items = cart_items(uid)
    if not items:
        await safe_edit_text(q, "🧺 Savatcha bo‘sh.", reply_markup=kb_cart(uid))
        return
    lines = []
    for it in items:
        line_total = float(it["unit_price"]) * int(it["qty"])
        lines.append(f"• {it['product_name']} ({it['unit']}) x{it['qty']} = <b>{line_total:.2f} SAR</b>")
    total = cart_total(uid)
    text = "🧺 <b>Savatcha</b>\n\n" + "\n".join(lines) + f"\n\n<b>Jami:</b> {total:.2f} SAR"
    await safe_edit_text(q, text, parse_mode=ParseMode.HTML, reply_markup=kb_cart(uid))

async def begin_checkout(update_or_q, context: ContextTypes.DEFAULT_TYPE, uid: int):
    if not cart_items(uid):
        if hasattr(update_or_q, "answer"):
            await update_or_q.answer("Savatcha bo‘sh.")
        return

    context.user_data["state"] = S_CHECKOUT_PHONE
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("📞 Telefon raqamni yuborish", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    msg = "📞 Telefon raqamingizni yuboring (Contact tugmasi orqali) yoki qo‘lda yozing:"
    if hasattr(update_or_q, "edit_message_text"):
        await update_or_q.edit_message_text(msg)
        await context.bot.send_message(uid, "Telefonni yuboring:", reply_markup=kb)
    else:
        await update_or_q.message.reply_text(msg, reply_markup=kb)

async def ask_location(uid: int, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = S_CHECKOUT_LOCATION
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton("📍 Lokatsiya yuborish", request_location=True)]],
        resize_keyboard=True,
        one_time_keyboard=True
    )
    await context.bot.send_message(uid, "📍 Lokatsiyani yuboring (Location tugmasi orqali):", reply_markup=kb)

async def ask_address(uid: int, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["state"] = S_CHECKOUT_ADDRESS
    await context.bot.send_message(uid, "🏠 Manzilni qo‘lda yozib yuboring:", reply_markup=ReplyKeyboardRemove())

async def finalize_order(uid: int, context: ContextTypes.DEFAULT_TYPE):
    phone = context.user_data.get("phone", "")
    address = context.user_data.get("address", "")
    lat = context.user_data.get("lat")
    lon = context.user_data.get("lon")

    oid = create_order_from_cart(uid, phone, address, lat, lon)
    context.user_data["state"] = None

    if oid == -1:
        await context.bot.send_message(uid, "Savatcha bo‘sh. /start", reply_markup=ReplyKeyboardRemove())
        return

    await context.bot.send_message(
        uid,
        f"✅ Buyurtmangiz qabul qilindi va ko‘rib chiqilmoqda.\nBuyurtma ID: <b>#{oid}</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=ReplyKeyboardRemove()
    )

    # notify admins
    if ADMIN_IDS:
        o = get_order(oid)
        items = get_order_items(oid)
        lines = [
            f"🆕 <b>Yangi buyurtma</b>  #{oid}",
            f"👤 User: <code>{o['user_id']}</code>",
            f"📞 {o['phone'] or '-'}",
            f"📍 Lokatsiya: {('bor' if o['lat'] is not None else 'yo‘q')}",
            f"🏠 Manzil: {o['address'] or '-'}",
            f"💰 Jami: <b>{float(o['total']):.2f} SAR</b>",
            "",
            "🧾 <b>Mahsulotlar:</b>"
        ]
        for it in items:
            lines.append(f"• {it['product_name']} ({it['unit']}) x{it['qty']} = {float(it['line_total']):.2f} SAR")

        msg = "\n".join(lines)
        for aid in ADMIN_IDS:
            try:
                await context.bot.send_message(aid, msg, parse_mode=ParseMode.HTML, reply_markup=kb_order_admin(oid))
                # agar lokatsiya bo‘lsa, adminlarga ham yuboramiz
                if o["lat"] is not None and o["lon"] is not None:
                    await context.bot.send_location(aid, latitude=float(o["lat"]), longitude=float(o["lon"]))
            except Exception:
                pass

# ================== ADMIN FLOWS ==================
async def admin_panel_cb(q, uid: int):
    await safe_edit_text(q, "🛠 Admin panel", reply_markup=kb_admin_panel())

async def admin_add_cat_start(q, context: ContextTypes.DEFAULT_TYPE, uid: int):
    context.user_data["state"] = S_ADD_CAT
    await safe_edit_text(q, "Kategoriya nomini yuboring (matn):")

async def admin_add_product_start(q, context: ContextTypes.DEFAULT_TYPE, uid: int):
    context.user_data["state"] = S_ADD_PROD_WAIT_PHOTO
    await safe_edit_text(q, "📸 Galereyadan mahsulot rasmini yuboring:")

async def admin_assign_start(q, uid: int):
    await safe_edit_text(q, "Qaysi mahsulotni kategoriya(ga) qo‘shamiz?", reply_markup=kb_admin_choose_product("A:CHPROD"))

# ================== CALLBACK HANDLER ==================
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = update.effective_user.id
    data = q.data
    try:
        await q.answer()
    except Exception:
        pass

    # noop
    if data == "noop":
        return

    # HOME
    if data == "U:HOME":
        await safe_edit_text(q, f"🛍 <b>{SHOP_NAME}</b>\n\nKerakli bo‘limni tanlang:", parse_mode=ParseMode.HTML, reply_markup=kb_home(uid))
        return

    # USER: categories
    if data == "U:CATS":
        await safe_edit_text(q, "🛒 Kategoriyalar:", reply_markup=kb_categories())
        return

    # USER: open category
    if data.startswith("U:CAT:"):
        cid = int(data.split(":")[2])
        c = get_category(cid)
        if not c:
            await q.answer("Kategoriya topilmadi.")
            return
        await safe_edit_text(q, f"📂 <b>{c['name']}</b>\nMahsulot tanlang:", parse_mode=ParseMode.HTML, reply_markup=kb_products(cid))
        context.user_data["last_cid"] = cid
        return

    # USER: product -> variant list
    if data.startswith("U:PROD:"):
        pid = int(data.split(":")[2])
        p = get_product(pid)
        if not p:
            await q.answer("Mahsulot topilmadi.")
            return
        vars_ = get_variants(pid)
        if not vars_:
            await safe_edit_text(q, "Bu mahsulotda hali Kg/Lt/Dona narxlari qo‘yilmagan (admin).", reply_markup=kb_categories())
            return
        text = f"🧾 <b>{p['name']}</b>\n\nKg/Lt/Dona variantini tanlang:"
        await safe_edit_text(q, text, parse_mode=ParseMode.HTML, reply_markup=kb_product_variants(pid))
        return

    # back to last category products
    if data == "U:BACKCAT":
        cid = context.user_data.get("last_cid")
        if not cid:
            await safe_edit_text(q, "🛒 Kategoriyalar:", reply_markup=kb_categories())
            return
        c = get_category(int(cid))
        await safe_edit_text(q, f"📂 <b>{c['name']}</b>\nMahsulot tanlang:", parse_mode=ParseMode.HTML, reply_markup=kb_products(int(cid)))
        return

    # USER: choose variant -> choose qty
    if data.startswith("U:VAR:"):
        vid = int(data.split(":")[2])
        v = get_variant(vid)
        if not v:
            await q.answer("Variant topilmadi.")
            return
        qty = 1
        text = (
            f"🧾 <b>{v['product_name']}</b>\n"
            f"📏 <b>{v['unit'].upper()}</b>\n"
            f"💰 1 {v['unit']} = <b>{float(v['unit_price']):.2f} SAR</b>\n\n"
            f"Hajmni tanlang (+/−):"
        )
        await safe_edit_text(q, text, parse_mode=ParseMode.HTML, reply_markup=kb_qty(vid, qty))
        return

    # USER: qty change
    if data.startswith("U:QTY:"):
        _, _, vid_s, qty_s = data.split(":")
        vid = int(vid_s)
        qty = int(qty_s)
        if qty < 1:
            qty = 1
        v = get_variant(vid)
        if not v:
            await q.answer("Variant topilmadi.")
            return
        total = float(v["unit_price"]) * qty
        text = (
            f"🧾 <b>{v['product_name']}</b>\n"
            f"📏 <b>{v['unit'].upper()}</b>\n"
            f"💰 1 {v['unit']} = <b>{float(v['unit_price']):.2f} SAR</b>\n"
            f"🧮 Jami: <b>{total:.2f} SAR</b>\n\n"
            f"Hajmni tanlang (+/−):"
        )
        await safe_edit_text(q, text, parse_mode=ParseMode.HTML, reply_markup=kb_qty(vid, qty))
        return

    # USER: add to cart
    if data.startswith("U:ADD:"):
        _, _, vid_s, qty_s = data.split(":")
        vid = int(vid_s)
        qty = int(qty_s)
        if qty < 1:
            qty = 1
        upsert_cart(uid, vid, qty)
        await show_cart_message(q, uid)
        return

    # USER: cart view
    if data == "U:CART":
        await show_cart_message(q, uid)
        return

    # USER: cart set qty
    if data.startswith("U:CSET:"):
        _, _, vid_s, qty_s = data.split(":")
        vid = int(vid_s)
        qty = int(qty_s)
        upsert_cart(uid, vid, qty)
        await show_cart_message(q, uid)
        return

    # USER: cart clear
    if data == "U:CCLEAR":
        cart_clear(uid)
        await safe_edit_text(q, "🧹 Savatcha tozalandi.", reply_markup=kb_home(uid))
        return

    # USER: cancel
    if data == "U:CANCEL":
        cart_clear(uid)
        await safe_edit_text(q, "❌ Bekor qilindi. Bosh menyu:", reply_markup=kb_home(uid))
        return

    # USER: checkout
    if data == "U:CHECKOUT":
        await begin_checkout(update, context, uid)
        return

    # ADMIN PANEL
    if data == "A:PANEL":
        if not is_admin(uid):
            await q.answer("Admin emassiz.")
            return
        await admin_panel_cb(q, uid)
        return

    if data == "A:PANEL" or data == "A:PANEL2":
        if not is_admin(uid):
            return
        await admin_panel_cb(q, uid)
        return

    if data == "A:ADDPROD":
        if not is_admin(uid):
            return
        await admin_add_product_start(q, context, uid)
        return

    if data == "A:ADDCAT":
        if not is_admin(uid):
            return
        await admin_add_cat_start(q, context, uid)
        return

    if data == "A:ASSIGN":
        if not is_admin(uid):
            return
        await admin_assign_start(q, uid)
        return

    # ADMIN choose product for assign
    if data.startswith("A:CHPROD:"):
        if not is_admin(uid):
            return
        pid = int(data.split(":")[2])
        p = get_product(pid)
        if not p:
            await q.answer("Mahsulot topilmadi.")
            return
        await safe_edit_text(q, f"Mahsulot: <b>{p['name']}</b>\nQaysi kategoriyaga qo‘shamiz?", parse_mode=ParseMode.HTML,
                             reply_markup=kb_admin_choose_category("A:CHCAT", pid))
        return

    # ADMIN choose category assign
    if data.startswith("A:CHCAT:"):
        if not is_admin(uid):
            return
        _, _, pid_s, cid_s = data.split(":")
        pid = int(pid_s)
        cid = int(cid_s)
        conn = db()
        conn.execute("INSERT OR IGNORE INTO product_categories(product_id, category_id) VALUES(?,?)", (pid, cid))
        conn.commit()
        conn.close()
        c = get_category(cid)
        await safe_edit_text(q, f"✅ <b>{get_product(pid)['name']}</b> → <b>{c['name']}</b> ga qo‘shildi.",
                             parse_mode=ParseMode.HTML, reply_markup=kb_admin_panel())
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
            "COLLECT": ("COLLECTING", "📦 Buyurtmangiz yig‘ilyapti."),
            "ONWAY": ("ONWAY", "🚚 Buyurtmangiz yo‘lda."),
            "DONE": ("DELIVERED", "✅ Buyurtmangiz yetkazildi."),
            "REJECT": ("REJECTED", "❌ Buyurtmangiz rad etildi."),
        }

        if action not in status_map:
            await q.answer("Noto‘g‘ri amal")
            return

        new_status, user_msg = status_map[action]

        # DB update
        cur.execute(
            "UPDATE orders SET status=? WHERE id=?",
            (new_status, oid)
        )
        conn.commit()

        # Userga xabar yuborish
        try:
            await context.bot.send_message(
                chat_id=user_id,
                text=f"📢 Buyurtma #{oid}\n{user_msg}"
            )
        except Exception:
            pass

        # Admin uchun yangilangan ko‘rinish
        kb = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Qabul", callback_data=f"O:ACCEPT:{oid}"),
                InlineKeyboardButton("❌ Rad", callback_data=f"O:REJECT:{oid}")
            ],
            [
                InlineKeyboardButton("📦 Yig‘ilyapti", callback_data=f"O:COLLECT:{oid}"),
                InlineKeyboardButton("🚚 Yo‘lda", callback_data=f"O:ONWAY:{oid}")
            ],
            [
                InlineKeyboardButton("🏁 Yetkazildi", callback_data=f"O:DONE:{oid}")
            ]
        ])

        await q.edit_message_text(
            f"🧾 Buyurtma #{oid}\n"
            f"👤 User ID: {user_id}\n"
            f"📌 Status: {new_status}",
            reply_markup=kb
        ) )
        return

    # Agar noma'lum callback kelsa
    await q.answer("Noma'lum buyruq.")
    return


# =========================
# BOT SETUP + RUN (Polling)
# =========================

def main() -> None:
    # DB init (sizda init_db() mavjud bo'lishi kerak)
    try:
        init_db()
    except Exception as e:
        logging.exception("DB init xato: %s", e)

    app = Application.builder().token(BOT_TOKEN).build()

    # Sizda quyidagi handler funksiyalar oldin yozilgan bo‘lishi kerak:
    # start(update, context)
    # on_callback(update, context)
    # on_text(update, context)   (checkout/admin text flow uchun)
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    # Polling ishga tushirish (Render uchun eng barqaror)
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )


if __name__ == "__main__":
    main()
