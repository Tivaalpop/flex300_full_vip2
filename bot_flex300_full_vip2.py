import asyncio
import json
import logging
import os
import random
import sqlite3
import string
import base64
import hashlib
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from collections import defaultdict

import aiohttp
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, BotCommand
)
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, filters, ContextTypes, ConversationHandler
)

# ================== إعداد التوقيت المحلي (مصر) ==================
CAIRO_TZ = ZoneInfo("Africa/Cairo")

def now_cairo() -> datetime:
    return datetime.now(CAIRO_TZ).replace(tzinfo=None)

# ================== محرك البروكسي والشبكة الخارق ==================
PROXY_URL = (
    os.getenv("VODAFONE_PROXY")
    or os.getenv("ALL_PROXY")
    or os.getenv("HTTPS_PROXY")
    or os.getenv("HTTP_PROXY")
    or ""
).strip()

PROXIES_DICT = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

def create_turbo_session() -> requests.Session:
    session = requests.Session()
    retry_strategy = Retry(
        total=2,
        backoff_factor=0.4,
        status_forcelist=[500, 502, 503, 504],
        allowed_methods=["HEAD", "GET", "OPTIONS"]
    )
    adapter = HTTPAdapter(pool_connections=150, pool_maxsize=150, max_retries=retry_strategy)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.trust_env = True
    if PROXIES_DICT:
        session.proxies.update(PROXIES_DICT)
    return session

HTTP_SESSION = create_turbo_session()
WEB_HTTP_SESSION = create_turbo_session()

# جلسة aiohttp مشتركة لمنع استهلاك الرامات وتفادي حظر SSL Handshake في تعليق الدعوتين
GLOBAL_AIOHTTP_SESSION: Optional[aiohttp.ClientSession] = None

async def get_aiohttp_session() -> aiohttp.ClientSession:
    global GLOBAL_AIOHTTP_SESSION
    if GLOBAL_AIOHTTP_SESSION is None or GLOBAL_AIOHTTP_SESSION.closed:
        connector = aiohttp.TCPConnector(limit=80, limit_per_host=30, ssl=False, keepalive_timeout=30)
        timeout = aiohttp.ClientTimeout(total=18, connect=8)
        GLOBAL_AIOHTTP_SESSION = aiohttp.ClientSession(connector=connector, timeout=timeout)
    return GLOBAL_AIOHTTP_SESSION

SYSTEM_CACHE: Dict[str, Tuple[float, str]] = {}
SYSTEM_CACHE_TTL = 30
USER_OPERATION_LOCKS: Dict[int, asyncio.Lock] = {}

def get_user_operation_lock(user_id: int) -> asyncio.Lock:
    lock = USER_OPERATION_LOCKS.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        USER_OPERATION_LOCKS[user_id] = lock
    return lock

# ================== الإعدادات العامة ==================
# جلب التوكن من الاستضافة، وإذا لم يجده (مثل حالة الهاتف) يستخدم التوكن المكتوب بالأسفل
BOT_TOKEN = os.getenv("BOT_TOKEN", "ضع_التوكن_الخاص_بك_هنا")

# جلب الآي دي وتحويله لرقم صحيح (Integer) لأن التليجرام يتطلبه كرقم وليس نص
ADMIN_ID = int(os.getenv("ADMIN_ID", "123456789"))

# حفظ القاعدة داخل الـ Volume الدائم على Railway، أو محلياً إذا لم يتوفر المجلد
DATA_DIR = Path("/data") if Path("/data").is_dir() else Path(".")
DB_PATH = DATA_DIR / "family_bot.db"
LOG_FILE = Path("bot_actions.log")
MAINTENANCE_MODE = False
TOKEN_REFRESH_THRESHOLD = 300

SYSTEM_TRANSLATIONS = {
    '14pt_Raya7Balak': 'ريح بالك كله 14 قرش',
    'Flex_40': 'فليكس 40', 'Flex_60': 'فليكس 60', 'Flex_90': 'فليكس 90',
    'Flex_130': 'فليكس 130', 'Flex_260': 'فليكس 260',
    'Flex_45': 'فليكس 45', 'Flex_70': 'فليكس 70', 'Flex_100': 'فليكس 100',
    'Flex_150': 'فليكس 150', 'Flex_300': 'فليكس 300',
    'Flex_Family_Member': 'فليكس فاميلي', 'Flex_Family': 'فليكس فاميلي', 'Family_Flex': 'فليكس فاميلي',
}

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()]
)
logger = logging.getLogger("family_bot")

def log_action(user_id: int, action: str, details: str = "") -> None:
    logger.info(f"User {user_id}: {action} - {details}")

def admin_log(action: str, details: str = "") -> None:
    logger.info(f"ADMIN: {action} - {details}")

# ================== محرك البصمة الثابتة لمنع الحظر (Sticky Anti-Ban Fingerprint) ==================
DEVICE_PROFILES = [
    ("Samsung SM-A515F", "13", "2026.2.3", "1117"),
    ("Samsung SM-S918B", "14", "2026.4.1", "1139"),
    ("OPPO CPH2473", "13", "2026.3.1", "1125"),
    ("Xiaomi M2101K7AG", "12", "2026.1.2", "1105"),
    ("Realme RMX3630", "13", "2026.4.1", "1139"),
    ("Infinix X6833B", "13", "2026.2.3", "1117"),
    ("Vivo V2202", "14", "2026.3.1", "1125"),
    ("Samsung SM-A165F", "15", "2025.12.2", "1080"),
]

STICKY_FINGERPRINT_CACHE: Dict[str, Dict[str, str]] = {}

def get_sticky_fingerprint(msisdn: Optional[str] = None) -> Dict[str, str]:
    """يولد بصمة جهاز ثابتة لكل رقم هاتف لمنع فودافون من كشف تغير الجهاز في كل طلب"""
    if not msisdn:
        model, os_v, app_v, build_n = random.choice(DEVICE_PROFILES)
        return {
            "device_model": model, "os_version": os_v, "app_ver": app_v, "build_num": build_n,
            "device_id": ''.join(random.choices('0123456789abcdef', k=16)),
            "digital_id": ''.join(random.choices(string.ascii_uppercase + string.digits, k=13))
        }
    if msisdn in STICKY_FINGERPRINT_CACHE:
        return STICKY_FINGERPRINT_CACHE[msisdn]

    seed_hash = hashlib.sha256(f"VF_STICKY_{msisdn}_SALT2026".encode()).hexdigest()
    profile_idx = int(seed_hash[:8], 16) % len(DEVICE_PROFILES)
    model, os_v, app_v, build_n = DEVICE_PROFILES[profile_idx]
    device_id = seed_hash[8:24]

    chars = string.ascii_uppercase + string.digits
    digital_id = ''.join(chars[int(seed_hash[24 + i*2 : 26 + i*2], 16) % len(chars)] for i in range(13))

    fp = {
        "device_model": model, "os_version": os_v, "app_ver": app_v, "build_num": build_n,
        "device_id": device_id, "digital_id": digital_id
    }
    STICKY_FINGERPRINT_CACHE[msisdn] = fp
    return fp

def convert_to_local(phone: str) -> str:
    return "0" + phone[2:] if phone.startswith("20") and len(phone) == 12 else phone

def translate_system(system_name: Optional[str]) -> str:
    if not system_name:
        return "غير محدد"
    for key, value in SYSTEM_TRANSLATIONS.items():
        if key in system_name:
            return value
    return system_name

def validate_phone(number: str) -> Tuple[bool, str]:
    if not number.isdigit():
        return False, "❌ الرقم يجب أن يحتوي على أرقام فقط"
    if len(number) != 11:
        return False, "❌ الرقم يجب أن يكون 11 رقماً"
    if not number.startswith('01'):
        return False, "❌ الرقم يجب أن يبدأ بـ 01"
    return True, "✅"

def extract_name_from_jwt(token: str, fallback_name: str = "صديقي") -> str:
    """استخراج اسم صاحب الحساب من توكن فودافون (JWT) أو استخدام الاسم البديل"""
    try:
        clean_tok = token.replace("Bearer ", "").strip()
        parts = clean_tok.split(".")
        if len(parts) >= 2:
            payload_b64 = parts[1]
            payload_b64 += "=" * ((4 - len(payload_b64) % 4) % 4)
            payload_json = base64.urlsafe_b64decode(payload_b64).decode("utf-8", errors="ignore")
            data = json.loads(payload_json)
            for key in ("given_name", "firstName", "name", "fullName", "customerName"):
                val = data.get(key)
                if val and isinstance(val, str) and not val.isdigit() and len(val.strip()) > 1:
                    first_word = val.strip().split()[0]
                    if not first_word.startswith("01") and not first_word.startswith("201"):
                        return first_word
    except Exception:
        pass
    return fallback_name.strip() if fallback_name and fallback_name.strip() else "بطل"

# ================== قاعدة البيانات عالية التحمل (WAL Mode) ==================
def get_db_connection():
    # تحديد المجلد الذي يوجد فيه ملف البايثون الحالي
    base_dir = os.path.dirname(os.path.abspath(__file__))
    
    # دمج المسار مع اسم قاعدة البيانات
    db_path = os.path.join(base_dir, "database.db")
    
    # الاتصال بقاعدة البيانات
    conn = sqlite3.connect(db_path)
    return conn


def encrypt_password(password: str) -> str:
    return base64.b64encode(password.encode()).decode()

def decrypt_password(encrypted: str) -> str:
    return base64.b64decode(encrypted.encode()).decode()

def save_password(user_id: int, number: str, password: str, expiry_hours: int = 72):
    with get_db_connection() as conn:
        c = conn.cursor()
        expiry = now_cairo() + timedelta(hours=expiry_hours)
        encrypted = encrypt_password(password)
        c.execute(
            "INSERT OR REPLACE INTO saved_passwords (telegram_id, number, password_encrypted, expiry) VALUES (?, ?, ?, ?)",
            (user_id, number, encrypted, expiry.isoformat())
        )
        conn.commit()

def get_saved_password(user_id: int, number: str) -> Optional[str]:
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute("SELECT password_encrypted, expiry FROM saved_passwords WHERE telegram_id = ? AND number = ?", (user_id, number))
        row = c.fetchone()
    if row:
        encrypted, expiry_str = row
        try:
            if datetime.fromisoformat(expiry_str) > now_cairo():
                return decrypt_password(encrypted)
        except Exception:
            delete_saved_password(user_id, number)
            return None
    return None

def delete_saved_password(user_id: int, number: str):
    with get_db_connection() as conn:
        conn.execute("DELETE FROM saved_passwords WHERE telegram_id = ? AND number = ?", (user_id, number))
        conn.commit()

