import telebot
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from mailtd import MailTD
import requests
import time
import threading
import re
import random
import string
import html
import os
import copy
import pyotp
from flask import Flask
from datetime import datetime

# --- Firebase Admin Initialization ---
import firebase_admin
from firebase_admin import credentials
from firebase_admin import firestore

try:
    cred = credentials.Certificate("firebase-admin-key.json")
    firebase_admin.initialize_app(cred)
    db = firestore.client()
    print("✅ Firebase Connected Successfully!")
except Exception as e:
    print(f"⚠️ Firebase Setup Error: {e}")
    db = None

# --- Configuration ---
TOKEN = '8572418006:AAEQBCXBPxa35yBiSWeaVWVvLP9N326fJos' # আপনার টোকেন
bot = telebot.TeleBot(TOKEN, parse_mode='HTML')
ADMIN_ID = "6670461311"

# --- Global Storage (Hybrid Memory) ---
user_data = {}
banned_users = set()
bot_stats = {'total_mails_generated': 0}
system_data = {'active_promos': {}, 'bot_active': True, 'force_sub_channels': []} 

# API Data Structure for MailTD 
api_data = {
    'mailtd_tokens': [ 
        'td_18c938ad445ea882ebc1110b22723e1ca1ddef7911dde89e80a095f3c2120119', 
        'td_d4ee26c571da82546f814b6d1595f59f780489afc162254cba00009fba83f48d', 
        'td_1d45403d07853397e061d49f21c1fa9e0a80816e0005401a11bdf84218d496ee',  
        'td_4af40882b5019f9be105e7b4e3beeeaf1cffd81060fc383d824622c4470d73f0'  
    ],
    'active_idx': {'mailtd': 0},
    'usage': {},
    'exhausted': {}
}
api_clients = {}

# --- Firebase Sync Functions ---
def save_system_data():
    if not db: return
    try:
        db.collection('system').document('api_data').set(api_data)
        db.collection('system').document('banned_users').set({'users': list(banned_users)})
        db.collection('system').document('bot_stats').set(bot_stats)
        db.collection('system').document('settings').set({
            'bot_active': system_data.get('bot_active', True),
            'force_sub_channels': system_data.get('force_sub_channels', [])
        })
    except Exception as e:
        pass

def save_user_data(chat_id):
    if not db: return
    try:
        data_to_save = copy.deepcopy(user_data[str(chat_id)])
        for acc in data_to_save.get('accounts', []):
            acc['seen_msgs'] = list(acc.get('seen_msgs', []))
        db.collection('users').document(str(chat_id)).set(data_to_save)
    except Exception as e:
        pass

def load_all_data_from_firebase():
    global api_data, banned_users, bot_stats, user_data, system_data
    if not db: return
    try:
        print("⏳ Loading data from Firebase...")
        api_doc = db.collection('system').document('api_data').get()
        if api_doc.exists: 
            loaded = api_doc.to_dict()
            if 'mailtd_tokens' in loaded: api_data['mailtd_tokens'] = loaded['mailtd_tokens']
            if 'usage' in loaded: api_data['usage'] = loaded['usage']
            if 'exhausted' in loaded: api_data['exhausted'] = loaded['exhausted']
            if 'active_idx' in loaded:
                if isinstance(loaded['active_idx'], dict): api_data['active_idx'] = loaded['active_idx']
            
        ban_doc = db.collection('system').document('banned_users').get()
        if ban_doc.exists: 
            banned_users = set(ban_doc.to_dict().get('users', []))
            if ADMIN_ID in banned_users: banned_users.discard(ADMIN_ID)
        
        stat_doc = db.collection('system').document('bot_stats').get()
        if stat_doc.exists: bot_stats.update(stat_doc.to_dict())

        set_doc = db.collection('system').document('settings').get()
        if set_doc.exists: 
            system_data['bot_active'] = set_doc.to_dict().get('bot_active', True)
            system_data['force_sub_channels'] = set_doc.to_dict().get('force_sub_channels', [])
        
        users_ref = db.collection('users').stream()
        for doc in users_ref:
            uid = doc.id
            u_data = doc.to_dict()
            for acc in u_data.get('accounts', []):
                acc['seen_msgs'] = set(acc.get('seen_msgs', []))
            user_data[uid] = u_data
        print("✅ Data Loading Complete!")
    except Exception as e:
        pass

# --- Force Sub Check ---
def check_force_sub(chat_id):
    if str(chat_id) == ADMIN_ID: return True
    channels = system_data.get('force_sub_channels', [])
    if not channels: return True
    
    not_joined = []
    for ch in channels:
        try:
            status = bot.get_chat_member(ch, chat_id).status
            if status in ['left', 'kicked']:
                not_joined.append(ch)
        except Exception:
            pass 
            
    if not_joined:
        markup = InlineKeyboardMarkup(row_width=1)
        for ch in not_joined:
            markup.add(InlineKeyboardButton(f"📢 Join Channel", url=f"https://t.me/{ch.replace('@', '')}"))
        markup.add(InlineKeyboardButton("✅ Verify", callback_data="verify_sub"))
        
        bot.send_message(chat_id, "⚠️ <b>Bot ব্যবহার করতে হলে আপনাকে আমাদের চ্যানেলগুলোতে যুক্ত হতে হবে!</b>\nনিচের বাটন থেকে জয়েন করে Verify এ ক্লিক করুন:", reply_markup=markup)
        return False
    return True

# --- Load Balancing & Mail Creation ---
def restore_apis():
    current_time = time.time()
    changed = False
    for token, exhaust_time in list(api_data['exhausted'].items()):
        if (current_time - exhaust_time) >= 30 * 86400:
            del api_data['exhausted'][token]
            api_data['usage'][token] = 0
            changed = True
    if changed: save_system_data()

def mark_api_exhausted(token):
    if token not in api_data['exhausted']:
        api_data['exhausted'][token] = time.time()
        api_data['usage'][token] = 1000
        save_system_data()
        try: bot.send_message(ADMIN_ID, f"⚠️ <b>API Limit Reached!</b>\n\nএকটি API এর লিমিট শেষ। পরবর্তী API তে সুইচ করা হচ্ছে।")
        except: pass

