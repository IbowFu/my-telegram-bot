# -- coding: utf-8 --
"""
Forex News Subscription Bot — الإصدار النهائي الكامل
"""
import asyncio
import json
import logging
import os
import sqlite3
import time
from contextlib import closing
from types import SimpleNamespace
from typing import Optional, Dict, Tuple
import pandas as pd
from aiogram import Bot, Dispatcher, Router, F
from aiogram.enums import ParseMode
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.filters import CommandStart, ChatMemberUpdatedFilter, IS_NOT_MEMBER, IS_MEMBER
from aiogram.types import (
    Message, CallbackQuery, ChatMemberUpdated,
    InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
)
from aiogram.client.default import DefaultBotProperties

# --- 🔽 قراءة ملف .env ---
from dotenv import load_dotenv 
load_dotenv()

# ---------------------- Configuration ----------------------
TOKEN = os.getenv("BOT_TOKEN")
if not TOKEN:
    raise RuntimeError("❌ BOT_TOKEN is missing in environment variables.")

ADMIN_ID = os.getenv("ADMIN_ID")
if not ADMIN_ID:
    raise RuntimeError("❌ ADMIN_ID is missing in environment variables.")
ADMIN_ID = int(ADMIN_ID)

PUBLIC_CHANNEL_USERNAME = os.getenv("PUBLIC_CHANNEL_USERNAME", "⚡📉 اخبار الفوركس 📈")
PRIVATE_CHANNEL_LINK = os.getenv("PRIVATE_CHANNEL_LINK", "https://t.me/ForexNews24hours")
PRIVATE_CHANNEL_ID = os.getenv("PRIVATE_CHANNEL_ID")

LINKS_FILE = "links.json"
WALLETS_FILE = "wallets.json"
BUTTONS_FILE = "buttons.json"
DB_FILE = os.getenv("DB_FILE", "subscriptions.db")
TEXTS_AR_FILE = "texts_ar.json"
TEXTS_EN_FILE = "texts_en.json"

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")

# تخزين آخر وقت أرسل فيه المستخدم طلب دعم
last_support_request = {}
SUPPORT_COOLDOWN = 180  # 3 دقائق

