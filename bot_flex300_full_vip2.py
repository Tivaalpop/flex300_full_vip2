import asyncio
import json
import logging
import os
import random
import sqlite3
import string
import threading
import base64
import concurrent.futures
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
from collections import defaultdict

import aiohttp
import requests
from requests.adapters import HTTPAdapter

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup, ReplyKeyboardRemove, BotCommand
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes, ConversationHandler
import re

# ================== طبقة تسريع واستقرار الشبكة ==================
_HTTP_ADAPTER = HTTPAdapter(pool_connections=50, pool_maxsize=50, max_retries=0)
HTTP_SESSION = requests.Session()
HTTP_SESSION.mount("https://", _HTTP_ADAPTER)
HTTP_SESSION.mount("http://", _HTTP_ADAPTER)

WEB_HTTP_SESSION = requests.Session()
WEB_HTTP_SESSION.mount("https://", _HTTP_ADAPTER)
WEB_HTTP_SESSION.mount("http://", _HTTP_ADAPTER)

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
BOT_TOKEN = "8750833576:AAFkRvSD4PBiEgnYk6rWSCv1PHfUqgV9U8c"
ADMIN_ID = 1444139300  
DB_PATH = Path("family_bot.db")
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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', handlers=[logging.FileHandler(LOG_FILE, encoding='utf-8'), logging.StreamHandler()])
logger = logging.getLogger("family_bot")

def log_action(user_id: int, action: str, details: str = "") -> None:
    logger.info(f"User {user_id}: {action} - {details}")

def admin_log(action: str, details: str = "") -> None:
    logger.info(f"ADMIN: {action} - {details}")

# ================== دوال مساعدة عامة ==================
def generate_digital_id() -> str: return ''.join(random.choices(string.ascii_uppercase + string.digits, k=13))
def generate_device_id() -> str: return ''.join(random.choices('0123456789abcdef', k=16))
def get_random_device_info() -> Tuple[str, str]: return random.choice(["Samsung SM-A515F", "OPPO CPH2473", "Xiaomi M2101K7AG", "Infinix X688B"]), random.choice(["12", "13", "14"])
def convert_to_local(phone: str) -> str: return "0" + phone[2:] if phone.startswith("20") and len(phone) == 12 else phone

def translate_system(system_name: Optional[str]) -> str:
    if not system_name: return "غير محدد"
    for key, value in SYSTEM_TRANSLATIONS.items():
        if key in system_name: return value
    return system_name

def validate_phone(number: str) -> Tuple[bool, str]:
    if not number.isdigit(): return False, "❌ الرقم يجب أن يحتوي على أرقام فقط"
    if len(number) != 11: return False, "❌ الرقم يجب أن يكون 11 رقماً"
    if not number.startswith('01'): return False, "❌ الرقم يجب أن يبدأ بـ 01"
    return True, "✅"

# ================== تشفير وإدارة كلمات المرور ==================
def encrypt_password(password: str) -> str: return base64.b64encode(password.encode()).decode()
def decrypt_password(encrypted: str) -> str: return base64.b64decode(encrypted.encode()).decode()

def save_password(user_id: int, number: str, password: str, expiry_hours: int = 24):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS saved_passwords (telegram_id INTEGER, number TEXT, password_encrypted TEXT, expiry TIMESTAMP, PRIMARY KEY (telegram_id, number))''')
    expiry = datetime.now() + timedelta(hours=expiry_hours); encrypted = encrypt_password(password)
    c.execute("INSERT OR REPLACE INTO saved_passwords (telegram_id, number, password_encrypted, expiry) VALUES (?, ?, ?, ?)", (user_id, number, encrypted, expiry.isoformat()))
    conn.commit(); conn.close()

def get_saved_password(user_id: int, number: str) -> Optional[str]:
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT password_encrypted, expiry FROM saved_passwords WHERE telegram_id = ? AND number = ?", (user_id, number)); row = c.fetchone(); conn.close()
    if row:
        encrypted, expiry_str = row
        try:
            if datetime.fromisoformat(expiry_str) > datetime.now(): return decrypt_password(encrypted)
        except: delete_saved_password(user_id, number); return None
    return None

def delete_saved_password(user_id: int, number: str):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("DELETE FROM saved_passwords WHERE telegram_id = ? AND number = ?", (user_id, number)); conn.commit(); conn.close()

# ================== قاعدة البيانات ==================
def init_db() -> None:
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (telegram_id INTEGER PRIMARY KEY, phone TEXT, access_token TEXT, token_expiry TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS saved_numbers (telegram_id INTEGER, number TEXT, PRIMARY KEY (telegram_id, number))''')
    c.execute('''CREATE TABLE IF NOT EXISTS banned_users (telegram_id INTEGER PRIMARY KEY)''')
    c.execute('''CREATE TABLE IF NOT EXISTS user_activity (telegram_id INTEGER PRIMARY KEY, first_seen TIMESTAMP, last_seen TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS saved_tokens (user_id INTEGER, number TEXT, token TEXT, expiry TIMESTAMP, PRIMARY KEY (user_id, number))''')
    c.execute('''CREATE TABLE IF NOT EXISTS saved_passwords (telegram_id INTEGER, number TEXT, password_encrypted TEXT, expiry TIMESTAMP, PRIMARY KEY (telegram_id, number))''')
    c.execute('''CREATE TABLE IF NOT EXISTS admins (telegram_id INTEGER PRIMARY KEY)''')
    c.execute('''CREATE TABLE IF NOT EXISTS subscriptions (telegram_id INTEGER PRIMARY KEY, expiry TIMESTAMP)''')
    c.execute('''CREATE TABLE IF NOT EXISTS bot_config (key TEXT PRIMARY KEY, value TEXT)''')
    conn.commit()
    c.execute("INSERT OR IGNORE INTO admins (telegram_id) VALUES (?)", (ADMIN_ID,))
    c.execute("INSERT OR IGNORE INTO bot_config (key, value) VALUES ('free_mode', '1')")
    conn.commit(); conn.close()

init_db()

# ================== دوال الأدمن والاشتراكات ==================
def is_admin(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT 1 FROM admins WHERE telegram_id = ?", (user_id,)); row = c.fetchone(); conn.close()
    return row is not None

def add_admin(user_id: int) -> bool:
    if is_admin(user_id): return False
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("INSERT INTO admins (telegram_id) VALUES (?)", (user_id,)); conn.commit(); conn.close()
    return True

def remove_admin(user_id: int) -> bool:
    if user_id == ADMIN_ID: return False
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("DELETE FROM admins WHERE telegram_id = ?", (user_id,)); affected = c.rowcount; conn.commit(); conn.close()
    return affected > 0

def list_admins() -> List[int]:
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT telegram_id FROM admins"); rows = c.fetchall(); conn.close()
    return [row[0] for row in rows]

def is_free_mode() -> bool:
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT value FROM bot_config WHERE key = 'free_mode'"); row = c.fetchone(); conn.close()
    return row is not None and row[0] == '1'

def set_free_mode(enabled: bool):
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO bot_config (key, value) VALUES ('free_mode', ?)", ('1' if enabled else '0',)); conn.commit(); conn.close()

def get_subscription_expiry(user_id: int) -> Optional[datetime]:
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT expiry FROM subscriptions WHERE telegram_id = ?", (user_id,)); row = c.fetchone(); conn.close()
    return datetime.fromisoformat(row[0]) if row else None

def is_subscription_active(user_id: int) -> bool:
    expiry = get_subscription_expiry(user_id)
    return expiry is not None and expiry > datetime.now()

def add_subscription(user_id: int, days: int) -> datetime:
    new_expiry = datetime.now() + timedelta(days=days)
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO subscriptions (telegram_id, expiry) VALUES (?, ?)", (user_id, new_expiry.isoformat())); conn.commit(); conn.close()
    return new_expiry

def remove_subscription(user_id: int) -> bool:
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("DELETE FROM subscriptions WHERE telegram_id = ?", (user_id,)); affected = c.rowcount; conn.commit(); conn.close()
    return affected > 0

def list_active_subscriptions() -> List[Tuple[int, datetime]]:
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT telegram_id, expiry FROM subscriptions WHERE expiry > ?", (datetime.now().isoformat(),)); rows = c.fetchall(); conn.close()
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
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT 1 FROM banned_users WHERE telegram_id = ?", (telegram_id,)); row = c.fetchone(); conn.close()
    return row is not None

def ban_user(telegram_id: int) -> None:
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO banned_users (telegram_id) VALUES (?)", (telegram_id,)); conn.commit(); conn.close()

def unban_user(telegram_id: int) -> None:
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("DELETE FROM banned_users WHERE telegram_id = ?", (telegram_id,)); conn.commit(); conn.close()

def record_user_activity(telegram_id: int) -> None:
    now = datetime.now(); conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO user_activity (telegram_id, first_seen, last_seen) VALUES (?, ?, ?)", (telegram_id, now, now))
    c.execute("UPDATE user_activity SET last_seen = ? WHERE telegram_id = ?", (now, telegram_id)); conn.commit(); conn.close()

def get_all_users() -> List[Tuple[int, datetime, datetime]]:
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT telegram_id, first_seen, last_seen FROM user_activity ORDER BY last_seen DESC"); rows = c.fetchall(); conn.close()
    return [(uid, datetime.fromisoformat(first) if isinstance(first, str) else first, datetime.fromisoformat(last) if isinstance(last, str) else last) for uid, first, last in rows]

def get_stats() -> Dict[str, int]:
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT COUNT(*) FROM user_activity"); total_users = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM users"); active_sessions = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM saved_numbers"); saved_numbers = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM banned_users"); banned_count = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM subscriptions WHERE expiry > datetime('now')"); active_subs = c.fetchone()[0]
    conn.close()
    return {'total_users': total_users, 'active_sessions': active_sessions, 'saved_numbers': saved_numbers, 'banned_count': banned_count, 'active_subs': active_subs}

def save_number(telegram_id: int, number: str) -> None:
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO saved_numbers (telegram_id, number) VALUES (?, ?)", (telegram_id, number)); conn.commit(); conn.close()

def get_saved_numbers(telegram_id: int) -> List[str]:
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT number FROM saved_numbers WHERE telegram_id = ?", (telegram_id,)); rows = c.fetchall(); conn.close()
    return [r[0] for r in rows]

def delete_saved_number(telegram_id: int, number: str) -> None:
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("DELETE FROM saved_numbers WHERE telegram_id = ? AND number = ?", (telegram_id, number)); conn.commit(); conn.close()
    delete_saved_password(telegram_id, number)

def save_token(user_id: int, number: str, token: str, expiry: datetime) -> None:
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO saved_tokens (user_id, number, token, expiry) VALUES (?, ?, ?, ?)", (user_id, number, token, expiry.isoformat())); conn.commit(); conn.close()

def get_valid_token(user_id: int, number: str) -> Optional[str]:
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute("SELECT token, expiry FROM saved_tokens WHERE user_id = ? AND number = ?", (user_id, number)); row = c.fetchone(); conn.close()
    if row:
        token, expiry_str = row
        try:
            if datetime.fromisoformat(expiry_str) > datetime.now(): return token
        except: pass
    return None

def save_user(telegram_id: int, phone: str, token: str, expiry: datetime) -> None:
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute('INSERT OR REPLACE INTO users (telegram_id, phone, access_token, token_expiry) VALUES (?, ?, ?, ?)', (telegram_id, phone, token, expiry.isoformat())); conn.commit(); conn.close()
    save_token(telegram_id, phone, token, expiry)

def get_user(telegram_id: int) -> Optional[Dict[str, Any]]:
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute('SELECT phone, access_token, token_expiry FROM users WHERE telegram_id = ?', (telegram_id,)); row = c.fetchone(); conn.close()
    if row:
        phone, token, expiry_str = row; return {'phone': phone, 'access_token': token, 'token_expiry': datetime.fromisoformat(expiry_str)}
    return None

def delete_user(telegram_id: int) -> None:
    conn = sqlite3.connect(DB_PATH); c = conn.cursor()
    c.execute('DELETE FROM users WHERE telegram_id = ?', (telegram_id,)); conn.commit(); conn.close()