def get_active_client(server_type='mailtd', exclude_tokens=None):
    restore_apis()
    if exclude_tokens is None: exclude_tokens = set()
    token_key = f"{server_type}_tokens"
    valid_tokens = [t for t in api_data.get(token_key, []) if len(t) > 5 and t not in exclude_tokens]
    
    if not valid_tokens: 
        if server_type == 'mailtd': raise Exception("All MailTD APIs Exhausted")
        return None, "fallback_token" 

    idx = api_data['active_idx'].get(server_type, 0)
    for _ in range(len(api_data[token_key])):
        token = api_data[token_key][idx % len(api_data[token_key])]
        idx = (idx + 1) % len(api_data[token_key])
        api_data['active_idx'][server_type] = idx
        
        if token in valid_tokens and token not in api_data['exhausted']:
            if api_data['usage'].get(token, 0) < 1000:
                if token not in api_clients: api_clients[token] = MailTD(token)
                save_system_data()
                return api_clients[token], token
            else:
                mark_api_exhausted(token)
                
    if server_type == 'mailtd': raise Exception("All APIs Exhausted")
    return None, "fallback_token"

def create_mail_with_server(chat_id, clean_name=None):
    preferred = user_data[chat_id].get('server_pref', 'mailtd')
    
    if preferred == 'mailtd':
        failed_tokens = set()
        for _ in range(len(api_data.get('mailtd_tokens', []))):
            try:
                client, token = get_active_client('mailtd', exclude_tokens=failed_tokens)
                domains = client.accounts.list_domains()
                domain_name = domains[0].domain if hasattr(domains[0], 'domain') else domains[0]
                email_address = f"{clean_name}@{domain_name}" if clean_name else f"{''.join(random.choices(string.ascii_lowercase + string.digits, k=8))}@{domain_name}"
                account = client.accounts.create(email_address, password="propassword123")
                return account.id, account.address, token, 'mailtd'
            except Exception as e:
                error_msg = str(e).lower()
                if clean_name and ("already exists" in error_msg or "taken" in error_msg or "400" in error_msg):
                    raise Exception("NameTaken")
                failed_tokens.add(token)

        # Fallback if MailTD fails
        preferred = 'mailgw'

    if preferred in ['mailgw', 'mailtm']:
        server_domain = "mail.gw" if preferred == 'mailgw' else "mail.tm"
        base_url = f"https://api.{server_domain}"
        try:
            domains_req = requests.get(f"{base_url}/domains", timeout=5).json()
            domain = domains_req['hydra:member'][0]['domain']
            email_addr = f"{clean_name}@{domain}" if clean_name else f"{''.join(random.choices(string.ascii_lowercase + string.digits, k=10))}@{domain}"
            password = "ProPassword123!"
            
            acc_req = requests.post(f"{base_url}/accounts", json={"address": email_addr, "password": password}, timeout=5)
            if acc_req.status_code in [200, 201]:
                acc_id = acc_req.json().get('id')
                token_req = requests.post(f"{base_url}/token", json={"address": email_addr, "password": password}, timeout=5).json()
                jwt_token = token_req.get('token')
                return acc_id, email_addr, jwt_token, preferred
            elif acc_req.status_code == 422:
                raise Exception("NameTaken")
            else:
                raise Exception(f"{server_domain} Error")
        except Exception as e:
            if str(e) == "NameTaken": raise
            raise Exception(f"Failed to connect to {server_domain}")

# --- Web Server ---
app = Flask('')
@app.route('/')
def home(): return "Pro Mail Bot is Running 24/7!"
def run_web_server(): app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

# --- Menus ---
def get_main_menu(chat_id):
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(KeyboardButton("✨ Generate Premium Mail"))
    markup.row(KeyboardButton("✏️ Custom ID"), KeyboardButton("🌐 Server Change"))
    markup.row(KeyboardButton("🏠 Dashboard"), KeyboardButton("🗑️ Delete Mail"))
    markup.row(KeyboardButton("👤 My Profile"), KeyboardButton("🔐 2FA Authenticator"))
    if str(chat_id) == ADMIN_ID: 
        markup.row(KeyboardButton("⚙️ Admin Panel"))
    return markup

def get_admin_menu():
    markup = InlineKeyboardMarkup(row_width=2)
    bot_state = "🟢 Bot is ON" if system_data.get('bot_active', True) else "🔴 Bot is OFF"
    markup.add(InlineKeyboardButton(bot_state, callback_data="admin_toggle_bot"))
    markup.add(InlineKeyboardButton("📢 Manage Channels", callback_data="admin_channels"))
    markup.add(InlineKeyboardButton("👥 User List", callback_data="admin_users"),
               InlineKeyboardButton("📊 Statistics", callback_data="admin_stats"))
    markup.add(InlineKeyboardButton("🔑 Manage APIs", callback_data="admin_apis_select"),
               InlineKeyboardButton("📢 Send Notice", callback_data="admin_send_promo"))
    markup.add(InlineKeyboardButton("🚫 Suspend User", callback_data="admin_ban"),
               InlineKeyboardButton("✅ Activate User", callback_data="admin_unban"))
    markup.add(InlineKeyboardButton("📄 Download Users (TXT)", callback_data="admin_download_txt"))
    markup.add(InlineKeyboardButton("🗑️ Del Promo", callback_data="admin_del_promo"))
    return markup

def get_back_button():
    return InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 Back to Panel", callback_data="admin_back"))

def get_server_markup(curr_srv):
    markup = InlineKeyboardMarkup(row_width=1)
    markup.add(
        InlineKeyboardButton(f"{'✅' if curr_srv == 'mailtd' else '⬜'} Default Mail.td", callback_data="set_srv_mailtd"),
        InlineKeyboardButton(f"{'✅' if curr_srv == 'mailgw' else '⬜'} Premium Mail.gw", callback_data="set_srv_mailgw"),
        InlineKeyboardButton(f"{'✅' if curr_srv == 'mailtm' else '⬜'} Premium Mail.tm", callback_data="set_srv_mailtm")
    )
    return markup