# ---------------------- تحميل الأزرار ----------------------
def load_buttons():
    try:
        with open(BUTTONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("ar", {}), data.get("en", {})
    except Exception as e:
        logging.error("❌ Failed to load buttons: %s", e)
        return {}, {}

BTN_AR, BTN_EN = load_buttons()

def btn(key: str, lang: str = "ar") -> str:
    if lang == "ar":
        return BTN_AR.get(key, key)
    else:
        return BTN_EN.get(key, BTN_AR.get(key, key))

# ---------------------- تحميل النصوص ----------------------
def load_texts(lang: str) -> Dict[str, str]:
    file_path = TEXTS_AR_FILE if lang == "ar" else TEXTS_EN_FILE
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception as e:
        logging.warning("⚠️ Texts file %s not found or invalid: %s", file_path, e)
    return {"error": "Texts not loaded"}

def get_text(key: str, lang: str = "ar", **kwargs) -> str:
    texts = load_texts(lang)
    text = texts.get(key, key)
    for k, v in kwargs.items():
        text = text.replace(f"%{k}%", str(v))
    return text

# ---------------------- تحميل الروابط والمحافظ ----------------------
def load_links():
    try:
        with open(LINKS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logging.error("❌ Failed to load links: %s", e)
        return []

def save_links(links):
    try:
        with open(LINKS_FILE, "w", encoding="utf-8") as f:
            json.dump(links, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error("❌ Failed to save links: %s", e)

def get_channel_link() -> Optional[str]:
    links = load_links()
    for channel in links:
        if not channel.get("used", False):
            channel["used"] = True
            save_links(links)
            return channel["link"]
    return PRIVATE_CHANNEL_LINK.strip()

def load_wallets():
    try:
        with open(WALLETS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {"USDT TRC20": "غير متوفر"}
    except Exception as e:
        logging.error("❌ Failed to load wallets: %s", e)
        return {"USDT TRC20": "غير متوفر"}

def save_wallets(wallets):
    try:
        with open(WALLETS_FILE, "w", encoding="utf-8") as f:
            json.dump(wallets, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logging.error("❌ Failed to save wallets: %s", e)

# ---------------------- قاعدة البيانات ----------------------
def init_db():
    with closing(sqlite3.connect(DB_FILE)) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                method TEXT,
                duration_months INTEGER,
                start_ts INTEGER,
                end_ts INTEGER,
                state TEXT,
                receipt_file_id TEXT,
                language TEXT
            )
            """
        )
        conn.commit()

def upsert_subscription(sub: SimpleNamespace):
    with closing(sqlite3.connect(DB_FILE)) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO subscriptions (user_id, username, method, duration_months, start_ts, end_ts, state, receipt_file_id, language)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
              username=excluded.username,
              method=excluded.method,
              duration_months=excluded.duration_months,
              start_ts=excluded.start_ts,
              end_ts=excluded.end_ts,
              state=excluded.state,
              receipt_file_id=excluded.receipt_file_id,
              language=excluded.language
            """,
            (
                sub.user_id,
                sub.username,
                sub.method,
                sub.duration_months,
                sub.start_ts,
                sub.end_ts,
                sub.state,
                sub.receipt_file_id,
                getattr(sub, "language", "ar"),
            ),
        )
        conn.commit()

def get_subscription(user_id: int) -> Optional[dict]:
    with closing(sqlite3.connect(DB_FILE)) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT user_id, username, method, duration_months, start_ts, end_ts, state, receipt_file_id, language FROM subscriptions WHERE user_id=?",
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        keys = ["user_id", "username", "method", "duration_months", "start_ts", "end_ts", "state", "receipt_file_id", "language"]
        return dict(zip(keys, row))

def list_df(query: str = "SELECT * FROM subscriptions", params: Tuple = ()) -> pd.DataFrame:
    with closing(sqlite3.connect(DB_FILE)) as conn:
        return pd.read_sql_query(query, conn, params=params)

# ---------------------- FSM ----------------------
class Flow(StatesGroup):
    choosing_language = State()
    choosing_subscription = State()
    choosing_duration = State()
    choosing_payment = State()
    waiting_receipt = State()
    broadcast_waiting = State()
    search_waiting = State()
    admin_search_waiting = State()
    edit_wallet_waiting = State()
    add_links_waiting = State()
    add_new_wallet_method_name = State()
    add_new_wallet_method_address = State()

# ---------------------- الكيبوردات ----------------------
def main_keyboard(lang: str = "ar", user_id: int = None) -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text=btn("free_news", lang), callback_data="free_news")],
        [InlineKeyboardButton(text=btn("paid_sub", lang), callback_data="paid_sub")],
        [InlineKeyboardButton(text=btn("my_account", lang), callback_data="my_account")]
    ]
    if user_id == ADMIN_ID:
        kb.append([InlineKeyboardButton(text=btn("admin_panel", lang), callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def admin_keyboard(lang: str = "ar") -> InlineKeyboardMarkup:
    kb = [
        [InlineKeyboardButton(text=btn("admin_stats", lang), callback_data="admin_stats")],
        [InlineKeyboardButton(text=btn("admin_pending", lang), callback_data="admin_pending")],
        [InlineKeyboardButton(text=btn("admin_all_users", lang), callback_data="admin_all_users")],
        [InlineKeyboardButton(text=btn("admin_search", lang), callback_data="admin_search")],
        [InlineKeyboardButton(text="📩 إرسال رسالة لمستخدم", callback_data="send_to_user")],
        [InlineKeyboardButton(text=btn("admin_links", lang), callback_data="admin_links")],
        [InlineKeyboardButton(text=btn("admin_wallets", lang), callback_data="admin_wallets")],
        [InlineKeyboardButton(text=btn("admin_broadcast", lang), callback_data="admin_broadcast")],
        [InlineKeyboardButton(text=btn("admin_export", lang), callback_data="admin_export")],
        [InlineKeyboardButton(text=btn("back", lang), callback_data="go_start")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=kb)

def links_keyboard(lang: str = "ar") -> InlineKeyboardMarkup:
    links = load_links()
    kb = []
    for i, link in enumerate(links):
        status = "✅" if link.get("used") else "🟢"
        kb.append([InlineKeyboardButton(
            text=f"{i+1}. {link['link']} {status}",
            callback_data=f"link_{i}"
        )])
    kb.append([InlineKeyboardButton(text=btn("add_links", lang), callback_data="add_links")])
    kb.append([InlineKeyboardButton(text=btn("clear_links", lang), callback_data="clear_links")])
    kb.append([InlineKeyboardButton(text=btn("back", lang), callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

def wallets_keyboard(lang: str = "ar") -> InlineKeyboardMarkup:
    wallets = load_wallets()
    kb = []
    for key, addr in wallets.items():
        kb.append([InlineKeyboardButton(text=f"{key}: {addr[:15]}...", callback_data=f"wallet_{key}")])
    kb.append([InlineKeyboardButton(text=btn("edit_wallets", lang), callback_data="edit_wallets")])
    kb.append([InlineKeyboardButton(text=btn("back", lang), callback_data="admin_panel")])
    return InlineKeyboardMarkup(inline_keyboard=kb)

# ---------------------- الروتر ----------------------
router = Router()

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    kb_lang = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🇸🇦 عربي", callback_data="lang_ar")],
        [InlineKeyboardButton(text="🇬🇧 English", callback_data="lang_en")],
    ])
    await message.answer("اختـــــــــــــر لغتـــــــــــك / Choose your language:", reply_markup=kb_lang)
    await state.set_state(Flow.choosing_language)

@router.message(F.text == "/admin")
async def admin_command(message: Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("🚫 هذا الأمر مخصص للمشرف فقط.")
        return
    lang = (get_subscription(message.from_user.id) or {}).get("language", "ar")
    markup = admin_keyboard(lang)
    await message.answer("🔧 *لوحة التحكم*", reply_markup=markup, parse_mode="Markdown")

@router.callback_query(F.data.in_(["lang_ar", "lang_en"]))
async def choose_language(cq: CallbackQuery, state: FSMContext):
    lang = "ar" if cq.data == "lang_ar" else "en"
    sub_dict = get_subscription(cq.from_user.id)
    if not sub_dict:
        sub = SimpleNamespace(
            user_id=cq.from_user.id,
            username=cq.from_user.username,
            method=None,
            duration_months=None,
            start_ts=None,
            end_ts=None,
            state="new",
            receipt_file_id=None,
            language=lang,
        )
    else:
        sub = SimpleNamespace(**sub_dict)
        sub.language = lang
    upsert_subscription(sub)

    markup = main_keyboard(lang=lang, user_id=cq.from_user.id)
    await cq.message.edit_text(get_text("choose_service", lang), reply_markup=markup)
    await state.set_state(Flow.choosing_subscription)
    await cq.answer()

@router.callback_query(F.data == "go_start")
async def go_start(cq: CallbackQuery, state: FSMContext):
    sub = get_subscription(cq.from_user.id)
    lang = (sub or {}).get("language", "ar")
    markup = main_keyboard(lang=lang, user_id=cq.from_user.id)
    await cq.message.edit_text(get_text("choose_service", lang), reply_markup=markup)
    await state.set_state(Flow.choosing_subscription)
    await cq.answer()

@router.callback_query(F.data == "free_news")
async def free_news(cq: CallbackQuery):
    lang = (get_subscription(cq.from_user.id) or {}).get("language", "ar")
    channel = f"https://t.me/{PUBLIC_CHANNEL_USERNAME}"
    text = f"📰 القناة العامة: {channel}"
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="فتح القناة", url=channel)],
        [InlineKeyboardButton(text=btn("back", lang), callback_data="go_start")]
    ])
    await cq.message.edit_text(text, reply_markup=kb)
    await cq.answer()

@router.callback_query(F.data == "paid_sub")
async def paid_sub(cq: CallbackQuery, state: FSMContext):
    lang = (get_subscription(cq.from_user.id) or {}).get("language", "ar")
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn("duration_1", lang), callback_data="duration_1")],
        [InlineKeyboardButton(text=btn("duration_3", lang), callback_data="duration_3")],
        [InlineKeyboardButton(text=btn("duration_6", lang), callback_data="duration_6")],
        [InlineKeyboardButton(text=btn("back", lang), callback_data="go_start")],
    ])
    await cq.message.edit_text(get_text("sub_duration", lang), reply_markup=kb)
    await state.set_state(Flow.choosing_duration)
    await cq.answer()