# ================== API فودافون الأساسي ==================
def build_base_headers(additional: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    device_model, os_version = get_random_device_info()
    headers = {'User-Agent': 'okhttp/4.12.0', 'Connection': 'Keep-Alive', 'Accept': 'application/json', 'device-id': generate_device_id(), 'x-agent-operatingsystem': os_version, 'clientId': 'AnaVodafoneAndroid', 'x-agent-device': device_model, 'x-agent-version': '2026.4.1', 'x-agent-build': '1139', 'Accept-Language': 'ar', 'Content-Type': 'application/json; charset=UTF-8', 'digitalId': generate_digital_id()}
    if additional: headers.update(additional)
    return headers

async def login_vodafone(phone: str, password: str) -> Tuple[str, datetime]:
    headers = build_base_headers({'Content-Type': 'application/x-www-form-urlencoded', 'silentLogin': 'true', 'msisdn': phone})
    data = {'grant_type': 'password', 'username': phone, 'password': password, 'client_secret': 'dca0pbLUWXVhXR266Gw1iT5rqwvvJQoN', 'client_id': 'AnaVF'}
    def sync_login():
        resp = HTTP_SESSION.post('https://mobile.vodafone.com.eg/auth/realms/vf-realm/protocol/openid-connect/token', headers=headers, data=data, timeout=30)
        if resp.status_code == 200: return resp.json().get('access_token'), resp.json().get('expires_in', 3600)
        raise Exception(f"فشل تسجيل الدخول: {resp.status_code}")
    token, exp_in = await asyncio.to_thread(sync_login)
    expiry = datetime.now() + timedelta(seconds=exp_in)
    return token, expiry

async def get_family_members(access_token: str, msisdn: str) -> Dict[str, List[Dict[str, Any]]]:
    headers = build_base_headers({'Authorization': f'Bearer {access_token}', 'api-version': 'v2', 'msisdn': msisdn})
    def sync_get(): return HTTP_SESSION.get('https://mobile.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup', params={'type': 'Family'}, headers=headers, timeout=30)
    resp = await asyncio.to_thread(sync_get)
    if resp.status_code == 404: return {'active': [], 'pending': []}
    if resp.status_code != 200: raise Exception(f"فشل جلب العائلة: {resp.status_code}")
    result = resp.json()
    if not result: return {'active': [], 'pending': []}
    members = result[0].get('parts', {}).get('member', [])
    active, pending_raw = [], []
    for m in members:
        if m.get('type') == 'Owner': continue
        phone = next((id_obj.get('value') for id_obj in m.get('id', []) if id_obj.get('schemeName') == 'MSISDN'), None)
        if not phone: continue
        phone_local = convert_to_local(phone)
        status_code = str(m.get('status'))
        flex_value = next((ch.get('value') for ch in m.get('characteristic', {}).get('characteristicsValue', []) if ch.get('characteristicName') == 'flex'), None)
        if status_code == "1": active.append({'phone': phone_local, 'flex': flex_value})
        elif status_code == "5": pending_raw.append({'phone': phone_local, 'flex': flex_value})
    pending_dict = defaultdict(list)
    for p in pending_raw: pending_dict[p['phone']].append(p['flex'])
    return {'active': active, 'pending': [{'phone': phone, 'flex': flexes[0], 'count': len(flexes)} for phone, flexes in pending_dict.items()]}

async def send_invitation(token: str, owner_msisdn: str, member_phone: str, percentage: int) -> bool:
    headers = build_base_headers({'Authorization': f'Bearer {token}', 'msisdn': owner_msisdn})
    json_data = {'category': [{'listHierarchyId': 'PackageID', 'value': '523'}, {'listHierarchyId': 'TemplateID', 'value': '47'}, {'listHierarchyId': 'TierID', 'value': '523'}], 'parts': {'characteristicsValue': {'characteristicsValue': [{'characteristicName': 'quotaDist1', 'type': 'percentage', 'value': str(percentage)}]}, 'member': [{'id': [{'schemeName': 'MSISDN', 'value': owner_msisdn}], 'type': 'Owner'}, {'id': [{'schemeName': 'MSISDN', 'value': member_phone}], 'type': 'Member'}]}, 'type': 'SendInvitation'}
    def sync_post(): return HTTP_SESSION.post('https://mobile.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup', headers=headers, json=json_data, timeout=30)
    resp = await asyncio.to_thread(sync_post)
    return resp.status_code in (200, 201)

async def accept_invitation(member_token: str, member_phone: str, owner_msisdn: str) -> bool:
    headers = build_base_headers({'api_id': 'APP', 'Authorization': f'Bearer {member_token}', 'msisdn': member_phone})
    json_data = {'category': [{'listHierarchyId': 'TemplateID', 'value': '47'}], 'name': 'FlexFamily', 'parts': {'member': [{'id': [{'schemeName': 'MSISDN', 'value': owner_msisdn}], 'type': 'Owner'}, {'id': [{'schemeName': 'MSISDN', 'value': member_phone}], 'type': 'Member'}]}, 'type': 'AcceptInvitation'}
    def sync_patch(): return WEB_HTTP_SESSION.patch('https://mobile.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup', headers=headers, json=json_data, timeout=30)
    resp = await asyncio.to_thread(sync_patch)
    return resp.status_code in (200, 201)

async def update_quota(token: str, owner_msisdn: str, member_phone: str, percentage: int) -> bool:
    headers = build_base_headers({'Authorization': f'Bearer {token}', 'msisdn': owner_msisdn})
    json_data = {'category': [{'listHierarchyId': 'TemplateID', 'value': '47'}], 'createdBy': {'value': 'MobileApp'}, 'parts': {'characteristicsValue': {'characteristicsValue': [{'characteristicName': 'quotaDist1', 'type': 'percentage', 'value': str(percentage)}]}, 'member': [{'id': [{'schemeName': 'MSISDN', 'value': owner_msisdn}], 'type': 'Owner'}, {'id': [{'schemeName': 'MSISDN', 'value': member_phone}], 'type': 'Member'}]}, 'type': 'QuotaRedistribution'}
    def sync_patch(): return WEB_HTTP_SESSION.patch('https://mobile.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup', headers=headers, json=json_data, timeout=30)
    resp = await asyncio.to_thread(sync_patch)
    return resp.status_code in (200, 201)

async def remove_member(token: str, owner_msisdn: str, member_phone: str) -> bool:
    headers = build_base_headers({'Authorization': f'Bearer {token}', 'msisdn': owner_msisdn})
    json_data = {'category': [{'listHierarchyId': 'TemplateID', 'value': '47'}], 'createdBy': {'value': 'MobileApp'}, 'parts': {'characteristicsValue': {'characteristicsValue': [{'characteristicName': 'Disconnect', 'value': '0'}, {'characteristicName': 'LastMemberDeletion', 'value': '1'}]}, 'member': [{'id': [{'schemeName': 'MSISDN', 'value': owner_msisdn}], 'type': 'Owner'}, {'id': [{'schemeName': 'MSISDN', 'value': member_phone}], 'type': 'Member'}]}, 'type': 'FamilyRemoveMember'}
    def sync_patch(): return WEB_HTTP_SESSION.patch('https://mobile.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup', headers=headers, json=json_data, timeout=30)
    resp = await asyncio.to_thread(sync_patch)
    return resp.status_code in (200, 201)

async def cancel_invitation(token: str, owner_msisdn: str, member_phone: str) -> bool:
    headers = build_base_headers({'Authorization': f'Bearer {token}', 'msisdn': owner_msisdn})
    json_data = {'category': [{'listHierarchyId': 'TemplateID', 'value': '0'}], 'createdBy': {'value': 'MobileApp'}, 'name': 'FlexFamily', 'parts': {'member': [{'id': [{'schemeName': 'MSISDN', 'value': member_phone}], 'type': 'Member'}, {'id': [{'schemeName': 'MSISDN', 'value': owner_msisdn}], 'type': 'Owner'}]}, 'type': 'CancelInvitation'}
    def sync_patch(): return WEB_HTTP_SESSION.patch('https://mobile.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup', headers=headers, json=json_data, timeout=30)
    resp = await asyncio.to_thread(sync_patch)
    return resp.status_code in (200, 201)

async def get_owner_remaining(token: str, phone: str) -> Optional[str]:
    headers = build_base_headers({'api-host': 'usageConsumptionHost', 'useCase': 'aggregated', 'Authorization': f'Bearer {token}', 'msisdn': phone})
    def sync_get(): return HTTP_SESSION.get('https://mobile.vodafone.com.eg/services/dxl/usage/usageConsumptionReport', params={'@type': 'aggregated', 'bucket.product.publicIdentifier': phone}, headers=headers, timeout=15)
    resp = await asyncio.to_thread(sync_get)
    if resp.status_code != 200: return None
    try:
        def find_limit(obj):
            if isinstance(obj, dict):
                if obj.get("usageType") == "limit":
                    for bal in obj.get("bucketBalance", []):
                        if bal.get("@type") == "Remaining":
                            amount, units = bal.get("remainingValue", {}).get("amount"), bal.get("remainingValue", {}).get("units")
                            if amount is not None and units is not None: return amount, units
                for v in obj.values():
                    res = find_limit(v)
                    if res: return res
            elif isinstance(obj, list):
                for item in obj:
                    res = find_limit(item)
                    if res: return res
        res = find_limit(resp.json())
        if res:
            amount, units = res
            if isinstance(amount, (int, float)) and amount.is_integer(): amount = int(amount)
            return f"{amount} {units}"
        return None
    except: return None

async def get_member_consumption_from_owner(owner_token: str, owner_phone: str, member_phone: str) -> Optional[Dict[str, str]]:
    headers = build_base_headers({'api-host': 'usageConsumptionHost', 'useCase': 'familyDetailed', 'Authorization': f'Bearer {owner_token}', 'msisdn': owner_phone})
    def sync_get(): return HTTP_SESSION.get('https://mobile.vodafone.com.eg/services/dxl/usage/usageConsumptionReport', params={'@type': 'familyDetailed', 'bucket.product.publicIdentifier': member_phone}, headers=headers, timeout=15)
    resp = await asyncio.to_thread(sync_get)
    if resp.status_code != 200: return None
    try:
        for item in resp.json():
            if item.get('name') == 'Flex_Family_Main_Bundle':
                for bucket in item.get('bucket', []):
                    if bucket.get('usageType') == 'FLEX':
                        remaining = next((bal.get('remainingValue', {}) for bal in bucket.get('bucketBalance', []) if bal.get('@type') == 'Remaining'), None)
                        used = next((counter.get('value', {}) for counter in bucket.get('bucketCounter', []) if counter.get('@type') == 'Used' and counter.get('level') == 'Local'), None)
                        return {'remaining': f"{remaining.get('amount', 0)} {remaining.get('units', 'FLEX')}" if remaining else "غير متاح", 'used': f"{used.get('amount', 0)} {used.get('units', 'FLEX')}" if used else "غير متاح"}
        return None
    except: return None

# ================== إدارة الرسائل والحالات ==================
user_messages: Dict[int, List[int]] = {}
user_states: Dict[int, Dict[str, Any]] = {}

async def clear_previous_messages(update: Update, user_id: int) -> None:
    if user_id not in user_messages: return
    remaining = []
    for msg_id in user_messages[user_id]:
        try: await update.effective_bot.delete_message(chat_id=update.effective_chat.id, message_id=msg_id)
        except: remaining.append(msg_id)
    user_messages[user_id] = remaining

async def send_message(update: Update, user_id: int, text: str, keyboard: Optional[InlineKeyboardMarkup] = None, clear_before: bool = False, parse_mode: Optional[str] = None) -> None:
    if clear_before: await clear_previous_messages(update, user_id)
    msg = await update.callback_query.message.reply_text(text, reply_markup=keyboard, parse_mode=parse_mode) if update.callback_query else await update.effective_message.reply_text(text, reply_markup=keyboard, parse_mode=parse_mode)
    user_messages.setdefault(user_id, []).append(msg.message_id)

async def query_or_message(update: Update, text: str, parse_mode: Optional[str] = None) -> None:
    if update.callback_query:
        try: await update.callback_query.edit_message_text(text, parse_mode=parse_mode); return
        except: pass
        await update.callback_query.message.reply_text(text, parse_mode=parse_mode)
    elif update.message: await update.message.reply_text(text, parse_mode=parse_mode)

async def ensure_valid_session(update: Update, user_id: int) -> Optional[Dict[str, Any]]:
    user = get_user(user_id)
    if not user: return None
    if datetime.now() < (user['token_expiry'] - timedelta(seconds=30)): return user
    saved_password = get_saved_password(user_id, user['phone'])
    if saved_password:
        try:
            token, expiry = await login_vodafone(user['phone'], saved_password)
            save_user(user_id, user['phone'], token, expiry)
            return get_user(user_id)
        except Exception as e: log_action(user_id, "AUTO_SESSION_REFRESH_FAIL", str(e))
    delete_user(user_id)
    await send_message(update, user_id, "⏰ انتهت صلاحية جلستك. برجاء تسجيل الدخول مجدداً.")
    await show_login_screen(update, user_id, None)
    return None

async def show_login_screen(update: Update, user_id: int, context: Optional[ContextTypes.DEFAULT_TYPE]) -> None:
    saved_numbers = get_saved_numbers(user_id)
    if saved_numbers:
        keyboard = [[InlineKeyboardButton(num + (" 🔑" if get_valid_token(user_id, num) else ""), callback_data=f"use_num_{num}")] for num in saved_numbers]
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
    headers = {'User-Agent': 'okhttp/4.12.0', 'Connection': 'Keep-Alive', 'Accept': 'application/json', 'api-host': 'ProductOrderingManagement', 'useCase': 'FlexACPRenewal', 'Authorization': f'Bearer {token}', 'api-version': 'v2', 'device-id': generate_device_id(), 'x-agent-operatingsystem': '13', 'clientId': 'AnaVodafoneAndroid', 'x-agent-device': 'Samsung SM-A515F', 'x-agent-version': '2025.11.1', 'x-agent-build': '1063', 'msisdn': phone, 'Accept-Language': 'ar', 'Content-Type': 'application/json; charset=UTF-8', 'X-Request-ID': f'FLEX_{int(datetime.now().timestamp())}'}
    payload = {"channel": {"name": "MobileApp"}, "orderItem": [{"action": "insert", "id": "FLEX_ROLLOVER", "product": {"characteristic": [{"name": "PaymentMethod", "value": "ACP"}, {"name": "ACP", "value": "True"}, {"name": "SMSID", "value": "MUTE_SMS"}], "encProductId": "8UaJf2TAFkQncd2YduZwRAbXrixRX0FbzsLM/WakGJhTeK0tEE5sc5f4a8TXyo9FYlvdb1bT75cEIo+D1y5rkWQUjSwKtd0EJRxzp9wIrbuKtupFTfAhNLcfTdjt7hDOMkiZSWlblb1Ne9CrZgq2/ptclZCRIlstBSrTme3YLhxCUzhbMrwOuYQoJaMg6uXx/FrIO9Dd3tgz2kTemb36IO5s7N0rSYVzyzPOoHJb5gGC69r3InrZ9JJql/vr5cS+BP33wKGpUNg9P207RLPthMUVyTZSrCnGQMQ+fXXMocCGTEwzOH/D7DRf2GCGg2OHZLL/DfL1cc0Sh6Yoc1hb", "id": "Flex_2021_523", "relatedParty": [{"id": phone, "name": "MSISDN", "role": "Subscriber"}]}, "eCode": 0}], "@type": "FlexACPRenewal"}
    
    try:
        await query_or_message(update, "⏳ جاري محاولة تزويد الباقة يومين...")
        def sync_request(): return HTTP_SESSION.post(url, headers=headers, json=payload, timeout=30)
        response = await asyncio.to_thread(sync_request)
        if 200 <= response.status_code < 300:
            try: result = response.json()
            except: result = {}
            msg = "✅ تم إضافة يومين لباقة فليكس بنجاح 🎉" if result.get("state") == "Completed" else f"✅ تم قبول الطلب بنجاح. كود الحالة: {response.status_code}"
        elif response.status_code == 400:
            try: error_data = response.json()
            except: error_data = {}
            code = str(error_data.get("code", ""))
            if code == "2037": msg = "⚠️ تم استخدام تزويد اليومين من قبل لهذا الشهر."
            elif code == "1001": msg = "🔒 انتهت صلاحية الجلسة. سجل الدخول مرة أخرى."
            elif code == "1005": msg = "📵 رقم الهاتف غير صالح."
            else: msg = f"❌ لم يتم تزويد اليومين.\nالسبب: {error_data.get('reason', 'الطلب غير متاح حالياً')}"
        elif response.status_code == 401: msg = "🔒 الجلسة غير صالحة أو انتهت. سجل الدخول مرة أخرى."
        elif response.status_code == 403: msg = "⛔ الخدمة غير متاحة لهذا الحساب حالياً."
        else: msg = f"❌ لم يتم تزويد اليومين. كود الخادم: {response.status_code}"
    except requests.exceptions.Timeout: msg = "⏰ انتهت مهلة الاتصال. حاول مرة أخرى."
    except Exception as e:
        log_action(user_id, "ADD_TWO_DAYS_FAIL", str(e)); msg = "❌ حدث خطأ أثناء تنفيذ تزويد اليومين."
    keyboard = [[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]
    await send_message(update, user_id, msg, InlineKeyboardMarkup(keyboard))

# ================== تنظيف العائلة ==================
async def perform_full_cleaning_silent(update: Update, user_id: int, owner_num: str, owner_pass: str, member_num: str, member_pass: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    progress_msg = await update.message.reply_text("🧹 **تنظيف العائلة**\n[░░░░░░░░░░░░░░░░░░░░] 0%", parse_mode='Markdown')
    def update_progress(percent: int, bar_length: int = 20):
        filled = int(bar_length * percent // 100)
        bar = "█" * filled + "░" * (bar_length - filled)
        asyncio.create_task(progress_msg.edit_text(f"🧹 **تنظيف العائلة**\n[{bar}] {percent}%", parse_mode='Markdown'))
    try:
        update_progress(10); owner_token, _ = await login_vodafone(owner_num, owner_pass); await asyncio.sleep(0.5)
        update_progress(20); await send_invitation(owner_token, owner_num, member_num, 10); await asyncio.sleep(7)
        update_progress(30); await remove_member(owner_token, owner_num, member_num); await asyncio.sleep(7)
        update_progress(50)
        if not await send_invitation(owner_token, owner_num, member_num, 10): await progress_msg.edit_text("❌ لم يتم تنظيف العائلة"); return
        await asyncio.sleep(7)
        update_progress(70); member_token, _ = await login_vodafone(member_num, member_pass)
        if not await accept_invitation(member_token, member_num, owner_num): await progress_msg.edit_text("❌ لم يتم تنظيف العائلة"); return
        await asyncio.sleep(7)
        update_progress(85)
        if not await update_quota(owner_token, owner_num, member_num, 40): await progress_msg.edit_text("❌ لم يتم تنظيف العائلة"); return
        await asyncio.sleep(7)
        update_progress(95)
        for _ in range(3):
            if await remove_member(owner_token, owner_num, member_num): break
            await asyncio.sleep(7)
        else: await progress_msg.edit_text("❌ لم يتم تنظيف العائلة"); return
        update_progress(100); await asyncio.sleep(2); await progress_msg.edit_text("✅ **تم تنظيف العائلة بنجاح!**", parse_mode='Markdown')
    except Exception: await progress_msg.edit_text("❌ لم يتم تنظيف العائلة")

# ================== القائمة الرئيسية ==================
async def show_main_menu(update: Update, user_id: int, context: Optional[ContextTypes.DEFAULT_TYPE]) -> None:
    if not await require_subscription_or_free(update, context, is_callback=False): return
    await clear_previous_messages(update, user_id)
    user = await ensure_valid_session(update, user_id)
    if not user: await show_login_screen(update, user_id, context); return
    if context and context.user_data.get('rocket_members') is None: context.user_data['rocket_members'] = []
    keyboard = [
        [InlineKeyboardButton("🏠 إدارة العائلة", callback_data="show_family"), InlineKeyboardButton("📅 تزويد يومين", callback_data="add_two_days")],
        [InlineKeyboardButton("➖ تنقيص فليكسات", callback_data="deduct_flexes"), InlineKeyboardButton("💰 خصم فليكس", callback_data="flex_discount")],
        [InlineKeyboardButton("📊 نسبة الأونر", callback_data="show_owner_remaining"), InlineKeyboardButton("📊 استهلاك الأعضاء", callback_data="show_consumption")],
        [InlineKeyboardButton("📞 أرقامي المحفوظة", callback_data="show_saved"), InlineKeyboardButton("🧹 تنظيف العائلة", callback_data="clean_family")],
        [InlineKeyboardButton("🔄 تحويل انظمه", callback_data="packages_services")],
        [InlineKeyboardButton("🚀 تعليق دعوتين (VIP)", callback_data="sendonly_menu"), InlineKeyboardButton("🔄 حساب آخر", callback_data="another_account")]
    ]
    text = ("👑 **أهلاً بك في لوحة التحكم الاحترافية** 👑\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"📱 **الحساب الحالي:** `{user['phone']}`\n"
            "⚡ **حالة الاتصال:** متصل بنجاح ✅\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "اختر أحد الخيارات أدناه للبدء:")
    await send_message(update, user_id, text, InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

# ================== إدارة العائلة ==================
async def show_family_screen(update: Update, user_id: int, context: Optional[ContextTypes.DEFAULT_TYPE]) -> None:
    is_cb = update.callback_query is not None
    if not await require_subscription_or_free(update, context, is_callback=is_cb, query=update.callback_query if is_cb else None): return
    user = await ensure_valid_session(update, user_id)
    if not user: await show_login_screen(update, user_id, context); return
    await clear_previous_messages(update, user_id)
    try: members = await get_family_members(user['access_token'], user['phone'])
    except Exception as e: await send_message(update, user_id, f"خطأ: {str(e)}"); return
    active, pending = members.get('active', []), members.get('pending', [])
    filtered_pending = [p for p in pending if p['phone'] not in [a['phone'] for a in active]]
    rocket_list = context.user_data.get('rocket_members', []) if context else []
    for m in active:
        keyboard = [[InlineKeyboardButton(f"فلكس {f}", callback_data=f"edit_{m['phone']}_{f}") for f in ["1300","2600","5200"] if f != m['flex']], [InlineKeyboardButton("📅 تزويد يومين", callback_data=f"add_two_days_member_{m['phone']}"), InlineKeyboardButton("🗑️ حذف الفرد", callback_data=f"del_{m['phone']}")]]
        await send_message(update, user_id, f"📞 الفرد: {m['phone']}{' 🚀' if m['phone'] in rocket_list else ''}\n✅ مفعل\n📊 النسبة: {m['flex']} فلكس\n✏️ تعديل نسبة الفرد", InlineKeyboardMarkup(keyboard))
    for p in filtered_pending:
        keyboard = [[InlineKeyboardButton("قبول الدعوة ✅", callback_data=f"accept_{p['phone']}"), InlineKeyboardButton("إلغاء الدعوة ❌", callback_data=f"cancel_{p['phone']}")], [InlineKeyboardButton("🗑️ حذف الفرد", callback_data=f"del_{p['phone']}")]]
        await send_message(update, user_id, f"📞 الفرد: {p['phone']}{' 🚀' if p['count'] >= 2 else ''}\n⏳ بانتظار القبول\n📊 النسبة: {p['flex']} فلكس", InlineKeyboardMarkup(keyboard))
    if len(active) < 2:
        await send_message(update, user_id, "📝 دعوة فرد جديد: اختر نسبة من الأزرار أدناه 📞", InlineKeyboardMarkup([[InlineKeyboardButton("فلكس 5200", callback_data="invite_40"), InlineKeyboardButton("فلكس 2600", callback_data="invite_20"), InlineKeyboardButton("فلكس 1300", callback_data="invite_10")], [InlineKeyboardButton("🔄 تحديث", callback_data="refresh_family")], [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]))
    else:
        await send_message(update, user_id, "العائلة ممتلئة (2 أعضاء). لا يمكن إضافة المزيد.", InlineKeyboardMarkup([[InlineKeyboardButton("🔄 تحديث", callback_data="refresh_family")], [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]))

async def show_owner_remaining(update: Update, user_id: int, context: Optional[ContextTypes.DEFAULT_TYPE]) -> None:
    is_cb = update.callback_query is not None
    if not await require_subscription_or_free(update, context, is_callback=is_cb, query=update.callback_query if is_cb else None): return
    user = await ensure_valid_session(update, user_id)
    if not user: await show_login_screen(update, user_id, context); return
    await send_message(update, user_id, "📊 جاري جلب النسبة المتبقية...")
    try: text = f"📊 نسبة الأونر المتبقية:\n{await get_owner_remaining(user['access_token'], user['phone'])}" if await get_owner_remaining(user['access_token'], user['phone']) else "❌ لم نتمكن من جلب النسبة المتبقية."
    except Exception as e: text = f"❌ خطأ: {str(e)}"
    await send_message(update, user_id, text, InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]))

async def show_consumption(update: Update, user_id: int, context: Optional[ContextTypes.DEFAULT_TYPE]) -> None:
    is_cb = update.callback_query is not None
    if not await require_subscription_or_free(update, context, is_callback=is_cb, query=update.callback_query if is_cb else None): return
    user = await ensure_valid_session(update, user_id)
    if not user: await show_login_screen(update, user_id, context); return
    try:
        active_members = (await get_family_members(user['access_token'], user['phone'])).get('active', [])
        if not active_members: await send_message(update, user_id, "ℹ️ لا يوجد أعضاء نشطون في العائلة لعرض استهلاكهم."); return
    except Exception as e: await send_message(update, user_id, f"خطأ: {str(e)}"); return
    await send_message(update, user_id, "📊 جاري جلب استهلاك الأعضاء...")
    results = []
    for member in active_members:
        consumption = await get_member_consumption_from_owner(user['access_token'], user['phone'], member['phone'])
        results.append(f"📞 {member['phone']}\n✅ المتبقي: {consumption['remaining']}\n📉 المستخدم: {consumption['used']}" if consumption else f"📞 {member['phone']}\n❌ لم نتمكن من جلب استهلاكه (قد لا يكون مفعلاً أو هناك خطأ).")
    await send_message(update, user_id, "📊 استهلاك الفليكسات للأعضاء:\n\n" + "\n\n".join(results) if results else "❌ حدث خطأ أثناء جلب الاستهلاك.", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]))

async def show_saved_numbers(update: Update, user_id: int, context: Optional[ContextTypes.DEFAULT_TYPE]) -> None:
    is_cb = update.callback_query is not None
    if not await require_subscription_or_free(update, context, is_callback=is_cb, query=update.callback_query if is_cb else None): return
    numbers = get_saved_numbers(user_id)
    if not numbers: await send_message(update, user_id, "📭 لا توجد أرقام محفوظة. يمكنك إضافة رقم باستخدام /savenumber <رقم>", InlineKeyboardMarkup([[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]])); return
    keyboard = [[InlineKeyboardButton(num + (" 🔑" if get_valid_token(user_id, num) else ""), callback_data=f"use_num_{num}"), InlineKeyboardButton("🗑 حذف", callback_data=f"del_num_{num}")] for num in numbers]
    keyboard.append([InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")])
    await send_message(update, user_id, "📞 أرقامي المحفوظة (🔑=توكن صالح):\nاختر رقم لتسجيل الدخول به أو حذفه:", InlineKeyboardMarkup(keyboard))

# ================== تنقيص فليكسات ==================
FLEX_DEDUCTION_PER_OPERATION = 225

async def deduct_flexes(update: Update, user_id: int, context: Optional[ContextTypes.DEFAULT_TYPE]) -> None:
    user = await ensure_valid_session(update, user_id)
    if not user: await show_login_screen(update, user_id, context); return
    context.user_data.pop('awaiting_flex_deduction_count', None)
    context.user_data['awaiting_flex_deduction_count'] = True
    keyboard = [[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]
    await send_message(update, user_id, f"➖ تنقيص فليكسات\n\nكل عملية = {FLEX_DEDUCTION_PER_OPERATION} فليكس.\nابعت عدد العمليات المطلوبة (مثال: 3):", InlineKeyboardMarkup(keyboard))

async def execute_flex_deduction(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE, times: int) -> None:
    operation_lock = get_user_operation_lock(user_id)
    if operation_lock.locked():
        await update.message.reply_text("⏳ فيه عملية أخرى شغالة على الحساب. استنى لحد ما تخلص.", parse_mode=None)
        return
    
    user = await ensure_valid_session(update, user_id)
    if not user:
        await show_login_screen(update, user_id, context); return

    phone = user['phone']
    token = user['access_token']
    url = "https://web.vodafone.com.eg/services/dxl/promo/promotion/"
    
    headers = {
        'User-Agent': 'vodafoneandroid',
        'Accept': 'application/json',
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {token}',
        'Accept-Language': 'AR',
        'msisdn': phone,
        'clientId': 'WebsiteConsumer',
        'client-journey-page': 'takhmeen_home',
        'channel': 'APP_PORTAL',
        'Origin': 'https://web.vodafone.com.eg',
        'X-Requested-With': 'com.emeint.android.myservices',
        'Referer': 'https://web.vodafone.com.eg/portal/afcon/home',
        'sec-ch-ua': '"Chromium";v="137", "Not/A)Brand";v="24"',
        'sec-ch-ua-mobile': '?1',
        'sec-ch-ua-platform': '"Android"',
    }

    payload = {
        "@type": "Promo",
        "channel": {"id": "5"},
        "context": {"type": "takhmeenPaymentAction"},
        "pattern": [{"characteristics": [{"name": "purchaseOption", "value": 3}]}]
    }

    total = times * FLEX_DEDUCTION_PER_OPERATION
    await update.message.reply_text(f"⏳ جاري تنفيذ {times} عملية...\nإجمالي التنقيص المتوقع: {total} فليكس.", parse_mode=None)

    success_count = 0
    failed_status = None
    last_error = ""

    for i in range(1, times + 1):
        try:
            def sync_request(): return WEB_HTTP_SESSION.patch(url, json=payload, headers=headers, timeout=30)
            response = await asyncio.to_thread(sync_request)

            if response.status_code == 204:
                success_count += 1
                if i < times: await asyncio.sleep(3)
            else:
                try:
                    error_data = response.json()
                    last_error = error_data.get('reason', '') or error_data.get('message', '') or f"كود {response.status_code}"
                except: last_error = f"كود {response.status_code}"
                
                failed_status = response.status_code
                await asyncio.sleep(5)
                response = await asyncio.to_thread(sync_request)
                
                if response.status_code == 204:
                    success_count += 1
                    if i < times: await asyncio.sleep(3)
                else:
                    failed_status = response.status_code
                    break
        except requests.exceptions.Timeout:
            failed_status = "TIMEOUT"; last_error = "انتهت مهلة الاتصال"; break
        except Exception as e:
            log_action(user_id, "FLEX_DEDUCTION_FAIL", str(e)); failed_status = "ERROR"; last_error = str(e); break

    actual = success_count * FLEX_DEDUCTION_PER_OPERATION
    if success_count == times:
        msg = f"✅ تمت العملية بنجاح.\nعدد العمليات: {success_count}/{times}\nإجمالي التنقيص: {actual} فليكس."
    else:
        msg = f"⚠️ تم تنفيذ {success_count} من {times} عملية.\nإجمالي ما تم تنقيصه: {actual} فليكس.\nآخر حالة: {failed_status}\nالسبب: {last_error if last_error else 'غير معروف'}"

    keyboard = [[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]
    await update.message.reply_text(msg, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

# ================== خصم فليكس (النسخة النهائية من الملف القديم) ==================
class FlexDiscountService:
    def __init__(self, token: str, phone: str):
        self.token = token
        self.phone = phone
        self.session = requests.Session()

    def login_for_discount(self, password: str) -> bool:
        if not password: return False
        url = "https://mobile.vodafone.com.eg/auth/realms/vf-realm/protocol/openid-connect/token"
        payload = {'grant_type': "password", 'username': self.phone, 'password': password, 'client_secret': "95fd95fb-7489-4958-8ae6-d31a525cd20a", 'client_id': "ana-vodafone-app"}
        headers = {'User-Agent': "okhttp/4.12.0", 'Accept': "application/json, text/plain, */*", 'Accept-Encoding': "gzip", 'Content-Type': "application/x-www-form-urlencoded", 'silentLogin': "true", 'x-agent-operatingsystem': "15", 'clientId': "AnaVodafoneAndroid", 'Accept-Language': "ar", 'x-agent-device': "Samsung SM-A165F", 'x-agent-version': "2025.12.2", 'x-agent-build': "1080", 'digitalId': "25VT5Q5QWG8DK", 'device-id': "b26ba335813fad21"}
        try:
            response = self.session.post(url, data=payload, headers=headers, timeout=20)
            if response.status_code == 200:
                token = response.json().get('access_token')
                if token: self.token = token; return True
        except Exception: pass
        return False

    def get_discount_offers(self) -> List[Dict]:
        url = "https://mobile.vodafone.com.eg/services/dxl/epo/eligibleProductOffering"
        params = {'customerAccountId': self.phone, 'parts.customerAccount.type': "Consumer", 'Accept-Language': "ar", 'type': "Tarrifs"}
        headers = {'User-Agent': "okhttp/4.12.0", 'Connection': "Keep-Alive", 'Accept': "application/json", 'Accept-Encoding': "gzip", 'api-host': "EligibleProductOfferingHost", 'useCase': "Tarrifs", 'Authorization': f"Bearer {self.token}", 'api-version': "v2", 'device-id': "b26ba335813fad21", 'x-agent-operatingsystem': "15", 'clientId': "AnaVodafoneAndroid", 'x-agent-device': "Samsung SM-A165F", 'x-agent-version': "2025.12.2", 'x-agent-build': "1080", 'msisdn': self.phone, 'Content-Type': "application/json", 'Accept-Language': "ar"}
        try:
            response = self.session.get(url, params=params, headers=headers, timeout=20)
            if response.status_code == 200:
                data = response.json()
                if isinstance(data, list): return data
                return []
        except Exception as e: logger.warning(f"Flex discount offers exception: {e}")
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
            if isinstance(char, dict) and char.get('characteristicName') == name: return str(char.get('value', default))
        return default

    @staticmethod
    def extract_bundle_name(name: str, description: str) -> str:
        for pattern in [r'فليكس (\d+)', r'باقة (\d+)', r'على (\d+)', r'في (\d+)']:
            match = re.search(pattern, description or "")
            if match: return f"فليكس {match.group(1)}"
        return name or ("فليكس" if "فليكس" in (description or "") else "الباقة الحالية")

    def extract_all_discount_offers(self, offers_data: List) -> List[Dict]:
        all_offers = []
        for offer_group in offers_data or []:
            if not isinstance(offer_group, dict): continue
            products = (offer_group.get('parts', {}) or {}).get('productOffering', []) or []
            for product in products:
                for line_item in product.get('lineItem', []) or []:
                    if not isinstance(line_item, dict): continue
                    offer_type = line_item.get('type', '')
                    description = line_item.get('desc', '')
                    if offer_type not in ('Access fees Discount', 'Usage fees Discount'): continue
                    if not any(k in description for k in ('جدد', 'خصم', 'بدل', 'خليك')): continue
                    price = self.extract_price_from_desc(description)
                    if not price: continue
                    tariff_id = self._char_value(line_item, 'TariffID')
                    product_id = self._char_value(line_item, 'TibcoID')
                    if not tariff_id or not product_id: continue
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
        headers = {
            'User-Agent': "okhttp/4.12.0", 'Connection': "Keep-Alive", 'Accept': "application/json", 'Accept-Encoding': "gzip", 'Content-Type': "application/json; charset=UTF-8", 'api-host': "ProductOrderingManagement", 'useCase': "InterventionTariff", 'Authorization': f"Bearer {self.token}", 'api-version': "v2", 'device-id': "b26ba335813fad21", 'x-agent-operatingsystem': "15", 'clientId': "AnaVodafoneAndroid", 'x-agent-device': "Samsung SM-A165F", 'x-agent-version': "2025.12.2", 'x-agent-build': "1080", 'msisdn': self.phone, 'Accept-Language': "ar"
        }
        try:
            response = self.session.post(url, data=json.dumps(payload, ensure_ascii=False), headers=headers, timeout=20)
            try: data = response.json()
            except Exception: data = {}
            status_value = str(data.get('status', '')).upper() if isinstance(data, dict) else ''
            if response.status_code in (200, 201) or (response.status_code == 400 and data.get('code') == "1008") or status_value == "SUCCESS":
                return True, f"✅ تم تفعيل خصم {offer['discount_amount']} جنيه على {offer['bundle_name']}"
            if data.get('code') == "1001": return False, "❌ تمت معالجة الطلب مسبقاً"
            if data.get('code') == "1002": return False, "❌ العرض غير متاح حالياً"
            return False, "❌ العرض غير متاح لخطك"
        except requests.exceptions.Timeout: return False, "⏰ انتهت مهلة الاتصال. حاول مرة أخرى."
        except Exception: return False, "❌ خطأ في الاتصال"


async def flex_discount_menu(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_valid_session(update, user_id)
    if not user: await show_login_screen(update, user_id, context); return

    context.user_data.pop('awaiting_flex_deduction_count', None)
    await query_or_message(update, "⏳ جاري البحث عن أفضل عرض خصم فليكس متاح...")

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

    if not all_offers:
        keyboard = [[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]
        await send_message(update, user_id, "❌ لم أجد عروض خصم فليكس متاحة على الخط حالياً.", InlineKeyboardMarkup(keyboard)); return

    context.user_data['flex_discount_token'] = service.token
    best = all_offers[0]
    context.user_data['flex_discount_offer'] = best
    text = (
        "💰 عرض خصم فليكس متاح\n\n"
        f"📦 الباقة: {best['bundle_name']}\n"
        f"💰 السعر الأصلي: {best['original_price']} جنيه\n"
        f"💸 سعر الخصم: {best['discounted_price']} جنيه\n"
        f"✨ نسبة الخصم: {best['discount_percentage']:.1f}%\n"
        f"📝 الوصف: {best['desc']}\n\n"
        "⚠️ هل تريد تفعيل العرض؟"
    )
    keyboard = [
        [InlineKeyboardButton("✅ تأكيد التفعيل", callback_data="flex_discount_confirm"), InlineKeyboardButton("❌ إلغاء", callback_data="flex_discount_cancel")],
        [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]
    ]
    await send_message(update, user_id, text, InlineKeyboardMarkup(keyboard))


async def flex_discount_confirm(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    offer = context.user_data.get('flex_discount_offer')
    if not offer: await query_or_message(update, "⚠️ بيانات العرض انتهت. افتح خصم فليكس من جديد."); return
    user = await ensure_valid_session(update, user_id)
    if not user: return

    lock = get_user_operation_lock(user_id)
    if lock.locked(): await query_or_message(update, "⏳ فيه عملية أخرى شغالة على الحساب. استنى لحد ما تخلص."); return

    async with lock:
        await query_or_message(update, "⏳ جاري تفعيل عرض خصم فليكس...")
        discount_token = context.user_data.get('flex_discount_token') or user['access_token']
        service = FlexDiscountService(discount_token, user['phone'])
        success, message = await asyncio.to_thread(service.purchase_offer, offer)

    context.user_data.pop('flex_discount_offer', None); context.user_data.pop('flex_discount_token', None)
    keyboard = [[InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]
    await send_message(update, user_id, message, InlineKeyboardMarkup(keyboard))

# ================== خدمات الباقات والتحويل ==================
FLEX_BUNDLES = [{"number": str(i), "name": n, "id": id_, "price": p} for i, (n, id_, p) in enumerate([("فليكس 40", "Flex_2021_511", 40), ("فليكس 45", "Flex_2024_627", 45), ("فليكس 60", "Flex_2021_513", 60), ("فليكس 70", "Flex_2024_629", 70), ("فليكس 90", "Flex_2021_515", 90), ("فليكس 100", "Flex_2024_631", 100), ("فليكس 130", "Flex_2021_517", 130), ("فليكس 150", "Flex_2024_633", 150), ("فليكس 260", "Flex_2021_523", 260), ("فليكس 300", "Flex_2024_637", 300)], 1)]
FLEX_ACP_ENC_PRODUCT_ID = "SBWbw/gsvm1cU1nPBj7HCg6MNEaAfyY56Kxz53nXBwpe6Z4c2t1DgiO2OM2hZwGVJaztwhZu7DWZiE2Ic5evFLqZfV/QaAOWQcS3m8bZCVD/wmRvbEvtfv16FTwgzWMjUQErPqXuYIMnePuK3H+MwQ8iFKqpvQ1d7qrPz05JlpUXKn2GM14uKA=="

def get_flex_bundle_by_number(number: str) -> Optional[Dict[str, Any]]:
    return next((b for b in FLEX_BUNDLES if b['number'] == str(number)), None)

async def packages_services_menu(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_valid_session(update, user_id): await show_login_screen(update, user_id, context); return
    keyboard = [[InlineKeyboardButton("⚡ باقات فليكس", callback_data="packages_flex")], [InlineKeyboardButton("🔄 تحويل 14 قرش", callback_data="packages_convert14")], [InlineKeyboardButton("🔙 القائمة الرئيسية", callback_data="main_menu")]]
    await query_or_message(update, "🔄 تحويل انظمه\n\nاختر الخدمة المطلوبة:")
    await send_message(update, user_id, "اختر من الأزرار:", InlineKeyboardMarkup(keyboard))

async def flex_bundles_menu(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_valid_session(update, user_id): return
    bundle_2021, bundle_2024 = [b for b in FLEX_BUNDLES if "2021" in b['id']], [b for b in FLEX_BUNDLES if "2024" in b['id']]
    keyboard = [[InlineKeyboardButton("⚡ نظام 2021", callback_data="packages_ignore"), InlineKeyboardButton("🆕 نظام 2024", callback_data="packages_ignore")]]
    for i in range(max(len(bundle_2021), len(bundle_2024))):
        row = []
        if i < len(bundle_2021): row.append(InlineKeyboardButton(f"{bundle_2021[i]['name']} - {bundle_2021[i]['price']}ج", callback_data=f"packages_flex_{bundle_2021[i]['number']}"))
        if i < len(bundle_2024): row.append(InlineKeyboardButton(f"{bundle_2024[i]['name']} - {bundle_2024[i]['price']}ج", callback_data=f"packages_flex_{bundle_2024[i]['number']}"))
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton("🔙 تحويل انظمه", callback_data="packages_services")])
    await query_or_message(update, "⚡ باقات فليكس\n\nاختر الباقة المناسبة:")
    await send_message(update, user_id, "نظام 2021 ونظام 2024:", InlineKeyboardMarkup(keyboard))

async def flex_bundle_confirm_screen(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE, bundle_number: str) -> None:
    bundle = get_flex_bundle_by_number(bundle_number)
    user = await ensure_valid_session(update, user_id)
    if not user or not bundle: await query_or_message(update, "❌ حدث خطأ في اختيار الباقة."); return
    context.user_data['packages_pending_flex'] = bundle_number
    keyboard = [[InlineKeyboardButton("✅ تأكيد التفعيل", callback_data=f"packages_flex_confirm_{bundle_number}"), InlineKeyboardButton("❌ إلغاء", callback_data="packages_services")]]
    await query_or_message(update, f"⚡ تأكيد تفعيل الباقة\n\n📱 الرقم: {user['phone']}\n📦 الباقة: {bundle['name']}\n💰 السعر: {bundle['price']} جنيه\n\nهل تريد تفعيل هذه الباقة؟")
    await send_message(update, user_id, "اضغط تأكيد للتنفيذ:", InlineKeyboardMarkup(keyboard))

async def execute_flex_bundle_service(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE, bundle_number: str) -> None:
    bundle = get_flex_bundle_by_number(bundle_number)
    user = await ensure_valid_session(update, user_id)
    if not user or not bundle: await query_or_message(update, "❌ بيانات الباقة غير متاحة."); return
    lock = get_user_operation_lock(user_id)
    if lock.locked(): await query_or_message(update, "⏳ فيه عملية أخرى شغالة على الحساب. استنى لحد ما تخلص."); return
    async with lock:
        await query_or_message(update, f"⏳ جاري تفعيل {bundle['name']}...")
        payload = {"channel": {"name": "MobileApp"}, "orderItem": [{"action": "insert", "id": bundle['id'], "product": {"characteristic": [{"name": "PaymentMethod", "value": "ACP"}, {"name": "ACP", "value": "True"}, {"name": "SMSID", "value": "MUTE_SMS"}, {"name": "LangId", "value": "en"}, {"name": "ExecutionType", "value": "Sync"}], "encProductId": FLEX_ACP_ENC_PRODUCT_ID, "id": bundle['id'], "relatedParty": [{"id": user['phone'], "name": "MSISDN", "role": "Subscriber"}]}, "eCode": 0}], "@type": "FlexACPRenewal"}
        headers = build_base_headers({'api-host': 'ProductOrderingManagement', 'useCase': 'FlexACPRenewal', 'Authorization': f"Bearer {user['access_token']}", 'api-version': 'v2', 'msisdn': user['phone']})
        try:
            response = await asyncio.to_thread(lambda: HTTP_SESSION.post('https://mobile.vodafone.com.eg/services/dxl/pom/productOrder', headers=headers, json=payload, timeout=30))
            try: result = response.json()
            except: result = {}
            code = str(result.get('code', '')) if isinstance(result, dict) else ''
            if response.status_code in (200, 201) or code == '3999':
                msg = f"✅ تم تحويل {bundle['name']} بنجاح!\n\n📱 الرقم: {user['phone']}\n💰 السعر: {bundle['price']} جنيه" + ("\n\n✅ برجاء التحقق من اشتراكاتك للتأكيد" if code == '3999' else "")
            else: msg = f"❌ فشل تحويل {bundle['name']}\nكود الحالة: {response.status_code}\nالسبب: {result.get('reason', 'الطلب غير متاح حالياً') if isinstance(result, dict) else 'الطلب غير متاح حالياً'}"
        except requests.exceptions.Timeout: msg = "⏰ انتهت مهلة الاتصال. حاول مرة أخرى."
        except Exception as e: log_action(user_id, 'FLEX_BUNDLE_FAIL', str(e)); msg = "❌ حدث خطأ أثناء تفعيل الباقة."
    context.user_data.pop('packages_pending_flex', None)
    await send_message(update, user_id, msg, InlineKeyboardMarkup([[InlineKeyboardButton("🔙 تحويل انظمه", callback_data="packages_services")]]))

async def convert14_confirm_screen(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = await ensure_valid_session(update, user_id)
    if not user: return
    context.user_data['packages_convert14_confirm'] = True
    await query_or_message(update, f"🔄 تحويل 14 قرش\n\n📱 الرقم: {user['phone']}\n📦 الباقة: ريح بالك كله ب14 قرش\n\n⚠️ هل تريد تنفيذ عملية التحويل؟")
    await send_message(update, user_id, "اضغط تأكيد للتنفيذ:", InlineKeyboardMarkup([[InlineKeyboardButton("✅ تأكيد التحويل", callback_data="packages_convert14_confirm"), InlineKeyboardButton("❌ إلغاء", callback_data="packages_services")]]))

async def execute_convert14_service(update: Update, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.user_data.get('packages_convert14_confirm'): await query_or_message(update, "⚠️ بيانات العملية انتهت. افتح الخدمة من جديد."); return
    user = await ensure_valid_session(update, user_id)
    if not user: return
    lock = get_user_operation_lock(user_id)
    if lock.locked(): await query_or_message(update, "⏳ فيه عملية أخرى شغالة على الحساب. استنى لحد ما تخلص."); return
    async with lock:
        await query_or_message(update, "⏳ جاري تحويل الرقم إلى باقة 14 قرش...")
        phone, token = user['phone'], user['access_token']
        headers = build_base_headers({'api-host': 'EligibleProductOfferingHost', 'useCase': 'Tarrifs', 'Authorization': f'Bearer {token}', 'api-version': 'v2', 'msisdn': phone})
        try:
            r = await asyncio.to_thread(lambda: HTTP_SESSION.get('https://mobile.vodafone.com.eg/services/dxl/epo/eligibleProductOffering', params={'customerAccountId': phone, 'parts.customerAccount.type': 'Consumer', 'Accept-Language': 'ar', 'type': 'Tarrifs'}, headers=headers, timeout=30))
            if r.status_code != 200: raise RuntimeError(f'EPO status {r.status_code}')
            enc_product_id = tibco_id = tariff_id = None; rank = None
            for item in r.json() if isinstance(r.json(), list) else []:
                for product in (item.get('parts', {}) or {}).get('productOffering', []) or []:
                    if product.get('name') != 'ريح بالك كله ب14 قرش': continue
                    for id_item in product.get('id', []) or []:
                        if id_item.get('schemeName') == 'EncProductID': enc_product_id = id_item.get('value')
                        elif id_item.get('schemeID') == 'ProductID': tibco_id = id_item.get('value')
                        elif id_item.get('schemeID') == 'TariffID': tariff_id = id_item.get('value')
                    for ch in (product.get('specification', {}) or {}).get('characteristicsValue', []) or []:
                        if ch.get('characteristicName') == 'Rank': rank = ch.get('value')
                    break
                if enc_product_id: break
            if not enc_product_id: msg = "❌ باقة ريح بالك كله ب14 قرش غير متاحة على الخط حالياً."
            else:
                payload = {"channel": {"name": "MobileApp"}, "orderItem": [{"action": "add", "id": tibco_id, "itemPrice": [{"name": "OriginalPrice", "price": {"taxIncludedAmount": {"unit": "LE", "value": "0"}}}], "product": {"characteristic": [{"name": "TariffRank", "value": str(rank or '1')}, {"name": "TariffID", "value": tariff_id or ''}, {"name": "Quota", "value": ""}, {"name": "Validity", "@type": "MONTH", "value": ""}, {"name": "InterventionFlag", "value": "0"}, {"name": "DestNoOfSeats", "value": ""}], "encProductId": enc_product_id, "productSpecification": [{"id": "ConsumerType", "name": "Category"}, {"id": "0", "name": "RatePlanType"}, {"id": "Other", "name": "BundleType"}], "relatedParty": [{"id": phone, "name": "MSISDN", "@referredType": "prepaid", "role": "Subscriber"}, {"id": tariff_id or '470', "name": "TariffID", "@referredType": "prepaid", "role": "TariffID"}]}, "eCode": 0}], "@type": "Tariff"}
                post_headers = build_base_headers({'api-host': 'ProductOrderingManagement', 'useCase': 'Tariff', 'Authorization': f'Bearer {token}', 'api-version': 'v2', 'msisdn': phone})
                rp = await asyncio.to_thread(lambda: HTTP_SESSION.post('https://mobile.vodafone.com.eg/services/dxl/pom/productOrder', headers=post_headers, json=payload, timeout=30))
                msg = f"✅ تم إرسال طلب التحويل إلى باقة 14 قرش بنجاح!\n\n📱 الرقم: {phone}\n📦 الباقة: ريح بالك كله ب14 قرش" if rp.status_code in (200, 201, 400) else f"❌ فشل التحويل - كود الخادم: {rp.status_code}"
        except requests.exceptions.Timeout: msg = "⏰ انتهت مهلة الاتصال. حاول مرة أخرى."
        except Exception as e: log_action(user_id, 'CONVERT14_FAIL', str(e)); msg = "❌ فشل في جلب أو تنفيذ تحويل 14 قرش."
    context.user_data.pop('packages_convert14_confirm', None)
    await send_message(update, user_id, msg, InlineKeyboardMarkup([[InlineKeyboardButton("🔙 تحويل انظمه", callback_data="packages_services")]]))

# ================== دوال تعليق دعوتين (Anti-Ban) ==================
sendonly_stats = {"total_success": 0, "total_attempts": 0, "user_logs": {}}
SENDONLY_STATS_FILE = "sendonly_stats.json"

def load_sendonly_stats():
    global sendonly_stats
    if os.path.exists(SENDONLY_STATS_FILE):
        with open(SENDONLY_STATS_FILE, "r", encoding="utf-8") as f: sendonly_stats = json.load(f)

def save_sendonly_stats():
    with open(SENDONLY_STATS_FILE, "w", encoding="utf-8") as f: json.dump(sendonly_stats, f, indent=4, ensure_ascii=False)

load_sendonly_stats()

SENDONLY_SELECT_OWNER, SENDONLY_OWNER_PASSWORD, SENDONLY_OWNER_NUM, SENDONLY_OWNER_PASS, SENDONLY_MEMBER_NUM, SENDONLY_PERCENTAGE, SENDONLY_COUNT = range(20, 27)

def get_authorization_sendonly(number: str, password: str) -> dict:
    headers = {'User-Agent': 'okhttp/4.12.0', 'Accept': 'application/json, text/plain, */*', 'Content-Type': 'application/x-www-form-urlencoded', 'silentLogin': 'true', 'x-agent-operatingsystem': '13', 'clientId': 'AnaVodafoneAndroid', 'Accept-Language': 'ar', 'x-agent-device': 'Samsung SM-A515F', 'x-agent-version': '2026.2.3', 'x-agent-build': '1117', 'digitalId': generate_digital_id(), 'device-id': generate_device_id()}
    data = {'grant_type': 'password', 'username': number, 'password': password, 'client_secret': '95fd95fb-7489-4958-8ae6-d31a525cd20a', 'client_id': 'ana-vodafone-app'}
    try:
        resp = HTTP_SESSION.post('https://mobile.vodafone.com.eg/auth/realms/vf-realm/protocol/openid-connect/token', headers=headers, data=data, timeout=10)
        if resp.status_code == 200:
            token = resp.json().get('access_token')
            if token: return {"status": "success", "token": "Bearer " + token}
        return {"status": "error", "message": f"كود {resp.status_code}"}
    except Exception as e: return {"status": "error", "message": str(e)}

async def get_multiple_tokens_sendonly(owner_number: str, owner_password: str, count: int) -> List[str]:
    tokens = []
    async def login_task():
        # التعديل هنا: تأخير من 3 لـ 4 ثواني
        await asyncio.sleep(random.uniform(3.0, 4.0))
        result = await asyncio.to_thread(get_authorization_sendonly, owner_number, owner_password)
        if result["status"] == "success": tokens.append(result["token"])
    await asyncio.gather(*(login_task() for _ in range(count)))
    return tokens

async def send_invitation_mobile_aiohttp(session: aiohttp.ClientSession, owner_number: str, member_number: str, token: str, percentage: int) -> dict:
    headers = {'User-Agent': 'okhttp/4.12.0', 'Connection': 'Keep-Alive', 'Accept': 'application/json', 'Authorization': token, 'api-version': 'v2', 'device-id': generate_device_id(), 'x-agent-operatingsystem': '13', 'clientId': 'AnaVodafoneAndroid', 'x-agent-device': 'Samsung SM-A515F', 'x-agent-version': '2026.2.3', 'x-agent-build': '1117', 'msisdn': owner_number, 'Accept-Language': 'ar', 'Content-Type': 'application/json; charset=UTF-8'}
    json_data = {'category': [{'listHierarchyId': 'PackageID', 'value': '523'}, {'listHierarchyId': 'TemplateID', 'value': '47'}, {'listHierarchyId': 'TierID', 'value': '523'}], 'parts': {'characteristicsValue': {'characteristicsValue': [{'characteristicName': 'quotaDist1', 'type': 'percentage', 'value': str(percentage)}]}, 'member': [{'id': [{'schemeName': 'MSISDN', 'value': owner_number}], 'type': 'Owner'}, {'id': [{'schemeName': 'MSISDN', 'value': member_number}], 'type': 'Member'}]}, 'type': 'SendInvitation'}
    try:
        async with session.post('https://mobile.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup', headers=headers, json=json_data, timeout=15) as resp:
            return {"success": resp.status in (200, 201), "status_code": resp.status, "source": "mobile"}
    except Exception as e: return {"success": False, "status_code": 0, "source": "mobile", "error": str(e)}

async def send_invitation_web_aiohttp(session: aiohttp.ClientSession, owner_number: str, member_number: str, token: str, percentage: int) -> dict:
    headers = {'User-Agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Mobile Safari/537.36', 'Accept': 'application/json', 'Content-Type': 'application/json', 'sec-ch-ua': '"Chromium";v="137", "Not/A)Brand";v="24"', 'msisdn': owner_number, 'Accept-Language': 'AR', 'sec-ch-ua-mobile': '?1', 'Authorization': token, 'clientId': 'WebsiteConsumer', 'sec-ch-ua-platform': '"Android"', 'Origin': 'https://web.vodafone.com.eg', 'Sec-Fetch-Site': 'same-origin', 'Sec-Fetch-Mode': 'cors', 'Sec-Fetch-Dest': 'empty', 'Referer': 'https://web.vodafone.com.eg/spa/familySharing'}
    json_data = {'name': 'FlexFamily', 'type': 'SendInvitation', 'category': [{'value': '523', 'listHierarchyId': 'PackageID'}, {'value': '47', 'listHierarchyId': 'TemplateID'}, {'value': '523', 'listHierarchyId': 'TierID'}, {'value': 'percentage', 'listHierarchyId': 'familybehavior'}], 'parts': {'member': [{'id': [{'value': owner_number, 'schemeName': 'MSISDN'}], 'type': 'Owner'}, {'id': [{'value': member_number, 'schemeName': 'MSISDN'}], 'type': 'Member'}], 'characteristicsValue': {'characteristicsValue': [{'characteristicName': 'quotaDist1', 'value': str(percentage), 'type': 'percentage'}]}}}
    try:
        async with session.post('https://web.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup', headers=headers, json=json_data, timeout=15) as resp:
            return {"success": resp.status in (200, 201), "status_code": resp.status, "source": "web"}
    except Exception as e: return {"success": False, "status_code": 0, "source": "web", "error": str(e)}

async def send_invites_batch(owner_number: str, member_number: str, tokens: List[str], percentage: int, count: int) -> List[dict]:
    async with aiohttp.ClientSession() as session:
        tasks = []
        half = count // 2
        for i in range(half):
            if i < len(tokens): tasks.append(send_invitation_mobile_aiohttp(session, owner_number, member_number, tokens[i], percentage))
        for i in range(half, count):
            if i < len(tokens): tasks.append(send_invitation_web_aiohttp(session, owner_number, member_number, tokens[i], percentage))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return [{"success": False, "status_code": 0, "error": str(r)} if isinstance(r, Exception) else r for r in results]

def remove_member_sendonly(owner_number: str, token: str, member_number: str) -> bool:
    headers = {'User-Agent': 'okhttp/4.12.0', 'Connection': 'Keep-Alive', 'Accept': 'application/json', 'Authorization': token, 'api-version': 'v2', 'device-id': generate_device_id(), 'x-agent-operatingsystem': '13', 'clientId': 'AnaVodafoneAndroid', 'x-agent-device': 'Samsung SM-A515F', 'x-agent-version': '2026.2.3', 'x-agent-build': '1117', 'msisdn': owner_number, 'Accept-Language': 'ar', 'Content-Type': 'application/json; charset=UTF-8'}
    payload = {'category': [{'listHierarchyId': 'TemplateID', 'value': '47'}], 'createdBy': {'value': 'MobileApp'}, 'parts': {'characteristicsValue': {'characteristicsValue': [{'characteristicName': 'Disconnect', 'value': '0'}, {'characteristicName': 'LastMemberDeletion', 'value': '1'}]}, 'member': [{'id': [{'schemeName': 'MSISDN', 'value': owner_number}], 'type': 'Owner'}, {'id': [{'schemeName': 'MSISDN', 'value': member_number}], 'type': 'Member'}]}, 'type': 'FamilyRemoveMember'}
    try: return HTTP_SESSION.post('https://mobile.vodafone.com.eg/services/dxl/cg/customerGroupAPI/customerGroup', headers=headers, json=payload, timeout=15).status_code in (200, 201)
    except: return False

# ================== واجهة تعليق دعوتين ==================
async def sendonly_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    user_id = update.effective_user.id
    if not await require_subscription_or_free(update, context, is_callback=False): return ConversationHandler.END
    keyboard = []
    current = get_user(user_id)
    if current: keyboard.append([InlineKeyboardButton(f"📱 استخدام الجلسة الحالية: {current['phone']}", callback_data="sendonly_use_current")])
    for num in get_saved_numbers(user_id):
        if not current or num != current['phone']: keyboard.append([InlineKeyboardButton(f"📞 {num}" + (" 🔑" if get_valid_token(user_id, num) else ""), callback_data=f"sendonly_owner_{num}")])
    keyboard.append([InlineKeyboardButton("➕ إدخال رقم جديد يدوياً", callback_data="sendonly_manual")])
    keyboard.append([InlineKeyboardButton("🔙 العودة للقائمة الرئيسية", callback_data="main_menu")])
    text = ("🚀 **نظام تعليق الدعوتين الخارق (VIP)** 🚀\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "✨ **المميزات:**\n"
            "• إرسال متزامن (حتى 50 دعوة في نفس الوقت)\n"
            "• محاولات لا نهائية حتى النجاح\n"
            "• تفادي الحظر بذكاء (Smart Scheduling)\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "🔐 **أولاً: اختر رقم المالك:**")
    if update.callback_query:
        await update.callback_query.answer()
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    else: await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
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
        phone = data[16:]
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
    password, owner, user_id = update.message.text, context.user_data['sendonly_owner'], update.effective_user.id
    if get_authorization_sendonly(owner, password)["status"] != "success":
        await update.message.reply_text("❌ فشل تسجيل الدخول.\nحاول مرة أخرى أو اختر رقماً آخر.", parse_mode=None)
        return await sendonly_menu(update, context)
    save_password(user_id, owner, password, 24)
    context.user_data['sendonly_owner_password'] = password
    await update.message.reply_text("✅ تم تسجيل الدخول.\n📱 الآن أرسل رقم العضو:", parse_mode=None)
    return SENDONLY_MEMBER_NUM

async def sendonly_owner_num_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    valid, msg = validate_phone(text)
    if not valid: await update.message.reply_text(f"{msg}\nأعد إرسال رقم المالك:", parse_mode=None); return SENDONLY_OWNER_NUM
    context.user_data['sendonly_owner'] = text
    await update.message.reply_text("🔑 أدخل كلمة مرور المالك:", parse_mode=None)
    return SENDONLY_OWNER_PASS

async def sendonly_owner_pass_manual(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    password, owner, user_id = update.message.text, context.user_data['sendonly_owner'], update.effective_user.id
    if get_authorization_sendonly(owner, password)["status"] != "success":
        await update.message.reply_text("❌ فشل تسجيل الدخول.\nحاول مرة أخرى.", parse_mode=None)
        return ConversationHandler.END
    save_password(user_id, owner, password, 24)
    context.user_data['sendonly_owner_password'] = password
    await update.message.reply_text("✅ تم تسجيل الدخول.\n📱 الآن أرسل رقم العضو:", parse_mode=None)
    return SENDONLY_MEMBER_NUM

async def sendonly_member_number(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    text = update.message.text.strip()
    valid, msg = validate_phone(text)
    if not valid: await update.message.reply_text(f"{msg}\nأعد إرسال رقم العضو:", parse_mode=None); return SENDONLY_MEMBER_NUM
    if text == context.user_data.get('sendonly_owner'): await update.message.reply_text("❌ رقم العضو لا يمكن أن يساوي رقم المالك. أرسل رقماً آخر:", parse_mode=None); return SENDONLY_MEMBER_NUM
    context.user_data['sendonly_member'] = text
    await update.message.reply_text("📊 اختر نسبة الدعوة:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("10%", callback_data="sendonly_perc_10")], [InlineKeyboardButton("20%", callback_data="sendonly_perc_20")], [InlineKeyboardButton("40%", callback_data="sendonly_perc_40")]]), parse_mode=None)
    return SENDONLY_PERCENTAGE

async def sendonly_percentage_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    context.user_data['sendonly_percentage'] = 10 if query.data == "sendonly_perc_10" else 20 if query.data == "sendonly_perc_20" else 40
    await query.edit_message_text("⚡ اختر عدد الدعوات المتزامنة:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("10 دعوات", callback_data="sendonly_cnt_10"), InlineKeyboardButton("20 دعوة", callback_data="sendonly_cnt_20")], [InlineKeyboardButton("30 دعوة", callback_data="sendonly_cnt_30"), InlineKeyboardButton("50 دعوة", callback_data="sendonly_cnt_50")]]), parse_mode=None)
    return SENDONLY_COUNT

async def sendonly_count_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query; await query.answer()
    count = int(query.data.split("_")[-1]); context.user_data['sendonly_count'] = count
    owner_number, owner_password, member_number, percentage = context.user_data['sendonly_owner'], context.user_data.get('sendonly_owner_password'), context.user_data['sendonly_member'], context.user_data['sendonly_percentage']
    if not owner_password: await query.edit_message_text("❌ لا توجد كلمة مرور للرقم. أعد استخدام الأمر /sendonly.", parse_mode=None); return ConversationHandler.END
    await query.edit_message_text(f"✅ تم التحقق.\n⚙️ هبدأ بإرسال {count} دعوة في نفس الوقت، ومحاولات لا نهائية لحد ما 2 دعوة ينجحوا ويتعلقوا فعلاً.", parse_mode=None)
    await context.bot.send_message(chat_id=query.message.chat_id, text="يمكنك إيقاف العملية في أي وقت بالضغط على الزر أدناه.", reply_markup=ReplyKeyboardMarkup([[KeyboardButton("⏹️ إيقاف العملية")]], resize_keyboard=True))
    asyncio.create_task(run_sendonly_process(update, context, owner_number, owner_password, member_number, percentage, count))
    return ConversationHandler.END

async def run_sendonly_process(update: Update, context: ContextTypes.DEFAULT_TYPE, owner_number: str, owner_password: str, member_number: str, percentage: int, count: int):
    user_id = update.effective_user.id
    stop_event = asyncio.Event(); context.user_data['sendonly_stop_event'] = stop_event
    chat_id = update.callback_query.message.chat_id if update.callback_query else update.message.chat_id
    status_msg = await context.bot.send_message(chat_id=chat_id, text="🔄 جاري تجهيز العملية...", parse_mode=None)

    async def update_status(text: str):
        try: await context.bot.edit_message_text(chat_id=chat_id, message_id=status_msg.message_id, text=text, parse_mode=None)
        except: pass

    attempt = 1
    while not stop_event.is_set():
        await update_status(f"🔄 المحاولة رقم {attempt}\n⏳ جلب {count} توكن...")
        tokens = await get_multiple_tokens_sendonly(owner_number, owner_password, count)
        if not tokens:
            await update_status(f"❌ المحاولة {attempt}: لم يتم جلب توكنات.\n⏳ انتظار 10 ثواني...")
            await asyncio.sleep(10); attempt += 1; continue
        
        await update_status(f"🔄 المحاولة رقم {attempt}\n✅ تم جلب {len(tokens)} توكن\n📡 إرسال {count} دعوة بنسبة {percentage}%...")
        results = await send_invites_batch(owner_number, member_number, tokens, percentage, count)
        successful = sum(1 for r in results if r.get("success"))
        
        is_verified = False
        try:
            member_data = await get_family_members(tokens[0].replace("Bearer ", ""), owner_number) if tokens else {'pending': []}
            pending_for_member = [p for p in member_data.get('pending', []) if p['phone'] == member_number]
            if pending_for_member and pending_for_member[0].get('count', 0) >= 2: is_verified = True
        except: pass

        if successful >= 2 and is_verified:
            success_message = ("🎉 نجاح للرقم `{}` في المحاولة {}!\n"
                               "تم إرسال الدعوات، والفحص أكد أن الرقم معلق مرتين الآن.\n"
                               "تم تعليق الحساب بنجاح.\n"
                               "📉 طلبات متبقية: {}\n\n"
                               "📊 تفاصيل الدعوات (للعلم):\n").format(member_number, attempt, count - successful)
            for i, r in enumerate(results[:6]):
                src = "🌐 سيرفر ويب" if r.get('source') == 'web' else "📱 سيرفر تطبيق"
                icon = '✅' if r.get('success') else '❌'
                status = "نجاح" if r.get('success') else "فشل"
                success_message += f"{src} {i+1} ➡️ {icon} {status}\n"
            success_message += "\n🎉 تم تحقيق المطلوب! العملية ناجحة."
            await update_status(success_message)
            sendonly_stats["total_success"] += 1; sendonly_stats["total_attempts"] += 1
            sendonly_stats["user_logs"].setdefault(user_id, {"success": 0, "attempts": 0})["success"] += 1
            sendonly_stats["user_logs"][user_id]["attempts"] += 1
            save_sendonly_stats(); break

        result_text = f"📊 نتائج المحاولة {attempt}:\n" + "".join(f"{'📱 موبايل' if r.get('source') == 'mobile' else '🌐 ويب'} (كود {r.get('status_code', '?')}) {'✅' if r.get('success') else '❌'}\n" for r in results) + f"\n✅ نجحت {successful}/{count} دعوات."
        
        await update_status(result_text + "\n⚠️ لم يتحقق شرط التعليق المزدوج.\n🗑️ جاري حذف العضو وإعادة المحاولة...")
        if tokens:
            for delay in [5, 10, 10]:
                if stop_event.is_set(): break
                await asyncio.sleep(delay)
                if await asyncio.to_thread(remove_member_sendonly, owner_number, tokens[0], member_number): break
        await asyncio.sleep(10); attempt += 1

    if stop_event.is_set():
        await update_status("⏹️ تم إيقاف العملية من قبل المستخدم.")
        sendonly_stats["total_attempts"] += 1; sendonly_stats["user_logs"].setdefault(user_id, {"success": 0, "attempts": 0})["attempts"] += 1; save_sendonly_stats()
    await context.bot.send_message(chat_id=chat_id, text="ℹ️ انتهت العملية.", reply_markup=ReplyKeyboardRemove(), parse_mode=None)
    context.user_data.pop('sendonly_stop_event', None)

async def stop_sendonly_process(update: Update, context: ContextTypes.DEFAULT_TYPE):
    stop_event = context.user_data.get('sendonly_stop_event')
    if stop_event:
        stop_event.set()
        await update.message.reply_text("⏹️ تم إرسال طلب إيقاف العملية... سيتم إيقافها قريباً.", reply_markup=ReplyKeyboardRemove(), parse_mode=None)
    else: await update.message.reply_text("ℹ️ لا توجد عملية 'تعليق دعوتين' جارية حالياً.", parse_mode=None)

async def sendonly_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): await update.message.reply_text("⛔ هذا الأمر للأدمن فقط.", parse_mode=None); return
    text = f"📊 إحصائيات تعليق دعوتين\n\n✅ عمليات ناجحة: {sendonly_stats['total_success']}\n🔄 إجمالي المحاولات: {sendonly_stats['total_attempts']}\n\n👥 المستخدمون:\n" + "".join(f"🆔 {uid}: نجاح {logs['success']} | محاولات {logs['attempts']}\n" for uid, logs in sendonly_stats["user_logs"].items())
    await update.message.reply_text(text, parse_mode=None)

async def reset_sendonly_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): return
    global sendonly_stats; sendonly_stats = {"total_success": 0, "total_attempts": 0, "user_logs": {}}
    save_sendonly_stats(); await update.message.reply_text("✅ تم تصفير إحصائيات تعليق دعوتين.", parse_mode=None)

# ================== لوحة تحكم الأدمن (المطورة) ==================
async def admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if not is_admin(user_id): 
        if update.message: await update.message.reply_text("⛔ غير مصرح.")
        return
    keyboard = [
        [InlineKeyboardButton("📊 إحصائيات البوت", callback_data="admin_stats")],
        [InlineKeyboardButton("👥 قائمة المستخدمين", callback_data="admin_users")],
        [InlineKeyboardButton("📢 بث رسالة جماعية", callback_data="admin_broadcast")],
        [InlineKeyboardButton("🎟️ إدارة الاشتراكات", callback_data="admin_subs_menu")],
        [InlineKeyboardButton("👑 إدارة الأدمن", callback_data="admin_manage_admins")],
        [InlineKeyboardButton("🔧 إعدادات البوت", callback_data="admin_settings")]
    ]
    text = "👑 **لوحة تحكم الأدمن الرئيسية** 👑\n━━━━━━━━━━━━━━━━━━━━\nأهلاً بك يا هندسة. اختر القسم الذي تريد إدارته:"
    if update.callback_query: await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    else: await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def admin_subs_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user.id): return
    keyboard = [
        [InlineKeyboardButton("➕ إضافة اشتراك", callback_data="admin_add_sub")],
        [InlineKeyboardButton("➖ إلغاء اشتراك", callback_data="admin_remove_sub")],
        [InlineKeyboardButton("📋 عرض المشتركين النشطين", callback_data="admin_list_subs")],
        [InlineKeyboardButton("⚙️ تبديل الوضع (مجاني/مدفوع)", callback_data="admin_toggle_mode")],
        [InlineKeyboardButton("🔙 رجوع للوحة التحكم", callback_data="admin_back")]
    ]
    text = "🎟️ **إدارة الاشتراكات**\n━━━━━━━━━━━━━━━━━━━━\nيمكنك من هنا التحكم في اشتراكات المستخدمين ووضع البوت:"
    try: await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')
    except: pass

async def admin_manage_admins(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user.id): return
    if query.from_user.id != ADMIN_ID:
        await query.edit_message_text("⛔ هذا القسم مخصص للأدمن الرئيسي فقط.", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 رجوع", callback_data="admin_back")]]))
        return
    keyboard = [
        [InlineKeyboardButton("➕ إضافة أدمن مساعد", callback_data="admin_add_admin")],
        [InlineKeyboardButton("➖ إزالة أدمن مساعد", callback_data="admin_remove_admin")],
        [InlineKeyboardButton("📋 عرض قائمة الأدمن", callback_data="admin_list_admins")],
        [InlineKeyboardButton("🔙 رجوع للوحة التحكم", callback_data="admin_back")]
    ]
    text = "👑 **إدارة الأدمن**\n━━━━━━━━━━━━━━━━━━━━\nيمكنك من هنا إضافة أو إزالة صلاحيات الأدمن المساعدين:"
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def admin_settings(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user.id): return
    keyboard = [
        [InlineKeyboardButton("🔄 تحديث حالة البوت", callback_data="admin_refresh_status")],
        [InlineKeyboardButton("🔙 رجوع للوحة التحكم", callback_data="admin_back")]
    ]
    text = "🔧 **إعدادات البوت**\n━━━━━━━━━━━━━━━━━━━━\nهنا يمكنك إدارة الإعدادات المتقدمة:"
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def admin_stats_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user.id): return
    st = get_stats()
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="admin_back")]]
    await query.edit_message_text(f"📊 إحصائيات البوت\n━━━━━━━━━━━━━━━━━━━━\n👥 إجمالي المستخدمين: {st['total_users']}\n🔐 جلسات نشطة: {st['active_sessions']}\n📞 أرقام محفوظة: {st['saved_numbers']}\n🚫 محظورون: {st['banned_count']}\n🎟️ مشتركون نشطون: {st['active_subs']}\n⚙️ الوضع الحالي: {'مجاني' if is_free_mode() else 'مدفوع (اشتراك)'}", reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def admin_users_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user.id): return
    users = get_all_users()
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="admin_back")]]
    if not users: await query.edit_message_text("لا يوجد مستخدمون حتى الآن.", reply_markup=InlineKeyboardMarkup(keyboard)); return
    text = "👥 قائمة المستخدمين:\n\n" + "".join(f"🆔 {uid}\n📅 أول ظهور: {first.strftime('%Y-%m-%d %H:%M')}\n🕒 آخر نشاط: {last.strftime('%Y-%m-%d %H:%M')}\n\n" for uid, first, last in users[:50])
    if len(users) > 50: text += f"... و {len(users)-50} مستخدم آخر. استخدم الأمر /users لرؤية الكل."
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user.id): return
    context.user_data['awaiting_broadcast'] = True
    await query.edit_message_text("📢 أرسل الرسالة التي تريد بثها لجميع المستخدمين:", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 إلغاء", callback_data="admin_back")]]))

async def admin_toggle_mode_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user.id): return
    current = is_free_mode(); set_free_mode(not current)
    await query.answer(f"✅ تم التبديل إلى: {'مجاني' if not current else 'مدفوع (اشتراك)'}", show_alert=True)
    await admin_subs_menu(update, context)

async def admin_add_sub_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user.id): return
    context.user_data['awaiting_add_sub'] = True
    await query.edit_message_text("➕ أرسل معرف المستخدم وعدد الأيام مفصولة بمسافة\nمثال: `123456789 30`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 إلغاء", callback_data="admin_subs_menu")]]), parse_mode='Markdown')

async def admin_remove_sub_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user.id): return
    context.user_data['awaiting_remove_sub'] = True
    await query.edit_message_text("➖ أرسل معرف المستخدم لإلغاء اشتراكه\nمثال: `123456789`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 إلغاء", callback_data="admin_subs_menu")]]), parse_mode='Markdown')