# --- Smart Anti-Spam ---
def handle_suspension(chat_id):
    uid = str(chat_id)
    if uid == ADMIN_ID: return 
    
    if uid not in banned_users:
        banned_users.add(uid)
        save_system_data()
        
    u_info = user_data.get(uid, {})
    uname = u_info.get('username', 'N/A')
    
    suspend_msg = (
        f"🚫 <b>Account Auto-Suspended!</b>\n\n"
        f"Spamming detected! আপনি কোনো মেসেজ রিসিভ না করেই বারবার মেইল তৈরি করেছেন।\n\n"
        f"👤 <b>Username:</b> {uname}\n"
        f"🆔 <b>User ID:</b> <code>{uid}</code>\n"
        f"<i>(Tap ID to copy)</i>\n\n"
        f"অ্যাকাউন্ট রিকভার করতে আপনার User ID কপি করে অ্যাডমিনের সাথে যোগাযোগ করুন।"
    )
    try:
        bot.send_message(chat_id, suspend_msg, reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("👨‍💻 Contact Admin", url="https://t.me/Ad_Walid")), disable_web_page_preview=True)
    except: pass

def check_anti_spam(chat_id):
    if str(chat_id) == ADMIN_ID: return False 
    now = time.time()
    user_data[chat_id].setdefault('recent_mails', [])
    user_data[chat_id]['recent_mails'] = [m for m in user_data[chat_id]['recent_mails'] if now - m['time'] < 300]
    
    if len(user_data[chat_id]['recent_mails']) >= 3:
        if all(m['msg_count'] == 0 for m in user_data[chat_id]['recent_mails']):
            handle_suspension(chat_id)
            return True
    return False

def record_mail_creation(chat_id, email_addr):
    user_data[chat_id].setdefault('recent_mails', []).append({'email': email_addr, 'time': time.time(), 'msg_count': 0})

def is_banned(chat_id):
    if str(chat_id) == ADMIN_ID: 
        if ADMIN_ID in banned_users:
            banned_users.discard(ADMIN_ID)
            save_system_data()
        return False
    if str(chat_id) in banned_users:
        handle_suspension(chat_id)
        return True
    return False

# --- UI Formatter Functions ---
def get_service_logo_and_name(sender):
    s = str(sender).lower()
    if 'facebook' in s or 'fb' in s: return '📘', 'Facebook'
    if 'instagram' in s or 'ig' in s: return '📸', 'Instagram'
    if 'google' in s or 'gmail' in s: return '🇬', 'Google'
    if 'tiktok' in s: return '🎵', 'TikTok'
    if 'netflix' in s: return '🎬', 'Netflix'
    if 'amazon' in s: return '🛒', 'Amazon'
    if 'twitter' in s or 'x.com' in s: return '🐦', 'X (Twitter)'
    match = re.search(r'@([a-zA-Z0-9.-]+)', str(sender))
    if match: return '🌐', match.group(1).split('.')[0].capitalize()
    return '🌐', 'Web Service'

def extract_and_format(subject, text_body, html_body=""):
    subject_text = subject if subject else "No Subject"
    clean_text = str(text_body) if text_body else ""
    clean_html = ""
    if html_body:
        clean_html = re.sub(r'<(script|style).*?>.*?</\1>', ' ', str(html_body), flags=re.IGNORECASE | re.DOTALL)
        clean_html = re.sub(r'<br\s*/?>|</p>|</div>', '\n', clean_html, flags=re.IGNORECASE)
        clean_html = re.sub(r'<[^>]+>', ' ', clean_html)
        clean_html = html.unescape(clean_html)
        clean_html = re.sub(r'[ \t]+', ' ', clean_html).strip()
    
    search_text = f"{subject_text}\n{clean_text}\n{clean_html}".replace('\u200c', '') 
    extracted_otp = ""
    digit_match = re.search(r'(?<!\d)(\d{6,8})(?!\d)', search_text)
    spaced_match = re.search(r'([A-Za-z0-9](?:\s+[A-Za-z0-9]){7})', search_text)
    promo_match = re.search(r'\b([A-Z0-9]{5,8})\b', search_text)
    
    if digit_match: extracted_otp = digit_match.group(1)
    elif spaced_match: extracted_otp = spaced_match.group(1).replace(" ", "")
    elif promo_match and not promo_match.group(1).isdigit(): extracted_otp = promo_match.group(1)

    link_match = re.search(r'(https?://[^\s\"\'<>]+)', search_text)
    extracted_link = link_match.group(1) if link_match else None
    
    display_body = clean_text.strip()
    if len(display_body) < 15 and clean_html: display_body = clean_html
    if not display_body: display_body = "No Content"
    return extracted_otp, html.escape(display_body[:800]), extracted_link

def generate_mail_layout(email_address, srv_type):
    srv_map = {'mailtd': 'Premium Mail.td API', 'mailgw': 'Premium Mail.gw', 'mailtm': 'Premium Mail.tm'}
    server_name = srv_map.get(srv_type, 'Premium Server')
    
    layout = (
        f"🎉 <b>Premium Mail Generated!</b>\n\n"
        f"📧 <b>Your Address :</b>\n"
        f"╔════════════════════════╗\n"
        f"  <code>{email_address}</code>\n"
        f"╚════════════════════════╝\n"
        f"<i>(Tap the address inside the box to copy)</i>\n\n"
        f"📡 <b>Server :</b> {server_name}\n"
        f"🟢 <b>Status :</b> Live Sync Active\n\n"
        f"<blockquote>•  Listening for incoming mails... ⏳</blockquote>"
    )
    
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(InlineKeyboardButton("🔄 Switch Mail", callback_data="quick_switch"), InlineKeyboardButton("🔄 Force Sync", callback_data="force_fetch"))
    return layout, markup