@router.callback_query(F.data.startswith("duration_"))
async def choose_duration(cq: CallbackQuery, state: FSMContext):
    months = int(cq.data.split("_")[1])
    lang = (get_subscription(cq.from_user.id) or {}).get("language", "ar")
    wallets = load_wallets()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=method, callback_data=f"method_{method}")]
        for method in wallets.keys()
    ] + [[InlineKeyboardButton(text=btn("back", lang), callback_data="paid_sub")]])
    await cq.message.edit_text(get_text("payment_method", lang, months=months), reply_markup=kb)
    await state.update_data(duration_months=months)
    await state.set_state(Flow.choosing_payment)
    await cq.answer()

@router.callback_query(F.data.startswith("method_"))
async def choose_payment(cq: CallbackQuery, state: FSMContext):
    method = cq.data.split("_", 1)[1]
    lang = (get_subscription(cq.from_user.id) or {}).get("language", "ar")
    wallets = load_wallets()
    address = wallets.get(method, "غير متوفر")
    await state.update_data(payment_method=method)
    await cq.message.edit_text(get_text("send_receipt", lang, address=address))
    await state.set_state(Flow.waiting_receipt)
    await cq.answer()

@router.message(Flow.waiting_receipt, F.photo)
async def receive_receipt(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    lang = (get_subscription(user_id) or {}).get("language", "ar")
    data = await state.get_data()
    sub_dict = get_subscription(user_id)
    sub = SimpleNamespace(**(sub_dict or {}))
    sub.user_id = user_id
    sub.username = message.from_user.username
    sub.method = data["payment_method"]
    sub.duration_months = data["duration_months"]
    sub.receipt_file_id = message.photo[-1].file_id
    sub.state = "pending"
    upsert_subscription(sub)

    try:
        username = f"@{message.from_user.username}" if message.from_user.username else f"ID: {user_id}"
        await bot.send_message(
            ADMIN_ID,
            f"📥 طلب اشتراك جديد!\n"
            f"👤 المستخدم: {username}\n"
            f"🆔 الرقم: {user_id}\n"
            f"📅 المدة: {sub.duration_months} شهر\n"
            f"🏦 الطريقة: {sub.method}"
        )
        await bot.send_photo(ADMIN_ID, sub.receipt_file_id)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=btn("admin_pending", lang), callback_data="admin_pending")]
        ])
        await bot.send_message(ADMIN_ID, "يمكنك مراجعة الطلب الآن:", reply_markup=kb)
    except Exception as e:
        logging.error("فشل في إرسال الإشعار للمشرف: %s", e)

    await message.answer(get_text("receipt_received", lang))
    await state.set_state(Flow.choosing_subscription)

@router.message(Flow.waiting_receipt)
async def invalid_receipt(message: Message):
    await message.answer("❌ يرجى إرسال صورة فقط.")