async def admin_list_subs_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user.id): return
    subs = list_active_subscriptions()
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="admin_subs_menu")]]
    if not subs: await query.edit_message_text("📭 لا يوجد مشتركون نشطون حالياً.", reply_markup=InlineKeyboardMarkup(keyboard)); return
    text = "✅ المشتركون النشطون:\n\n" + "".join(f"🆔 `{uid}` : ينتهي {exp.strftime('%Y-%m-%d')}\n" for uid, exp in subs[:50])
    if len(subs) > 50: text += f"\n... و {len(subs)-50} آخر."
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode='Markdown')

async def admin_add_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user.id) or query.from_user.id != ADMIN_ID: return
    context.user_data['awaiting_add_admin'] = True
    await query.edit_message_text("➕ أرسل معرف المستخدم لإضافته كأدمن مساعد:\nمثال: `123456789`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 إلغاء", callback_data="admin_manage_admins")]]), parse_mode='Markdown')

async def admin_remove_admin_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user.id) or query.from_user.id != ADMIN_ID: return
    context.user_data['awaiting_remove_admin'] = True
    await query.edit_message_text("➖ أرسل معرف المستخدم لإزالة صلاحيات الأدمن عنه:\nمثال: `123456789`", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙 إلغاء", callback_data="admin_manage_admins")]]), parse_mode='Markdown')

async def admin_list_admins_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user.id): return
    admins = list_admins()
    keyboard = [[InlineKeyboardButton("🔙 رجوع", callback_data="admin_manage_admins")]]
    text = "👑 قائمة الأدمن:\n" + "".join(f"👑 {aid} (رئيسي)\n" if aid == ADMIN_ID else f"👤 {aid}\n" for aid in admins)
    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode=None)