def init_db() -> None:
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS users (telegram_id INTEGER PRIMARY KEY, phone TEXT, access_token TEXT, token_expiry TEXT, account_name TEXT DEFAULT '')''')
        # ترقية تلقائية لجدول users إذا كان قديماً ولا يحتوي على عمود account_name
        try:
            c.execute("ALTER TABLE users ADD COLUMN account_name TEXT DEFAULT ''")
        except Exception:
            pass
        c.execute('''CREATE TABLE IF NOT EXISTS saved_numbers (telegram_id INTEGER, number TEXT, account_name TEXT DEFAULT '', PRIMARY KEY (telegram_id, number))''')
        try:
            c.execute("ALTER TABLE saved_numbers ADD COLUMN account_name TEXT DEFAULT ''")
        except Exception:
            pass
        c.execute('''CREATE TABLE IF NOT EXISTS banned_users (telegram_id INTEGER PRIMARY KEY)''')
        c.execute('''CREATE TABLE IF NOT EXISTS user_activity (telegram_id INTEGER PRIMARY KEY, first_seen TIMESTAMP, last_seen TIMESTAMP)''')
        c.execute('''CREATE TABLE IF NOT EXISTS saved_tokens (user_id INTEGER, number TEXT, token TEXT, expiry TIMESTAMP, PRIMARY KEY (user_id, number))''')
        c.execute('''CREATE TABLE IF NOT EXISTS saved_passwords (telegram_id INTEGER, number TEXT, password_encrypted TEXT, expiry TIMESTAMP, PRIMARY KEY (telegram_id, number))''')
        c.execute('''CREATE TABLE IF NOT EXISTS admins (telegram_id INTEGER PRIMARY KEY)''')
        c.execute('''CREATE TABLE IF NOT EXISTS subscriptions (telegram_id INTEGER PRIMARY KEY, expiry TIMESTAMP)''')
        c.execute('''CREATE TABLE IF NOT EXISTS bot_config (key TEXT PRIMARY KEY, value TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS scheduled_discounts (
            telegram_id INTEGER,
            phone TEXT,
            offer_json TEXT,
            scheduled_time TEXT,
            status TEXT DEFAULT 'pending',
            created_at TEXT,
            PRIMARY KEY (telegram_id, phone)
        )''')
        c.execute("INSERT OR IGNORE INTO admins (telegram_id) VALUES (?)", (ADMIN_ID,))
        c.execute("INSERT OR IGNORE INTO bot_config (key, value) VALUES ('free_mode', '1')")
        conn.commit()

init_db()

# ================== دوال الأدمن والاشتراكات ==================
def is_admin(user_id: int) -> bool:
    with get_db_connection() as conn:
        row = conn.execute("SELECT 1 FROM admins WHERE telegram_id = ?", (user_id,)).fetchone()
    return row is not None

def add_admin(user_id: int) -> bool:
    if is_admin(user_id):
        return False
    with get_db_connection() as conn:
        conn.execute("INSERT INTO admins (telegram_id) VALUES (?)", (user_id,))
        conn.commit()
    return True

def remove_admin(user_id: int) -> bool:
    if user_id == ADMIN_ID:
        return False
    with get_db_connection() as conn:
        cur = conn.execute("DELETE FROM admins WHERE telegram_id = ?", (user_id,))
        conn.commit()
        return cur.rowcount > 0

def list_admins() -> List[int]:
    with get_db_connection() as conn:
        rows = conn.execute("SELECT telegram_id FROM admins").fetchall()
    return [row[0] for row in rows]

def is_free_mode() -> bool:
    with get_db_connection() as conn:
        row = conn.execute("SELECT value FROM bot_config WHERE key = 'free_mode'").fetchone()
    return row is not None and row[0] == '1'

def set_free_mode(enabled: bool):
    with get_db_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO bot_config (key, value) VALUES ('free_mode', ?)", ('1' if enabled else '0',))
        conn.commit()

def get_subscription_expiry(user_id: int) -> Optional[datetime]:
    with get_db_connection() as conn:
        row = conn.execute("SELECT expiry FROM subscriptions WHERE telegram_id = ?", (user_id,)).fetchone()
    return datetime.fromisoformat(row[0]) if row else None

def is_subscription_active(user_id: int) -> bool:
    expiry = get_subscription_expiry(user_id)
    return expiry is not None and expiry > now_cairo()

def add_subscription(user_id: int, days: int) -> datetime:
    new_expiry = now_cairo() + timedelta(days=days)
    with get_db_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO subscriptions (telegram_id, expiry) VALUES (?, ?)", (user_id, new_expiry.isoformat()))
        conn.commit()
    return new_expiry

def remove_subscription(user_id: int) -> bool:
    with get_db_connection() as conn:
        cur = conn.execute("DELETE FROM subscriptions WHERE telegram_id = ?", (user_id,))
        conn.commit()
        return cur.rowcount > 0

def list_active_subscriptions() -> List[Tuple[int, datetime]]:
    with get_db_connection() as conn:
        rows = conn.execute("SELECT telegram_id, expiry FROM subscriptions WHERE expiry > ?", (now_cairo().isoformat(),)).fetchall()
    return [(row[0], datetime.fromisoformat(row[1])) for row in rows]

async def require_subscription_or_free(update: Update, context: ContextTypes.DEFAULT_TYPE, is_callback: bool = False, query=None) -> bool:
    user_id = query.from_user.id if is_callback and query else update.effective_user.id
    if is_admin(user_id) or is_free_mode() or is_subscription_active(user_id):
        return True
    text = "🔒 **هذا البوت يعمل بنظام الاشتراك.**\n\nلم يعد لديك اشتراك نشط.\nيرجى التواصل مع الأدمن للحصول على اشتراك.\n📞 تواصل مع المالك: @Q9_TV"
    try:
        if is_callback and query:
            await query.edit_message_text(text, parse_mode='Markdown')
        else:
            await update.message.reply_text(text, parse_mode='Markdown')
    except Exception:
        pass
    return False

# ================== دوال المستخدم الأساسية ==================
def is_user_banned(telegram_id: int) -> bool:
    with get_db_connection() as conn:
        row = conn.execute("SELECT 1 FROM banned_users WHERE telegram_id = ?", (telegram_id,)).fetchone()
    return row is not None

def ban_user(telegram_id: int) -> None:
    with get_db_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO banned_users (telegram_id) VALUES (?)", (telegram_id,))
        conn.commit()

def unban_user(telegram_id: int) -> None:
    with get_db_connection() as conn:
        conn.execute("DELETE FROM banned_users WHERE telegram_id = ?", (telegram_id,))
        conn.commit()

def record_user_activity(telegram_id: int) -> None:
    now = now_cairo()
    with get_db_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO user_activity (telegram_id, first_seen, last_seen) VALUES (?, ?, ?)", (telegram_id, now, now))
        conn.execute("UPDATE user_activity SET last_seen = ? WHERE telegram_id = ?", (now, telegram_id))
        conn.commit()

def get_all_users() -> List[Tuple[int, datetime, datetime]]:
    with get_db_connection() as conn:
        rows = conn.execute("SELECT telegram_id, first_seen, last_seen FROM user_activity ORDER BY last_seen DESC").fetchall()
    return [(uid, datetime.fromisoformat(first) if isinstance(first, str) else first, datetime.fromisoformat(last) if isinstance(last, str) else last) for uid, first, last in rows]

def get_stats() -> Dict[str, int]:
    with get_db_connection() as conn:
        total_users = conn.execute("SELECT COUNT(*) FROM user_activity").fetchone()[0]
        active_sessions = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        saved_numbers = conn.execute("SELECT COUNT(*) FROM saved_numbers").fetchone()[0]
        banned_count = conn.execute("SELECT COUNT(*) FROM banned_users").fetchone()[0]
        active_subs = conn.execute("SELECT COUNT(*) FROM subscriptions WHERE expiry > ?", (now_cairo().isoformat(),)).fetchone()[0]
        scheduled_cnt = conn.execute("SELECT COUNT(*) FROM scheduled_discounts WHERE status = 'pending'").fetchone()[0]
    return {
        'total_users': total_users, 'active_sessions': active_sessions,
        'saved_numbers': saved_numbers, 'banned_count': banned_count,
        'active_subs': active_subs, 'scheduled_cnt': scheduled_cnt
    }

def save_number(telegram_id: int, number: str, account_name: str = "") -> None:
    with get_db_connection() as conn:
        if account_name:
            conn.execute("INSERT OR REPLACE INTO saved_numbers (telegram_id, number, account_name) VALUES (?, ?, ?)", (telegram_id, number, account_name))
        else:
            conn.execute("INSERT OR IGNORE INTO saved_numbers (telegram_id, number, account_name) VALUES (?, ?, '')", (telegram_id, number))
        conn.commit()

def get_saved_numbers(telegram_id: int) -> List[str]:
    with get_db_connection() as conn:
        rows = conn.execute("SELECT number FROM saved_numbers WHERE telegram_id = ?", (telegram_id,)).fetchall()
    return [r[0] for r in rows]

def get_saved_account_name(telegram_id: int, number: str) -> str:
    with get_db_connection() as conn:
        row = conn.execute("SELECT account_name FROM saved_numbers WHERE telegram_id = ? AND number = ?", (telegram_id, number)).fetchone()
    return row[0] if row and row[0] else ""

def delete_saved_number(telegram_id: int, number: str) -> None:
    with get_db_connection() as conn:
        conn.execute("DELETE FROM saved_numbers WHERE telegram_id = ? AND number = ?", (telegram_id, number))
        conn.commit()
    delete_saved_password(telegram_id, number)

def save_token(user_id: int, number: str, token: str, expiry: datetime) -> None:
    with get_db_connection() as conn:
        conn.execute("INSERT OR REPLACE INTO saved_tokens (user_id, number, token, expiry) VALUES (?, ?, ?, ?)", (user_id, number, token, expiry.isoformat()))
        conn.commit()

def get_valid_token(user_id: int, number: str) -> Optional[str]:
    with get_db_connection() as conn:
        row = conn.execute("SELECT token, expiry FROM saved_tokens WHERE user_id = ? AND number = ?", (user_id, number)).fetchone()
    if row:
        token, expiry_str = row
        try:
            if datetime.fromisoformat(expiry_str) > now_cairo():
                return token
        except Exception:
            pass
    return None

def save_user(telegram_id: int, phone: str, token: str, expiry: datetime, account_name: str = "") -> None:
    if not account_name:
        account_name = get_saved_account_name(telegram_id, phone) or "بطل"
    with get_db_connection() as conn:
        conn.execute(
            'INSERT OR REPLACE INTO users (telegram_id, phone, access_token, token_expiry, account_name) VALUES (?, ?, ?, ?, ?)',
            (telegram_id, phone, token, expiry.isoformat(), account_name)
        )
        conn.commit()
    save_token(telegram_id, phone, token, expiry)
    save_number(telegram_id, phone, account_name)

def get_user(telegram_id: int) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        row = conn.execute('SELECT phone, access_token, token_expiry, account_name FROM users WHERE telegram_id = ?', (telegram_id,)).fetchone()
    if row:
        phone, token, expiry_str, acc_name = row
        return {
            'phone': phone,
            'access_token': token,
            'token_expiry': datetime.fromisoformat(expiry_str),
            'account_name': acc_name or "بطل"
        }
    return None

def delete_user(telegram_id: int) -> None:
    with get_db_connection() as conn:
        conn.execute('DELETE FROM users WHERE telegram_id = ?', (telegram_id,))
        conn.commit()

# ================== API فودافون الأساسي (مع البصمة الثابتة) ==================
def build_base_headers(additional: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    msisdn = additional.get('msisdn') if additional else None
    fp = get_sticky_fingerprint(msisdn)
    headers = {
        'User-Agent': 'okhttp/4.12.0',
        'Connection': 'Keep-Alive',
        'Accept': 'application/json',
        'device-id': fp['device_id'],
        'x-agent-operatingsystem': fp['os_version'],
        'clientId': 'AnaVodafoneAndroid',
        'x-agent-device': fp['device_model'],
        'x-agent-version': fp['app_ver'],
        'x-agent-build': fp['build_num'],
        'Accept-Language': 'ar',
        'Content-Type': 'application/json; charset=UTF-8',
        'digitalId': fp['digital_id']
    }
    if additional:
        headers.update(additional)
    return headers

async def fetch_vodafone_customer_name(token: str, phone: str, fallback_name: str = "صديقي") -> str:
    """يجلب اسم صاحب الخط من توكن فودافون أو بروفايل العميل، أو يعود لاسم تليجرام"""
    jwt_name = extract_name_from_jwt(token, "")
    if jwt_name:
        return jwt_name
    headers = build_base_headers({'Authorization': f'Bearer {token}', 'msisdn': phone})
    def sync_fetch_profile():
        try:
            resp = HTTP_SESSION.get(
                f'https://mobile.vodafone.com.eg/services/dxl/customer/customer/{phone}',
                headers=headers, timeout=6
            )
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict):
                    name = data.get('name') or data.get('givenName') or data.get('fullName')
                    if name and isinstance(name, str) and not name.isdigit():
                        return name.strip().split()[0]
        except Exception:
            pass
        return ""
    profile_name = await asyncio.to_thread(sync_fetch_profile)
    if profile_name:
        return profile_name
    return fallback_name.strip() if fallback_name and fallback_name.strip() else "بطل"

async def login_vodafone(phone: str, password: str) -> Tuple[str, datetime]:
    headers = build_base_headers({'Content-Type': 'application/x-www-form-urlencoded', 'silentLogin': 'true', 'msisdn': phone})
    data = {'grant_type': 'password', 'username': phone, 'password': password, 'client_secret': 'dca0pbLUWXVhXR266Gw1iT5rqwvvJQoN', 'client_id': 'AnaVF'}
    def sync_login():
        resp = HTTP_SESSION.post('https://mobile.vodafone.com.eg/auth/realms/vf-realm/protocol/openid-connect/token', headers=headers, data=data, timeout=30)
        if resp.status_code == 200:
            return resp.json().get('access_token'), resp.json().get('expires_in', 3600)
        raise Exception(f"فشل تسجيل الدخول: {resp.status_code}")
    token, exp_in = await asyncio.to_thread(sync_login)
    expiry = now_cairo() + timedelta(seconds=exp_in)
    return token, expiry

async def get_family_members(access_token: str, msisdn: str) -> Dict[str, List[Dict[str, Any]]]:
    headers = build_base_headers({'Authorization': f'Bearer {access_token}', 'api-version': 'v2', 'msisdn': msisdn})
    def sync_get():
        return HTTP_SESSION.get('https://mobile.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup', params={'type': 'Family'}, headers=headers, timeout=30)
    resp = await asyncio.to_thread(sync_get)
    if resp.status_code == 404:
        return {'active': [], 'pending': []}
    if resp.status_code != 200:
        raise Exception(f"فشل جلب العائلة: {resp.status_code}")
    result = resp.json()
    if not result:
        return {'active': [], 'pending': []}
    members = result[0].get('parts', {}).get('member', [])
    active, pending_raw = [], []
    for m in members:
        if m.get('type') == 'Owner':
            continue
        phone = next((id_obj.get('value') for id_obj in m.get('id', []) if id_obj.get('schemeName') == 'MSISDN'), None)
        if not phone:
            continue
        phone_local = convert_to_local(phone)
        status_code = str(m.get('status'))
        flex_value = next((ch.get('value') for ch in m.get('characteristic', {}).get('characteristicsValue', []) if ch.get('characteristicName') == 'flex'), None)
        if status_code == "1":
            active.append({'phone': phone_local, 'flex': flex_value})
        elif status_code == "5":
            pending_raw.append({'phone': phone_local, 'flex': flex_value})
    pending_dict = defaultdict(list)
    for p in pending_raw:
        pending_dict[p['phone']].append(p['flex'])
    return {'active': active, 'pending': [{'phone': phone, 'flex': flexes[0], 'count': len(flexes)} for phone, flexes in pending_dict.items()]}

async def send_invitation(token: str, owner_msisdn: str, member_phone: str, percentage: int) -> bool:
    headers = build_base_headers({'Authorization': f'Bearer {token}', 'msisdn': owner_msisdn})
    json_data = {'category': [{'listHierarchyId': 'PackageID', 'value': '523'}, {'listHierarchyId': 'TemplateID', 'value': '47'}, {'listHierarchyId': 'TierID', 'value': '523'}], 'parts': {'characteristicsValue': {'characteristicsValue': [{'characteristicName': 'quotaDist1', 'type': 'percentage', 'value': str(percentage)}]}, 'member': [{'id': [{'schemeName': 'MSISDN', 'value': owner_msisdn}], 'type': 'Owner'}, {'id': [{'schemeName': 'MSISDN', 'value': member_phone}], 'type': 'Member'}]}, 'type': 'SendInvitation'}
    def sync_post():
        return HTTP_SESSION.post('https://mobile.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup', headers=headers, json=json_data, timeout=30)
    resp = await asyncio.to_thread(sync_post)
    return resp.status_code in (200, 201)

async def accept_invitation(member_token: str, member_phone: str, owner_msisdn: str) -> bool:
    headers = build_base_headers({'api_id': 'APP', 'Authorization': f'Bearer {member_token}', 'msisdn': member_phone})
    json_data = {'category': [{'listHierarchyId': 'TemplateID', 'value': '47'}], 'name': 'FlexFamily', 'parts': {'member': [{'id': [{'schemeName': 'MSISDN', 'value': owner_msisdn}], 'type': 'Owner'}, {'id': [{'schemeName': 'MSISDN', 'value': member_phone}], 'type': 'Member'}]}, 'type': 'AcceptInvitation'}
    def sync_patch():
        return WEB_HTTP_SESSION.patch('https://mobile.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup', headers=headers, json=json_data, timeout=30)
    resp = await asyncio.to_thread(sync_patch)
    return resp.status_code in (200, 201)

async def update_quota(token: str, owner_msisdn: str, member_phone: str, percentage: int) -> bool:
    headers = build_base_headers({'Authorization': f'Bearer {token}', 'msisdn': owner_msisdn})
    json_data = {'category': [{'listHierarchyId': 'TemplateID', 'value': '47'}], 'createdBy': {'value': 'MobileApp'}, 'parts': {'characteristicsValue': {'characteristicsValue': [{'characteristicName': 'quotaDist1', 'type': 'percentage', 'value': str(percentage)}]}, 'member': [{'id': [{'schemeName': 'MSISDN', 'value': owner_msisdn}], 'type': 'Owner'}, {'id': [{'schemeName': 'MSISDN', 'value': member_phone}], 'type': 'Member'}]}, 'type': 'QuotaRedistribution'}
    def sync_patch():
        return WEB_HTTP_SESSION.patch('https://mobile.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup', headers=headers, json=json_data, timeout=30)
    resp = await asyncio.to_thread(sync_patch)
    return resp.status_code in (200, 201)

async def remove_member(token: str, owner_msisdn: str, member_phone: str) -> bool:
    headers = build_base_headers({'Authorization': f'Bearer {token}', 'msisdn': owner_msisdn})
    json_data = {'category': [{'listHierarchyId': 'TemplateID', 'value': '47'}], 'createdBy': {'value': 'MobileApp'}, 'parts': {'characteristicsValue': {'characteristicsValue': [{'characteristicName': 'Disconnect', 'value': '0'}, {'characteristicName': 'LastMemberDeletion', 'value': '1'}]}, 'member': [{'id': [{'schemeName': 'MSISDN', 'value': owner_msisdn}], 'type': 'Owner'}, {'id': [{'schemeName': 'MSISDN', 'value': member_phone}], 'type': 'Member'}]}, 'type': 'FamilyRemoveMember'}
    def sync_patch():
        return WEB_HTTP_SESSION.patch('https://mobile.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup', headers=headers, json=json_data, timeout=30)
    resp = await asyncio.to_thread(sync_patch)
    return resp.status_code in (200, 201)

async def cancel_invitation(token: str, owner_msisdn: str, member_phone: str) -> bool:
    headers = build_base_headers({'Authorization': f'Bearer {token}', 'msisdn': owner_msisdn})
    json_data = {'category': [{'listHierarchyId': 'TemplateID', 'value': '0'}], 'createdBy': {'value': 'MobileApp'}, 'name': 'FlexFamily', 'parts': {'member': [{'id': [{'schemeName': 'MSISDN', 'value': member_phone}], 'type': 'Member'}, {'id': [{'schemeName': 'MSISDN', 'value': owner_msisdn}], 'type': 'Owner'}]}, 'type': 'CancelInvitation'}
    def sync_patch():
        return WEB_HTTP_SESSION.patch('https://mobile.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup', headers=headers, json=json_data, timeout=30)
    resp = await asyncio.to_thread(sync_patch)
    return resp.status_code in (200, 201)

async def get_owner_remaining(token: str, phone: str) -> Optional[str]:
    headers = build_base_headers({'api-host': 'usageConsumptionHost', 'useCase': 'aggregated', 'Authorization': f'Bearer {token}', 'msisdn': phone})
    def sync_get():
        return HTTP_SESSION.get('https://mobile.vodafone.com.eg/services/dxl/usage/usageConsumptionReport', params={'@type': 'aggregated', 'bucket.product.publicIdentifier': phone}, headers=headers, timeout=15)
    resp = await asyncio.to_thread(sync_get)
    if resp.status_code != 200:
        return None
    try:
        def find_limit(obj):
            if isinstance(obj, dict):
                if obj.get("usageType") == "limit":
                    for bal in obj.get("bucketBalance", []):
                        if bal.get("@type") == "Remaining":
                            amount, units = bal.get("remainingValue", {}).get("amount"), bal.get("remainingValue", {}).get("units")
                            if amount is not None and units is not None:
                                return amount, units
                for v in obj.values():
                    res = find_limit(v)
                    if res:
                        return res
            elif isinstance(obj, list):
                for item in obj:
                    res = find_limit(item)
                    if res:
                        return res
        res = find_limit(resp.json())
        if res:
            amount, units = res
            if isinstance(amount, (int, float)) and amount.is_integer():
                amount = int(amount)
            return f"{amount} {units}"
        return None
    except Exception:
        return None

async def get_member_consumption_from_owner(owner_token: str, owner_phone: str, member_phone: str) -> Optional[Dict[str, str]]:
    headers = build_base_headers({'api-host': 'usageConsumptionHost', 'useCase': 'familyDetailed', 'Authorization': f'Bearer {owner_token}', 'msisdn': owner_phone})
    def sync_get():
        return HTTP_SESSION.get('https://mobile.vodafone.com.eg/services/dxl/usage/usageConsumptionReport', params={'@type': 'familyDetailed', 'bucket.product.publicIdentifier': member_phone}, headers=headers, timeout=15)
    resp = await asyncio.to_thread(sync_get)
    if resp.status_code != 200:
        return None
    try:
        for item in resp.json():
            if item.get('name') == 'Flex_Family_Main_Bundle':
                for bucket in item.get('bucket', []):
                    if bucket.get('usageType') == 'FLEX':
                        remaining = next((bal.get('remainingValue', {}) for bal in bucket.get('bucketBalance', []) if bal.get('@type') == 'Remaining'), None)
                        used = next((counter.get('value', {}) for counter in bucket.get('bucketCounter', []) if counter.get('@type') == 'Used' and counter.get('level') == 'Local'), None)
                        return {'remaining': f"{remaining.get('amount', 0)} {remaining.get('units', 'FLEX')}" if remaining else "غير متاح", 'used': f"{used.get('amount', 0)} {used.get('units', 'FLEX')}" if used else "غير متاح"}
        return None
    except Exception:
        return None

# ================== إدارة الرسائل والحالات ==================
user_messages: Dict[int, List[int]] = {}
user_states: Dict[int, Dict[str, Any]] = {}

async def clear_previous_messages(update: Update, user_id: int) -> None:
    if user_id not in user_messages:
        return
    remaining = []
    for msg_id in user_messages[user_id]:
        try:
            await update.effective_bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
        except Exception:
            remaining.append(msg_id)
    user_messages[user_id] = remaining

async def send_message(update: Update, user_id: int, text: str, keyboard: Optional[InlineKeyboardMarkup] = None, clear_before: bool = False, parse_mode: Optional[str] = None) -> None:
    if clear_before:
        await clear_previous_messages(update, user_id)
    msg = await update.callback_query.message.reply_text(text, reply_markup=keyboard, parse_mode=parse_mode) if update.callback_query else await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=parse_mode)
    user_messages.setdefault(user_id, []).append(msg.message_id)

async def query_or_message(update: Update, text: str, parse_mode: Optional[str] = None) -> None:
    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(text, parse_mode=parse_mode)
            return
        except Exception:
            pass
        await update.callback_query.message.reply_text(text, parse_mode=parse_mode)
    elif update.message:
        await update.message.reply_text(text, parse_mode=parse_mode)

async def ensure_valid_session(update: Update, user_id: int) -> Optional[Dict[str, Any]]:
    user = get_user(user_id)
    if not user:
        return None
    if now_cairo() < (user['token_expiry'] - timedelta(seconds=60)):
        return user
    saved_password = get_saved_password(user_id, user['phone'])
    if saved_password:
        try:
            token, expiry = await login_vodafone(user['phone'], saved_password)
            save_user(user_id, user['phone'], token, expiry, user.get('account_name', ''))
            return get_user(user_id)
        except Exception as e:
            log_action(user_id, "AUTO_SESSION_REFRESH_FAIL", str(e))
    delete_user(user_id)
    await send_message(update, user_id, "⏰ انتهت صلاحية جلستك. برجاء تسجيل الدخول مجدداً.")
    await show_login_screen(update, user_id, None)
    return None

async def show_login_screen(update: Update, user_id: int, context: Optional[ContextTypes.DEFAULT_TYPE]) -> None:
    saved_numbers = get_saved_numbers(user_id)
    if saved_numbers:
        keyboard = []
        for num in saved_numbers:
            acc_name = get_saved_account_name(user_id, num)
            label = f"{num} ({acc_name})" if acc_name else num
            if get_valid_token(user_id, num):
                label += " 🔑"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"use_num_{num}")])
        keyboard.append([InlineKeyboardButton("📞 إدخال رقم جديد", callback_data="new_manual_login")])
        await send_message(update, user_id, "📱 اختر رقمًا لتسجيل الدخول أو قم بإدخال رقم جديد:", InlineKeyboardMarkup(keyboard))
    else:
        await send_message(update, user_id, "يرجى تسجيل الدخول أولاً.\nأرسل رقم هاتفك (مثال: 01012345678)")
        user_states[user_id] = {'state': 'login_phone'}

# ================== تزويد يومين ==================
async def add_two_days(update: Update, user_id: int, context: Optional[ContextTypes.DEFAULT_TYPE], target_phone: Optional[str] = None) -> None:
    operation_lock = get_user_operation_lock(user_id)
    if operation_lock.locked():
        await send_message(update, user_id, "⏳ فيه عملية أخرى شغالة على الحساب. استنى لحد ما تخلص.", None)
        return
    async with operation_lock:
        if target_phone:
            token = get_valid_token(user_id, target_phone)
            if not token:
                saved_password = get_saved_password(user_id, target_phone)
                if saved_password:
                    try:
                        token, expiry = await login_vodafone(target_phone, saved_password)
                        save_token(user_id, target_phone, token, expiry)
                    except Exception as e:
                        log_action(user_id, "ADD_TWO_DAYS_MEMBER_LOGIN_FAIL", f"{target_phone}: {e}")
                        token = None
            if not token:
                await send_message(update, user_id, f"🔐 لا توجد جلسة صالحة للرقم {target_phone}. سجّل الدخول للرقم واحفظه أولًا، ثم أعد المحاولة.")
                return
            phone = target_phone
        else:
            user = await ensure_valid_session(update, user_id)
            if not user:
                await show_login_screen(update, user_id, context)
                return
            phone = user['phone']
            token = user['access_token']

        url = "https://mobile.vodafone.com.eg/services/dxl/pom/productOrder"
        headers = build_base_headers({'api-host': 'ProductOrderingManagement', 'useCase': 'FlexACPRenewal', 'Authorization': f'Bearer {token}', 'api-version': 'v2', 'msisdn': phone, 'X-Request-ID': f'FLEX_{int(now_cairo().timestamp())}'})
        payload = {"channel": {"name": "MobileApp"}, "orderItem": [{"action": "insert", "id": "FLEX_ROLLOVER", "product": {"characteristic": [{"name": "PaymentMethod", "value": "ACP"}, {"name": "ACP", "value": "True"}, {"name": "SMSID", "value": "MUTE_SMS"}], "encProductId": "8UaJf2TAFkQncd2YduZwRAbXrixRX0FbzsLM/WakGJhTeK0tEE5sc5f4a8TXyo9FYlvdb1bT75cEIo+D1y5rkWQUjSwKtd0EJRxzp9wIrbuKtupFTfAhNLcfTdjt7hDOMkiZSWlblb1Ne9CrZgq2/ptclZCRIlstBSrTme3YLhxCUzhbMrwOuYQoJaMg6uXx/FrIO9Dd3tgz2kTemb36IO5s7N0rSYVzyzPOoHJb5gGC69r3InrZ9JJql/vr5cS+BP33wKGpUNg9P207RLPthMUVyTZSrCnGQMQ+fXXMocCGTEwzOH/D7DRf2GCGg2OHZLL/DfL1cc0Sh6Yoc1hb", "id": "Flex_2021_523", "relatedParty": [{"id": phone, "name": "MSISDN", "role": "Subscriber"}]}, "eCode": 0}], "@type": "FlexACPRenewal"}
        try:
            await query_or_message(update, "⏳ جاري محاولة تزويد الباقة يومين...")
            response = await asyncio.to_thread(lambda: HTTP_SESSION.post(url, headers=headers, json=payload, timeout=30))
            if 200 <= response.status_code < 300:
                try:
                    result = response.json()
                except Exception:
                    result = {}
                state = str(result.get("state", "")).strip().lower()
                if state == "completed":
                    msg = "✅ تم تأكيد إضافة يومين لباقة فليكس بنجاح 🎉"
                elif response.status_code == 202 or state in {"pending", "inprogress", "in_progress", "accepted"}:
                    msg = "⏳ تم استلام الطلب، لكن التنفيذ لم يتأكد بعد. راجع حالة الباقة قبل إعادة المحاولة."
                else:
                    msg = "⚠️ الخادم استقبل الطلب، لكنه لم يرسل تأكيد اكتمال. لم أعتبر العملية ناجحة لتجنب تكرار الطلب."
            elif response.status_code == 400:
                try:
                    error_data = response.json()
                except Exception:
                    error_data = {}
                code = str(error_data.get("code", ""))
                if code == "2037":
                    msg = "⚠️ تم استخدام تزويد اليومين من قبل لهذا الشهر."
                elif code == "1001":
                    msg = "🔒 انتهت صلاحية الجلسة. سجل الدخول مرة أخرى."
                elif code == "1005":
                    msg = "📵 رقم الهاتف غير صالح."
                else:
                    msg = f"❌ لم يتم تزويد اليومين.\nالسبب: {error_data.get('reason', 'الطلب غير متاح حالياً')}"
            elif response.status_code == 401:
                msg = "🔒 الجلسة غير صالحة أو انتهت. سجل الدخول مرة أخرى."
            elif response.status_code == 403:
                msg = "⛔ الخدمة غير متاحة لهذا الحساب حالياً."
            else:
                msg = f"❌ لم يتم تزويد اليومين. كود الخادم: {response.status_code}"
        except requests.exceptions.Timeout:
            msg = "⏰ انتهت مهلة الاتصال. حاول مرة أخرى."
        except Exception as e:
            log_action(user_id, "ADD_TWO_DAYS_FAIL", str(e))
            msg = "❌ حدث خطأ أثناء تنفيذ تزويد اليومين."
        keyboard = [[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]
        await send_message(update, user_id, msg, InlineKeyboardMarkup(keyboard))

# ================== واجهة باقات الإنترنت ==================
async def show_internet_packages_menu(update: Update, user_id: int, context: Optional[ContextTypes.DEFAULT_TYPE]) -> None:
    is_callback = update.callback_query is not None
    if not await require_subscription_or_free(update, context, is_callback=is_callback, query=update.callback_query if is_callback else None):
        return
    user = await ensure_valid_session(update, user_id)
    if not user:
        await show_login_screen(update, user_id, context)
        return

    keyboard = [
        [InlineKeyboardButton("⚡ باقات إكستريم", callback_data="internet_extreme_info"), InlineKeyboardButton("➕ باقات بلص", callback_data="internet_plus_info")],
        [InlineKeyboardButton("📊 تقرير باقات الإنترنت", callback_data="internet_report_info")],
        [InlineKeyboardButton("🗑️ إلغاء باقة إنترنت", callback_data="internet_cancel_info")],
        [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")],
    ]
    menu_text = (
        "🌐 **إدارة باقات الإنترنت**\n\n"
        "اختر القسم المطلوب. العرض الحالي معلوماتي؛ التفعيل وقراءة البيانات والإلغاء "
        "لن تُنفذ قبل توفير واجهة رسمية موثقة."
    )
    markup = InlineKeyboardMarkup(keyboard)
    if update.callback_query:
        await update.callback_query.edit_message_text(menu_text, reply_markup=markup, parse_mode="Markdown")
    else:
        await send_message(update, user_id, menu_text, markup, parse_mode="Markdown")


async def show_internet_package_info(update: Update, user_id: int, section: str) -> None:
    if section == "extreme":
        body = (
            "⚡ **باقات إكستريم**\n\n"
            "• إكستريم 13 — 10 جنيه\n"
            "• إكستريم 26 — 20 جنيه\n"
            "• إكستريم 52 — 40 جنيه\n\n"
            "هذه التفاصيل من الملف المرجعي المرفق، وليست تحققًا من الأسعار الحالية."
        )
    elif section == "plus":
        body = (
            "➕ **باقات بلص**\n\n"
            "• باقة بلص 20\n• باقة بلص 32\n• باقة بلص 45\n\n"
            "الملف المرجعي لا يحدد أسعار هذه الباقات."
        )
    elif section == "report":
        body = "📊 التقرير المباشر غير مفعّل حاليًا. يلزم توثيق واجهة رسمية لقراءة استهلاك الباقات قبل ربطها."
    else:
        body = "🗑️ إلغاء باقات الإنترنت غير مفعّل حاليًا. لن يتم إرسال طلب إلغاء قبل اعتماد واجهة رسمية موثقة واختبارها."

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 رجوع لباقات النت", callback_data="internet_packages_menu")],
        [InlineKeyboardButton("🏠 القائمة الرئيسية", callback_data="main_menu")],
    ])
    if update.callback_query:
        await update.callback_query.edit_message_text(body, reply_markup=keyboard, parse_mode="Markdown")
    else:
        await send_message(update, user_id, body, keyboard, parse_mode="Markdown")


# ================== القائمة الرئيسية وإدارة العائلة (بعد التعديلات) ==================
async def show_main_menu(update: Update, user_id: int, context: Optional[ContextTypes.DEFAULT_TYPE]) -> None:
    if not await require_subscription_or_free(update, context, is_callback=False):
        return
    await clear_previous_messages(update, user_id)
    user = await ensure_valid_session(update, user_id)
    if not user:
        await show_login_screen(update, user_id, context); return
    if context and context.user_data.get('rocket_members') is None:
        context.user_data['rocket_members'] = []

    acc_name = user.get('account_name') or "بطل"
    keyboard = [
        [InlineKeyboardButton("🚀 تعليق دعوتين (VIP)", callback_data="sendonly_menu")],
        [InlineKeyboardButton("📅 تزويد يومين", callback_data="add_two_days"), InlineKeyboardButton("🏠 إدارة العائلة", callback_data="show_family")],
        [InlineKeyboardButton("💰 خصم فليكس", callback_data="flex_discount")],
        [InlineKeyboardButton("🔄 تحويل انظمه", callback_data="packages_services"), InlineKeyboardButton("📊 نسبة الأونر", callback_data="show_owner_remaining")],
        [InlineKeyboardButton("🌐 باقات نت", callback_data="internet_packages_menu")],
        [InlineKeyboardButton("🔄 حساب آخر", callback_data="another_account"), InlineKeyboardButton("📞 أرقامي المحفوظة", callback_data="show_saved")]
    ]
    text = (
        f"👑 **أهلاً بيك يا أستاذ {acc_name} في لوحة التحكم** 👑\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 **اسم الحساب:** `{acc_name}`\n"
        f"📱 **الرقم الحالي:** `{user['phone']}`\n"
        "🛡️ **نظام الحماية:** بصمة جهاز ثابتة (Anti-Ban) ✅\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "اختر الخدمة المطلوبة من الأزرار أدناه:"
    )
    await send_message(update, user_id, text, InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def show_family_screen(update: Update, user_id: int, context: Optional[ContextTypes.DEFAULT_TYPE]) -> None:
    is_cb = update.callback_query is not None
    if not await require_subscription_or_free(update, context, is_callback=is_cb, query=update.callback_query if is_cb else None):
        return
    user = await ensure_valid_session(update, user_id)
    if not user:
        await show_login_screen(update, user_id, context); return
    await clear_previous_messages(update, user_id)
    try:
        members = await get_family_members(user['access_token'], user['phone'])
    except Exception as e:
        await send_message(update, user_id, f"خطأ: {str(e)}"); return
    active, pending = members.get('active', []), members.get('pending', [])
    filtered_pending = [p for p in pending if p['phone'] not in [a['phone'] for a in active]]
    rocket_list = context.user_data.get('rocket_members', []) if context else []
    for m in active:
        keyboard = [
            [InlineKeyboardButton(f"فلكس {f}", callback_data=f"edit_{m['phone']}_{f}") for f in ["1300", "2600", "5200"] if f != m['flex']],
            [InlineKeyboardButton("📅 تزويد يومين", callback_data=f"add_two_days_member_{m['phone']}"), InlineKeyboardButton("🗑️ حذف الفرد", callback_data=f"del_{m['phone']}")]
        ]
        await send_message(update, user_id, f"📞 الفرد: {m['phone']}{' 🚀' if m['phone'] in rocket_list else ''}\n✅ مفعل\n📊 النسبة: {m['flex']} فلكس\n✏️ تعديل نسبة الفرد", InlineKeyboardMarkup(keyboard))
    for p in filtered_pending:
        keyboard = [
            [InlineKeyboardButton("قبول الدعوة ✅", callback_data=f"accept_{p['phone']}"), InlineKeyboardButton("إلغاء الدعوة ❌", callback_data=f"cancel_{p['phone']}")],
            [InlineKeyboardButton("🗑️ حذف الفرد", callback_data=f"del_{p['phone']}")]
        ]
        await send_message(update, user_id, f"📞 الفرد: {p['phone']}{' 🚀' if p['count'] >= 2 else ''}\n⏳ بانتظار القبول\n📊 النسبة: {p['flex']} فلكس", InlineKeyboardMarkup(keyboard))
    if len(active) < 2:
        await send_message(update, user_id, "📝 دعوة فرد جديد: اختر نسبة من الأزرار أدناه 📞", InlineKeyboardMarkup([[InlineKeyboardButton("فلكس 5200", callback_data="invite_40"), InlineKeyboardButton("فلكس 2600", callback_data="invite_20"), InlineKeyboardButton("فلكس 1300", callback_data="invite_10")], [InlineKeyboardButton("🔄 تحديث", callback_data="refresh_family")], [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]))
    else:
        await send_message(update, user_id, "العائلة ممتلئة (2 أعضاء). لا يمكن إضافة المزيد.", InlineKeyboardMarkup([[InlineKeyboardButton("🔄 تحديث", callback_data="refresh_family")], [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]))

async def show_owner_remaining(update: Update, user_id: int, context: Optional[ContextTypes.DEFAULT_TYPE]) -> None:
    is_cb = update.callback_query is not None
    if not await require_subscription_or_free(update, context, is_callback=is_cb, query=update.callback_query if is_cb else None):
        return
    user = await ensure_valid_session(update, user_id)
    if not user:
        await show_login_screen(update, user_id, context); return
    await send_message(update, user_id, "📊 جاري جلب النسبة المتبقية...")
    try:
        rem = await get_owner_remaining(user['access_token'], user['phone'])
        text = f"📊 نسبة الأونر المتبقية:\n{rem}" if rem else "❌ لم نتمكن من جلب النسبة المتبقية."
    except Exception as e:
        text = f"❌ خطأ: {str(e)}"
    await send_message(update, user_id, text, InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]))

async def show_consumption(update: Update, user_id: int, context: Optional[ContextTypes.DEFAULT_TYPE]) -> None:
    is_cb = update.callback_query is not None
    if not await require_subscription_or_free(update, context, is_callback=is_cb, query=update.callback_query if is_cb else None):
        return
    user = await ensure_valid_session(update, user_id)
    if not user:
        await show_login_screen(update, user_id, context); return
    try:
        active_members = (await get_family_members(user['access_token'], user['phone'])).get('active', [])
        if not active_members:
            await send_message(update, user_id, "ℹ️ لا يوجد أعضاء نشطون في العائلة لعرض استهلاكهم."); return
    except Exception as e:
        await send_message(update, user_id, f"خطأ: {str(e)}"); return
    await send_message(update, user_id, "📊 جاري جلب استهلاك الأعضاء...")
    results = []
    for member in active_members:
        consumption = await get_member_consumption_from_owner(user['access_token'], user['phone'], member['phone'])
        results.append(f"📞 {member['phone']}\n✅ المتبقي: {consumption['remaining']}\n📉 المستخدم: {consumption['used']}" if consumption else f"📞 {member['phone']}\n❌ لم نتمكن من جلب استهلاكه (قد لا يكون مفعلاً أو هناك خطأ).")
    await send_message(update, user_id, "📊 استهلاك الفليكسات للأعضاء:\n\n" + "\n\n".join(results) if results else "❌ حدث خطأ أثناء جلب الاستهلاك.", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]))

async def show_saved_numbers(update: Update, user_id: int, context: Optional[ContextTypes.DEFAULT_TYPE]) -> None:
    is_cb = update.callback_query is not None
    if not await require_subscription_or_free(update, context, is_callback=is_cb, query=update.callback_query if is_cb else None):
        return
    numbers = get_saved_numbers(user_id)
    if not numbers:
        await send_message(update, user_id, "📭 لا توجد أرقام محفوظة. يمكنك إضافة رقم باستخدام /savenumber <رقم>", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]])); return
    keyboard = []
    for num in numbers:
        acc_name = get_saved_account_name(user_id, num)
        label = f"{num} ({acc_name})" if acc_name else num
        if get_valid_token(user_id, num):
            label += " 🔑"
        keyboard.append([InlineKeyboardButton(label, callback_data=f"use_num_{num}"), InlineKeyboardButton("🗑 حذف", callback_data=f"del_num_{num}")])
    keyboard.append([InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")])
    await send_message(update, user_id, "📞 أرقامي المحفوظة (🔑=توكن صالح):\nاختر رقم لتسجيل الدخول به أو حذفه:", InlineKeyboardMarkup(keyboard))

    # ================== خصم فليكس (تفعيل لحظي + حجز 12:01 منتصف الليل) ==================
class FlexDiscountService:
    def __init__(self, token: str, phone: str):
        self.token = token
        self.phone = phone
        self.session = HTTP_SESSION

    def login_for_discount(self, password: str) -> bool:
        if not password:
            return False
        url = "https://mobile.vodafone.com.eg/auth/realms/vf-realm/protocol/openid-connect/token"
        payload = {
            'grant_type': "password", 'username': self.phone, 'password': password,
            'client_secret': "95fd95fb-7489-4958-8ae6-d31a525cd20a", 'client_id': "ana-vodafone-app"
        }
        headers = build_base_headers({'Content-Type': "application/x-www-form-urlencoded", 'silentLogin': "true", 'msisdn': self.phone})
        try:
            response = self.session.post(url, data=payload, headers=headers, timeout=20)
            if response.status_code == 200:
                token = response.json().get('access_token')
                if token:
                    self.token = token
                    return True
        except Exception:
            pass
        return False

    def get_discount_offers(self) -> List[Dict]:
        url = "https://mobile.vodafone.com.eg/services/dxl/epo/eligibleProductOffering"
        params = {'customerAccountId': self.phone, 'parts.customerAccount.type': "Consumer", 'Accept-Language': "ar", 'type': "Tarrifs"}
        headers = build_base_headers({'api-host': "EligibleProductOfferingHost", 'useCase': "Tarrifs", 'Authorization': f"Bearer {self.token}", 'msisdn': self.phone})
        try:
            response = self.session.get(url, params=params, headers=headers, timeout=20)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, list):
                    return data
                return []
        except Exception as e:
            logger.warning(f"Flex discount offers exception: {e}")
        return []

    @staticmethod
    def extract_price_from_desc(description: str) -> Optional[Dict]:
        patterns = [r'جدد ب(\d+) بدل (\d+)', r'ب(\d+) بدل (\d+)', r'بسعر (\d+) بدل (\d+)', r'و جدد ب(\d+) بدل (\d+)', r'خصم (\d+) بدل (\d+)']
        for pattern in patterns:
            match = re.search(pattern, description or "")
            if match:
                discounted = int(match.group(1)); original = int(match.group(2))
                percentage = ((original - discounted) / original * 100) if original > 0 else 0
                return {'original': original, 'discounted': discounted, 'discount_percentage': percentage}
        return None

    @staticmethod
    def _char_value(line_item: Dict, name: str, default: str = "") -> str:
        characteristics = line_item.get('characteristic', {}) or {}
        values = characteristics.get('characteristicsValue', []) or []
        for char in values:
            if isinstance(char, dict) and char.get('characteristicName') == name:
                return str(char.get('value', default))
        return default

    @staticmethod
    def extract_bundle_name(name: str, description: str) -> str:
        for pattern in [r'فليكس (\d+)', r'باقة (\d+)', r'على (\d+)', r'في (\d+)']:
            match = re.search(pattern, description or "")
            if match:
                return f"فليكس {match.group(1)}"
        return name or ("فليكس" if "فليكس" in (description or "") else "الباقة الحالية")

    def extract_all_discount_offers(self, offers_data: List) -> List[Dict]:
        all_offers = []
        for offer_group in offers_data or []:
            if not isinstance(offer_group, dict):
                continue
            products = (offer_group.get('parts', {}) or {}).get('productOffering', []) or []
            for product in products:
                for line_item in product.get('lineItem', []) or []:
                    if not isinstance(line_item, dict):
                        continue
                    offer_type = line_item.get('type', '')
                    description = line_item.get('desc', '')
                    if offer_type not in ('Access fees Discount', 'Usage fees Discount'):
                        continue
                    if not any(k in description for k in ('جدد', 'خصم', 'بدل', 'خليك')):
                        continue
                    price = self.extract_price_from_desc(description)
                    if not price:
                        continue
                    tariff_id = self._char_value(line_item, 'TariffID')
                    product_id = self._char_value(line_item, 'TibcoID')
                    if not tariff_id or not product_id:
                        continue
                    tariff_rank = '1'
                    for cat in line_item.get('category', []) or []:
                        if isinstance(cat, dict) and cat.get('listHierarchyId') == 'TariffRank':
                            tariff_rank = str(cat.get('value', '1')); break
                    offer = {
                        'name': line_item.get('name', ''),
                        'bundle_name': self.extract_bundle_name(line_item.get('name', ''), description),
                        'desc': description,
                        'original_price': price['original'],
                        'discounted_price': price['discounted'],
                        'discount_amount': price['original'] - price['discounted'],
                        'discount_percentage': price['discount_percentage'],
                        'type': offer_type,
                        'tariff_id': tariff_id,
                        'product_id': product_id,
                        'tariff_rank': tariff_rank,
                        'offer_rank': self._char_value(line_item, 'OfferRank', '1'),
                        'cohort_id': self._char_value(line_item, 'CohortId', '30'),
                    }
                    all_offers.append(offer)
        return sorted(all_offers, key=lambda x: x['discount_percentage'], reverse=True)

    def purchase_offer(self, offer: Dict) -> Tuple[bool, str]:
        url = "https://mobile.vodafone.com.eg/services/dxl/pom/productOrder"
        payload = {
            "channel": {"name": "MobileApp"},
            "orderItem": [{
                "action": "add", "id": offer['product_id'],
                "itemPrice": [
                    {"name": "OriginalPrice", "price": {"taxIncludedAmount": {"unit": "LE", "value": str(offer['discounted_price'])}}},
                    {"name": "MigrationFees", "price": {"taxIncludedAmount": {"unit": "LE", "value": "0.0"}}}
                ],
                "product": {
                    "characteristic": [
                        {"name": "TariffRank", "value": offer.get('tariff_rank', '1')},
                        {"name": "TariffID", "value": offer['tariff_id']},
                        {"name": "Quota"}, {"name": "Validity", "@type": "MONTH", "value": "1"},
                        {"name": "MaxAdjustmentNumber", "value": "1"}, {"name": "offerRank", "value": offer.get('offer_rank', '1')},
                        {"name": "MigrationDesc", "value": "Intervention Offer Migration"}, {"name": "CohortId", "value": offer.get('cohort_id', '30')}
                    ],
                    "productSpecification": [
                        {"id": "Retention With Offer", "name": "Category"}, {"id": "Upon Renewal / Repurchase", "name": "MigrationRule"},
                        {"id": "0", "name": "RatePlanType"}, {"id": "Flex Family", "name": "BundleType"}
                    ],
                    "relatedParty": [
                        {"id": self.phone, "name": "MSISDN", "@referredType": "prepaid", "role": "Subscriber"},
                        {"id": offer['tariff_id'], "name": "TariffID", "@referredType": "prepaid", "role": "TariffID"}
                    ]
                },
                "@type": offer.get('type', 'Access fees Discount')
            }],
            "@type": "InterventionTariff"
        }
        headers = build_base_headers({'api-host': "ProductOrderingManagement", 'useCase': "InterventionTariff", 'Authorization': f"Bearer {self.token}", 'msisdn': self.phone})
        try:
            response = self.session.post(url, data=json.dumps(payload, ensure_ascii=False), headers=headers, timeout=20)
            try:
                data = response.json()
            except Exception:
                data = {}
            status_value = str(data.get('status', '')).upper() if isinstance(data, dict) else ''
            if response.status_code in (200, 201) or (response.status_code == 400 and data.get('code') == "1008") or status_value == "SUCCESS":
                return True, f"✅ تم تفعيل خصم {offer['discount_amount']} جنيه على {offer['bundle_name']} بنجاح!"
            if data.get('code') == "1001":
                return False, "❌ تمت معالجة الطلب مسبقاً"
            if data.get('code') == "1002":
                return False, "❌ العرض غير متاح حالياً"
            return False, "❌ العرض غير متاح لخطك حالياً"
        except requests.exceptions.Timeout:
            return False, "⏰ انتهت مهلة الاتصال. حاول مرة أخرى."
        except Exception:
            return False, "❌ خطأ في الاتصال بسيرفر فودافون"

# دوال إدارة جدول الحجوزات الليلية (12:01 ص)
def get_next_midnight_one_minute() -> datetime:
    now = now_cairo()
    target = (now + timedelta(days=1)).replace(hour=0, minute=1, second=0, microsecond=0)
    # إذا كانت الساعة الآن بين 00:00 و 00:01 صباحاً، ينفذ في نفس الليلة 00:01
    if now.hour == 0 and now.minute < 1:
        target = now.replace(hour=0, minute=1, second=0, microsecond=0)
    return target

def save_scheduled_discount(user_id: int, phone: str, offer: Dict, scheduled_dt: datetime) -> None:
    with get_db_connection() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO scheduled_discounts
               (telegram_id, phone, offer_json, scheduled_time, status, created_at)
               VALUES (?, ?, ?, ?, 'pending', ?)""",
            (user_id, phone, json.dumps(offer, ensure_ascii=False), scheduled_dt.isoformat(), now_cairo().isoformat())
        )
        conn.commit()

def get_user_scheduled_discount(user_id: int, phone: str) -> Optional[Dict[str, Any]]:
    with get_db_connection() as conn:
        row = conn.execute(
            "SELECT offer_json, scheduled_time FROM scheduled_discounts WHERE telegram_id = ? AND phone = ? AND status = 'pending'",
            (user_id, phone)
        ).fetchone()
    if row:
        try:
            return {'offer': json.loads(row[0]), 'scheduled_time': datetime.fromisoformat(row[1])}
        except Exception:
            pass
    return None

def cancel_user_scheduled_discount(user_id: int, phone: str) -> bool:
    with get_db_connection() as conn:
        cur = conn.execute("DELETE FROM scheduled_discounts WHERE telegram_id = ? AND phone = ?", (user_id, phone))
        conn.commit()
        return cur.rowcount > 0

async def flex_discount_menu(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_valid_session(update, user_id)
    if not user:
        await show_login_screen(update, user_id, context); return
    await query_or_message(update, "⏳ جاري فحص الخط والبحث عن أفضل عرض خصم فليكس متاح...")
    service = FlexDiscountService(user['access_token'], user['phone'])
    password = get_saved_password(user_id, user['phone'])
    if password:
        discount_login_ok = await asyncio.to_thread(service.login_for_discount, password)
        if not discount_login_ok:
            keyboard = [[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]
            await send_message(update, user_id, "❌ تعذر تسجيل الدخول لخدمة خصم فليكس.\nجرّب تسجيل الخروج والدخول مرة أخرى لتحديث كلمة المرور المحفوظة.", InlineKeyboardMarkup(keyboard))
            return
    try:
        offers = await asyncio.wait_for(asyncio.to_thread(service.get_discount_offers), timeout=25)
        all_offers = await asyncio.to_thread(service.extract_all_discount_offers, offers)
    except asyncio.TimeoutError:
        keyboard = [[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]
        await send_message(update, user_id, "⏰ سيرفر عروض الخصم اتأخر في الرد. جرّب تاني بعد لحظات.", InlineKeyboardMarkup(keyboard)); return
    except Exception as e:
        keyboard = [[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]
        await send_message(update, user_id, f"❌ حصل خطأ أثناء جلب الخصم: {str(e)[:120]}", InlineKeyboardMarkup(keyboard)); return

    existing_schedule = get_user_scheduled_discount(user_id, user['phone'])

    if not all_offers:
        keyboard = []
        if existing_schedule:
            keyboard.append([InlineKeyboardButton("🗑️ إلغاء الحجز الليلي القادم", callback_data="flex_discount_cancel_schedule")])
        keyboard.append([InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")])
        extra_note = f"\n\n🕛 لديك حجز مفعل مسبقاً للتنفيذ الساعة 12:01 ص." if existing_schedule else ""
        await send_message(update, user_id, f"❌ لم أجد عروض خصم فليكس متاحة على الخط حالياً.{extra_note}", InlineKeyboardMarkup(keyboard))
        return

    context.user_data['flex_discount_token'] = service.token
    best = all_offers[0]
    context.user_data['flex_discount_offer'] = best

    schedule_note = ""
    if existing_schedule:
        sched_str = existing_schedule['scheduled_time'].strftime('%Y-%m-%d الساعة 12:01 ص')
        schedule_note = f"\n📌 **حالة الحجز:** يوجد حجز مسبق للتنفيذ يوم `{sched_str}`\n"

    text = (
        "💰 **أفضل عرض خصم فليكس متاح لخطك**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 **الرقم:** `{user['phone']}`\n"
        f"📦 **الباقة:** {best['bundle_name']}\n"
        f"💰 **السعر الأصلي:** {best['original_price']} جنيه\n"
        f"💸 **سعر الخصم:** {best['discounted_price']} جنيه (توفير {best['discount_amount']} ج)\n"
        f"✨ **نسبة الخصم:** {best['discount_percentage']:.1f}%\n"
        f"📝 **الوصف:** {best['desc']}\n"
        f"{schedule_note}"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "👇 **اختر طريقة تفعيل الخصم:**\n"
        "• **تفعيل لحظي:** يفعل العرض الآن فوراً.\n"
        "• **حجز تفعيل:** يحجز العرض ويفعله تلقائياً الساعة **12:01 منتصف الليل**."
    )
    keyboard = [
        [
            InlineKeyboardButton("⚡ تفعيل لحظي", callback_data="flex_discount_confirm"),
            InlineKeyboardButton("🕛 حجز تفعيل (12:01 ص)", callback_data="flex_discount_schedule")
        ]
    ]
    if existing_schedule:
        keyboard.append([InlineKeyboardButton("🗑️ إلغاء الحجز الليلي الحالي", callback_data="flex_discount_cancel_schedule")])
    keyboard.append([InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")])
    await send_message(update, user_id, text, InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def flex_discount_confirm(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    offer = context.user_data.get('flex_discount_offer')
    if not offer:
        await query_or_message(update, "⚠️ بيانات العرض انتهت. افتح خصم فليكس من جديد."); return
    user = await ensure_valid_session(update, user_id)
    if not user:
        return
    lock = get_user_operation_lock(user_id)
    if lock.locked():
        await query_or_message(update, "⏳ فيه عملية أخرى شغالة على الحساب. استنى لحد ما تخلص."); return
    async with lock:
        await query_or_message(update, "⚡ جاري التفعيل اللحظي لعرض خصم فليكس...")
        discount_token = context.user_data.get('flex_discount_token') or user['access_token']
        service = FlexDiscountService(discount_token, user['phone'])
        success, message = await asyncio.to_thread(service.purchase_offer, offer)
    context.user_data.pop('flex_discount_offer', None); context.user_data.pop('flex_discount_token', None)
    keyboard = [[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]
    await send_message(update, user_id, message, InlineKeyboardMarkup(keyboard))

async def flex_discount_schedule_handler(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    offer = context.user_data.get('flex_discount_offer')
    if not offer:
        await query_or_message(update, "⚠️ بيانات العرض انتهت. افتح خصم فليكس من جديد."); return
    user = await ensure_valid_session(update, user_id)
    if not user:
        return
    saved_pass = get_saved_password(user_id, user['phone'])
    if not saved_pass:
        await query_or_message(update, "⚠️ لحجز التفعيل الليلي، يرجى تسجيل الخروج والدخول مرة أخرى بالرقم ليتم حفظ جلسة التجديد التلقائي.")
        return
    # تمديد حفظ كلمة المرور لمدة 72 ساعة لضمان نجاح التفعيل الساعة 12:01 ص
    save_password(user_id, user['phone'], saved_pass, expiry_hours=72)
    target_dt = get_next_midnight_one_minute()
    save_scheduled_discount(user_id, user['phone'], offer, target_dt)

    context.user_data.pop('flex_discount_offer', None); context.user_data.pop('flex_discount_token', None)
    keyboard = [
        [InlineKeyboardButton("🗑️ إلغاء الحجز", callback_data="flex_discount_cancel_schedule")],
        [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]
    ]
    msg = (
        "🕛 **تم حجز تفعيل خصم فليكس بنجاح!** ✅\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        f"📱 **الرقم:** `{user['phone']}`\n"
        f"📦 **الباقة:** {offer['bundle_name']}\n"
        f"💸 **السعر بعد الخصم:** {offer['discounted_price']} جنيه (بدلاً من {offer['original_price']} ج)\n"
        f"⏰ **موعد التنفيذ التلقائي:** `{target_dt.strftime('%Y-%m-%d')}` الساعة **12:01 منتصف الليل**\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔔 سيقوم البوت تلقائياً بتفعيل العرض في الموعد المحدد وإرسال رسالة تأكيد لك هنا."
    )
    await send_message(update, user_id, msg, InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def flex_discount_cancel_schedule_handler(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = get_user(user_id)
    if not user:
        await query_or_message(update, "⚠️ يجب تسجيل الدخول أولاً."); return
    removed = cancel_user_scheduled_discount(user_id, user['phone'])
    text = "✅ تم إلغاء حجز التفعيل الليلي للرقم بنجاح." if removed else "ℹ️ لا يوجد حجز نشط لهذا الرقم حالياً."
    keyboard = [[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]
    await send_message(update, user_id, text, InlineKeyboardMarkup(keyboard))

async def midnight_discount_scheduler_loop(app: Application) -> None:
    """محرك خلفي يعمل طوال الوقت لتنفيذ حجوزات خصم فليكس في تمام الساعة 12:01 منتصف الليل"""
    logger.info("🕛 Midnight Flex Discount Scheduler started (Cairo Time).")
    while True:
        try:
            now = now_cairo()
            with get_db_connection() as conn:
                rows = conn.execute(
                    "SELECT telegram_id, phone, offer_json, scheduled_time FROM scheduled_discounts WHERE status = 'pending' AND scheduled_time <= ?",
                    (now.isoformat(),)
                ).fetchall()

            for uid, phone, offer_json_str, sched_time_str in rows:
                with get_db_connection() as conn:
                    conn.execute(
                        "UPDATE scheduled_discounts SET status = 'processing' WHERE telegram_id = ? AND phone = ?",
                        (uid, phone)
                    )
                    conn.commit()

                try:
                    offer = json.loads(offer_json_str)
                    password = get_saved_password(uid, phone)
                    token = get_valid_token(uid, phone)
                    service = FlexDiscountService(token or "", phone)

                    if password:
                        logged_in = await asyncio.to_thread(service.login_for_discount, password)
                        if not logged_in and not token:
                            tok, exp = await login_vodafone(phone, password)
                            save_token(uid, phone, tok, exp)
                            service.token = tok

                    # محاولة التفعيل (مع إعادة المحاولة مرة إضافية لو السيرفر مضغوط الساعة 12:01)
                    success, result_msg = await asyncio.to_thread(service.purchase_offer, offer)
                    if not success:
                        await asyncio.sleep(3)
                        if password:
                            await asyncio.to_thread(service.login_for_discount, password)
                        success, result_msg = await asyncio.to_thread(service.purchase_offer, offer)

                    final_status = 'completed' if success else 'failed'
                    with get_db_connection() as conn:
                        conn.execute("DELETE FROM scheduled_discounts WHERE telegram_id = ? AND phone = ?", (uid, phone))
                        conn.commit()

                    notify_text = (
                        f"🕛 **تقرير الحجز التلقائي (12:01 منتصف الليل)**\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"📱 **الرقم:** `{phone}`\n"
                        f"📦 **العرض:** {offer.get('bundle_name', 'فليكس')}\n"
                        f"📊 **النتيجة:** {result_msg}"
                    )
                    await app.bot.send_message(chat_id=uid, text=notify_text, parse_mode='Markdown')
                    log_action(uid, f"MIDNIGHT_DISCOUNT_{final_status.upper()}", f"{phone}: {result_msg}")
                except Exception as ex:
                    logger.error(f"Error executing scheduled discount for {phone}: {ex}")
                    with get_db_connection() as conn:
                        conn.execute("DELETE FROM scheduled_discounts WHERE telegram_id = ? AND phone = ?", (uid, phone))
                        conn.commit()
                    try:
                        await app.bot.send_message(
                            chat_id=uid,
                            text=f"⚠️ تعذر تنفيذ حجز خصم فليكس للرقم `{phone}` الساعة 12:01 ص بسبب خطأ في الاتصال: {str(ex)[:80]}",
                            parse_mode='Markdown'
                        )
                    except Exception:
                        pass
        except Exception as loop_err:
            logger.error(f"Scheduler loop error: {loop_err}")
        await asyncio.sleep(15)

# ================== خدمات الباقات والتحويل ==================
FLEX_BUNDLES = [{"number": str(i), "name": n, "id": id_, "price": p} for i, (n, id_, p) in enumerate([("فليكس 40", "Flex_2021_511", 40), ("فليكس 45", "Flex_2024_627", 45), ("فليكس 60", "Flex_2021_513", 60), ("فليكس 70", "Flex_2024_629", 70), ("فليكس 90", "Flex_2021_515", 90), ("فليكس 100", "Flex_2024_631", 100), ("فليكس 130", "Flex_2021_517", 130), ("فليكس 150", "Flex_2024_633", 150), ("فليكس 260", "Flex_2021_523", 260), ("فليكس 300", "Flex_2024_637", 300)], 1)]
FLEX_ACP_ENC_PRODUCT_ID = "SBWbw/gsvm1cU1nPBj7HCg6MNEaAfyY56Kxz53nXBwpe6Z4c2t1DgiO2OM2hZwGVJaztwhZu7DWZiE2Ic5evFLqZfV/QaAOWQcS3m8bZCVD/wmRvbEvtfv16FTwgzWMjUQErPqXuYIMnePuK3H+MwQ8iFKqpvQ1d7qrPz05JlpUXKn2GM14uKA=="

def get_flex_bundle_by_number(number: str) -> Optional[Dict[str, Any]]:
    return next((b for b in FLEX_BUNDLES if b['number'] == str(number)), None)

async def packages_services_menu(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_valid_session(update, user_id):
        await show_login_screen(update, user_id, context); return
    keyboard = [[InlineKeyboardButton("⚡ باقات فليكس", callback_data="packages_flex")], [InlineKeyboardButton("🔄 تحويل 14 قرش", callback_data="packages_convert14")], [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]
    await query_or_message(update, "🔄 تحويل انظمه\n\nاختر الخدمة المطلوبة:")
    await send_message(update, user_id, "اختر من الأزرار:", InlineKeyboardMarkup(keyboard))

async def flex_bundles_menu(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_valid_session(update, user_id):
        return
    bundle_2021, bundle_2024 = [b for b in FLEX_BUNDLES if "2021" in b['id']], [b for b in FLEX_BUNDLES if "2024" in b['id']]
    keyboard = [[InlineKeyboardButton("⚡ نظام 2021", callback_data="packages_ignore"), InlineKeyboardButton("🆕 نظام 2024", callback_data="packages_ignore")]]
    for i in range(max(len(bundle_2021), len(bundle_2024))):
        row = []
        if i < len(bundle_2021):
            row.append(InlineKeyboardButton(f"{bundle_2021[i]['name']} - {bundle_2021[i]['price']}ج", callback_data=f"packages_flex_{bundle_2021[i]['number']}"))
        if i < len(bundle_2024):
            row.append(InlineKeyboardButton(f"{bundle_2024[i]['name']} - {bundle_2024[i]['price']}ج", callback_data=f"packages_flex_{bundle_2024[i]['number']}"))
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 تحويل انظمه", callback_data="packages_services")])
    await query_or_message(update, "⚡ باقات فليكس\n\nاختر الباقة المناسبة:")
    await send_message(update, user_id, "نظام 2021 ونظام 2024:", InlineKeyboardMarkup(keyboard))

async def flex_bundle_confirm_screen(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE, bundle_number: str) -> None:
    bundle = get_flex_bundle_by_number(bundle_number)
    user = await ensure_valid_session(update, user_id)
    if not user or not bundle:
        await query_or_message(update, "❌ حدث خطأ في اختيار الباقة."); return
    context.user_data['packages_pending_flex'] = bundle_number
    keyboard = [[InlineKeyboardButton("✅ تأكيد التفعيل", callback_data=f"packages_flex_confirm_{bundle_number}"), InlineKeyboardButton("❌ إلغاء", callback_data="packages_services")]]
    await query_or_message(update, f"⚡ تأكيد تفعيل الباقة\n\n📱 الرقم: {user['phone']}\n📦 الباقة: {bundle['name']}\n💰 السعر: {bundle['price']} جنيه\n\nهل تريد تفعيل هذه الباقة؟")
    await send_message(update, user_id, "اضغط تأكيد للتنفيذ:", InlineKeyboardMarkup(keyboard))

async def execute_flex_bundle_service(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE, bundle_number: str) -> None:
    bundle = get_flex_bundle_by_number(bundle_number)
    user = await ensure_valid_session(update, user_id)
    if not user or not bundle:
        await query_or_message(update, "❌ بيانات الباقة غير متاحة."); return
    lock = get_user_operation_lock(user_id)
    if lock.locked():
        await query_or_message(update, "⏳ فيه عملية أخرى شغالة على الحساب. استنى لحد ما تخلص."); return
    async with lock:
        await query_or_message(update, f"⏳ جاري تفعيل {bundle['name']}...")
        payload = {"channel": {"name": "MobileApp"}, "orderItem": [{"action": "insert", "id": bundle['id'], "product": {"characteristic": [{"name": "PaymentMethod", "value": "ACP"}, {"name": "ACP", "value": "True"}, {"name": "SMSID", "value": "MUTE_SMS"}, {"name": "LangId", "value": "en"}, {"name": "ExecutionType", "value": "Sync"}], "encProductId": FLEX_ACP_ENC_PRODUCT_ID, "id": bundle['id'], "relatedParty": [{"id": user['phone'], "name": "MSISDN", "role": "Subscriber"}]}, "eCode": 0}], "@type": "FlexACPRenewal"}
        headers = build_base_headers({'api-host': 'ProductOrderingManagement', 'useCase': 'FlexACPRenewal', 'Authorization': f"Bearer {user['access_token']}", 'api-version': 'v2', 'msisdn': user['phone']})
        try:
            response = await asyncio.to_thread(lambda: HTTP_SESSION.post('https://mobile.vodafone.com.eg/services/dxl/pom/productOrder', headers=headers, json=payload, timeout=30))
            try:
                result = response.json()
            except Exception:
                result = {}
            code = str(result.get('code', '')) if isinstance(result, dict) else ''
            if response.status_code in (200, 201) or code == '3999':
                msg = f"✅ تم تحويل {bundle['name']} بنجاح!\n\n📱 الرقم: {user['phone']}\n💰 السعر: {bundle['price']} جنيه" + ("\n\n✅ برجاء التحقق من اشتراكاتك للتأكيد" if code == '3999' else "")
            else:
                msg = f"❌ فشل تحويل {bundle['name']}\nكود الحالة: {response.status_code}\nالسبب: {result.get('reason', 'الطلب غير متاح حالياً') if isinstance(result, dict) else 'الطلب غير متاح حالياً'}"
        except requests.exceptions.Timeout:
            msg = "⏰ انتهت مهلة الاتصال. حاول مرة أخرى."
        except Exception as e:
            log_action(user_id, 'FLEX_BUNDLE_FAIL', str(e)); msg = "❌ حدث خطأ أثناء تفعيل الباقة."
    context.user_data.pop('packages_pending_flex', None)
    await send_message(update, user_id, msg, InlineKeyboardMarkup([[InlineKeyboardButton("🔙 تحويل انظمه", callback_data="packages_services")]]))

async def convert14_confirm_screen(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_valid_session(update, user_id)
    if not user:
        return
    context.user_data['packages_convert14_confirm'] = True
    await query_or_message(update, f"🔄 تحويل 14 قرش\n\n📱 الرقم: {user['phone']}\n📦 الباقة: ريح بالك كله ب14 قرش\n\n⚠️ هل تريد تنفيذ عملية التحويل؟")
    await send_message(update, user_id, "اضغط تأكيد للتنفيذ:", InlineKeyboardMarkup([[InlineKeyboardButton("✅ تأكيد التحويل", callback_data="packages_convert14_confirm"), InlineKeyboardButton("❌ إلغاء", callback_data="packages_services")]]))

async def execute_convert14_service(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get('packages_convert14_confirm'):
        await query_or_message(update, "⚠️ بيانات العملية انتهت. افتح الخدمة من جديد."); return
    user = await ensure_valid_session(update, user_id)
    if not user:
        return
    lock = get_user_operation_lock(user_id)
    if lock.locked():
        await query_or_message(update, "⏳ فيه عملية أخرى شغالة على الحساب. استنى لحد ما تخلص."); return
    async with lock:
        await query_or_message(update, "⏳ جاري تحويل الرقم إلى باقة 14 قرش...")
        phone, token = user['phone'], user['access_token']
        headers = build_base_headers({'api-host': 'EligibleProductOfferingHost', 'useCase': 'Tarrifs', 'Authorization': f'Bearer {token}', 'api-version': 'v2', 'msisdn': phone})
        try:
            r = await asyncio.to_thread(lambda: HTTP_SESSION.get('https://mobile.vodafone.com.eg/services/dxl/epo/eligibleProductOffering', params={'customerAccountId': phone, 'parts.customerAccount.type': 'Consumer', 'Accept-Language': 'ar', 'type': 'Tarrifs'}, headers=headers, timeout=30))
            if r.status_code != 200:
                raise RuntimeError(f'EPO status {r.status_code}')
            enc_product_id = tibco_id = tariff_id = None; rank = None
            for item in r.json() if isinstance(r.json(), list) else []:
                for product in (item.get('parts', {}) or {}).get('productOffering', []) or []:
                    if product.get('name') != 'ريح بالك كله ب14 قرش':
                        continue
                    for id_item in product.get('id', []) or []:
                        if id_item.get('schemeName') == 'EncProductID':
                            enc_product_id = id_item.get('value')
                        elif id_item.get('schemeID') == 'ProductID':
                            tibco_id = id_item.get('value')
                        elif id_item.get('schemeID') == 'TariffID':
                            tariff_id = id_item.get('value')
                    for ch in (product.get('specification', {}) or {}).get('characteristicsValue', []) or []:
                        if ch.get('characteristicName') == 'Rank':
                            rank = ch.get('value')
                    break
                if enc_product_id:
                    break
            if not enc_product_id:
                msg = "❌ باقة ريح بالك كله ب14 قرش غير متاحة على الخط حالياً."
            else:
                payload = {"channel": {"name": "MobileApp"}, "orderItem": [{"action": "add", "id": tibco_id, "itemPrice": [{"name": "OriginalPrice", "price": {"taxIncludedAmount": {"unit": "LE", "value": "0"}}}], "product": {"characteristic": [{"name": "TariffRank", "value": str(rank or '1')}, {"name": "TariffID", "value": tariff_id or ''}, {"name": "Quota", "value": ""}, {"name": "Validity", "@type": "MONTH", "value": ""}, {"name": "InterventionFlag", "value": "0"}, {"name": "DestNoOfSeats", "value": ""}], "encProductId": enc_product_id, "productSpecification": [{"id": "ConsumerType", "name": "Category"}, {"id": "0", "name": "RatePlanType"}, {"id": "Other", "name": "BundleType"}], "relatedParty": [{"id": phone, "name": "MSISDN", "@referredType": "prepaid", "role": "Subscriber"}, {"id": tariff_id or '470', "name": "TariffID", "@referredType": "prepaid", "role": "TariffID"}]}, "eCode": 0}], "@type": "Tariff"}
                post_headers = build_base_headers({'api-host': 'ProductOrderingManagement', 'useCase': 'Tariff', 'Authorization': f'Bearer {token}', 'api-version': 'v2', 'msisdn': phone})
                rp = await asyncio.to_thread(lambda: HTTP_SESSION.post('https://mobile.vodafone.com.eg/services/dxl/pom/productOrder', headers=post_headers, json=payload, timeout=30))
                msg = f"✅ تم إرسال طلب التحويل إلى باقة 14 قرش بنجاح!\n\n📱 الرقم: {phone}\n📦 الباقة: ريح بالك كله ب14 قرش" if rp.status_code in (200, 201, 400) else f"❌ فشل التحويل - كود الخادم: {rp.status_code}"
        except requests.exceptions.Timeout:
            msg = "⏰ انتهت مهلة الاتصال. حاول مرة أخرى."
        except Exception as e:
            log_action(user_id, 'CONVERT14_FAIL', str(e)); msg = "❌ فشل في جلب أو تنفيذ تحويل 14 قرش."
    context.user_data.pop('packages_convert14_confirm', None)
    await send_message(update, user_id, msg, InlineKeyboardMarkup([[InlineKeyboardButton("🔙 تحويل انظمه", callback_data="packages_services")]]))

# ================== محرك تعليق دعوتين الخارق (Anti-Ban 100% & Smart Token Pool) ==================
sendonly_stats = {"total_success": 0, "total_attempts": 0, "user_logs": {}}
SENDONLY_STATS_FILE = "sendonly_stats.json"

# كاش ذكي لتوكنات تعليق الدعوتين لمنع تكرار تسجيل الدخول وحظر الخط من فودافون
SENDONLY_TOKEN_POOL: Dict[str, List[Tuple[str, datetime]]] = defaultdict(list)

def load_sendonly_stats():
    global sendonly_stats
    if os.path.exists(SENDONLY_STATS_FILE):
        try:
            with open(SENDONLY_STATS_FILE, "r", encoding="utf-8") as f:
                sendonly_stats = json.load(f)
        except Exception:
            pass

def save_sendonly_stats():
    try:
        with open(SENDONLY_STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(sendonly_stats, f, indent=4, ensure_ascii=False)
    except Exception:
        pass

load_sendonly_stats()

SENDONLY_SELECT_OWNER, SENDONLY_OWNER_PASSWORD, SENDONLY_OWNER_NUM, SENDONLY_OWNER_PASS, SENDONLY_MEMBER_NUM, SENDONLY_PERCENTAGE, SENDONLY_COUNT = range(20, 27)

def get_authorization_sendonly(number: str, password: str, slot_index: int = 0) -> dict:
    """تسجيل دخول آمن ببصمة جهاز ثابتة لكل Slot لمنع كشف تعدد الجلسات من جدار فودافون"""
    fp = get_sticky_fingerprint(f"{number}_slot_{slot_index}")
    headers = {
        'User-Agent': 'okhttp/4.12.0',
        'Accept': 'application/json, text/plain, */*',
        'Content-Type': 'application/x-www-form-urlencoded',
        'silentLogin': 'true',
        'x-agent-operatingsystem': fp['os_version'],
        'clientId': 'AnaVodafoneAndroid',
        'Accept-Language': 'ar',
        'x-agent-device': fp['device_model'],
        'x-agent-version': fp['app_ver'],
        'x-agent-build': fp['build_num'],
        'digitalId': fp['digital_id'],
        'device-id': fp['device_id'],
        'msisdn': number
    }
    data = {
        'grant_type': 'password', 'username': number, 'password': password,
        'client_secret': '95fd95fb-7489-4958-8ae6-d31a525cd20a', 'client_id': 'ana-vodafone-app'
    }
    try:
        resp = HTTP_SESSION.post(
            'https://mobile.vodafone.com.eg/auth/realms/vf-realm/protocol/openid-connect/token',
            headers=headers, data=data, timeout=15
        )
        if resp.status_code == 200:
            js = resp.json()
            token = js.get('access_token')
            exp_sec = int(js.get('expires_in', 1800))
            if token:
                return {"status": "success", "token": "Bearer " + token, "expiry": now_cairo() + timedelta(seconds=exp_sec - 120)}
        elif resp.status_code in (429, 403):
            return {"status": "rate_limit", "message": f"حظر مؤقت {resp.status_code}"}
        return {"status": "error", "message": f"كود {resp.status_code}"}
    except Exception as e:
        return {"status": "error", "message": str(e)}

async def get_multiple_tokens_sendonly(owner_number: str, owner_password: str, count: int) -> List[str]:
    """
    الدرع الذكي لمنع الحظر:
    1. يعيد استخدام التوكنات الصالحة المخزنة في الذاكرة بدلاً من تسجيل الدخول في كل محاولة.
    2. يكتفي بـ 3 إلى 4 توكنات قوية فقط مهما كان عدد الدعوات، لتجنب Rate Limit على سيرفر Auth.
    """
    now = now_cairo()
    valid_cached = [(tok, exp) for tok, exp in SENDONLY_TOKEN_POOL.get(owner_number, []) if exp > now]
    SENDONLY_TOKEN_POOL[owner_number] = valid_cached

    target_pool_size = min(max(3, count // 10), 4)
    if len(valid_cached) >= target_pool_size:
        return [tok for tok, _ in valid_cached[:target_pool_size]]

    needed = target_pool_size - len(valid_cached)
    new_entries: List[Tuple[str, datetime]] = []
    rate_limited = False

    for idx in range(needed):
        if rate_limited:
            break
        if idx > 0:
            await asyncio.sleep(random.uniform(1.1, 1.8))
        slot_id = len(valid_cached) + idx
        result = await asyncio.to_thread(get_authorization_sendonly, owner_number, owner_password, slot_id)
        if result["status"] == "success":
            new_entries.append((result["token"], result["expiry"]))
        elif result["status"] == "rate_limit":
            rate_limited = True
            break

    SENDONLY_TOKEN_POOL[owner_number].extend(new_entries)
    return [tok for tok, _ in SENDONLY_TOKEN_POOL[owner_number]]

async def send_invitation_mobile_aiohttp(
    session: aiohttp.ClientSession, sem: asyncio.Semaphore,
    owner_number: str, member_number: str, token: str, percentage: int, slot_idx: int, jitter_delay: float
) -> dict:
    if jitter_delay > 0:
        await asyncio.sleep(jitter_delay)
    headers = build_base_headers({'Authorization': token, 'msisdn': owner_number})
    fp = get_sticky_fingerprint(f"{owner_number}_slot_{slot_idx}")
    headers['device-id'] = fp['device_id']
    headers['digitalId'] = fp['digital_id']
    headers['x-agent-device'] = fp['device_model']
    json_data = {
        'category': [{'listHierarchyId': 'PackageID', 'value': '523'}, {'listHierarchyId': 'TemplateID', 'value': '47'}, {'listHierarchyId': 'TierID', 'value': '523'}],
        'parts': {
            'characteristicsValue': {'characteristicsValue': [{'characteristicName': 'quotaDist1', 'type': 'percentage', 'value': str(percentage)}]},
            'member': [{'id': [{'schemeName': 'MSISDN', 'value': owner_number}], 'type': 'Owner'}, {'id': [{'schemeName': 'MSISDN', 'value': member_number}], 'type': 'Member'}]
        },
        'type': 'SendInvitation'
    }
    async with sem:
        try:
            async with session.post(
                'https://mobile.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup',
                headers=headers, json=json_data, proxy=PROXY_URL if PROXY_URL else None, timeout=14
            ) as resp:
                return {"success": resp.status in (200, 201), "status_code": resp.status, "source": "mobile"}
        except Exception as e:
            return {"success": False, "status_code": 0, "source": "mobile", "error": str(e)}

async def send_invitation_web_aiohttp(
    session: aiohttp.ClientSession, sem: asyncio.Semaphore,
    owner_number: str, member_number: str, token: str, percentage: int, jitter_delay: float
) -> dict:
    if jitter_delay > 0:
        await asyncio.sleep(jitter_delay)
    headers = {
        'User-Agent': 'Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Mobile Safari/537.36',
        'Accept': 'application/json', 'Content-Type': 'application/json', 'msisdn': owner_number,
        'Accept-Language': 'AR', 'Authorization': token, 'clientId': 'WebsiteConsumer',
        'Origin': 'https://web.vodafone.com.eg', 'Referer': 'https://web.vodafone.com.eg/spa/familySharing'
    }
    json_data = {
        'name': 'FlexFamily', 'type': 'SendInvitation',
        'category': [{'value': '523', 'listHierarchyId': 'PackageID'}, {'value': '47', 'listHierarchyId': 'TemplateID'}, {'value': '523', 'listHierarchyId': 'TierID'}, {'value': 'percentage', 'listHierarchyId': 'familybehavior'}],
        'parts': {
            'member': [{'id': [{'value': owner_number, 'schemeName': 'MSISDN'}], 'type': 'Owner'}, {'id': [{'value': member_number, 'schemeName': 'MSISDN'}], 'type': 'Member'}],
            'characteristicsValue': {'characteristicsValue': [{'characteristicName': 'quotaDist1', 'value': str(percentage), 'type': 'percentage'}]}
        }
    }
    async with sem:
        try:
            async with session.post(
                'https://web.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup',
                headers=headers, json=json_data, proxy=PROXY_URL if PROXY_URL else None, timeout=14
            ) as resp:
                return {"success": resp.status in (200, 201), "status_code": resp.status, "source": "web"}
        except Exception as e:
            return {"success": False, "status_code": 0, "source": "web", "error": str(e)}

async def send_invites_batch(owner_number: str, member_number: str, tokens: List[str], percentage: int, count: int) -> List[dict]:
    if not tokens:
        return []
    session = await get_aiohttp_session()
    # تحديد سقف التوازي اللحظي بـ 16 طلب مع Micro-Jitter ذكي لضرب Race Condition بدون تفعيل WAF Flood Ban
    sem = asyncio.Semaphore(16)
    tasks = []
    half = count // 2
    for i in range(half):
        slot_idx = i % len(tokens)
        token = tokens[slot_idx]
        jitter = (i // 4) * 0.015
        tasks.append(send_invitation_mobile_aiohttp(session, sem, owner_number, member_number, token, percentage, slot_idx, jitter))
    for i in range(half, count):
        slot_idx = i % len(tokens)
        token = tokens[slot_idx]
        jitter = ((i - half) // 4) * 0.015
        tasks.append(send_invitation_web_aiohttp(session, sem, owner_number, member_number, token, percentage, jitter))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    return [{"success": False, "status_code": 0, "error": str(r)} if isinstance(r, Exception) else r for r in results]

def remove_member_sendonly(owner_number: str, token: str, member_number: str) -> bool:
    headers = build_base_headers({'Authorization': token, 'msisdn': owner_number})
    payload = {
        'category': [{'listHierarchyId': 'TemplateID', 'value': '47'}], 'createdBy': {'value': 'MobileApp'},
        'parts': {
            'characteristicsValue': {'characteristicsValue': [{'characteristicName': 'Disconnect', 'value': '0'}, {'characteristicName': 'LastMemberDeletion', 'value': '1'}]},
            'member': [{'id': [{'schemeName': 'MSISDN', 'value': owner_number}], 'type': 'Owner'}, {'id': [{'schemeName': 'MSISDN', 'value': member_number}], 'type': 'Member'}]
        },
        'type': 'FamilyRemoveMember'
    }
    try:
        r = WEB_HTTP_SESSION.patch('https://mobile.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup', headers=headers, json=payload, timeout=15)
        if r.status_code in (200, 201):
            return True
        return HTTP_SESSION.post('https://mobile.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup', headers=headers, json=payload, timeout=15).status_code in (200, 201)
    except Exception:
        return False

# ================== واجهة تعليق دعوتين ==================
async def sendonly_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not await require_subscription_or_free(update, context, is_callback=False):
        return ConversationHandler.END
    keyboard = []
    current = get_user(user_id)
    if current:
        keyboard.append([InlineKeyboardButton(f"📱 استخدام الجلسة الحالية: {current['phone']}", callback_data="sendonly_use_current")])
    for num in get_saved_numbers(user_id):
        if not current or num != current['phone']:
            keyboard.append([InlineKeyboardButton(f"📞 {num}" + (" 🔑" if get_valid_token(user_id, num) else ""), callback_data=f"sendonly_owner_{num}")])
    keyboard.append([InlineKeyboardButton("➕ إدخال رقم جديد يدوياً", callback_data="sendonly_manual")])
    keyboard.append([InlineKeyboardButton("🔙 العودة للقائمة الرئيسية", callback_data="main_menu")])
    text = (
        "🚀 **نظام تعليق الدعوتين الخارق (VIP Anti-Ban)** 🚀\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🛡️ **التحسينات الهندسية الجديدة:**\n"
        "• إعادة استخدام التوكنات (صفر حظر تسجيل دخول)\n"
        "• بصمة جهاز ثابتة وتوزيع ذكي للطلبات (Micro-Jitter)\n"
        "• محاولات تلقائية ذكية حتى تأكيد تعليق الدعوتين\n"
        "━━━━━━━━━━━━━━━━━━━━\n"
        "🔐 **أولاً: اختر رقم المالك:**"
    )
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    return SENDONLY_SELECT_OWNER

async def sendonly_select_owner(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    data, user_id = query.data, query.from_user.id
    if data == "sendonly_use_current":
        current = get_user(user_id)
        if current:
            context.user_data['sendonly_owner'] = current['phone']
            saved_pass = get_saved_password(user_id, current['phone'])
            if saved_pass:
                context.user_data['sendonly_owner_password'] = saved_pass
                await query.edit_message_text(f"✅ تم استخدام الجلسة الحالية للرقم {current['phone']}\n📱 أرسل رقم العضو:", parse_mode=None)
                return SENDONLY_MEMBER_NUM
            await query.edit_message_text(f"🔐 لا توجد كلمة مرور مخزنة للرقم {current['phone']}\nأدخل كلمة المرور:", parse_mode=None)
            return SENDONLY_OWNER_PASSWORD
        await query.edit_message_text("❌ لا توجد جلسة نشطة.", parse_mode=None)
        return await sendonly_menu(update, context)
    elif data.startswith("sendonly_owner_"):
        phone = data[len("sendonly_owner_"):]
        context.user_data['sendonly_owner'] = phone
        saved_pass = get_saved_password(user_id, phone)
        if saved_pass:
            context.user_data['sendonly_owner_password'] = saved_pass
            await query.edit_message_text(f"✅ تم اختيار الرقم {phone}\n📱 أرسل رقم العضو:", parse_mode=None)
            return SENDONLY_MEMBER_NUM
        await query.edit_message_text(f"🔐 أدخل كلمة مرور الرقم {phone}:", parse_mode=None)
        return SENDONLY_OWNER_PASSWORD
    elif data == "sendonly_manual":
        await query.edit_message_text("📱 أرسل رقم المالك (مثال: 01012345678):", parse_mode=None)
        return SENDONLY_OWNER_NUM
    return SENDONLY_SELECT_OWNER

async def sendonly_owner_password(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password, owner, user_id = update.message.text.strip(), context.user_data['sendonly_owner'], update.effective_user.id
    auth_res = await asyncio.to_thread(get_authorization_sendonly, owner, password, 0)
    if auth_res["status"] != "success":
        await update.message.reply_text("❌ فشل تسجيل الدخول.\nحاول مرة أخرى أو اختر رقماً آخر.", parse_mode=None)
        return await sendonly_menu(update, context)
    SENDONLY_TOKEN_POOL[owner] = [(auth_res["token"], auth_res["expiry"])]
    save_password(user_id, owner, password, 72)
    context.user_data['sendonly_owner_password'] = password
    await update.message.reply_text("✅ تم تسجيل الدخول وتأمين البصمة.\n📱 الآن أرسل رقم العضو:", parse_mode=None)
    return SENDONLY_MEMBER_NUM

async def sendonly_owner_num_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    valid, msg = validate_phone(text)
    if not valid:
        await update.message.reply_text(f"{msg}\nأعد إرسال رقم المالك:", parse_mode=None); return SENDONLY_OWNER_NUM
    context.user_data['sendonly_owner'] = text
    await update.message.reply_text("🔑 أدخل كلمة مرور المالك:", parse_mode=None)
    return SENDONLY_OWNER_PASS

async def sendonly_owner_pass_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password, owner, user_id = update.message.text.strip(), context.user_data['sendonly_owner'], update.effective_user.id
    auth_res = await asyncio.to_thread(get_authorization_sendonly, owner, password, 0)
    if auth_res["status"] != "success":
        await update.message.reply_text("❌ فشل تسجيل الدخول.\nحاول مرة أخرى.", parse_mode=None)
        return ConversationHandler.END
    SENDONLY_TOKEN_POOL[owner] = [(auth_res["token"], auth_res["expiry"])]
    save_password(user_id, owner, password, 72)
    context.user_data['sendonly_owner_password'] = password
    await update.message.reply_text("✅ تم تسجيل الدخول وتأمين البصمة.\n📱 الآن أرسل رقم العضو:", parse_mode=None)
    return SENDONLY_MEMBER_NUM

async def sendonly_member_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    valid, msg = validate_phone(text)
    if not valid:
        await update.message.reply_text(f"{msg}\nأعد إرسال رقم العضو:", parse_mode=None); return SENDONLY_MEMBER_NUM
    if text == context.user_data.get('sendonly_owner'):
        await update.message.reply_text("❌ رقم العضو لا يمكن أن يساوي رقم المالك. أرسل رقماً آخر:", parse_mode=None); return SENDONLY_MEMBER_NUM
    context.user_data['sendonly_member'] = text
    await update.message.reply_text("📊 اختر نسبة الدعوة:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("10% (1300 فليكس)", callback_data="sendonly_perc_10")], [InlineKeyboardButton("20% (2600 فليكس)", callback_data="sendonly_perc_20")], [InlineKeyboardButton("40% (5200 فليكس)", callback_data="sendonly_perc_40")]]), parse_mode=None)
    return SENDONLY_PERCENTAGE

async def sendonly_percentage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    context.user_data['sendonly_percentage'] = 10 if query.data == "sendonly_perc_10" else 20 if query.data == "sendonly_perc_20" else 40
    await query.edit_message_text("⚡ اختر قوة الدفعة المتزامنة (يُنصح بـ 10 أو 20 لأعلى سرعة وأمان):", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("10 دعوات (آمن وسريع)", callback_data="sendonly_cnt_10"), InlineKeyboardButton("20 دعوة (موصى به 🔥)", callback_data="sendonly_cnt_20")], [InlineKeyboardButton("30 دعوة", callback_data="sendonly_cnt_30"), InlineKeyboardButton("50 دعوة", callback_data="sendonly_cnt_50")]]), parse_mode=None)
    return SENDONLY_COUNT

async def sendonly_count_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    count = int(query.data.split("_")[-1]); context.user_data['sendonly_count'] = count
    owner_number, owner_password, member_number, percentage = context.user_data['sendonly_owner'], context.user_data.get('sendonly_owner_password'), context.user_data['sendonly_member'], context.user_data['sendonly_percentage']
    if not owner_password:
        await query.edit_message_text("❌ لا توجد كلمة مرور للرقم. أعد استخدام الأمر /sendonly.", parse_mode=None); return ConversationHandler.END
    await query.edit_message_text(f"✅ تم تفعيل الدرع المضاد للحظر (Anti-Ban).\n🚀 جاري بدء إرسال الدعوات للرقم {member_number}...", parse_mode=None)
    await context.bot.send_message(chat_id=query.message.chat_id, text="يمكنك إيقاف العملية في أي وقت بالضغط على الزر أدناه.", reply_markup=ReplyKeyboardMarkup([[KeyboardButton("⏹️ إيقاف العملية")]], resize_keyboard=True))
    asyncio.create_task(run_sendonly_process(update, context, owner_number, owner_password, member_number, percentage, count))
    return ConversationHandler.END

async def run_sendonly_process(update: Update, context: ContextTypes.DEFAULT_TYPE, owner_number: str, owner_password: str, member_number: str, percentage: int, count: int):
    user_id = update.effective_user.id
    stop_event = asyncio.Event(); context.user_data['sendonly_stop_event'] = stop_event
    chat_id = update.callback_query.message.chat_id if update.callback_query else update.message.chat_id
    status_msg = await context.bot.send_message(chat_id=chat_id, text="🔄 جاري تهيئة مخزن التوكنات الذكي...", parse_mode=None)

    async def update_status(text: str):
        try:
            await context.bot.edit_message_text(chat_id=chat_id, message_id=status_msg.message_id, text=text, parse_mode=None)
        except Exception:
            pass

    attempt = 1
    while not stop_event.is_set():
        await update_status(f"🔄 المحاولة رقم {attempt}\n🛡️ فحص وتجهيز التوكنات المحمية...")
        tokens = await get_multiple_tokens_sendonly(owner_number, owner_password, count)
        if not tokens:
            await update_status(f"⚠️ المحاولة {attempt}: تهدئة استباقية لحماية الخط من الحظر.\n⏳ انتظار 10 ثوانٍ...")
            await asyncio.sleep(10); attempt += 1; continue

        await update_status(f"🔄 المحاولة رقم {attempt}\n✅ التوكنات جاهزة ({len(tokens)} جلسات ثابتة)\n📡 إطلاق {count} دعوة متزامنة بنسبة {percentage}%...")
        results = await send_invites_batch(owner_number, member_number, tokens, percentage, count)
        successful = sum(1 for r in results if r.get("success"))
        rate_limited_hits = sum(1 for r in results if r.get("status_code") in (429, 403))

        # إذا ظهر 401 في أي توكن نفرغ الكاش لتجديده في الدورة القادمة
        if any(r.get("status_code") == 401 for r in results):
            SENDONLY_TOKEN_POOL.pop(owner_number, None)

        is_verified = False
        try:
            await asyncio.sleep(1.2)
            member_data = await get_family_members(tokens[0].replace("Bearer ", ""), owner_number) if tokens else {'pending': []}
            pending_for_member = [p for p in member_data.get('pending', []) if p['phone'] == member_number]
            if pending_for_member and pending_for_member[0].get('count', 0) >= 2:
                is_verified = True
        except Exception:
            pass

        if is_verified or (successful >= 2 and is_verified):
            if context and context.user_data is not None:
                rm = context.user_data.setdefault('rocket_members', [])
                if member_number not in rm:
                    rm.append(member_number)
            success_message = (
                f"🎉 تم تعليق الدعوتين بنجاح للرقم {member_number} في المحاولة {attempt}! 🚀\n"
                f"✅ الفحص الفعلي للعائلة أكد وجود دعوتين معلقتين جاهزتين.\n\n"
            )
            for i, r in enumerate(results[:6]):
                src = "🌐 سيرفر ويب" if r.get('source') == 'web' else "📱 سيرفر تطبيق"
                icon = '✅' if r.get('success') else '❌'
                success_message += f"{src} {i+1} ➡️ {icon}\n"
            await update_status(success_message)
            sendonly_stats["total_success"] += 1; sendonly_stats["total_attempts"] += 1
            sendonly_stats["user_logs"].setdefault(str(user_id), {"success": 0, "attempts": 0})["success"] += 1
            sendonly_stats["user_logs"][str(user_id)]["attempts"] += 1
            save_sendonly_stats(); break

        await update_status(f"📊 نتائج المحاولة {attempt}: استجابت {successful}/{count} دعوات.\n🧹 جاري تنظيف الدعوة الفردية وإعادة المحاولة...")
        if tokens:
            for delay in [2.5, 4.0]:
                if stop_event.is_set():
                    break
                await asyncio.sleep(delay)
                if await asyncio.to_thread(remove_member_sendonly, owner_number, tokens[0], member_number):
                    break
                await asyncio.to_thread(cancel_invitation, tokens[0].replace("Bearer ", ""), owner_number, member_number)

        cooldown = 8 if rate_limited_hits > 0 else 4
        await asyncio.sleep(cooldown); attempt += 1

    if stop_event.is_set():
        await update_status("⏹️ تم إيقاف العملية بناءً على طلبك.")
        sendonly_stats["total_attempts"] += 1
        sendonly_stats["user_logs"].setdefault(str(user_id), {"success": 0, "attempts": 0})["attempts"] += 1
        save_sendonly_stats()
    await context.bot.send_message(chat_id=chat_id, text="ℹ️ انتهت عملية تعليق الدعوتين.", reply_markup=ReplyKeyboardRemove(), parse_mode=None)
    context.user_data.pop('sendonly_stop_event', None)

async def stop_sendonly_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stop_event = context.user_data.get('sendonly_stop_event')
    if stop_event:
        stop_event.set()
        await update.message.reply_text("⏹️ تم إرسال طلب إيقاف العملية...", reply_markup=ReplyKeyboardRemove(), parse_mode=None)
    else:
        await update.message.reply_text("ℹ️ لا توجد عملية 'تعليق دعوتين' جارية حالياً.", parse_mode=None)

async def sendonly_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    text = f"📊 إحصائيات تعليق دعوتين\n\n✅ عمليات ناجحة: {sendonly_stats['total_success']}\n🔄 إجمالي المحاولات: {sendonly_stats['total_attempts']}"
    await update.message.reply_text(text, parse_mode=None)

async def reset_sendonly_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    global sendonly_stats; sendonly_stats = {"total_success": 0, "total_attempts": 0, "user_logs": {}}
    save_sendonly_stats(); await update.message.reply_text("✅ تم تصفير إحصائيات تعليق دعوتين.", parse_mode=None)

    # ================== لوحة تحكم الأدمن والرسائل ==================
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id):
        if update.message:
            await update.message.reply_text("⛔ غير مصرح.")
        return
    keyboard = [
        [InlineKeyboardButton("📊 إحصائيات البوت", callback_data="admin_stats")],
        [InlineKeyboardButton("👥 قائمة المستخدمين", callback_data="admin_users")],
        [InlineKeyboardButton("📢 بث رسالة جماعية", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🎟️ إدارة الاشتراكات", callback_data="admin_subs_menu")],
        [InlineKeyboardButton("👑 إدارة الأدمن", callback_data="admin_manage_admins")],
        [InlineKeyboardButton("🔧 إعدادات البوت", callback_data="admin_settings")]
    ]
    text = "👑 **لوحة تحكم الأدمن الرئيسية** 👑\n━━━━━━━━━━━━━━━━━━━━\nأهلاً بك يا هندسة. اختر القسم:"
    if update.callback_query:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def admin_subs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if query:
        await query.answer()
    if not is_admin(query.from_user.id if query else update.effective_user.id):
        return
    
    mode_status = "مجاني 🔓" if is_free_mode() else "مدفوع 🔒"
    text = f"🎟️ إدارة الاشتراكات\n━━━━━━━━━━━━━━━━━━━━\n📌 وضع البوت الحالي: {mode_status}"
    
    keyboard = [
        [InlineKeyboardButton("➕ إضافة اشتراك", callback_data="admin_add_sub")],
        [InlineKeyboardButton("➖ إلغاء اشتراك", callback_data="admin_remove_sub")],
        [InlineKeyboardButton("📋 المشتركون النشطون", callback_data="admin_list_subs")],
        [InlineKeyboardButton(f"⚙️ تبديل الوضع (الحالي: {mode_status})", callback_data="admin_toggle_mode")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="admin_back")]
    ]
    
    if query:
        try:
            await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
        except Exception as e:
            if "Message is not modified" not in str(e):
                raise e
    else:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def admin_manage_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user.id) or query.from_user.id != ADMIN_ID:
        return
    keyboard = [
        [InlineKeyboardButton("➕ إضافة أدمن", callback_data="admin_add_admin")],
        [InlineKeyboardButton("➖ إزالة أدمن", callback_data="admin_remove_admin")],
        [InlineKeyboardButton("📋 قائمة الأدمن", callback_data="admin_list_admins")],
        [InlineKeyboardButton("🔙 رجوع", callback_data="admin_back")]
    ]
    await query.edit_message_text("👑 **إدارة الأدمن**", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def admin_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user.id):
        return
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="admin_back")]]
    await query.edit_message_text("🔧 **إعدادات البوت**\n🛡️ نظام Anti-Ban: مفعل\n🕛 جدولة 12:01 ص: مفعلة", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def admin_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user.id):
        return
    st = get_stats()
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="admin_back")]]
    text = (
        f"📊 إحصائيات البوت الشاملة\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 إجمالي المستخدمين: {st['total_users']}\n"
        f"🔐 جلسات نشطة حالياً: {st['active_sessions']}\n"
        f"📞 أرقام محفوظة: {st['saved_numbers']}\n"
        f"🎟️ اشتراكات نشطة: {st['active_subs']}\n"
        f"🕛 حجوزات خصم فليكس (12:01 ص): {st['scheduled_cnt']}\n"
        f"⚙️ وضع التشغيل: {'مجاني 🔓' if is_free_mode() else 'مدفوع 🔒'}"
    )
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_users_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user.id):
        return
    users = get_all_users()
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="admin_back")]]
    text = f"👥 إجمالي المستخدمين المسجلين: {len(users)}"
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user.id):
        return
    context.user_data['awaiting_broadcast'] = True
    await query.edit_message_text("📢 أرسل الرسالة التي تريد بثها لجميع المستخدمين:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 إلغاء", callback_data="admin_back")]]))

async def admin_toggle_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if not is_admin(query.from_user.id):
        return
    current = is_free_mode()
    set_free_mode(not current)
    
    # رسالة نجاح مؤقتة تظهر للمشرف فوراً
    new_mode_text = 'مجاني 🔓' if not current else 'مدفوع 🔒'
    await query.answer(f"✅ تم التبديل إلى الوضع: {new_mode_text}", show_alert=True)
    
    # إعادة تحميل قائمة الاشتراكات مع محاولة آمنة لمنع خطأ Message is not modified
    try:
        await admin_subs_menu(update, context)
    except Exception as e:
        if "Message is not modified" not in str(e):
            logger.error(f"Toggle mode UI error: {e}")

async def admin_add_sub_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user.id):
        return
    context.user_data['awaiting_add_sub'] = True
    await query.edit_message_text("➕ أرسل معرف المستخدم وعدد الأيام (مثال: `123456789 30`):", parse_mode='Markdown')

async def admin_remove_sub_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user.id):
        return
    context.user_data['awaiting_remove_sub'] = True
    await query.edit_message_text("➖ أرسل معرف المستخدم لإلغاء اشتراكه:", parse_mode='Markdown')

async def admin_list_subs_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user.id):
        return
    subs = list_active_subscriptions()
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="admin_subs_menu")]]
    text = f"✅ المشتركون النشطون حالياً: {len(subs)}"
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_add_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user.id) or query.from_user.id != ADMIN_ID:
        return
    context.user_data['awaiting_add_admin'] = True
    await query.edit_message_text("➕ أرسل معرف المستخدم لإضافته كأدمن مساعد:", parse_mode='Markdown')

async def admin_remove_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user.id) or query.from_user.id != ADMIN_ID:
        return
    context.user_data['awaiting_remove_admin'] = True
    await query.edit_message_text("➖ أرسل معرف المستخدم لإزالة صلاحيات الأدمن:", parse_mode='Markdown')

async def admin_list_admins_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user.id):
        return
    admins = list_admins()
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="admin_manage_admins")]]
    text = "👑 قائمة الأدمن:\n" + "\n".join(f"👤 {aid}" for aid in admins)
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

async def admin_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user.id):
        return
    context.user_data.pop('awaiting_broadcast', None)
    context.user_data.pop('awaiting_add_sub', None)
    context.user_data.pop('awaiting_remove_sub', None)
    context.user_data.pop('awaiting_add_admin', None)
    context.user_data.pop('awaiting_remove_admin', None)
    await admin_panel(update, context)

# ================== أوامر نصية وإدارة عامة ==================
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in user_states:
        del user_states[user_id]
    context.user_data.clear()
    await update.message.reply_text("✅ تم إلغاء العملية الحالية.", parse_mode=None)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔹 الأوامر المتاحة:\n"
        "/start - بدء البوت ولوحة التحكم\n"
        "/savenumber <رقم> - حفظ رقم في قائمتك\n"
        "/sendonly - تعليق دعوتين (VIP Anti-Ban)\n"
        "/cancel - إلغاء العملية الحالية",
        parse_mode=None
    )

async def save_number_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if len(context.args) != 1:
        await update.message.reply_text("الاستخدام: /savenumber <رقم الهاتف>", parse_mode=None); return
    number = context.args[0].strip()
    valid, msg = validate_phone(number)
    if not valid:
        await update.message.reply_text(msg, parse_mode=None); return
    save_number(user_id, number)
    await update.message.reply_text(f"✅ تم حفظ الرقم {number}.", parse_mode=None)

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if MAINTENANCE_MODE and not is_admin(user_id):
        await update.message.reply_text("🛠️ البوت في وضع الصيانة حالياً.", parse_mode=None); return
    record_user_activity(user_id)
    if is_user_banned(user_id):
        await update.message.reply_text("🚫 أنت محظور من استخدام البوت.", parse_mode=None); return
    if get_user(user_id):
        await show_main_menu(update, user_id, context)
    else:
        tg_name = update.effective_user.first_name if update.effective_user and update.effective_user.first_name else "بطل"
        await send_message(update, user_id, f"🐋 حــوت الـمـجـال | HOOT ALMGAL\n\n⚡ أهلاً بيك يا أستاذ {tg_name} في لوحة التحكم الذكية\n🔐 سجّل دخولك للبدء:")
        await show_login_screen(update, user_id, context)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if MAINTENANCE_MODE and not is_admin(user_id):
        return
    if is_user_banned(user_id):
        return
    record_user_activity(user_id)
    text = update.message.text.strip()

    # معالجة أوامر الأدمن التفاعلية
    if context.user_data.get('awaiting_broadcast'):
        if not is_admin(user_id):
            context.user_data.pop('awaiting_broadcast', None); return
        sent = 0
        for uid, _, _ in get_all_users():
            try:
                await context.bot.send_message(chat_id=uid, text=f"📢 إشعار من الإدارة:\n{text}", parse_mode=None)
                sent += 1
                await asyncio.sleep(0.04)  # حماية البوت من حظر Flood تليجرام أثناء الإذاعة
            except Exception:
                pass
        await update.message.reply_text(f"✅ تم إرسال الإشعار إلى {sent} مستخدم.", parse_mode=None)
        context.user_data.pop('awaiting_broadcast', None); return

    if context.user_data.get('awaiting_add_sub'):
        if not is_admin(user_id):
            context.user_data.pop('awaiting_add_sub', None); return
        parts = text.split()
        if len(parts) != 2:
            await update.message.reply_text("⚠️ أرسل المعرف والأيام مسافة بينهما."); return
        try:
            exp = add_subscription(int(parts[0]), int(parts[1]))
            await update.message.reply_text(f"✅ تم إضافة اشتراك للمستخدم {parts[0]} ينتهي في {exp.strftime('%Y-%m-%d')}")
        except Exception:
            await update.message.reply_text("❌ بيانات غير صالحة.")
        context.user_data.pop('awaiting_add_sub', None); return

    if context.user_data.get('awaiting_remove_sub'):
        if not is_admin(user_id):
            context.user_data.pop('awaiting_remove_sub', None); return
        try:
            uid = int(text)
            res = remove_subscription(uid)
            await update.message.reply_text(f"✅ تم إلغاء اشتراك {uid}" if res else f"⚠️ لا يوجد اشتراك نشط لـ {uid}")
        except Exception:
            await update.message.reply_text("❌ معرف غير صالح.")
        context.user_data.pop('awaiting_remove_sub', None); return

    if context.user_data.get('awaiting_add_admin'):
        if user_id != ADMIN_ID:
            context.user_data.pop('awaiting_add_admin', None); return
        try:
            new_adm = int(text)
            ok = add_admin(new_adm)
            await update.message.reply_text(f"✅ تم ترقية {new_adm} إلى أدمن." if ok else "⚠️ المستخدم أدمن بالفعل.")
        except Exception:
            await update.message.reply_text("❌ معرف غير صالح.")
        context.user_data.pop('awaiting_add_admin', None); return

    if context.user_data.get('awaiting_remove_admin'):
        if user_id != ADMIN_ID:
            context.user_data.pop('awaiting_remove_admin', None); return
        try:
            rem_adm = int(text)
            ok = remove_admin(rem_adm)
            await update.message.reply_text(f"✅ تم إزالة الأدمن {rem_adm}." if ok else "⚠️ لا يمكن إزالة هذا المعرف.")
        except Exception:
            await update.message.reply_text("❌ معرف غير صالح.")
        context.user_data.pop('awaiting_remove_admin', None); return

    if user_id not in user_states:
        await show_login_screen(update, user_id, context); return
    state_data = user_states[user_id]
    state = state_data.get('state')

    if state == 'add_two_days_member_password':
        member_phone, password = state_data.get('member_phone'), text
        try:
            member_token, expiry = await login_vodafone(member_phone, password)
            save_token(user_id, member_phone, member_token, expiry)
            save_password(user_id, member_phone, password, 72)
        except Exception:
            user_states.pop(user_id, None)
            await update.message.reply_text("❌ فشل تسجيل الدخول للرقم. راجع كلمة المرور.", parse_mode=None)
            return
        user_states.pop(user_id, None)
        await add_two_days(update, user_id, context, target_phone=member_phone)
        return

    if state == 'login_phone':
        valid, msg = validate_phone(text)
        if not valid:
            await update.message.reply_text(f"{msg}\nأرسل رقم الهاتف الصحيح:")
            return
        user_states[user_id] = {'state': 'login_password', 'phone': text}
        await update.message.reply_text("🔑 أدخل كلمة المرور:", parse_mode=None)

    elif state == 'login_password':
        phone, password = state_data.get('phone'), text
        await update.message.reply_text("🔄 جاري تسجيل الدخول والتحقق من بيانات الحساب...", parse_mode=None)
        try:
            token, expiry = await login_vodafone(phone, password)
            fallback_name = update.effective_user.first_name if update.effective_user and update.effective_user.first_name else "بطل"
            acc_name = await fetch_vodafone_customer_name(token, phone, fallback_name)
            save_user(user_id, phone, token, expiry, acc_name)
            save_password(user_id, phone, password, 72)
            await update.message.reply_text(f"✅ أهلاً بيك يا أستاذ {acc_name}، تم تسجيل الدخول بنجاح 🎉", parse_mode=None)
            del user_states[user_id]
            await show_main_menu(update, user_id, context)
        except Exception as e:
            await update.message.reply_text(f"❌ فشل تسجيل الدخول: {str(e)}", parse_mode=None)
            del user_states[user_id]

    elif state == 'accept_password':
        member_phone, password = state_data.get('member_phone'), text
        owner = get_user(user_id)
        if not owner:
            await show_login_screen(update, user_id, context); del user_states[user_id]; return
        try:
            member_token, exp = await login_vodafone(member_phone, password)
            save_token(user_id, member_phone, member_token, exp)
            save_password(user_id, member_phone, password, 72)
            if await accept_invitation(member_token, member_phone, owner['phone']):
                await update.message.reply_text(f"✅ تم قبول الدعوة بنجاح للرقم {member_phone}", parse_mode=None)
                await show_family_screen(update, user_id, context)
            else:
                await update.message.reply_text("❌ فشل قبول الدعوة", parse_mode=None)
            del user_states[user_id]
        except Exception as e:
            await update.message.reply_text(f"❌ خطأ: {str(e)}", parse_mode=None); del user_states[user_id]

    elif state == 'invite_phone':
        member_phone, percentage = text, state_data.get('percentage')
        valid, msg = validate_phone(member_phone)
        if not valid:
            await update.message.reply_text(f"{msg}\nأعد إرسال رقم العضو الصحيح:")
            return
        owner = get_user(user_id)
        if not owner:
            await show_login_screen(update, user_id, context); del user_states[user_id]; return
        if await send_invitation(owner['access_token'], owner['phone'], member_phone, percentage):
            await update.message.reply_text(f"✅ تم إرسال الدعوة إلى {member_phone}", parse_mode=None)
        else:
            await update.message.reply_text("❌ فشل إرسال الدعوة", parse_mode=None)
        del user_states[user_id]
        await show_family_screen(update, user_id, context)

    elif state == 'switch_account_ask':
        new_phone, password = state_data.get('new_phone'), text
        try:
            token, expiry = await login_vodafone(new_phone, password)
            fallback_name = update.effective_user.first_name if update.effective_user and update.effective_user.first_name else "بطل"
            acc_name = await fetch_vodafone_customer_name(token, new_phone, fallback_name)
            save_user(user_id, new_phone, token, expiry, acc_name)
            save_password(user_id, new_phone, password, 72)
            await update.message.reply_text(f"✅ أهلاً بيك يا أستاذ {acc_name}، تم التبديل إلى الرقم {new_phone} بنجاح 🎉", parse_mode=None)
            del user_states[user_id]
            await show_main_menu(update, user_id, context)
        except Exception as e:
            await update.message.reply_text(f"❌ فشل التبديل: {str(e)}", parse_mode=None); del user_states[user_id]


    else:
        await send_message(update, user_id, "حدث خطأ. استخدم /start للبدء من جديد.")
        if user_id in user_states:
            del user_states[user_id]

# ================== معالج الأزرار التفاعلية ==================
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    data, user_id = query.data, query.from_user.id
    if MAINTENANCE_MODE and not is_admin(user_id):
        await query.edit_message_text("🛠️ البوت في وضع الصيانة.", parse_mode=None); return
    if is_user_banned(user_id):
        await query.edit_message_text("🚫 محظور.", parse_mode=None); return
    record_user_activity(user_id)

    if data.startswith("admin_"):
        if not is_admin(user_id):
            return
        if data == "admin_stats": await admin_stats_callback(update, context)
        elif data == "admin_users": await admin_users_callback(update, context)
        elif data == "admin_broadcast": await admin_broadcast_start(update, context)
        elif data == "admin_subs_menu": await admin_subs_menu(update, context)
        elif data == "admin_toggle_mode": await admin_toggle_mode_callback(update, context)
        elif data == "admin_add_sub": await admin_add_sub_start(update, context)
        elif data == "admin_remove_sub": await admin_remove_sub_start(update, context)
        elif data == "admin_list_subs": await admin_list_subs_callback(update, context)
        elif data == "admin_manage_admins": await admin_manage_admins(update, context)
        elif data == "admin_add_admin": await admin_add_admin_start(update, context)
        elif data == "admin_remove_admin": await admin_remove_admin_start(update, context)
        elif data == "admin_list_admins": await admin_list_admins_callback(update, context)
        elif data == "admin_settings": await admin_settings(update, context)
        elif data == "admin_back": await admin_back(update, context)
        return

    if data == "another_account":
        # إنهاء الجلسة النشطة فقط مع الإبقاء على الأرقام والتوكنات المحفوظة.
        delete_user(user_id)
        user_states.pop(user_id, None)
        for key in ("rocket_members", "flex_discount_offer", "flex_discount_token", "packages_pending_flex", "packages_convert14_confirm"):
            context.user_data.pop(key, None)
        # لا تحذف رسالة الزر الحالية قبل تعديلها؛ امسح بقية الرسائل فقط.
        current_message_id = query.message.message_id if query.message else None
        if current_message_id is not None:
            user_messages[user_id] = [mid for mid in user_messages.get(user_id, []) if mid != current_message_id]
        await clear_previous_messages(update, user_id)
        await query.edit_message_text("✅ تم تسجيل الخروج من الحساب الحالي. بيانات الأرقام المحفوظة ما زالت موجودة.", parse_mode=None)
        await show_login_screen(update, user_id, context)
        return

    if data == "main_menu":
        context.user_data.pop('flex_discount_offer', None)
        context.user_data.pop('flex_discount_token', None)
        await show_main_menu(update, user_id, context); return

    if data == "refresh_family":
        try:
            await query.delete_message()
        except Exception:
            pass
        await clear_previous_messages(update, user_id)
        await show_family_screen(update, user_id, context); return

    if data == "internet_packages_menu":
        await show_internet_packages_menu(update, user_id, context)
        return

    if data in {"internet_extreme_info", "internet_plus_info", "internet_report_info", "internet_cancel_info"}:
        if not await require_subscription_or_free(update, context, is_callback=True, query=query):
            return
        user = await ensure_valid_session(update, user_id)
        if not user:
            await show_login_screen(update, user_id, context)
            return
        section = {
            "internet_extreme_info": "extreme",
            "internet_plus_info": "plus",
            "internet_report_info": "report",
            "internet_cancel_info": "cancel",
        }[data]
        await show_internet_package_info(update, user_id, section)
        return

    if data == "show_consumption":
        await show_consumption(update, user_id, context); return

    if data == "new_manual_login":
        user_states[user_id] = {'state': 'login_phone'}
        await query.edit_message_text("📱 أرسل رقم هاتفك (مثال: 01012345678):", parse_mode=None); return

    if data.startswith("use_num_"):
        num = data[8:]
        if num not in get_saved_numbers(user_id):
            await query.answer("❌ هذا الرقم غير موجود ضمن أرقامك المحفوظة.", show_alert=True)
            return
        token = get_valid_token(user_id, num)
        exp_dt = None
        if token:
            with get_db_connection() as conn:
                row = conn.execute("SELECT expiry FROM saved_tokens WHERE user_id = ? AND number = ?", (user_id, num)).fetchone()
            exp_dt = datetime.fromisoformat(row[0]) if row else now_cairo() + timedelta(hours=1)
        else:
            saved_pass = get_saved_password(user_id, num)
            if saved_pass:
                try:
                    token, exp_dt = await login_vodafone(num, saved_pass)
                except Exception:
                    token = None

        if token and exp_dt:
            acc_name = get_saved_account_name(user_id, num)
            if not acc_name:
                fallback_name = query.from_user.first_name if query.from_user and query.from_user.first_name else "بطل"
                acc_name = await fetch_vodafone_customer_name(token, num, fallback_name)
            save_user(user_id, num, token, exp_dt, acc_name)
            await query.edit_message_text(f"✅ أهلاً بيك يا أستاذ {acc_name}، تم تسجيل الدخول للرقم {num} بنجاح 🎉", parse_mode=None)
            await show_main_menu(update, user_id, context)
        else:
            user_states[user_id] = {'state': 'switch_account_ask', 'new_phone': num}
            await query.edit_message_text(f"🔐 أدخل كلمة مرور الرقم {num}:", parse_mode=None)
        return

    if data.startswith("del_num_"):
        num = data[8:]
        delete_saved_number(user_id, num)
        with get_db_connection() as conn:
            conn.execute("DELETE FROM saved_tokens WHERE user_id = ? AND number = ?", (user_id, num))
            conn.commit()
        await query.edit_message_text(f"✅ تم حذف الرقم {num}", parse_mode=None)
        await show_saved_numbers(update, user_id, context); return

    if data == "confirm_yes":
        confirm_data = user_states.get(user_id, {})
        action = confirm_data.get('confirm_action')
        user = await ensure_valid_session(update, user_id)
        if not user:
            await query.edit_message_text("الرجاء تسجيل الدخول أولاً.", parse_mode=None); return
        if action == 'edit':
            if await update_quota(user['access_token'], user['phone'], confirm_data.get('member_phone'), confirm_data.get('percent')):
                await query.answer("✅ تم التعديل", show_alert=True); await show_family_screen(update, user_id, context)
            else:
                await query.answer("❌ فشل التعديل", show_alert=True)
            del user_states[user_id]
        elif action == 'delete':
            if await remove_member(user['access_token'], user['phone'], confirm_data.get('member_phone')):
                await query.answer("✅ تم الحذف", show_alert=True); await show_family_screen(update, user_id, context)
            else:
                await query.answer("❌ فشل الحذف", show_alert=True)
            del user_states[user_id]
        elif action == 'cancel_invite':
            if await cancel_invitation(user['access_token'], user['phone'], confirm_data.get('member_phone')):
                await query.answer("✅ تم الإلغاء", show_alert=True); await show_family_screen(update, user_id, context)
            else:
                await query.answer("❌ فشل الإلغاء", show_alert=True)
            del user_states[user_id]
        return

    if data == "confirm_no":
        if user_id in user_states:
            del user_states[user_id]
        await query.answer("❌ تم الإلغاء", show_alert=True)
        await show_family_screen(update, user_id, context); return

    if data == "sendonly_menu":
        if not await require_subscription_or_free(update, context, is_callback=True, query=query):
            return
        await sendonly_menu(update, context); return

    if data == "packages_services":
        if not await require_subscription_or_free(update, context, is_callback=True, query=query):
            return
        await packages_services_menu(update, user_id, context); return

    if data == "packages_flex":
        if not await require_subscription_or_free(update, context, is_callback=True, query=query):
            return
        await flex_bundles_menu(update, user_id, context); return

    if data == "packages_ignore":
        return

    if data.startswith("packages_flex_confirm_"):
        if not await require_subscription_or_free(update, context, is_callback=True, query=query):
            return
        await execute_flex_bundle_service(update, user_id, context, data.rsplit("_", 1)[-1]); return

    if data.startswith("packages_flex_"):
        if not await require_subscription_or_free(update, context, is_callback=True, query=query):
            return
        await flex_bundle_confirm_screen(update, user_id, context, data.rsplit("_", 1)[-1]); return

    if data == "packages_convert14":
        if not await require_subscription_or_free(update, context, is_callback=True, query=query):
            return
        await convert14_confirm_screen(update, user_id, context); return

    if data == "packages_convert14_confirm":
        if not await require_subscription_or_free(update, context, is_callback=True, query=query):
            return
        await execute_convert14_service(update, user_id, context); return

    # معالجات خصم فليكس (التفعيل اللحظي + حجز 12:01 منتصف الليل)
    if data == "flex_discount":
        if not await require_subscription_or_free(update, context, is_callback=True, query=query):
            return
        await flex_discount_menu(update, user_id, context); return

    if data == "flex_discount_confirm":
        if not await require_subscription_or_free(update, context, is_callback=True, query=query):
            return
        await flex_discount_confirm(update, user_id, context); return

    if data == "flex_discount_schedule":
        if not await require_subscription_or_free(update, context, is_callback=True, query=query):
            return
        await flex_discount_schedule_handler(update, user_id, context); return

    if data == "flex_discount_cancel_schedule":
        if not await require_subscription_or_free(update, context, is_callback=True, query=query):
            return
        await flex_discount_cancel_schedule_handler(update, user_id, context); return

    if data == "flex_discount_cancel":
        context.user_data.pop('flex_discount_offer', None)
        context.user_data.pop('flex_discount_token', None)
        await query.answer("❌ تم الإلغاء")
        await show_main_menu(update, user_id, context); return

    if data == "add_two_days":
        if not await require_subscription_or_free(update, context, is_callback=True, query=query):
            return
        await add_two_days(update, user_id, context); return

    if data.startswith("add_two_days_member_"):
        if not await require_subscription_or_free(update, context, is_callback=True, query=query):
            return
        member_phone = data[len("add_two_days_member_"):]
        if not re.fullmatch(r"01\d{9}", member_phone):
            await query.answer("❌ رقم غير صالح", show_alert=True); return
        if get_valid_token(user_id, member_phone) or get_saved_password(user_id, member_phone):
            await add_two_days(update, user_id, context, target_phone=member_phone)
            return
        user_states[user_id] = {'state': 'add_two_days_member_password', 'member_phone': member_phone}
        await query.edit_message_text(f"🔐 أدخل كلمة مرور الفرد {member_phone}:", parse_mode=None); return

    if not await require_subscription_or_free(update, context, is_callback=True, query=query):
        return
    user = await ensure_valid_session(update, user_id)
    if not user:
        await query.edit_message_text("الرجاء تسجيل الدخول أولاً.", parse_mode=None); return

    if data == "show_family":
        await show_family_screen(update, user_id, context)
    elif data == "show_owner_remaining":
        await show_owner_remaining(update, user_id, context)
    elif data == "show_saved":
        await show_saved_numbers(update, user_id, context)
    elif data.startswith("edit_"):
        parts = data.split("_")
        if len(parts) == 3:
            member_phone, new_flex = parts[1], parts[2]
            percent = 10 if new_flex == "1300" else 20 if new_flex == "2600" else 40
            user_states[user_id] = {'confirm_action': 'edit', 'member_phone': member_phone, 'new_flex': new_flex, 'percent': percent}
            await query.edit_message_text(
                f"⚠️ تأكيد تعديل نسبة {member_phone} إلى {new_flex} فليكس؟",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ نعم", callback_data="confirm_yes"), InlineKeyboardButton("❌ لا", callback_data="confirm_no")]]),
                parse_mode=None
            )
    elif data.startswith("del_"):
        user_states[user_id] = {'confirm_action': 'delete', 'member_phone': data[4:]}
        await query.edit_message_text(
            f"⚠️ تأكيد حذف الفرد {data[4:]}؟",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ نعم", callback_data="confirm_yes"), InlineKeyboardButton("❌ لا", callback_data="confirm_no")]]),
            parse_mode=None
        )
    elif data.startswith("cancel_"):
        user_states[user_id] = {'confirm_action': 'cancel_invite', 'member_phone': data[7:]}
        await query.edit_message_text(
            f"⚠️ تأكيد إلغاء دعوة {data[7:]}؟",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ نعم", callback_data="confirm_yes"), InlineKeyboardButton("❌ لا", callback_data="confirm_no")]]),
            parse_mode=None
        )
    elif data.startswith("accept_"):
        member_phone = data[7:]
        # إذا كان للعضو توكن صالح أو باسورد محفوظ، يقبل الدعوة فوراً بدون طلب الباسورد مرة ثانية
        member_tok = get_valid_token(user_id, member_phone)
        if not member_tok:
            saved_mem_pass = get_saved_password(user_id, member_phone)
            if saved_mem_pass:
                try:
                    member_tok, exp = await login_vodafone(member_phone, saved_mem_pass)
                    save_token(user_id, member_phone, member_tok, exp)
                except Exception:
                    member_tok = None
        if member_tok:
            if await accept_invitation(member_tok, member_phone, user['phone']):
                await query.answer(f"✅ تم قبول الدعوة للرقم {member_phone} تلقائياً!", show_alert=True)
                await show_family_screen(update, user_id, context)
                return
        user_states[user_id] = {'state': 'accept_password', 'member_phone': member_phone}
        await query.edit_message_text(f"🔐 أدخل كلمة مرور العضو {member_phone}:", parse_mode=None)
    elif data.startswith("invite_"):
        user_states[user_id] = {'state': 'invite_phone', 'percentage': int(data.split("_")[1])}
        await query.edit_message_text("📞 أدخل رقم العضو المراد دعوته:", parse_mode=None)

# ================== تشغيل البوت ومحرك الجدولة والضغط العالي ==================
async def post_init(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "بدء البوت ولوحة التحكم"),
        BotCommand("savenumber", "حفظ رقم"),
        BotCommand("sendonly", "تعليق دعوتين (VIP)"),
        BotCommand("cancel", "إلغاء العملية"),
        BotCommand("help", "مساعدة"),
        BotCommand("admin", "لوحة الأدمن")
    ])
    # إطلاق محرك جدولة خصم فليكس (12:01 منتصف الليل) في الخلفية
    asyncio.create_task(midnight_discount_scheduler_loop(app))

async def post_shutdown(app: Application):
    global GLOBAL_AIOHTTP_SESSION
    if GLOBAL_AIOHTTP_SESSION and not GLOBAL_AIOHTTP_SESSION.closed:
        await GLOBAL_AIOHTTP_SESSION.close()

def main():
    if not BOT_TOKEN:
        raise RuntimeError("لم يتم ضبط BOT_TOKEN. أضف توكنًا جديدًا في متغيرات البيئة بعد إلغاء التوكن المكشوف.")
    init_db()
    # تفعيل concurrent_updates لتحمل الضغط العالي جداً بدون أي تهنيج
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )
    sendonly_conv = ConversationHandler(
        entry_points=[CommandHandler('sendonly', sendonly_menu), CallbackQueryHandler(sendonly_menu, pattern='^sendonly_menu$')],
        states={
            SENDONLY_SELECT_OWNER: [CallbackQueryHandler(sendonly_select_owner, pattern='^(sendonly_use_current|sendonly_owner_|sendonly_manual)$')],
            SENDONLY_OWNER_PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, sendonly_owner_password)],
            SENDONLY_OWNER_NUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, sendonly_owner_num_manual)],
            SENDONLY_OWNER_PASS: [MessageHandler(filters.TEXT & ~filters.COMMAND, sendonly_owner_pass_manual)],
            SENDONLY_MEMBER_NUM: [MessageHandler(filters.TEXT & ~filters.COMMAND, sendonly_member_number)],
            SENDONLY_PERCENTAGE: [CallbackQueryHandler(sendonly_percentage_callback, pattern='^sendonly_perc_')],
            SENDONLY_COUNT: [CallbackQueryHandler(sendonly_count_callback, pattern='^sendonly_cnt_')],
        },
        fallbacks=[CommandHandler('cancel', cancel_command)],
        allow_reentry=True
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("savenumber", save_number_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(CommandHandler("sendonly_stats", sendonly_stats_command))
    app.add_handler(CommandHandler("reset_sendonly_stats", reset_sendonly_stats))
    app.add_handler(sendonly_conv)
    app.add_handler(MessageHandler(filters.Text("⏹️ إيقاف العملية"), stop_sendonly_process))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))
    print("✅ البوت يعمل بنجاح - النسخة المطورة (Midnight Scheduler + Concurrent Updates)")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