@router.callback_query(F.data == "my_account")
async def my_account(cq: CallbackQuery):
    sub = get_subscription(cq.from_user.id)
    lang = (sub or {}).get("language", "ar")
    if not sub or sub["state"] != "active":
        await cq.message.edit_text(get_text("account_inactive", lang), reply_markup=main_keyboard(lang, user_id=cq.from_user.id))
    else:
        end_date = time.strftime('%Y-%m-%d', time.localtime(sub["end_ts"]))
        days_left = max(0, (sub["end_ts"] - int(time.time())) // (24 * 3600))
        text = get_text("account_active", lang, end_date=end_date, days_left=days_left)
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=btn("back", lang), callback_data="go_start")]
        ])
        await cq.message.edit_text(text, reply_markup=kb)
    await cq.answer()

@router.callback_query(F.data == "admin_panel")
async def admin_panel(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("🚫 غير مصرح", show_alert=True)
        return
    lang = (get_subscription(cq.from_user.id) or {}).get("language", "ar")
    await cq.message.edit_text("🔧 *لوحة التحكم*", reply_markup=admin_keyboard(lang))
    await cq.answer()

@router.callback_query(F.data == "admin_stats")
async def admin_stats(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("🚫 غير مصرح", show_alert=True)
        return
    df = list_df()
    lang = (get_subscription(cq.from_user.id) or {}).get("language", "ar")
    
    if df.empty:
        text = "📊 لا توجد بيانات حتى الآن."
    else:
        total = len(df)
        active = len(df[df["state"] == "active"])
        pending = len(df[df["state"] == "pending"])
        ended = len(df[df["state"] == "ended"])
        rejected = len(df[df["state"] == "rejected"])

        ar_count = len(df[df["language"] == "ar"])
        en_count = len(df[df["language"] == "en"])

        months_1 = len(df[df["duration_months"] == 1])
        months_3 = len(df[df["duration_months"] == 3])
        months_6 = len(df[df["duration_months"] == 6])

        top_users = df[df["state"] == "active"].nlargest(5, 'duration_months')
        top_text = ""
        for _, row in top_users.iterrows():
            username = f"@{row['username']}" if row['username'] else f"ID: {row['user_id']}"
            top_text += f"• {username} - {row['duration_months']} شهر\n"

        text = (
            f"<b>📊 الإحصائيات التفصيلية</b>\n\n"
            f"👥 <b>الإجمالي:</b> <code>{total}</code>\n"
            f"✅ <b>نشط:</b> <code>{active}</code> | ⏳ <b>معلق:</b> <code>{pending}</code>\n"
            f"❌ <b>منتهي:</b> <code>{ended}</code> | 🚫 <b>مرفوض:</b> <code>{rejected}</code>\n\n"
            f"🔹 <b>حسب اللغة:</b>\n"
            f"  🇸🇦 عربي: <code>{ar_count}</code> | 🇬🇧 إنجليزي: <code>{en_count}</code>\n\n"
            f"🔹 <b>مدة الاشتراك:</b>\n"
            f"  1 شهر: <code>{months_1}</code> | 3 شهور: <code>{months_3}</code> | 6 شهور: <code>{months_6}</code>\n\n"
            f"🏆 <b>الأعلى دفعًا (5 أعضاء نشطين):</b>\n"
            f"{top_text if top_text else 'لا يوجد'}"
        )

    try:
        if cq.message.text != text:
            await cq.message.edit_text(text, reply_markup=admin_keyboard(lang), parse_mode="HTML")
        else:
            await cq.answer("📊 البيانات محدثة بالفعل.", show_alert=True)
    except Exception as e:
        logging.warning("Error in admin_stats: %s", e)
        try:
            await cq.message.reply(text, reply_markup=admin_keyboard(lang), parse_mode="HTML")
        except Exception as e2:
            await cq.message.reply("❌ تعذر تحميل الإحصائيات.")
            logging.error("فشل إرسال الإحصائيات: %s", e2)
    await cq.answer()

@router.callback_query(F.data == "admin_pending")
async def admin_pending(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("🚫 غير مصرح", show_alert=True)
        return
    df = list_df("SELECT * FROM subscriptions WHERE state = 'pending'")
    lang = (get_subscription(cq.from_user.id) or {}).get("language", "ar")
    
    if df.empty:
        text = "📭 لا توجد طلبات معلقة."
        try:
            await cq.message.edit_text(text, reply_markup=admin_keyboard(lang))
        except Exception as e:
            if "message is not modified" not in str(e):
                logging.warning("Error in admin_pending: %s", e)
        await cq.answer()
        return

    try:
        await cq.message.delete()
    except Exception as e:
        logging.warning("فشل حذف رسالة الطلبات: %s", e)

    for _, row in df.iterrows():
        user_id = row['user_id']
        username = f"@{row['username']}" if row['username'] else f"ID: `{user_id}`"
        duration = row['duration_months']
        method = row['method']
        receipt_id = row['receipt_file_id']

        text = (
            get_text("admin_pending_title", lang) + "\n\n"
            f"👤 المستخدم: {username}\n"
            f"📆 المدة: {duration} شهر\n"
            f"🏦 الطريقة: {method}\n"
        )

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=btn("approve", lang), callback_data=f"approve_{user_id}"),
             InlineKeyboardButton(text=btn("reject", lang), callback_data=f"reject_{user_id}")]
        ])

        try:
            await cq.message.answer(text, reply_markup=kb)
            if receipt_id:
                await cq.message.answer_photo(receipt_id, caption=f"إيصال الدفع - {username}")
        except Exception as e:
            logging.warning("فشل إرسال الإيصال: %s", e)

    await cq.message.answer("🔚 جميع الطلبات المعلقة معروضة.", reply_markup=admin_keyboard(lang))
    await cq.answer()