async def admin_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer()
    if not is_admin(query.from_user.id): return
    await admin_panel(update, context)

# ================== أوامر الأدمن النصية ==================
async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): await update.message.reply_text("⚠️ هذا الأمر متاح فقط للمطور.", parse_mode=None); return
    users = get_all_users()
    if not users: await update.message.reply_text("لا يوجد مستخدمون حتى الآن.", parse_mode=None); return
    text = "👥 قائمة المستخدمين:\n\n"
    for uid, first, last in users:
        text += f"🆔 {uid}\n📅 أول ظهور: {first.strftime('%Y-%m-%d %H:%M')}\n🕒 آخر نشاط: {last.strftime('%Y-%m-%d %H:%M')}\n\n"
        if len(text) > 3800: await update.message.reply_text(text, parse_mode=None); text = ""
    if text: await update.message.reply_text(text, parse_mode=None)

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): await update.message.reply_text("⚠️ هذا الأمر متاح فقط للمطور.", parse_mode=None); return
    st = get_stats()
    await update.message.reply_text(f"📊 إحصائيات البوت\n\n👥 إجمالي المستخدمين: {st['total_users']}\n🔐 جلسات نشطة: {st['active_sessions']}\n📞 أرقام محفوظة: {st['saved_numbers']}\n🚫 محظورون: {st['banned_count']}\n🎟️ مشتركون نشطون: {st['active_subs']}\n⚙️ الوضع الحالي: {'مجاني' if is_free_mode() else 'مدفوع (اشتراك)'}", parse_mode=None)