# --- Auto Checker Engine ---
def auto_check_mail():
    while True:
        try:
            for chat_id, data in list(user_data.items()):
                if str(chat_id) in banned_users: continue
                
                active_index = data.get('active_index', -1)
                if active_index >= 0 and data['accounts']:
                    account = data['accounts'][active_index]
                    acc_token = account.get('api_token', '')
                    email_addr = account['email']
                    srv_type = account.get('server_type', 'mailtd')
                    needs_sync = False
                    
                    try:
                        messages_to_process = []
                        if srv_type in ['mailtm', 'mailgw']:
                            server_domain = "mail.tm" if srv_type == 'mailtm' else "mail.gw"
                            headers = {"Authorization": f"Bearer {acc_token}"}
                            resp = requests.get(f"https://api.{server_domain}/messages", headers=headers, timeout=10)
                            
                            if resp.status_code == 200:
                                resp_json = resp.json()
                                if 'hydra:member' in resp_json:
                                    for msg_preview in resp_json['hydra:member']:
                                        msg_id = msg_preview['id']
                                        if msg_id not in account['seen_msgs']:
                                            account['seen_msgs'].add(msg_id)
                                            needs_sync = True
                                            for m in data.get('recent_mails', []):
                                                if m['email'] == email_addr: m['msg_count'] += 1
                                                
                                            full_msg_resp = requests.get(f"https://api.{server_domain}/messages/{msg_id}", headers=headers, timeout=10)
                                            if full_msg_resp.status_code == 200:
                                                full_msg = full_msg_resp.json()
                                                messages_to_process.append({
                                                    'subject': full_msg.get('subject', 'No Subject'),
                                                    'sender': full_msg.get('from', {}).get('address', 'Unknown'),
                                                    'text': full_msg.get('text', ''),
                                                    'html': full_msg.get('html', '')
                                                })
                        else:
                            # MailTD Logic
                            account_id = account['account_id']
                            if acc_token not in api_clients: api_clients[acc_token] = MailTD(acc_token)
                            temp_client = api_clients[acc_token]
                            
                            messages, _ = temp_client.messages.list(account_id)
                            for msg_preview in messages:
                                msg_id = msg_preview.id
                                if msg_id not in account['seen_msgs']:
                                    account['seen_msgs'].add(msg_id)
                                    needs_sync = True
                                    for m in data.get('recent_mails', []):
                                        if m['email'] == email_addr: m['msg_count'] += 1

                                    full_msg = temp_client.messages.get(account_id, msg_id)
                                    messages_to_process.append({
                                        'subject': getattr(full_msg, 'subject', 'No Subject'),
                                        'sender': getattr(full_msg, 'from_address', getattr(full_msg, 'sender', 'Unknown')),
                                        'text': getattr(full_msg, 'text_body', ''),
                                        'html': getattr(full_msg, 'html_body', '')
                                    })

                        for msg_data in messages_to_process:
                            extracted_otp, smart_body, verify_link = extract_and_format(msg_data['subject'], msg_data['text'], msg_data['html'])
                            logo, s_name = get_service_logo_and_name(msg_data['sender'])
                            short_email = email_addr.split('@')[0]
                            
                            mail_alert = (
                                f"╭ {logo} {s_name} • {short_email}\n"
                                f"╰ 📌 Sub: {html.escape(msg_data['subject'][:25])}\n\n"
                            )
                            if extracted_otp:
                                mail_alert += (
                                    f"🔑 <b>Verification Code:</b>\n"
                                    f"╔════════════════════════╗\n"
                                    f"  <code>{extracted_otp}</code>\n"
                                    f"╚════════════════════════╝\n"
                                    f"<i>(Tap the code inside the box to copy)</i>\n\n"
                                )
                            mail_alert += f"<blockquote>💬 {smart_body[:400]}...</blockquote>"
                            
                            markup = InlineKeyboardMarkup(row_width=2)
                            row = []
                            if extracted_otp: row.append(InlineKeyboardButton(f"📋 {extracted_otp}", callback_data=f"cp_{extracted_otp}"))
                            if verify_link: row.append(InlineKeyboardButton("🔗 Open Link", url=verify_link))
                            if row: markup.add(*row)
                            
                            sent_msg = bot.send_message(chat_id, mail_alert, reply_markup=markup, disable_web_page_preview=True)
                            account['msg_ids'].append(sent_msg.message_id)

                    except Exception: pass 
                    if needs_sync: save_user_data(chat_id)
        except Exception: pass
        time.sleep(3)

# --- Init User ---
def init_user(message):
    chat_id = str(message.chat.id)
    if chat_id not in user_data:
        user_data[chat_id] = {'accounts': [], 'active_index': -1, 'total_generated': 0, 'name': message.from_user.first_name or "Unknown", 'username': f"@{message.from_user.username}" if message.from_user.username else "N/A", 'joined': datetime.now().strftime("%Y-%m-%d"), 'custom_mail_msgs': [], 'server_pref': 'mailtd'}
        save_user_data(chat_id)

# --- 2FA Handlers ---
def show_2fa_otp(chat_id, message_id=None):
    secret = user_data[chat_id].get('2fa_secret', '')
    if not secret: return
    # Remove spaces and dashes, convert to uppercase to prevent pyotp errors
    secret = secret.upper().replace(" ", "").replace("-", "")
    
    try:
        totp = pyotp.TOTP(secret)
        current_otp = totp.now()
    except Exception:
        # If pyotp validation completely fails
        msg_text = "❌ <b>Error:</b> ইনভ্যালিড 2FA সিক্রেট কোড। দয়া করে সঠিক কোড দিন।"
        try:
            if message_id: bot.edit_message_text(msg_text, chat_id, message_id)
            else: bot.send_message(chat_id, msg_text)
        except Exception: pass
        user_data[chat_id]['2fa_secret'] = None
        save_user_data(chat_id)
        return

    text = (
        f"🔐 <b>Your 2FA Authenticator</b>\n\n"
        f"🔑 <b>Current OTP:</b>\n"
        f"╔════════════════════════╗\n"
        f"  <code>{current_otp}</code>\n"
        f"╚════════════════════════╝\n"
        f"<i>(Tap the code inside the box to copy)</i>\n\n"
        f"⏳ <i>Updates every 30 seconds. Click Refresh to get latest OTP.</i>"
    )
    markup = InlineKeyboardMarkup(row_width=2)
    markup.add(
        InlineKeyboardButton("🔄 Refresh", callback_data="2fa_refresh"),
        InlineKeyboardButton("➕ New", callback_data="2fa_new")
    )
    markup.add(InlineKeyboardButton("🏠 Return to Home", callback_data="2fa_home"))
    
    # Message update block
    try:
        if message_id: bot.edit_message_text(text, chat_id, message_id, reply_markup=markup)
        else: bot.send_message(chat_id, text, reply_markup=markup)
    except Exception:
        # Ignore Telegram's "Message is not modified" error when clicking refresh quickly
        pass