async def show_user_details(message: Message, user_id: int, bot: Bot):
    sub = get_subscription(user_id)
    if not sub:
        await message.answer("❌ لم يتم العثور على المستخدم.")
        return

    username = f"@{sub['username']}" if sub['username'] else "غير متوفر"
    start_date = time.strftime('%Y-%m-%d', time.localtime(sub["start_ts"])) if sub["start_ts"] else "غير محدد"
    end_date = time.strftime('%Y-%m-%d', time.localtime(sub["end_ts"])) if sub["end_ts"] else "غير محدد"
    days_left = max(0, (sub["end_ts"] - int(time.time())) // (24 * 3600)) if sub["end_ts"] else 0
    lang = sub.get("language", "ar")

    text = (
        "🔍 **معلومات المستخدم**\n\n"
        f"🆔 `{user_id}`\n"
        f"👤 {username}\n"
        f"🌐 {'عربي' if lang == 'ar' else 'إنجليزي'}\n"
        f"💳 {sub['duration_months']} شهر\n"
        f"📅 البدء: `{start_date}`\n"
        f"📆 الانتهاء: `{end_date}`\n"
        f"⏳ المتبقية: `{days_left}` يوم\n"
        f"📌 الحالة: `{sub['state'].upper()}`\n"
        f"🏦 الدفع: `{sub['method']}`\n"
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=btn("approve", lang), callback_data=f"approve_{user_id}"),
         InlineKeyboardButton(text=btn("reject", lang), callback_data=f"reject_{user_id}")],
        [InlineKeyboardButton(text=btn("extend", lang), callback_data=f"extend_menu_{user_id}"),
         InlineKeyboardButton(text=btn("shorten", lang), callback_data=f"shorten_menu_{user_id}")],
        [InlineKeyboardButton(text=btn("delete", lang), callback_data=f"delete_{user_id}")],
        [InlineKeyboardButton(text=btn("back", lang), callback_data="admin_panel")]
    ])

    try:
        await message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
    except Exception as e:
        if "message is not modified" in str(e):
            await message.answer(text, reply_markup=kb, parse_mode="Markdown")
        else:
            logging.warning("Error editing message: %s", e)