async def block_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args) != 1: await update.message.reply_text("الاستخدام: /block <user_id>", parse_mode=None); return
    try:
        uid = int(context.args[0])
        if uid == ADMIN_ID: await update.message.reply_text("لا يمكن حظر المطور نفسه."); return
        ban_user(uid); await update.message.reply_text(f"✅ تم حظر المستخدم {uid}.", parse_mode=None)
    except: await update.message.reply_text("معرف غير صالح.", parse_mode=None)

async def unblock_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args) != 1: await update.message.reply_text("الاستخدام: /unblock <user_id>", parse_mode=None); return
    try: uid = int(context.args[0]); unban_user(uid); await update.message.reply_text(f"✅ تم رفع الحظر عن المستخدم {uid}.", parse_mode=None)
    except: await update.message.reply_text("معرف غير صالح.", parse_mode=None)

async def maintenance_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    global MAINTENANCE_MODE
    if not is_admin(update.effective_user.id) or len(context.args) != 1: await update.message.reply_text("الاستخدام: /maintenance on  أو  /maintenance off", parse_mode=None); return
    MAINTENANCE_MODE = context.args[0].lower() == "on"
    await update.message.reply_text(f"{'🛠️ تم تفعيل وضع الصيانة.' if MAINTENANCE_MODE else '✅ تم إلغاء وضع الصيانة.'}", parse_mode=None)