def process_2fa_secret(message):
    chat_id = str(message.chat.id)
    if message.text and message.text.startswith('/'): return
    
    # Advanced normalization of secret key
    secret = message.text.strip().upper().replace(" ", "").replace("-", "")
    
    try:
        pyotp.TOTP(secret).now() # Validate before saving
        user_data[chat_id]['2fa_secret'] = secret
        save_user_data(chat_id)
        show_2fa_otp(chat_id)
    except Exception:
        msg = bot.send_message(chat_id, "❌ ইনভ্যালিড 2FA কোড। আবার সঠিকভাবে দিন:", reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Cancel", callback_data="2fa_cancel")))
        bot.register_next_step_handler(msg, process_2fa_secret)

# --- Bot Handlers ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    init_user(message)
    if not check_force_sub(message.chat.id): return
    if is_banned(message.chat.id): return
    if not system_data.get('bot_active', True) and str(message.chat.id) != ADMIN_ID:
        bot.send_message(message.chat.id, "🛠 <b>Bot Under Maintenance!</b>\n\nআপডেটের কাজ চলছে। দয়া করে কিছুক্ষণ পর আবার চেষ্টা করুন।")
        return
        
    welcome_text = (
        "🌟 <b>Welcome to Pro Mail Assistant!</b> 🌟\n\n"
        "Protect your personal inbox from spam, phishing, and unwanted newsletters. Generate high-quality temporary emails instantly!\n\n"
        "🔥 <b>Key Features:</b>\n"
        "• High-Quality Domains (FB/Insta Supported)\n"
        "• Real-time Auto Sync\n"
        "• Smart OTP Extraction\n"
        "• Built-in 2FA Authenticator\n\n"
        "<i>👇 Select an option from the menu below to get started!</i>"
    )
    bot.send_message(message.chat.id, welcome_text, reply_markup=get_main_menu(str(message.chat.id)))

@bot.message_handler(func=lambda message: True)
def handle_text(message):
    chat_id = str(message.chat.id)
    text = message.text
    init_user(message)
    
    if not check_force_sub(chat_id): return
    if is_banned(chat_id): return
    if not system_data.get('bot_active', True) and chat_id != ADMIN_ID:
        bot.send_message(chat_id, "🛠 <b>Bot Under Maintenance!</b>\n\nআপডেটের কাজ চলছে। দয়া করে কিছুক্ষণ পর আবার চেষ্টা করুন।")
        return

    if text == "✨ Generate Premium Mail":
        if check_anti_spam(chat_id): return
        
        anim_msg = bot.send_message(chat_id, "<i>🔄 Connecting...</i>")
        time.sleep(0.1)
        srv_map = {'mailtd': 'MailTD', 'mailgw': 'Mail.gw', 'mailtm': 'Mail.tm'}
        srv_name = srv_map.get(user_data[chat_id].get('server_pref', 'mailtd'))
        bot.edit_message_text(f"<i>⚡ Allocating {srv_name} Server...</i>", chat_id, anim_msg.message_id)
        
        try:
            acc_id, email_addr, used_token, srv_type = create_mail_with_server(chat_id)
            if srv_type == 'mailtd': api_data['usage'][used_token] = api_data['usage'].get(used_token, 0) + 1
            
            record_mail_creation(chat_id, email_addr)
            user_data[chat_id]['accounts'].append({'account_id': acc_id, 'email': email_addr, 'seen_msgs': set(), 'msg_ids': [anim_msg.message_id], 'api_token': used_token, 'server_type': srv_type})
            user_data[chat_id]['active_index'] = len(user_data[chat_id]['accounts']) - 1
            user_data[chat_id]['total_generated'] += 1
            bot_stats['total_mails_generated'] += 1
            
            layout, markup = generate_mail_layout(email_addr, srv_type)
            bot.edit_message_text(layout, chat_id, anim_msg.message_id, reply_markup=markup)
            
            save_user_data(chat_id)
            save_system_data()
        except Exception as e:
            bot.edit_message_text(f"❌ Error Details: {str(e)}", chat_id, anim_msg.message_id)

    elif text == "✏️ Custom ID":
        if check_anti_spam(chat_id): return
        msg = bot.send_message(chat_id, "✏️ <b>Custom Mail Creation</b>\n\nমেইলের শুরুতে কী নাম দিতে চান লিখুন:", reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Cancel", callback_data="cancel_custom")))
        user_data[chat_id]['custom_mail_msgs'] = [message.message_id, msg.message_id]
        save_user_data(chat_id)
        bot.register_next_step_handler(msg, process_custom_mail)

    elif text == "🌐 Server Change":
        curr_srv = user_data[chat_id].get('server_pref', 'mailtd')
        srv_text = "🌐 <b>Select Your Preferred Server</b>\n\nযেকোনো সোশ্যাল মিডিয়া অ্যাকাউন্ট খুলতে হাই-কোয়ালিটি সার্ভার বেছে নিন:"
        bot.send_message(chat_id, srv_text, reply_markup=get_server_markup(curr_srv))

    elif text == "🏠 Dashboard":
        accounts = user_data[chat_id]['accounts']
        if not accounts: bot.send_message(chat_id, "⚠️ আপনার কোনো অ্যাক্টিভ মেইল নেই।")
        else:
            dash_text = "🗂️ <b>Your Mail Dashboard</b>\n\n"
            markup = InlineKeyboardMarkup(row_width=1)
            for i, acc in enumerate(accounts):
                status = "🟢 Active" if i == user_data[chat_id]['active_index'] else "⚪ Standby"
                srv = acc.get('server_type', 'mailtd').upper()
                dash_text += f"{i+1}. <code>{acc['email']}</code> [{status} - {srv}]\n\n"
                markup.add(InlineKeyboardButton(f"🔄 Switch to Mail {i+1}", callback_data=f"switch_{i}"))
            bot.send_message(chat_id, dash_text, reply_markup=markup)

    elif text == "🗑️ Delete Mail":
        if user_data[chat_id]['accounts']:
            active_idx = user_data[chat_id]['active_index']
            del_mail = user_data[chat_id]['accounts'].pop(active_idx)
            for msg_id in del_mail['msg_ids']:
                try: bot.delete_message(chat_id, msg_id)
                except: pass
            user_data[chat_id]['active_index'] = 0 if user_data[chat_id]['accounts'] else -1
            bot.send_message(chat_id, f"✅ <b>Deleted Successfully!</b>\n\nমেইল <code>{del_mail['email']}</code> সিস্টেম থেকে মুছে ফেলা হয়েছে।", reply_markup=get_main_menu(chat_id))
            save_user_data(chat_id)
        else: bot.send_message(chat_id, "⚠️ ডিলেট করার মতো মেইল নেই।")

    elif text == "🔐 2FA Authenticator":
        if '2fa_secret' in user_data[chat_id] and user_data[chat_id]['2fa_secret']:
            show_2fa_otp(chat_id)
        else:
            msg = bot.send_message(chat_id, "🔐 <b>2FA Setup</b>\n\nঅনুগ্রহ করে আপনার 2FA সিক্রেট কোডটি (Secret Key) দিন:", reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Cancel", callback_data="2fa_cancel")))
            bot.register_next_step_handler(msg, process_2fa_secret)

    elif text == "👤 My Profile":
        ui = user_data[chat_id]
        bot.send_message(chat_id, f"👤 <b>User Profile</b>\n\n📛 <b>Name :</b> {ui['name']}\n🆔 <b>User ID :</b> <code>{chat_id}</code>\n📊 <b>Total Generated :</b> {ui['total_generated']} Mails\n🟢 <b>Current Active :</b> {len(ui['accounts'])} Mails")

    elif text == "⚙️ Admin Panel" and chat_id == ADMIN_ID:
        bot.send_message(chat_id, "⚙️ <b>Admin Control Panel</b>\n\nবেছে নিন আপনি কী করতে চান:", reply_markup=get_admin_menu())

def process_custom_mail(message):
    chat_id = str(message.chat.id)
    if message.text.startswith('/'): return
    
    clean_name = re.sub(r'[^a-z0-9]', '', message.text.lower().strip())
    if len(clean_name) < 3:
        msg = bot.send_message(chat_id, "⚠️ নাম কমপক্ষে ৩ অক্ষরের হতে হবে। আবার দিন:")
        bot.register_next_step_handler(msg, process_custom_mail)
        return
        
    anim_msg = bot.send_message(chat_id, "<i>✨ Checking Name Availability...</i>")
    try:
        acc_id, email_addr, used_token, srv_type = create_mail_with_server(chat_id, clean_name)
        if srv_type == 'mailtd': api_data['usage'][used_token] = api_data['usage'].get(used_token, 0) + 1
            
        record_mail_creation(chat_id, email_addr)
        user_data[chat_id]['accounts'].append({'account_id': acc_id, 'email': email_addr, 'seen_msgs': set(), 'msg_ids': [], 'api_token': used_token, 'server_type': srv_type})
        user_data[chat_id]['active_index'] = len(user_data[chat_id]['accounts']) - 1
        user_data[chat_id]['total_generated'] += 1
        bot_stats['total_mails_generated'] += 1
        
        for msg_id in user_data[chat_id].get('custom_mail_msgs', []):
            try: bot.delete_message(chat_id, msg_id)
            except: pass
        user_data[chat_id]['custom_mail_msgs'] = []
        
        layout, markup = generate_mail_layout(email_addr, srv_type)
        bot.edit_message_text(layout, chat_id, anim_msg.message_id, reply_markup=markup)
        user_data[chat_id]['accounts'][-1]['msg_ids'].append(anim_msg.message_id)
        
        save_user_data(chat_id)
        save_system_data()
    except Exception as e:
        if str(e) == "NameTaken":
            bot.delete_message(chat_id, anim_msg.message_id)
            msg = bot.send_message(chat_id, f"❌ <b>দুঃখিত!</b> <code>{clean_name}</code> নামটি আগে থেকেই কেউ নিয়ে নিয়েছে। অন্য কোনো নাম দিন:", reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Cancel", callback_data="cancel_custom")))
            user_data[chat_id]['custom_mail_msgs'].append(msg.message_id)
            save_user_data(chat_id)
            bot.register_next_step_handler(msg, process_custom_mail)
        else:
            bot.edit_message_text(f"❌ Error Details: {str(e)}", chat_id, anim_msg.message_id)

# --- Admin API Flow ---
def process_add_api(message):
    new_token = message.text.strip()
    if len(new_token) > 5: 
        if new_token not in api_data.get('mailtd_tokens', []):
            if 'mailtd_tokens' not in api_data: api_data['mailtd_tokens'] = []
            api_data['mailtd_tokens'].append(new_token)
            save_system_data()
            bot.send_message(message.chat.id, f"✅ <b>API Added Successfully!</b>\n\nমোট API সংখ্যা এখন: {len(api_data['mailtd_tokens'])}", reply_markup=get_back_button())
        else: bot.send_message(message.chat.id, "⚠️ এই API Token টি আগেই লিস্টে আছে।", reply_markup=get_back_button())
    else: bot.send_message(message.chat.id, "❌ ইনভ্যালিড টোকেন!", reply_markup=get_back_button())

def process_add_channel(message):
    ch = message.text.strip()
    if not ch.startswith('@'): ch = '@' + ch
    
    if 'force_sub_channels' not in system_data: system_data['force_sub_channels'] = []
    if ch not in system_data['force_sub_channels']:
        system_data['force_sub_channels'].append(ch)
        save_system_data()
        bot.send_message(message.chat.id, f"✅ Channel <b>{ch}</b> added successfully!", reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("🔙 Back", callback_data="admin_channels")))
    else:
        bot.send_message(message.chat.id, "⚠️ Channel already exists!", reply_markup=get_back_button())

# --- Callback Handlers ---
@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    chat_id = str(call.message.chat.id)
    if call.data != "verify_sub" and not check_force_sub(chat_id): return
    if is_banned(chat_id): return
    
    if call.data.startswith('cp_'): bot.answer_callback_query(call.id)

    # 2FA Callbacks
    elif call.data == "2fa_refresh":
        try: bot.answer_callback_query(call.id, "✅ Refreshing OTP...")
        except: pass
        show_2fa_otp(chat_id, call.message.message_id)
        
    elif call.data == "2fa_new":
        user_data[chat_id]['2fa_secret'] = None
        save_user_data(chat_id)
        msg = bot.edit_message_text("🔐 <b>New 2FA Setup</b>\n\nঅনুগ্রহ করে আপনার নতুন 2FA সিক্রেট কোডটি দিন:", chat_id, call.message.message_id, reply_markup=InlineKeyboardMarkup().add(InlineKeyboardButton("❌ Cancel", callback_data="2fa_cancel")))
        bot.register_next_step_handler(msg, process_2fa_secret)
        
    elif call.data == "2fa_home":
        bot.delete_message(chat_id, call.message.message_id)
        bot.send_message(chat_id, "🏠 <b>Home</b>", reply_markup=get_main_menu(chat_id))

    elif call.data == "2fa_cancel":
        bot.clear_step_handler_by_chat_id(call.message.chat.id)
        bot.delete_message(chat_id, call.message.message_id)

    elif call.data == "verify_sub":
        if check_force_sub(chat_id):
            bot.delete_message(chat_id, call.message.message_id)
            bot.send_message(chat_id, "✅ <b>Verify Success!</b>\nআপনি এখন Bot ব্যবহার করতে পারেন।", reply_markup=get_main_menu(chat_id))
        else:
            bot.answer_callback_query(call.id, "❌ আপনি এখনো সব চ্যানেলে জয়েন করেননি!", show_alert=True)

    elif call.data == "cancel_custom":
        bot.clear_step_handler_by_chat_id(call.message.chat.id)
        for msg_id in user_data.get(chat_id, {}).get('custom_mail_msgs', []):
            try: bot.delete_message(chat_id, msg_id)
            except: pass
        user_data[chat_id]['custom_mail_msgs'] = []
        save_user_data(chat_id)
        bot.send_message(chat_id, "❌ Custom Mail creation cancelled.", reply_markup=get_main_menu(chat_id))

    elif call.data == "force_fetch":
        bot.answer_callback_query(call.id, "🔄 Syncing with server... please wait!")

    elif call.data == "quick_switch":
        accounts = user_data.get(chat_id, {}).get('accounts', [])
        if len(accounts) > 1: bot.answer_callback_query(call.id, "Please use Dashboard to switch mails.")
        else: bot.answer_callback_query(call.id, "You only have one active mail.")

    elif call.data.startswith('switch_'):
        idx = int(call.data.split('_')[1])
        if idx < len(user_data.get(chat_id, {}).get('accounts', [])):
            user_data[chat_id]['active_index'] = idx
            bot.answer_callback_query(call.id, "Switched successfully!")
            acc = user_data[chat_id]['accounts'][idx]
            layout, markup = generate_mail_layout(acc['email'], acc.get('server_type', 'mailtd'))
            bot.edit_message_text(layout, chat_id, call.message.message_id, reply_markup=markup)
            save_user_data(chat_id)

    elif call.data.startswith("set_srv_"):
        new_pref = call.data.split('_')[2]
        user_data[chat_id]['server_pref'] = new_pref
        save_user_data(chat_id)
        bot.answer_callback_query(call.id, "Server Updated Successfully!")
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=get_server_markup(new_pref))
            
    elif chat_id == ADMIN_ID:
        if call.data == "admin_back":
            bot.edit_message_text("⚙️ <b>Admin Control Panel</b>\n\nবেছে নিন আপনি কী করতে চান:", chat_id, call.message.message_id, reply_markup=get_admin_menu())
            
        elif call.data == "admin_toggle_bot":
            system_data['bot_active'] = not system_data.get('bot_active', True)
            save_system_data()
            bot.answer_callback_query(call.id, f"Bot is now {'ON' if system_data['bot_active'] else 'OFF'}")
            bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=get_admin_menu())
            
        # Admin Channel Management
        elif call.data == "admin_channels":
            ch_list = system_data.get('force_sub_channels', [])
            text = "📢 <b>Force Sub Channels:</b>\n\n"
            if ch_list:
                for idx, ch in enumerate(ch_list): text += f"{idx+1}. {ch}\n"
            else: text += "কোনো চ্যানেল অ্যাড করা নেই।\n"
            
            markup = InlineKeyboardMarkup()
            markup.add(InlineKeyboardButton("➕ Add Channel", callback_data="admin_add_channel"), InlineKeyboardButton("🗑️ Remove Channel", callback_data="admin_remove_channel"))
            markup.add(InlineKeyboardButton("🔙 Back to Panel", callback_data="admin_back"))
            bot.edit_message_text(text, chat_id, call.message.message_id, reply_markup=markup)
            
        elif call.data == "admin_add_channel":
            msg = bot.edit_message_text("➕ <b>Add Channel</b>\n\nচ্যানেলের ইউজারনেম দিন (যেমন: @MyChannel):", chat_id, call.message.message_id, reply_markup=get_back_button())
            bot.register_next_step_handler(msg, process_add_channel)

        elif call.data == "admin_remove_channel":
            ch_list = system_data.get('force_sub_channels', [])
            if not ch_list:
                bot.answer_callback_query(call.id, "কোনো চ্যানেল নেই!", show_alert=True)
                return
            markup = InlineKeyboardMarkup(row_width=1)
            for i, ch in enumerate(ch_list):
                markup.add(InlineKeyboardButton(f"❌ Remove: {ch}", callback_data=f"del_ch_{i}"))
            markup.add(InlineKeyboardButton("🔙 Back", callback_data="admin_channels"))
            bot.edit_message_text("🗑️ <b>Select Channel to Remove:</b>", chat_id, call.message.message_id, reply_markup=markup)
            
        elif call.data.startswith("del_ch_"):
            idx = int(call.data.split("_")[2])
            ch_list = system_data.get('force_sub_channels', [])
            if 0 <= idx < len(ch_list):
                removed = ch_list.pop(idx)
                save_system_data()
                bot.answer_callback_query(call.id, f"✅ Removed {removed}")
            # Refresh list
            markup = InlineKeyboardMarkup(row_width=1)
            for i, ch in enumerate(system_data.get('force_sub_channels', [])):
                markup.add(InlineKeyboardButton(f"❌ Remove: {ch}", callback_data=f"del_ch_{i}"))
            markup.add(InlineKeyboardButton("🔙 Back", callback_data="admin_channels"))
            bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=markup)

        elif call.data == "admin_apis_select":
            restore_apis()
            markup = InlineKeyboardMarkup(row_width=2)
            markup.add(InlineKeyboardButton("➕ Add Mail.td API", callback_data="admin_addapi"), InlineKeyboardButton("🗑️ Delete API", callback_data="admin_delapi_list"))
            markup.add(InlineKeyboardButton("🔙 Back to Panel", callback_data="admin_back"))
            
            api_info = f"🔑 <b>Mail.td Server Limit Management</b>\n<i>(Mail.gw & Mail.tm auto-generates internally)</i>\n\n"
            for i, token in enumerate(api_data.get('mailtd_tokens', [])):
                usage = api_data['usage'].get(token, 0)
                status = "🔴 Exhausted" if token in api_data['exhausted'] else "🟢 Active"
                short_token = f"{token[:6]}...{token[-4:]}" if len(token) > 10 else token
                api_info += f"<b>{i+1}.</b> <code>{short_token}</code>\n└ Ops: <b>{usage} / 1000</b> | {status}\n\n"
            bot.edit_message_text(api_info, chat_id, call.message.message_id, reply_markup=markup)

        elif call.data == "admin_addapi":
            msg = bot.edit_message_text("➕ <b>Add New API Token for Mail.td</b>\n\nআপনার নতুন API Token টি টাইপ করে সেন্ড করুন:", chat_id, call.message.message_id, reply_markup=get_back_button())
            bot.register_next_step_handler(msg, process_add_api)

        elif call.data == "admin_delapi_list":
            markup = InlineKeyboardMarkup(row_width=1)
            for i, token in enumerate(api_data.get('mailtd_tokens', [])):
                short_token = f"{token[:6]}...{token[-4:]}" if len(token) > 10 else token
                markup.add(InlineKeyboardButton(f"❌ Delete: {short_token}", callback_data=f"delapi_{i}"))
            markup.add(InlineKeyboardButton("🔙 Back", callback_data="admin_apis_select"))
            bot.edit_message_text("🗑️ <b>Select Mail.td API to Delete:</b>", chat_id, call.message.message_id, reply_markup=markup)

        elif call.data.startswith("delapi_"):
            idx = int(call.data.split('_')[1])
            if 0 <= idx < len(api_data.get('mailtd_tokens', [])):
                deleted_token = api_data['mailtd_tokens'].pop(idx)
                if deleted_token in api_data['usage']: del api_data['usage'][deleted_token]
                if deleted_token in api_data['exhausted']: del api_data['exhausted'][deleted_token]
                save_system_data()
                bot.answer_callback_query(call.id, "✅ API Deleted Successfully!", show_alert=True)
                
                markup = InlineKeyboardMarkup(row_width=1)
                for i, token in enumerate(api_data.get('mailtd_tokens', [])):
                    short_token = f"{token[:6]}...{token[-4:]}" if len(token) > 10 else token
                    markup.add(InlineKeyboardButton(f"❌ Delete: {short_token}", callback_data=f"delapi_{i}"))
                markup.add(InlineKeyboardButton("🔙 Back", callback_data="admin_apis_select"))
                bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=markup)
            
        elif call.data == "admin_stats":
            total_users = len(user_data)
            active_accounts = sum(len(d.get('accounts', [])) for d in user_data.values())
            
            mtd = mgw = mtm = 0
            for d in user_data.values():
                for acc in d.get('accounts', []):
                    stype = acc.get('server_type')
                    if stype == 'mailgw': mgw += 1
                    elif stype == 'mailtm': mtm += 1
                    else: mtd += 1
                    
            stats = f"📊 <b>Bot Live Statistics</b>\n\n👥 Total Users: <b>{total_users}</b>\n🚫 Suspended Users: <b>{len(banned_users)}</b>\n\n📧 Total Mails Gen: <b>{bot_stats['total_mails_generated']}</b>\n🟢 Current Active Mails: <b>{active_accounts}</b>\n\n🌐 Server Usage Distribution:\n- MailTD: <b>{mtd}</b>\n- Mail.gw: <b>{mgw}</b>\n- Mail.tm: <b>{mtm}</b>"
            bot.edit_message_text(stats, chat_id, call.message.message_id, reply_markup=get_back_button())
            
if __name__ == "__main__":
    load_all_data_from_firebase()
    threading.Thread(target=run_web_server, daemon=True).start()
    threading.Thread(target=auto_check_mail, daemon=True).start()
    print("🚀 Pro Mail Bot is Live...")
    while True:
        try: bot.polling(none_stop=True, interval=0, timeout=20)
        except Exception: time.sleep(5)