@router.callback_query(F.data == "admin_all_users")
async def admin_all_users(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("🚫 غير مصرح", show_alert=True)
        return

    df = list_df()
    if df.empty:
        await cq.message.edit_text("📭 لا يوجد مستخدمين بعد.", reply_markup=admin_keyboard())
        await cq.answer()
        return

    df['days_left'] = df['end_ts'].apply(lambda x: max(0, (x - int(time.time())) // (24 * 3600)) if x else 0)
    df = df.sort_values(by=['state', 'duration_months', 'days_left'], ascending=[False, False, False])
    df = df.head(50)

    text = "👥 **جميع المستخدمين (أعلى 50)**\n\n"
    keyboard = []

    for _, row in df.iterrows():
        user_id = row['user_id']
        username = f"@{row['username']}" if row['username'] else f"ID: {user_id}"
        duration = row['duration_months']
        days_left = row['days_left']
        state = row['state']

        status_emoji = "✅" if state == "active" else "⏳" if state == "pending" else "❌"
        keyboard.append([InlineKeyboardButton(text=f"{status_emoji} {username}", callback_data=f"view_user_{user_id}")])

    keyboard.append([InlineKeyboardButton(text=btn("back", "ar"), callback_data="admin_panel")])
    markup = InlineKeyboardMarkup(inline_keyboard=keyboard)

    try:
        await cq.message.edit_text(text, reply_markup=markup)
    except Exception as e:
        logging.warning("Error in admin_all_users: %s", e)
    await cq.answer()

@router.callback_query(F.data.startswith("view_user_"))
async def view_user_from_list(cq: CallbackQuery, bot: Bot):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("🚫 غير مصرح", show_alert=True)
        return
    user_id = int(cq.data.split("_")[2])
    await show_user_details(cq.message, user_id, bot)
    await cq.answer()

@router.callback_query(F.data.startswith("extend_menu_"))
async def extend_menu(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("🚫 غير مصرح", show_alert=True)
        return
    user_id = int(cq.data.split("_")[2])
    lang = (get_subscription(cq.from_user.id) or {}).get("language", "ar")
    markup = get_duration_keyboard(user_id, "extend", lang)
    await cq.message.edit_text(f"➕ اختر عدد الأيام لتمديد اشتراك المستخدم {user_id}:", reply_markup=markup)
    await cq.answer()

@router.callback_query(F.data.startswith("shorten_menu_"))
async def shorten_menu(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("🚫 غير مصرح", show_alert=True)
        return
    user_id = int(cq.data.split("_")[2])
    lang = (get_subscription(cq.from_user.id) or {}).get("language", "ar")
    markup = get_duration_keyboard(user_id, "shorten", lang)
    await cq.message.edit_text(f"➖ اختر عدد الأيام لتقصير اشتراك المستخدم {user_id}:", reply_markup=markup)
    await cq.answer()

def get_duration_keyboard(user_id: int, action: str, lang: str) -> InlineKeyboardMarkup:
    buttons = []
    for days in [7, 15, 30, 60, 90]:
        text = f"{'➕' if action == 'extend' else '➖'} {days} يوم"
        callback_data = f"{action}_{user_id}_{days}"
        buttons.append([InlineKeyboardButton(text=text, callback_data=callback_data)])
    buttons.append([InlineKeyboardButton(text=btn("back", lang), callback_data=f"view_user_{user_id}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@router.callback_query(F.data.startswith("extend_"))
@router.callback_query(F.data.startswith("shorten_"))
async def modify_duration(cq: CallbackQuery, bot: Bot):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("🚫 غير مصرح", show_alert=True)
        return

    parts = cq.data.split("_")
    action = parts[0]
    user_id = int(parts[1])
    days = int(parts[2])
    seconds = days * 24 * 3600

    sub_dict = get_subscription(user_id)
    if not sub_dict or sub_dict["state"] != "active":
        await cq.answer("❌ يمكن التعديل فقط على الاشتراكات النشطة.", show_alert=True)
        return

    sub = SimpleNamespace(**sub_dict)
    if action == "extend":
        sub.end_ts += seconds
        user_msg = f"🎉 تم تمديد اشتراكك لمدة {days} يوم! استمتع."
    else:
        sub.end_ts = max(sub.start_ts, sub.end_ts - seconds)
        user_msg = f"⚠️ تم تعديل مدة اشتراكك."

    upsert_subscription(sub)
    try:
        await bot.send_message(user_id, user_msg)
    except Exception as e:
        logging.warning("فشل إرسال رسالة التعديل: %s", e)

    await show_user_details(cq.message, user_id, bot)
    await cq.answer()

@router.callback_query(F.data.startswith("approve_"))
async def approve_user_handler(cq: CallbackQuery, bot: Bot):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("🚫 غير مصرح", show_alert=True)
        return
    user_id = int(cq.data.split("_")[1])
    sub_dict = get_subscription(user_id)
    if not sub_dict or sub_dict["state"] != "pending":
        text = f"❌ هذا المستخدم ليس لديه طلب معلق."
    else:
        sub = SimpleNamespace(**sub_dict)
        now = int(time.time())
        months = sub.duration_months or 1
        add_seconds = months * 30 * 24 * 3600
        sub.start_ts = now
        sub.end_ts = now + add_seconds
        sub.state = "active"
        upsert_subscription(sub)
        link = get_channel_link().strip()

        try:
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔐 انضم إلى القناة الخاصة", url=link)]
            ])
            await bot.send_message(user_id, "✅ تم تفعيل اشتراكك! اضغط على الزر أدناه للانضمام:", reply_markup=kb)
        except Exception as e:
            logging.warning("فشل إرسال التفعيل: %s", e)
            await bot.send_message(user_id, f"✅ تم تفعيل اشتراكك! رابط الدخول: {link}")

        text = f"✅ تم تفعيل الاشتراك للمستخدم {user_id}"
    await cq.message.edit_text(text)
    await cq.answer()

@router.callback_query(F.data.startswith("reject_"))
async def reject_user_handler(cq: CallbackQuery, bot: Bot):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("🚫 غير مصرح", show_alert=True)
        return
    user_id = int(cq.data.split("_")[1])
    sub_dict = get_subscription(user_id)
    if sub_dict:
        sub = SimpleNamespace(**sub_dict)
        sub.state = "rejected"
        upsert_subscription(sub)
        try:
            await bot.send_message(user_id, "❌ تم رفض طلب اشتراكك.")
        except Exception as e:
            logging.warning("فشل إرسال الرفض: %s", e)
    await cq.message.edit_text(f"❌ تم رفض الطلب للمستخدم {user_id}")
    await cq.answer()

@router.callback_query(F.data.startswith("delete_"))
async def delete_user_handler(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("🚫 غير مصرح", show_alert=True)
        return
    user_id = int(cq.data.split("_")[1])
    with closing(sqlite3.connect(DB_FILE)) as conn:
        cur = conn.cursor()
        cur.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
        conn.commit()
    await cq.message.edit_text(f"🗑 تم حذف المستخدم {user_id} من قاعدة البيانات.")
    await cq.answer()

@router.callback_query(F.data == "admin_export")
async def admin_export(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("🚫 غير مصرح", show_alert=True)
        return
    df = list_df()
    file_path = "subscriptions.csv"
    df.to_csv(file_path, index=False)
    await cq.message.answer_document(FSInputFile(file_path), caption="📄 بيانات المستخدمين")
    await cq.answer()

@router.callback_query(F.data == "admin_broadcast")
async def admin_broadcast_prompt(cq: CallbackQuery, state: FSMContext):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("🚫 غير مصرح", show_alert=True)
        return
    await cq.message.edit_text("✉️ أرسل الرسالة للإرسال الجماعي:")
    await state.set_state(Flow.broadcast_waiting)
    await cq.answer()

@router.callback_query(F.data == "send_to_user")
async def send_to_user_prompt(cq: CallbackQuery, state: FSMContext):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("🚫 غير مصرح", show_alert=True)
        return
    await cq.message.edit_text("🆔 أرسل معرف المستخدم (User ID) الذي تريد إرسال رسالة إليه:")
    await state.set_state(Flow.broadcast_waiting)
    await cq.answer()

@router.message(Flow.broadcast_waiting)
async def send_to_user_send(message: Message, state: FSMContext, bot: Bot):
    if message.from_user.id != ADMIN_ID:
        return

    if message.text.isdigit():
        target_id = int(message.text)
        await message.answer(f"📩 الآن، أرسل الرسالة التي تريد إرسالها للمستخدم `{target_id}`:")
        await state.update_data(target_user_id=target_id)
        return

    data = await state.get_data()
    target_id = data.get("target_user_id")
    if not target_id:
        await message.answer("❌ لم يتم تحديد المستخدم.")
        await state.set_state(Flow.choosing_subscription)
        return

    try:
        await bot.copy_message(target_id, message.from_user.id, message.message_id)
        await message.answer(f"✅ تم إرسال الرسالة إلى المستخدم {target_id}.")
    except Exception as e:
        await message.answer(f"❌ فشل في إرسال الرسالة: {str(e)}")

    await state.set_state(Flow.choosing_subscription)

@router.callback_query(F.data == "admin_links")
async def admin_manage_links(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("🚫 غير مصرح", show_alert=True)
        return
    lang = (get_subscription(cq.from_user.id) or {}).get("language", "ar")
    await cq.message.edit_text("🔗 إدارة الروابط:", reply_markup=links_keyboard(lang))
    await cq.answer()

@router.callback_query(F.data == "add_links")
async def admin_add_links_prompt(cq: CallbackQuery, state: FSMContext):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("🚫 غير مصرح", show_alert=True)
        return
    await cq.message.edit_text("📌 أرسل الروابط (رابط في كل سطر):")
    await state.set_state(Flow.add_links_waiting)
    await cq.answer()

@router.message(Flow.add_links_waiting)
async def admin_add_links_save(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    new_links = [{"link": line.strip(), "used": False} for line in message.text.strip().splitlines() if line.strip()]
    links = load_links()
    links.extend(new_links)
    save_links(links)
    await message.answer("✅ تم إضافة الروابط.")
    await state.set_state(Flow.choosing_subscription)

@router.callback_query(F.data == "clear_links")
async def admin_clear_links(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("🚫 غير مصرح", show_alert=True)
        return
    save_links([])
    await cq.message.edit_text("🗑 تم حذف جميع الروابط.", reply_markup=admin_keyboard())
    await cq.answer()

@router.callback_query(F.data == "admin_wallets")
async def admin_manage_wallets(cq: CallbackQuery):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("🚫 غير مصرح", show_alert=True)
        return
    lang = (get_subscription(cq.from_user.id) or {}).get("language", "ar")
    await cq.message.edit_text("💳 المحافظ الحالية:", reply_markup=wallets_keyboard(lang))
    await cq.answer()

@router.callback_query(F.data == "edit_wallets")
async def admin_edit_wallets_prompt(cq: CallbackQuery, state: FSMContext):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("🚫 غير مصرح", show_alert=True)
        return
    wallets = load_wallets()
    kb = []
    for method in wallets.keys():
        kb.append([InlineKeyboardButton(text=f"✏️ {method}", callback_data=f"edit_wallet_{method}")])
    kb.append([InlineKeyboardButton(text="➕ إضافة طريقة جديدة", callback_data="add_new_wallet_method")])
    kb.append([InlineKeyboardButton(text="🔙 رجوع", callback_data="admin_wallets")])
    await cq.message.edit_text("💳 اختر طريقة الدفع التي تريد تعديلها:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))
    await cq.answer()

@router.callback_query(F.data.startswith("edit_wallet_"))
async def edit_wallet_address_prompt(cq: CallbackQuery, state: FSMContext):
    method = cq.data.split("edit_wallet_")[1]
    await state.update_data(editing_wallet_method=method)
    await cq.message.edit_text(f"📌 أرسل العنوان الجديد لطريقة الدفع:\n\n<b>{method}</b>")
    await state.set_state(Flow.edit_wallet_waiting)
    await cq.answer()

@router.message(Flow.edit_wallet_waiting)
async def save_updated_wallet(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    data = await state.get_data()
    method = data["editing_wallet_method"]
    address = message.text.strip()

    wallets = load_wallets()
    wallets[method] = address
    save_wallets(wallets)

    await message.answer(f"✅ تم تحديث طريقة الدفع:\n\n<b>{method}</b>\n{address}")
    await state.set_state(Flow.choosing_subscription)

@router.callback_query(F.data == "add_new_wallet_method")
async def add_new_wallet_method_prompt(cq: CallbackQuery, state: FSMContext):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("🚫 غير مصرح", show_alert=True)
        return
    await cq.message.edit_text("📌 أرسل اسم طريقة الدفع الجديدة (مثلاً: باي بال):")
    await state.set_state(Flow.add_new_wallet_method_name)
    await cq.answer()

@router.message(Flow.add_new_wallet_method_name)
async def add_new_wallet_method_name_received(message: Message, state: FSMContext):
    method_name = message.text.strip()
    if not method_name:
        await message.answer("❌ الاسم لا يمكن أن يكون فارغًا. أعد المحاولة:")
        return
    await state.update_data(new_wallet_method_name=method_name)
    await message.answer(f"📌 أرسل عنوان الدفع لطريقة '{method_name}':")
    await state.set_state(Flow.add_new_wallet_method_address)

@router.message(Flow.add_new_wallet_method_address)
async def add_new_wallet_method_address_received(message: Message, state: FSMContext):
    if message.from_user.id != ADMIN_ID:
        return
    address = message.text.strip()
    data = await state.get_data()
    method_name = data["new_wallet_method_name"]

    wallets = load_wallets()
    wallets[method_name] = address
    save_wallets(wallets)

    await message.answer(f"✅ تم إضافة طريقة دفع جديدة:\n\n**{method_name}**: `{address}`")
    await state.set_state(Flow.choosing_subscription)

@router.callback_query(F.data == "admin_search")
async def admin_search_prompt(cq: CallbackQuery, state: FSMContext):
    if cq.from_user.id != ADMIN_ID:
        await cq.answer("🚫 غير مصرح", show_alert=True)
        return
    text = (
        "🔍 **ابحث عن مستخدم**\n\n"
        "أرسل:\n"
        "• *معرف المستخدم (ID)*\n"
        "• أو *اسم المستخدم (Username)* مثل @username"
    )
    await cq.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 رجوع", callback_data="admin_panel")]
    ]), parse_mode="Markdown")
    await state.set_state(Flow.admin_search_waiting)
    await cq.answer()

@router.message(Flow.admin_search_waiting)
async def admin_search_handle(message: Message, state: FSMContext, bot: Bot):
    if message.from_user.id != ADMIN_ID:
        return

    query = message.text.strip()
    sub = None

    if query.isdigit():
        sub = get_subscription(int(query))
    elif query.startswith('@'):
        df = list_df("SELECT * FROM subscriptions WHERE username = ?", (query[1:],))
        if not df.empty:
            sub = df.iloc[0].to_dict()
    else:
        df = list_df("SELECT * FROM subscriptions WHERE username = ?", (query,))
        if not df.empty:
            sub = df.iloc[0].to_dict()

    if not sub:
        await message.answer("❌ لم يتم العثور على مستخدم بهذا المعرف أو اسم المستخدم.")
        await state.set_state(Flow.choosing_subscription)
        return

    await show_user_details(message, sub["user_id"], bot)
    await state.set_state(Flow.choosing_subscription)




# ---------------------- الترحيب عند الدخول للقناة ----------------------
@router.chat_member(ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER))
async def welcome_new_member(event: ChatMemberUpdated, bot: Bot):
    if PRIVATE_CHANNEL_ID and str(event.chat.id) == PRIVATE_CHANNEL_ID:
        try:
            lang = "ar"
            await bot.send_message(
                event.new_chat_member.user.id,
                get_text("welcome_to_channel", lang)
            )
        except Exception as e:
            logging.warning("فشل إرسال رسالة الترحيب: %s", e)

# ---------------------- مهمة التذكير والطرد التلقائي ----------------------
async def reminder_task(bot: Bot):
    while True:
        try:
            df = list_df()
            now = int(time.time())
            for _, row in df.iterrows():
                user_id = int(row["user_id"])
                if row["state"] != "active" or not row["end_ts"]:
                    continue

                time_left = row["end_ts"] - now
                days_left = time_left // (24 * 3600)

                if days_left == 3:
                    try:
                        lang = row["language"] or "ar"
                        await bot.send_message(user_id, get_text("reminder_3_days", lang))
                    except Exception as e:
                        logging.warning("فشل إرسال التحذير قبل 3 أيام لـ %s: %s", user_id, e)

                if days_left == 1:
                    try:
                        lang = row["language"] or "ar"
                        await bot.send_message(user_id, get_text("reminder_1_day", lang))
                    except Exception as e:
                        logging.warning("فشل إرسال التحذير قبل يوم لـ %s: %s", user_id, e)

                if time_left <= -24 * 3600:
                    sub = SimpleNamespace(**row)
                    if sub.state == "active":
                        sub.state = "ended"
                        upsert_subscription(sub)

                        if PRIVATE_CHANNEL_ID:
                            try:
                                await bot.ban_chat_member(int(PRIVATE_CHANNEL_ID), user_id)
                                await asyncio.sleep(1)
                                await bot.unban_chat_member(int(PRIVATE_CHANNEL_ID), user_id)
                            except Exception as e:
                                logging.warning("فشل طرد المستخدم %s من القناة: %s", user_id, e)

                        try:
                            lang = row["language"] or "ar"
                            await bot.send_message(user_id, get_text("sub_expired", lang))
                        except Exception as e:
                            logging.warning("فشل إرسال رسالة الانتهاء لـ %s: %s", user_id, e)
        except Exception as e:
            logging.exception("Reminder task error: %s", e)
        await asyncio.sleep(3600)

# ---------------------- بدء البوت ----------------------
async def main():
    init_db()
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    asyncio.create_task(reminder_task(bot))
    logging.info("Bot is starting...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logging.info("Bot stopped.")