async def broadcast_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or not context.args: await update.message.reply_text("الاستخدام: /broadcast <الرسالة>", parse_mode=None); return
    message = ' '.join(context.args); sent = 0
    for uid, _, _ in get_all_users():
        try: await context.bot.send_message(chat_id=uid, text=f"📢 إشعار من الإدارة:\n{message}", parse_mode=None); sent += 1
        except: pass
    await update.message.reply_text(f"✅ تم إرسال الإشعار إلى {sent} مستخدم.", parse_mode=None)

async def add_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or len(context.args) != 1: await update.message.reply_text("الاستخدام: /add_admin <user_id>", parse_mode=None); return
    try:
        uid = int(context.args[0])
        if is_admin(uid): await update.message.reply_text(f"⚠️ المستخدم {uid} هو بالفعل أدمن."); return
        conn = sqlite3.connect(DB_PATH); c = conn.cursor(); c.execute("INSERT INTO admins (telegram_id) VALUES (?)", (uid,)); conn.commit(); conn.close()
        await update.message.reply_text(f"✅ تم إضافة المستخدم {uid} كأدمن مساعد.")
    except: await update.message.reply_text("❌ معرف غير صالح.")

async def remove_admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID or len(context.args) != 1: await update.message.reply_text("الاستخدام: /remove_admin <user_id>", parse_mode=None); return
    try:
        uid = int(context.args[0])
        if uid == ADMIN_ID: await update.message.reply_text("⚠️ لا يمكن إزالة الأدمن الرئيسي."); return
        conn = sqlite3.connect(DB_PATH); c = conn.cursor(); c.execute("DELETE FROM admins WHERE telegram_id = ?", (uid,)); affected = c.rowcount; conn.commit(); conn.close()
        await update.message.reply_text(f"✅ تم إزالة صلاحيات الأدمن عن المستخدم {uid}." if affected > 0 else f"⚠️ المستخدم {uid} ليس أدمن.")
    except: await update.message.reply_text("❌ معرف غير صالح.")

async def list_admins_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): await update.message.reply_text("⛔ غير مصرح."); return
    admins = list_admins()
    await update.message.reply_text("👑 قائمة الأدمن:\n" + "".join(f"👑 {aid} (رئيسي)\n" if aid == ADMIN_ID else f"👤 {aid}\n" for aid in admins))

async def add_subscription_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args) != 2: await update.message.reply_text("الاستخدام: /add_subscription <user_id> <days>"); return
    try:
        uid, days = int(context.args[0]), int(context.args[1])
        if days <= 0: await update.message.reply_text("⚠️ عدد الأيام يجب أن يكون موجباً."); return
        expiry = add_subscription(uid, days)
        await update.message.reply_text(f"✅ تم إضافة اشتراك للمستخدم {uid} لمدة {days} يوماً.\nينتهي في: {expiry.strftime('%Y-%m-%d %H:%M:%S')}")
    except: await update.message.reply_text("❌ معرف أو عدد أيام غير صالح.")

async def remove_subscription_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args) != 1: await update.message.reply_text("الاستخدام: /remove_subscription <user_id>"); return
    try:
        uid = int(context.args[0])
        if remove_subscription(uid): await update.message.reply_text(f"✅ تم إلغاء اشتراك المستخدم {uid}.")
        else: await update.message.reply_text(f"⚠️ المستخدم {uid} ليس لديه اشتراك نشط.")
    except: await update.message.reply_text("❌ معرف غير صالح.")

async def check_subscription_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id) or len(context.args) != 1: await update.message.reply_text("الاستخدام: /check_subscription <user_id>"); return
    try:
        uid = int(context.args[0]); expiry = get_subscription_expiry(uid)
        await update.message.reply_text(f"📅 اشتراك المستخدم {uid} ينتهي في: {expiry.strftime('%Y-%m-%d %H:%M:%S')}" if expiry else f"ℹ️ المستخدم {uid} ليس لديه اشتراك نشط.")
    except: await update.message.reply_text("❌ معرف غير صالح.")

async def list_subscriptions_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): await update.message.reply_text("⛔ غير مصرح."); return
    subs = list_active_subscriptions()
    if not subs: await update.message.reply_text("📭 لا يوجد مشتركون نشطون حالياً."); return
    text = "✅ المشتركون النشطون:\n\n"
    for uid, exp in subs:
        text += f"🆔 {uid} : ينتهي {exp.strftime('%Y-%m-%d')}\n"
        if len(text) > 4000: await update.message.reply_text(text); text = ""
    if text: await update.message.reply_text(text)

async def toggle_free_mode_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id): await update.message.reply_text("⛔ غير مصرح."); return
    current = is_free_mode(); set_free_mode(not current)
    await update.message.reply_text(f"✅ تم تبديل وضع البوت إلى: {'مجاني' if not current else 'مدفوع (اشتراك)'}")

# ================== أوامر المساعدة والحفظ ==================
async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id in user_states: del user_states[user_id]
    context.user_data.pop('awaiting_broadcast', None)
    context.user_data.pop('awaiting_add_sub', None)
    context.user_data.pop('awaiting_remove_sub', None)
    context.user_data.pop('awaiting_add_admin', None)
    context.user_data.pop('awaiting_remove_admin', None)
    context.user_data.pop('awaiting_flex_deduction_count', None)
    await update.message.reply_text("✅ تم إلغاء العملية الحالية.", parse_mode=None)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_user_banned(user_id): await update.message.reply_text("🚫 أنت محظور من استخدام هذا البوت.", parse_mode=None); return
    text = "🔹 الأوامر المتاحة للمطور:\n/start - بدء البوت\n/savenumber <رقم> - حفظ رقم هاتف\n/cancel - إلغاء العملية\n/help - هذه المساعدة\n/users - عرض جميع المستخدمين\n/stats - إحصائيات البوت\n/block <user_id> - حظر مستخدم\n/unblock <user_id> - رفع الحظر\n/maintenance on/off - تفعيل/إلغاء وضع الصيانة\n/broadcast <رسالة> - إرسال إشعار للجميع\n/sendonly - تعليق دعوتين\n/sendonly_stats - إحصائيات التعليق\n/resetsendonly - تصفير الإحصائيات\n\n🔐 أوامر إدارة الأدمن والاشتراكات:\n/add_admin <user_id>\n/remove_admin <user_id>\n/list_admins\n/add_subscription <user_id> <days>\n/remove_subscription <user_id>\n/check_subscription <user_id>\n/list_subscriptions\n/toggle_free_mode\n\n📌 يمكنك استخدام الأزرار التفاعلية أيضًا." if is_admin(user_id) else "🔹 الأوامر المتاحة:\n/start - بدء البوت\n/savenumber <رقم> - حفظ رقم هاتف\n/cancel - إلغاء العملية\n/help - عرض هذه المساعدة\n/sendonly - تعليق دعوتين\n\n📌 يمكنك استخدام الأزرار التفاعلية لإدارة عائلتك."
    await update.message.reply_text(text, parse_mode=None)

async def save_number_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if is_user_banned(user_id): await update.message.reply_text("🚫 أنت محظور من استخدام هذا البوت.", parse_mode=None); return
    if len(context.args) != 1: await update.message.reply_text("الرجاء استخدام الأمر بالشكل: /savenumber <رقم الهاتف>", parse_mode=None); return
    number = context.args[0].strip(); save_number(user_id, number)
    await update.message.reply_text(f"✅ تم حفظ الرقم {number} في قائمة الأرقام المحفوظة.", parse_mode=None)

# ================== المعالجات الأساسية ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if MAINTENANCE_MODE and not is_admin(user_id): await update.message.reply_text("🛠️ البوت في وضع الصيانة حاليًا. عذرًا للإزعاج.", parse_mode=None); return
    record_user_activity(user_id)
    if is_user_banned(user_id): await update.message.reply_text("🚫 لقد تم حظرك من استخدام هذا البوت.", parse_mode=None); return
    if get_user(user_id): await show_main_menu(update, user_id, context)
    else:
        await send_message(update, user_id, "🐋 حــوت الـمـجـال | HOOT ALMGAL\n\n⚡ أهلاً بيك في عالم السرعة والتحكم\nإدارة حسابك وخدمات فليكس بسهولة من مكان واحد.\n\n🔐 سجّل دخولك للبدء")
        await show_login_screen(update, user_id, context)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if MAINTENANCE_MODE and not is_admin(user_id): await update.message.reply_text("🛠️ البوت في وضع الصيانة حاليًا. عذرًا للإزعاج.", parse_mode=None); return
    if is_user_banned(user_id): await update.message.reply_text("🚫 أنت محظور من استخدام هذا البوت.", parse_mode=None); return
    record_user_activity(user_id); text = update.message.text.strip()

    if context.user_data.get('awaiting_broadcast'):
        if not is_admin(user_id): context.user_data.pop('awaiting_broadcast', None); await update.message.reply_text("⛔ غير مصرح."); return
        sent = 0
        for uid, _, _ in get_all_users():
            try: await context.bot.send_message(chat_id=uid, text=f"📢 إشعار من الإدارة:\n{text}", parse_mode=None); sent += 1
            except: pass
        await update.message.reply_text(f"✅ تم إرسال الإشعار إلى {sent} مستخدم.", parse_mode=None)
        context.user_data.pop('awaiting_broadcast', None); return

    if context.user_data.get('awaiting_add_sub'):
        if not is_admin(user_id): context.user_data.pop('awaiting_add_sub', None); await update.message.reply_text("⛔ غير مصرح."); return
        parts = text.split()
        if len(parts) != 2: await update.message.reply_text("⚠️ أرسل معرف المستخدم وعدد الأيام مفصولة بمسافة."); return
        try: await update.message.reply_text(f"✅ تم إضافة اشتراك للمستخدم {int(parts[0])} لمدة {int(parts[1])} يوماً.\nينتهي في: {add_subscription(int(parts[0]), int(parts[1])).strftime('%Y-%m-%d %H:%M:%S')}")
        except: await update.message.reply_text("❌ معرف أو عدد أيام غير صالح.")
        context.user_data.pop('awaiting_add_sub', None); return

    if context.user_data.get('awaiting_remove_sub'):
        if not is_admin(user_id): context.user_data.pop('awaiting_remove_sub', None); await update.message.reply_text("⛔ غير مصرح."); return
        try:
            uid = int(text)
            await update.message.reply_text(f"✅ تم إلغاء اشتراك المستخدم {uid}." if remove_subscription(uid) else f"⚠️ المستخدم {uid} ليس لديه اشتراك نشط.")
        except: await update.message.reply_text("❌ معرف غير صالح.")
        context.user_data.pop('awaiting_remove_sub', None); return

    if context.user_data.get('awaiting_add_admin'):
        if user_id != ADMIN_ID: context.user_data.pop('awaiting_add_admin', None); await update.message.reply_text("⛔ غير مصرح."); return
        try:
            uid = int(text)
            if is_admin(uid): await update.message.reply_text(f"⚠️ المستخدم {uid} هو بالفعل أدمن.")
            else:
                conn = sqlite3.connect(DB_PATH); c = conn.cursor(); c.execute("INSERT INTO admins (telegram_id) VALUES (?)", (uid,)); conn.commit(); conn.close()
                await update.message.reply_text(f"✅ تم إضافة المستخدم {uid} كأدمن مساعد.")
        except: await update.message.reply_text("❌ معرف غير صالح.")
        context.user_data.pop('awaiting_add_admin', None); return

    if context.user_data.get('awaiting_remove_admin'):
        if user_id != ADMIN_ID: context.user_data.pop('awaiting_remove_admin', None); await update.message.reply_text("⛔ غير مصرح."); return
        try:
            uid = int(text)
            if uid == ADMIN_ID: await update.message.reply_text("⚠️ لا يمكن إزالة الأدمن الرئيسي.")
            else:
                conn = sqlite3.connect(DB_PATH); c = conn.cursor(); c.execute("DELETE FROM admins WHERE telegram_id = ?", (uid,)); affected = c.rowcount; conn.commit(); conn.close()
                await update.message.reply_text(f"✅ تم إزالة صلاحيات الأدمن عن المستخدم {uid}." if affected > 0 else f"⚠️ المستخدم {uid} ليس أدمن.")
        except: await update.message.reply_text("❌ معرف غير صالح.")
        context.user_data.pop('awaiting_remove_admin', None); return

    if context.user_data.get('awaiting_flex_deduction_count'):
        if not text.isdigit() or int(text) <= 0: await update.message.reply_text("❌ ابعت عدد صحيح أكبر من صفر.", parse_mode=None); return
        times = int(text)
        if times > 50: await update.message.reply_text("❌ الحد الأقصى 50 عملية في المرة الواحدة.", parse_mode=None); return
        context.user_data['awaiting_flex_deduction_count'] = False
        await execute_flex_deduction(update, user_id, context, times); return

    if user_id not in user_states:
        await show_login_screen(update, user_id, context); return
    state_data = user_states[user_id]; state = state_data.get('state')

    if state == 'add_two_days_member_password':
        member_phone, password = state_data.get('member_phone'), text
        if not member_phone or not re.fullmatch(r"01\d{9}", member_phone): 
            user_states.pop(user_id, None)
            await update.message.reply_text("❌ رقم الفرد غير صالح. افتح إدارة العائلة وحاول مرة أخرى.", parse_mode=None)
            return
        await update.message.reply_text(f"🔄 جاري تسجيل الدخول للرقم {member_phone}...", parse_mode=None)
        try:
            member_token, expiry = await login_vodafone(member_phone, password)
            if not member_token: raise ValueError("لم يتم استلام رمز جلسة صالح")
            save_token(user_id, member_phone, member_token, expiry)
        except Exception:
            log_action(user_id, "ADD_TWO_DAYS_MEMBER_LOGIN_FAIL", member_phone)
            user_states.pop(user_id, None)
            await update.message.reply_text("❌ فشل تسجيل الدخول للرقم ده. راجع الباسورد وحاول تاني من زرار تزويد يومين.", parse_mode=None)
            return
        user_states.pop(user_id, None)
        await add_two_days(update, user_id, context, target_phone=member_phone)
        return

    if state == 'login_phone':
        valid, msg = validate_phone(text)
        if not valid: await update.message.reply_text(f"{msg}\nأرسل رقم الهاتف الصحيح (مثال: 01012345678):", parse_mode=None); return
        user_states[user_id] = {'state': 'login_password', 'phone': text}
        await update.message.reply_text("🔑 أدخل كلمة المرور:", parse_mode=None)
    elif state == 'login_password':
        phone, password = state_data.get('phone'), text
        await update.message.reply_text("🔄 جاري تسجيل الدخول...", parse_mode=None)
        try:
            token, expiry = await login_vodafone(phone, password)
            save_user(user_id, phone, token, expiry); save_number(user_id, phone); save_password(user_id, phone, password, 24)
            await update.message.reply_text("✅ تم تسجيل الدخول بنجاح وتم حفظ الرقم.", parse_mode=None)
            del user_states[user_id]; await show_main_menu(update, user_id, context)
        except Exception as e:
            await update.message.reply_text(f"❌ فشل تسجيل الدخول: {str(e)}", parse_mode=None); del user_states[user_id]
    elif state == 'accept_password':
        member_phone, password = state_data.get('member_phone'), text
        owner = get_user(user_id)
        if not owner: await show_login_screen(update, user_id, context); del user_states[user_id]; return
        await update.message.reply_text("🔄 جاري قبول الدعوة...", parse_mode=None)
        try:
            member_token, _ = await login_vodafone(member_phone, password)
            rocket_add = next((p['phone'] for p in (await get_family_members(owner['access_token'], owner['phone'])).get('pending', []) if p['phone'] == member_phone and p.get('count', 0) >= 2), None)
            if await accept_invitation(member_token, member_phone, owner['phone']):
                await update.message.reply_text(f"✅ تم قبول الدعوة للرقم {member_phone}", parse_mode=None)
                if rocket_add:
                    rocket_list = context.user_data.get('rocket_members', [])
                    if rocket_add not in rocket_list: rocket_list.append(rocket_add); context.user_data['rocket_members'] = rocket_list
                await show_family_screen(update, user_id, context)
            else: await update.message.reply_text("❌ فشل قبول الدعوة", parse_mode=None)
            del user_states[user_id]
        except Exception as e: await update.message.reply_text(f"❌ خطأ: {str(e)}", parse_mode=None); del user_states[user_id]
    elif state == 'invite_phone':
        member_phone, percentage = text, state_data.get('percentage')
        owner = get_user(user_id)
        if not owner: await show_login_screen(update, user_id, context); del user_states[user_id]; return
        await update.message.reply_text(f"🔄 جاري إرسال دعوة إلى {member_phone}...", parse_mode=None)
        if await send_invitation(owner['access_token'], owner['phone'], member_phone, percentage):
            await update.message.reply_text(f"✅ تم إرسال الدعوة إلى {member_phone}", parse_mode=None)
        else: await update.message.reply_text("❌ فشل إرسال الدعوة", parse_mode=None)
        del user_states[user_id]; await show_family_screen(update, user_id, context)
    elif state == 'switch_account_ask':
        new_phone, password = state_data.get('new_phone'), text
        await update.message.reply_text("🔄 جاري تسجيل الدخول...", parse_mode=None)
        try:
            token, expiry = await login_vodafone(new_phone, password)
            save_user(user_id, new_phone, token, expiry); save_number(user_id, new_phone); save_password(user_id, new_phone, password, 24)
            await update.message.reply_text(f"✅ تم التبديل إلى الرقم {new_phone} بنجاح.", parse_mode=None)
            del user_states[user_id]; await show_main_menu(update, user_id, context)
        except Exception as e: await update.message.reply_text(f"❌ فشل التبديل: {str(e)}", parse_mode=None); del user_states[user_id]
    elif state == 'cleaning_ask_owner_pass':
        user_states[user_id] = {'state': 'cleaning_ask_member', 'owner_num': state_data.get('owner_num'), 'owner_pass': text}
        await update.message.reply_text("📞 أدخل رقم العضو المراد تنظيفه (مثال: 01012345678):", parse_mode=None)
    elif state == 'cleaning_ask_member':
        user_states[user_id] = {'state': 'cleaning_ask_member_pass', 'owner_num': state_data.get('owner_num'), 'owner_pass': state_data.get('owner_pass'), 'member_num': text}
        await update.message.reply_text("🔐 أدخل كلمة مرور حساب العضو (الذي سيتم تنظيفه):", parse_mode=None)
    elif state == 'cleaning_ask_member_pass':
        await perform_full_cleaning_silent(update, user_id, state_data.get('owner_num'), state_data.get('owner_pass'), state_data.get('member_num'), text, context)
        del user_states[user_id]
    else:
        await send_message(update, user_id, "حدث خطأ. استخدم /start للبدء من جديد.")
        del user_states[user_id]

# ================== معالج الأزرار ==================
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query; await query.answer(); data, user_id = query.data, query.from_user.id
    if MAINTENANCE_MODE and not is_admin(user_id): await query.edit_message_text("🛠️ البوت في وضع الصيانة حاليًا. عذرًا للإزعاج.", parse_mode=None); return
    if is_user_banned(user_id): await query.edit_message_text("🚫 أنت محظور من استخدام هذا البوت.", parse_mode=None); return
    record_user_activity(user_id)

    if data.startswith("admin_"):
        if not is_admin(user_id): 
            await query.edit_message_text("⛔ غير مصرح."); return
            
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
        delete_user(user_id)
        if user_id in user_states: del user_states[user_id]
        if context.user_data.get('rocket_members'): context.user_data['rocket_members'] = []
        await clear_previous_messages(update, user_id)
        await query.edit_message_text("✅ تم تسجيل الخروج.", parse_mode=None)
        await show_login_screen(update, user_id, context); return

    if data == "main_menu":
        context.user_data.pop('awaiting_flex_deduction_count', None); context.user_data.pop('flex_discount_offer', None); context.user_data.pop('flex_discount_token', None)
        await show_main_menu(update, user_id, context); return

    if data == "refresh_family":
        try: await query.delete_message()
        except: pass
        await clear_previous_messages(update, user_id); await show_family_screen(update, user_id, context); return

    if data == "clean_family":
        owner_data = get_user(user_id)
        if not owner_data: await query.edit_message_text("❌ يجب تسجيل الدخول أولاً.", parse_mode=None); return
        user_states[user_id] = {'state': 'cleaning_ask_owner_pass', 'owner_num': owner_data['phone']}
        await query.edit_message_text("🔐 لتأكيد عملية تنظيف العائلة، أدخل كلمة مرور حساب المالك:", parse_mode=None); return

    if data == "show_consumption": await show_consumption(update, user_id, context); return
    if data == "new_manual_login": user_states[user_id] = {'state': 'login_phone'}; await query.edit_message_text("📱 أرسل رقم هاتفك (مثال: 01012345678):", parse_mode=None); return

    if data.startswith("use_num_"):
        num = data[8:]
        token = get_valid_token(user_id, num)
        if token:
            conn = sqlite3.connect(DB_PATH); c = conn.cursor(); c.execute("SELECT expiry FROM saved_tokens WHERE user_id = ? AND number = ?", (user_id, num)); row = c.fetchone(); conn.close()
            save_user(user_id, num, token, datetime.fromisoformat(row[0]) if row else datetime.now() + timedelta(hours=1))
            await query.edit_message_text(f"✅ تم التبديل إلى الرقم {num} باستخدام التوكن المخزن.", parse_mode=None)
            await show_main_menu(update, user_id, context)
        else:
            user_states[user_id] = {'state': 'switch_account_ask', 'new_phone': num}
            await query.edit_message_text(f"🔐 أدخل كلمة مرور الرقم {num}:", parse_mode=None)
        return

    if data.startswith("del_num_"):
        num = data[8:]; delete_saved_number(user_id, num)
        conn = sqlite3.connect(DB_PATH); c = conn.cursor(); c.execute("DELETE FROM saved_tokens WHERE user_id = ? AND number = ?", (user_id, num)); conn.commit(); conn.close()
        await query.edit_message_text(f"✅ تم حذف الرقم {num}", parse_mode=None)
        await show_saved_numbers(update, user_id, context); return

    if data == "confirm_yes":
        confirm_data = user_states.get(user_id, {}); action = confirm_data.get('confirm_action')
        user = await ensure_valid_session(update, user_id)
        if not user: await query.edit_message_text("الرجاء تسجيل الدخول أولاً.", parse_mode=None); return
        if action == 'edit':
            await query.edit_message_text(f"🔄 جاري تعديل نسبة {confirm_data.get('member_phone')}...", parse_mode=None)
            if await update_quota(user['access_token'], user['phone'], confirm_data.get('member_phone'), confirm_data.get('percent')):
                await query.answer("✅ تم تعديل النسبة بنجاح", show_alert=True); await show_family_screen(update, user_id, context)
            else: await query.answer("❌ فشل تعديل النسبة", show_alert=True); await query.edit_message_text(f"❌ فشل تعديل النسبة", parse_mode=None)
            del user_states[user_id]
        elif action == 'delete':
            await query.edit_message_text(f"🔄 جاري حذف {confirm_data.get('member_phone')}...", parse_mode=None)
            if await remove_member(user['access_token'], user['phone'], confirm_data.get('member_phone')):
                await query.answer("✅ تم حذف الفرد بنجاح", show_alert=True); await show_family_screen(update, user_id, context)
            else: await query.answer("❌ فشل حذف الفرد", show_alert=True); await query.edit_message_text(f"❌ فشل حذف {confirm_data.get('member_phone')}", parse_mode=None)
            del user_states[user_id]
        elif action == 'cancel_invite':
            await query.edit_message_text(f"🔄 جاري إلغاء دعوة {confirm_data.get('member_phone')}...", parse_mode=None)
            if await cancel_invitation(user['access_token'], user['phone'], confirm_data.get('member_phone')):
                await query.answer("✅ تم إلغاء الدعوة بنجاح", show_alert=True); await show_family_screen(update, user_id, context)
            else: await query.answer("❌ فشل إلغاء الدعوة", show_alert=True); await query.edit_message_text(f"❌ فشل إلغاء الدعوة", parse_mode=None)
            del user_states[user_id]
        else: await query.answer("⚠️ إجراء غير معروف", show_alert=True); del user_states[user_id]
        return

    if data == "confirm_no":
        if user_id in user_states: del user_states[user_id]
        await query.answer("❌ تم إلغاء العملية", show_alert=True); await show_family_screen(update, user_id, context); return

    if data == "sendonly_menu":
        if not await require_subscription_or_free(update, context, is_callback=True, query=query): return
        await sendonly_menu(update, context); return

    if data == "packages_services":
        if not await require_subscription_or_free(update, context, is_callback=True, query=query): return
        await packages_services_menu(update, user_id, context); return

    if data == "packages_flex":
        if not await require_subscription_or_free(update, context, is_callback=True, query=query): return
        await flex_bundles_menu(update, user_id, context); return

    if data == "packages_ignore": return

    if data.startswith("packages_flex_confirm_"):
        if not await require_subscription_or_free(update, context, is_callback=True, query=query): return
        await execute_flex_bundle_service(update, user_id, context, data.rsplit("_", 1)[-1]); return

    if data.startswith("packages_flex_"):
        if not await require_subscription_or_free(update, context, is_callback=True, query=query): return
        await flex_bundle_confirm_screen(update, user_id, context, data.rsplit("_", 1)[-1]); return

    if data == "packages_convert14":
        if not await require_subscription_or_free(update, context, is_callback=True, query=query): return
        await convert14_confirm_screen(update, user_id, context); return

    if data == "packages_convert14_confirm":
        if not await require_subscription_or_free(update, context, is_callback=True, query=query): return
        await execute_convert14_service(update, user_id, context); return

    if data == "deduct_flexes":
        if not await require_subscription_or_free(update, context, is_callback=True, query=query): return
        await deduct_flexes(update, user_id, context); return

    if data == "flex_discount":
        if not await require_subscription_or_free(update, context, is_callback=True, query=query): return
        await flex_discount_menu(update, user_id, context); return

    if data == "flex_discount_confirm":
        if not await require_subscription_or_free(update, context, is_callback=True, query=query): return
        await flex_discount_confirm(update, user_id, context); return

    if data == "flex_discount_cancel":
        context.user_data.pop('flex_discount_offer', None); context.user_data.pop('flex_discount_token', None)
        await query.answer("❌ تم إلغاء التفعيل"); await show_main_menu(update, user_id, context); return

    if data == "add_two_days":
        if not await require_subscription_or_free(update, context, is_callback=True, query=query): return
        await add_two_days(update, user_id, context); return

    if data.startswith("add_two_days_member_"):
        if not await require_subscription_or_free(update, context, is_callback=True, query=query): return
        member_phone = data[len("add_two_days_member_"):]
        if not re.fullmatch(r"01\d{9}", member_phone): await query.answer("❌ رقم العضو غير صالح", show_alert=True); return
        user_states[user_id] = {'state': 'add_two_days_member_password', 'member_phone': member_phone}
        await query.edit_message_text(f"🔐 أدخل كلمة مرور حساب الفرد {member_phone} لتزويده يومين:", parse_mode=None); return

    if not await require_subscription_or_free(update, context, is_callback=True, query=query): return
    user = await ensure_valid_session(update, user_id)
    if not user: await query.edit_message_text("الرجاء تسجيل الدخول أولاً.", parse_mode=None); return

    if data == "show_family": context.user_data.pop('awaiting_flex_deduction_count', None); await show_family_screen(update, user_id, context)
    elif data == "show_owner_remaining": await show_owner_remaining(update, user_id, context)
    elif data == "show_saved": await show_saved_numbers(update, user_id, context)
    elif data.startswith("edit_"):
        parts = data.split("_")
        if len(parts) == 3:
            member_phone, new_flex = parts[1], parts[2]
            percent = 10 if new_flex == "1300" else 20 if new_flex == "2600" else 40
            user_states[user_id] = {'confirm_action': 'edit', 'member_phone': member_phone, 'new_flex': new_flex, 'percent': percent}
            await query.edit_message_text(f"⚠️ هل أنت متأكد من تعديل نسبة {member_phone} إلى {new_flex} فليكس؟", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ نعم", callback_data="confirm_yes"), InlineKeyboardButton("❌ لا", callback_data="confirm_no")]]), parse_mode=None)
    elif data.startswith("del_"):
        user_states[user_id] = {'confirm_action': 'delete', 'member_phone': data[4:]}
        await query.edit_message_text(f"⚠️ هل أنت متأكد من حذف الفرد {data[4:]}؟", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ نعم", callback_data="confirm_yes"), InlineKeyboardButton("❌ لا", callback_data="confirm_no")]]), parse_mode=None)
    elif data.startswith("cancel_"):
        user_states[user_id] = {'confirm_action': 'cancel_invite', 'member_phone': data[7:]}
        await query.edit_message_text(f"⚠️ هل أنت متأكد من إلغاء دعوة {data[7:]}؟", reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("✅ نعم", callback_data="confirm_yes"), InlineKeyboardButton("❌ لا", callback_data="confirm_no")]]), parse_mode=None)
    elif data.startswith("accept_"):
        user_states[user_id] = {'state': 'accept_password', 'member_phone': data[7:]}
        await query.edit_message_text(f"🔐 أدخل كلمة مرور حساب العضو {data[7:]}:", parse_mode=None)
    elif data.startswith("invite_"):
        context.user_data.pop('awaiting_flex_deduction_count', None)
        user_states[user_id] = {'state': 'invite_phone', 'percentage': int(data.split("_")[1])}
        await query.edit_message_text("📞 أدخل رقم العضو المراد دعوته (مثال: 01012345678):", parse_mode=None)
    else: await query.edit_message_text("حدث خطأ غير متوقع.", parse_mode=None)

# ================== تشغيل البوت ==================
async def post_init(app: Application):
    await app.bot.set_my_commands([BotCommand("start", "بدء البوت"), BotCommand("savenumber", "حفظ رقم هاتف"), BotCommand("sendonly", "تعليق دعوتين"), BotCommand("cancel", "إلغاء العملية الحالية"), BotCommand("help", "عرض المساعدة"), BotCommand("admin", "لوحة تحكم الأدمن")])

def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
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
        fallbacks=[CommandHandler('cancel', cancel_command)], allow_reentry=True
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("savenumber", save_number_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("users", users_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("block", block_command))
    app.add_handler(CommandHandler("unblock", unblock_command))
    app.add_handler(CommandHandler("maintenance", maintenance_command))
    app.add_handler(CommandHandler("broadcast", broadcast_command))
    app.add_handler(CommandHandler("sendonly_stats", sendonly_stats_command))
    app.add_handler(CommandHandler("resetsendonly", reset_sendonly_stats))
    app.add_handler(CommandHandler("add_admin", add_admin_command))
    app.add_handler(CommandHandler("remove_admin", remove_admin_command))
    app.add_handler(CommandHandler("list_admins", list_admins_command))
    app.add_handler(CommandHandler("add_subscription", add_subscription_command))
    app.add_handler(CommandHandler("remove_subscription", remove_subscription_command))
    app.add_handler(CommandHandler("check_subscription", check_subscription_command))
    app.add_handler(CommandHandler("list_subscriptions", list_subscriptions_command))
    app.add_handler(CommandHandler("toggle_free_mode", toggle_free_mode_command))
    app.add_handler(CommandHandler("admin", admin_panel))
    app.add_handler(sendonly_conv)
    app.add_handler(MessageHandler(filters.Text("⏹️ إيقاف العملية"), stop_sendonly_process))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(CallbackQueryHandler(button_callback))
    print("✅ البوت يعمل بنجاح - نسخة المهندس الجبار (Anti-Ban System)")
    app.run_polling()

if __name__ == "__main__":
    main()