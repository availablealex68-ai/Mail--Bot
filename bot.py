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
TOKEN = '8572418006:AAEQBCXBPxa35yBiSWeaVWVvLP9N326fJos'
bot = telebot.TeleBot(TOKEN, parse_mode='HTML')
ADMIN_ID = "6670461311"

# --- Global Storage (Hybrid Memory) ---
user_data = {}
banned_users = set()
bot_stats = {'total_mails_generated': 0}
system_data = {'active_promos': {}, 'bot_active': True} 

# Updated API Data Structure for Dual Servers
api_data = {
    'mailtd_tokens': [ 
        'td_18c938ad445ea882ebc1110b22723e1ca1ddef7911dde89e80a095f3c2120119', 
        'td_d4ee26c571da82546f814b6d1595f59f780489afc162254cba00009fba83f48d', 
        'td_1d45403d07853397e061d49f21c1fa9e0a80816e0005401a11bdf84218d496ee',  
        'td_4af40882b5019f9be105e7b4e3beeeaf1cffd81060fc383d824622c4470d73f0'  
    ],
    'tmailor_tokens': [], 
    'active_idx': {'mailtd': 0, 'tmailor': 0},
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
        db.collection('system').document('settings').set({'bot_active': system_data.get('bot_active', True)})
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
            if 'mailtd_tokens' in loaded: 
                api_data['mailtd_tokens'] = loaded['mailtd_tokens']
            elif 'tokens' in loaded: 
                api_data['mailtd_tokens'] = loaded['tokens'] 
                
            if 'tmailor_tokens' in loaded: api_data['tmailor_tokens'] = loaded['tmailor_tokens']
            if 'usage' in loaded: api_data['usage'] = loaded['usage']
            if 'exhausted' in loaded: api_data['exhausted'] = loaded['exhausted']
            
            if 'active_idx' in loaded:
                if isinstance(loaded['active_idx'], dict):
                    api_data['active_idx'] = loaded['active_idx']
                else:
                    api_data['active_idx'] = {'mailtd': loaded.get('active_idx', 0), 'tmailor': 0}
            
        ban_doc = db.collection('system').document('banned_users').get()
        if ban_doc.exists: 
            banned_users = set(ban_doc.to_dict().get('users', []))
            if ADMIN_ID in banned_users: banned_users.discard(ADMIN_ID) # Protect Admin
        
        stat_doc = db.collection('system').document('bot_stats').get()
        if stat_doc.exists: bot_stats.update(stat_doc.to_dict())

        set_doc = db.collection('system').document('settings').get()
        if set_doc.exists: system_data['bot_active'] = set_doc.to_dict().get('bot_active', True)
        
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

# --- Tmailor / Fast IG Fallback Helper ---
def get_tmailor_domain():
    try:
        resp = requests.get("https://www.1secmail.com/api/v1/?action=getDomainList", timeout=5).json()
        for d in ['esiix.com', 'xojxe.com', 'yoggm.com']:
            if d in resp: return d
        return resp[0]
    except: return "tmailor.com"

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
                if server_type == 'mailtd':
                    if token not in api_clients: api_clients[token] = MailTD(token)
                    save_system_data()
                    return api_clients[token], token
                elif server_type == 'tmailor':
                    return "tmailor_client", token
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
                if 'token' in locals(): failed_tokens.add(token)

        preferred = 'tmailor'

    if preferred == 'tmailor':
        _, token = get_active_client('tmailor')
        domain = get_tmailor_domain()
        email_addr = f"{clean_name}@{domain}" if clean_name else f"{''.join(random.choices(string.ascii_lowercase + string.digits, k=10))}@{domain}"
        return "tmailor_acc", email_addr, token, 'tmailor'

# --- Web Server ---
app = Flask('')
@app.route('/')
def home(): return "Pro Mail Bot is Running 24/7!"
def run_web_server(): app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

# --- Premium Menus ---
def get_main_menu(chat_id):
    markup = ReplyKeyboardMarkup(resize_keyboard=True)
    markup.row(KeyboardButton("✨ Generate Premium Mail"))
    markup.row(KeyboardButton("✏️ Custom ID"), KeyboardButton("🌐 Server Change"))
    markup.row(KeyboardButton("🏠 Dashboard"), KeyboardButton("🗑️ Delete Mail"))
    markup.row(KeyboardButton("👤 My Profile"), KeyboardButton("⚡ About System"))
    if str(chat_id) == ADMIN_ID: 
        markup.row(KeyboardButton("⚙️ Admin Panel"))
    return markup

def get_admin_menu():
    markup = InlineKeyboardMarkup(row_width=2)
    bot_state = "🟢 Bot is ON" if system_data.get('bot_active', True) else "🔴 Bot is OFF"
    markup.add(InlineKeyboardButton(bot_state, callback_data="admin_toggle_bot"))
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

# --- Smart Anti-Spam & Suspension Handling ---
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
    markup = InlineKeyboardMarkup().add(InlineKeyboardButton("👨‍💻 Contact Admin", url="https://t.me/Ad_Walid"))
    try:
        bot.send_message(chat_id, suspend_msg, reply_markup=markup, disable_web_page_preview=True)
    except: pass

def check_anti_spam(chat_id):
    if str(chat_id) == ADMIN_ID: return False 
    now = time.time()
    user_data[chat_id].setdefault('recent_mails', [])
    user_data[chat_id]['recent_mails'] = [m for m in user_data[chat_id]['recent_mails'] if now - m['time'] < 300]
    
    if len(user_data[chat_id]['recent_mails']) >= 3:
        spam = all(m['msg_count'] == 0 for m in user_data[chat_id]['recent_mails'])
        if spam:
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
    if match:
        domain = match.group(1).split('.')[0].capitalize()
        return '🌐', domain
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
        clean_html = re.sub(r'\n+', '\n', clean_html)
    
    search_text = f"{subject_text}\n{clean_text}\n{clean_html}"
    search_text_clean = search_text.replace('\u200c', '') 
    
    extracted_otp = ""
    
    # PERFECTED SMART REGEX 
    digit_match = re.search(r'(?<!\d)(\d{6,8})(?!\d)', search_text_clean)
    spaced_match = re.search(r'([A-Za-z0-9](?:\s+[A-Za-z0-9]){7})', search_text_clean)
    promo_match = re.search(r'\b([A-Z0-9]{5,8})\b', search_text_clean)
    
    if digit_match:
        extracted_otp = digit_match.group(1)
    elif spaced_match:
        extracted_otp = spaced_match.group(1).replace(" ", "")
    elif promo_match and not promo_match.group(1).isdigit():
        extracted_otp = promo_match.group(1)

    link_match = re.search(r'(https?://[^\s\"\'<>]+)', search_text)
    extracted_link = link_match.group(1) if link_match else None
    
    display_body = clean_text.strip()
    if len(display_body) < 15 and clean_html: display_body = clean_html
    if not display_body: display_body = "No Content"
    
    escaped_body = html.escape(display_body[:800])
    return extracted_otp, escaped_body, extracted_link

def generate_mail_layout(email_address, srv_type):
    server_name = "Premium Mail.td API" if srv_type == 'mailtd' else "Premium Tmailor.com"
    
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
                        if srv_type == 'tmailor':
                            login, domain = email_addr.split('@')
                            resp = requests.get(f"https://www.1secmail.com/api/v1/?action=getMessages&login={login}&domain={domain}", timeout=10)
                            if resp.status_code == 200:
                                for msg_preview in resp.json():
                                    msg_id = msg_preview['id']
                                    if msg_id not in account['seen_msgs']:
                                        account['seen_msgs'].add(msg_id)
                                        needs_sync = True
                                        for m in data.get('recent_mails', []):
                                            if m['email'] == email_addr: m['msg_count'] += 1
                                            
                                        full_msg_resp = requests.get(f"https://www.1secmail.com/api/v1/?action=readMessage&login={login}&domain={domain}&id={msg_id}", timeout=10)
                                        if full_msg_resp.status_code == 200:
                                            full_msg = full_msg_resp.json()
                                            messages_to_process.append({
                                                'subject': full_msg.get('subject', 'No Subject'),
                                                'sender': full_msg.get('from', 'Unknown'),
                                                'text': full_msg.get('textBody', ''),
                                                'html': full_msg.get('htmlBody', '')
                                            })
                        else:
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
                            # Inline Copy Button included as per NumberX screenshot
                            if extracted_otp:
                                row.append(InlineKeyboardButton(f"📋 {extracted_otp}", callback_data=f"cp_{extracted_otp}"))
                            if verify_link:
                                row.append(InlineKeyboardButton("🔗 Open Link", url=verify_link))
                                
                            if row: markup.add(*row)
                            
                            sent_msg = bot.send_message(chat_id, mail_alert, reply_markup=markup, disable_web_page_preview=True)
                            account['msg_ids'].append(sent_msg.message_id)

                    except Exception as e:
                        pass 
                        
                    if needs_sync: save_user_data(chat_id)
        except Exception as e: 
            pass
        time.sleep(3)

# --- Init User ---
def init_user(message):
    chat_id = str(message.chat.id)
    if chat_id not in user_data:
        user_data[chat_id] = {'accounts': [], 'active_index': -1, 'total_generated': 0, 'name': message.from_user.first_name or "Unknown", 'username': f"@{message.from_user.username}" if message.from_user.username else "N/A", 'joined': datetime.now().strftime("%Y-%m-%d"), 'custom_mail_msgs': [], 'server_pref': 'mailtd'}
        save_user_data(chat_id)

# --- Bot Handlers ---
@bot.message_handler(commands=['start'])
def send_welcome(message):
    init_user(message)
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
        "• Smart OTP Extraction\n\n"
        "<i>👇 Select an option from the menu below to get started!</i>"
    )
    bot.send_message(message.chat.id, welcome_text, reply_markup=get_main_menu(str(message.chat.id)))

@bot.message_handler(func=lambda message: True)
def handle_text(message):
    chat_id = str(message.chat.id)
    text = message.text
    init_user(message)
    
    if is_banned(chat_id): return
    if not system_data.get('bot_active', True) and chat_id != ADMIN_ID:
        bot.send_message(chat_id, "🛠 <b>Bot Under Maintenance!</b>\n\nআপডেটের কাজ চলছে। দয়া করে কিছুক্ষণ পর আবার চেষ্টা করুন।")
        return

    if text == "✨ Generate Premium Mail":
        if check_anti_spam(chat_id): return
        
        anim_msg = bot.send_message(chat_id, "<i>🔄 Connecting...</i>")
        time.sleep(0.1)
        srv_name = "Tmailor" if user_data[chat_id].get('server_pref') == 'tmailor' else "MailTD"
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
        markup = InlineKeyboardMarkup(row_width=1)
        markup.add(
            InlineKeyboardButton(f"{'✅' if curr_srv == 'mailtd' else '⬜'} Default Mail.td", callback_data="set_srv_mailtd"),
            InlineKeyboardButton(f"{'✅' if curr_srv == 'tmailor' else '⬜'} Premium Tmailor.com", callback_data="set_srv_tmailor")
        )
        bot.send_message(chat_id, srv_text, reply_markup=markup)

    elif text == "🏠 Dashboard":
        accounts = user_data[chat_id]['accounts']
        if not accounts: bot.send_message(chat_id, "⚠️ আপনার কোনো অ্যাক্টিভ মেইল নেই।")
        else:
            dash_text = "🗂️ <b>Your Mail Dashboard</b>\n\n"
            markup = InlineKeyboardMarkup(row_width=1)
            for i, acc in enumerate(accounts):
                status = "🟢 Active" if i == user_data[chat_id]['active_index'] else "⚪ Standby"
                srv = "Tmailor" if acc.get('server_type') == 'tmailor' else "MailTD"
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

    elif text == "👤 My Profile":
        ui = user_data[chat_id]
        bot.send_message(chat_id, f"👤 <b>User Profile</b>\n\n📛 <b>Name :</b> {ui['name']}\n🆔 <b>User ID :</b> <code>{chat_id}</code>\n📊 <b>Total Generated :</b> {ui['total_generated']} Mails\n🟢 <b>Current Active :</b> {len(ui['accounts'])} Mails")

    elif text == "⚡ About System":
        about_text = (
            "🚀 <b>Premium Temp Mail Bot</b>\n\n"
            "• Engine: MailTD Architecture & Tmailor Proxy\n"
            "• Performance: Zero-Lag Sync & Anti-Spam\n"
            "• Developer: <a href='https://t.me/Ad_Walid'>Md Walid</a>\n"
            "• Bot Admin: <a href='https://t.me/Ad_Walid'>Md Walid</a>\n\n"
            "<i>Crafted with modern interface aesthetics.</i>"
        )
        bot.send_message(chat_id, about_text, disable_web_page_preview=True)

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
def process_add_api(message, srv_type):
    new_token = message.text.strip()
    if len(new_token) > 5: 
        token_key = f"{srv_type}_tokens"
        if new_token not in api_data.get(token_key, []):
            if token_key not in api_data: api_data[token_key] = []
            api_data[token_key].append(new_token)
            save_system_data()
            bot.send_message(message.chat.id, f"✅ <b>API Added Successfully!</b>\n\nমোট API সংখ্যা এখন: {len(api_data[token_key])}", reply_markup=get_back_button())
        else: bot.send_message(message.chat.id, "⚠️ এই API Token টি আগেই লিস্টে আছে।", reply_markup=get_back_button())
    else: bot.send_message(message.chat.id, "❌ ইনভ্যালিড টোকেন!", reply_markup=get_back_button())

# --- Admin Other Functions ---
def process_ban(message):
    if not message.text.isdigit(): return
    banned_users.add(message.text.strip())
    save_system_data()
    bot.send_message(message.chat.id, f"✅ <b>{message.text}</b> কে সাসপেন্ড করা হয়েছে!", reply_markup=get_back_button())

def process_unban(message):
    if not message.text.isdigit(): return
    banned_users.discard(message.text.strip())
    save_system_data()
    bot.send_message(message.chat.id, f"✅ <b>{message.text}</b> অ্যাকাউন্ট অ্যাক্টিভ করা হয়েছে!", reply_markup=get_back_button())

def process_promo_text(message):
    bot.clear_step_handler_by_chat_id(message.chat.id)
    msg = bot.send_message(message.chat.id, "🔗 বাটনের জন্য লিংক দিন (না দিতে চাইলে 'no' লিখুন):")
    bot.register_next_step_handler(msg, broadcast_promo, promo_message=message)

def broadcast_promo(button_message, promo_message):
    link = button_message.text.strip()
    markup = InlineKeyboardMarkup()
    if link.lower() != 'no' and link.startswith('http'): 
        markup.add(InlineKeyboardButton("🚀 Visit Link", url=link))
        
    bot.send_message(button_message.chat.id, "🚀 <b>Premium Broadcast Started...</b>")
    
    def send_to_all():
        system_data['active_promos'].clear()
        for uid in list(user_data.keys()):
            try:
                header = "🌟 <b>Important Notice from Admin</b> 🌟\n━━━━━━━━━━━━━━━━━━━━\n\n"
                if promo_message.content_type == 'text':
                    sent = bot.send_message(uid, f"{header}{promo_message.text}", reply_markup=markup if markup.keyboard else None)
                else:
                    sent = bot.copy_message(chat_id=uid, from_chat_id=promo_message.chat.id, message_id=promo_message.message_id, reply_markup=markup if markup.keyboard else None)
                system_data['active_promos'][uid] = sent.message_id
            except: pass
            time.sleep(0.05)
    threading.Thread(target=send_to_all, daemon=True).start()

@bot.callback_query_handler(func=lambda call: True)
def handle_callback(call):
    chat_id = str(call.message.chat.id)
    if is_banned(chat_id): return
    
    if call.data.startswith('cp_'):
        # Silent response so no popup disrupts the UX, allows client handling
        bot.answer_callback_query(call.id)

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
        
        curr_srv = new_pref
        srv_text = "🌐 <b>Select Your Preferred Server</b>\n\nযেকোনো সোশ্যাল মিডিয়া অ্যাকাউন্ট খুলতে হাই-কোয়ালিটি সার্ভার বেছে নিন:"
        markup = InlineKeyboardMarkup(row_width=1)
        markup.add(
            InlineKeyboardButton(f"{'✅' if curr_srv == 'mailtd' else '⬜'} Default Mail.td", callback_data="set_srv_mailtd"),
            InlineKeyboardButton(f"{'✅' if curr_srv == 'tmailor' else '⬜'} Premium Tmailor.com", callback_data="set_srv_tmailor")
        )
        bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=markup)
            
    elif chat_id == ADMIN_ID:
        if call.data == "admin_back":
            bot.edit_message_text("⚙️ <b>Admin Control Panel</b>\n\nবেছে নিন আপনি কী করতে চান:", chat_id, call.message.message_id, reply_markup=get_admin_menu())
            
        elif call.data == "admin_toggle_bot":
            system_data['bot_active'] = not system_data.get('bot_active', True)
            save_system_data()
            bot.answer_callback_query(call.id, f"Bot is now {'ON' if system_data['bot_active'] else 'OFF'}")
            bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=get_admin_menu())

        elif call.data == "admin_apis_select":
            markup = InlineKeyboardMarkup(row_width=2)
            markup.add(
                InlineKeyboardButton("⚙️ Mail.td APIs", callback_data="admin_mngapi_mailtd"),
                InlineKeyboardButton("⚙️ Tmailor.com APIs", callback_data="admin_mngapi_tmailor")
            )
            markup.add(InlineKeyboardButton("🔙 Back to Panel", callback_data="admin_back"))
            bot.edit_message_text("🌐 <b>Select Server to Manage API:</b>", chat_id, call.message.message_id, reply_markup=markup)

        elif call.data.startswith("admin_mngapi_"):
            srv_type = call.data.split('_')[2]
            restore_apis()
            markup = InlineKeyboardMarkup(row_width=2)
            markup.add(
                InlineKeyboardButton("➕ Add API Token", callback_data=f"admin_addapi_{srv_type}"),
                InlineKeyboardButton("🗑️ Delete API", callback_data=f"admin_delapi_list_{srv_type}")
            )
            markup.add(InlineKeyboardButton("🔙 Back", callback_data="admin_apis_select"))
            
            token_key = f"{srv_type}_tokens"
            api_info = f"🔑 <b>{srv_type.capitalize()} Limit Management</b>\n\n"
            for i, token in enumerate(api_data.get(token_key, [])):
                usage = api_data['usage'].get(token, 0)
                status = "🔴 Exhausted" if token in api_data['exhausted'] else "🟢 Active"
                short_token = f"{token[:6]}...{token[-4:]}" if len(token) > 10 else token
                api_info += f"<b>{i+1}.</b> <code>{short_token}</code>\n└ Ops: <b>{usage} / 1000</b> | {status}\n\n"
            bot.edit_message_text(api_info, chat_id, call.message.message_id, reply_markup=markup)

        elif call.data.startswith("admin_addapi_"):
            srv_type = call.data.split('_')[2]
            msg = bot.edit_message_text(f"➕ <b>Add New API Token for {srv_type.capitalize()}</b>\n\nআপনার নতুন API Token টি টাইপ করে সেন্ড করুন:", chat_id, call.message.message_id, reply_markup=get_back_button())
            bot.register_next_step_handler(msg, lambda m: process_add_api(m, srv_type))

        elif call.data.startswith("admin_delapi_list_"):
            srv_type = call.data.split('_')[3]
            markup = InlineKeyboardMarkup(row_width=1)
            token_key = f"{srv_type}_tokens"
            for i, token in enumerate(api_data.get(token_key, [])):
                short_token = f"{token[:6]}...{token[-4:]}" if len(token) > 10 else token
                markup.add(InlineKeyboardButton(f"❌ Delete: {short_token}", callback_data=f"delapi_{srv_type}_{i}"))
            markup.add(InlineKeyboardButton("🔙 Back", callback_data=f"admin_mngapi_{srv_type}"))
            bot.edit_message_text(f"🗑️ <b>Select {srv_type.capitalize()} API to Delete:</b>", chat_id, call.message.message_id, reply_markup=markup)

        elif call.data.startswith("delapi_"):
            parts = call.data.split('_')
            srv_type = parts[1]
            idx = int(parts[2])
            token_key = f"{srv_type}_tokens"
            
            if 0 <= idx < len(api_data.get(token_key, [])):
                deleted_token = api_data[token_key].pop(idx)
                if deleted_token in api_data['usage']: del api_data['usage'][deleted_token]
                if deleted_token in api_data['exhausted']: del api_data['exhausted'][deleted_token]
                save_system_data()
                bot.answer_callback_query(call.id, "✅ API Deleted Successfully!", show_alert=True)
                
                markup = InlineKeyboardMarkup(row_width=1)
                for i, token in enumerate(api_data.get(token_key, [])):
                    short_token = f"{token[:6]}...{token[-4:]}" if len(token) > 10 else token
                    markup.add(InlineKeyboardButton(f"❌ Delete: {short_token}", callback_data=f"delapi_{srv_type}_{i}"))
                markup.add(InlineKeyboardButton("🔙 Back", callback_data=f"admin_mngapi_{srv_type}"))
                bot.edit_message_reply_markup(chat_id, call.message.message_id, reply_markup=markup)
            
        elif call.data == "admin_stats":
            total_users = len(user_data)
            active_accounts = sum(len(d.get('accounts', [])) for d in user_data.values())
            
            mtd = tml = 0
            for d in user_data.values():
                for acc in d.get('accounts', []):
                    if acc.get('server_type') == 'tmailor': tml += 1
                    else: mtd += 1
                    
            stats = f"📊 <b>Bot Live Statistics</b>\n\n👥 Total Users: <b>{total_users}</b>\n🚫 Suspended Users: <b>{len(banned_users)}</b>\n\n📧 Total Mails Gen: <b>{bot_stats['total_mails_generated']}</b>\n🟢 Current Active Mails: <b>{active_accounts}</b>\n\n🌐 Server Usage Distribution:\n- MailTD Base: <b>{mtd}</b>\n- Tmailor Base: <b>{tml}</b>"
            bot.edit_message_text(stats, chat_id, call.message.message_id, reply_markup=get_back_button())
            
        elif call.data == "admin_users":
            user_list = "👥 <b>Recent Users List:</b>\n\n"
            for uid, data in list(user_data.items())[-20:]:
                user_list += f"• {data.get('name', 'Unknown')} (<code>{uid}</code>) - <b>{data.get('total_generated', 0)} Mails</b>\n"
            bot.edit_message_text(user_list, chat_id, call.message.message_id, reply_markup=get_back_button())
            
        elif call.data == "admin_download_txt":
            bot.answer_callback_query(call.id, "Generating TXT file...")
            txt_content = "ID | Name | Username | Total Generated\n" + "-"*50 + "\n"
            for uid, data in user_data.items():
                txt_content += f"{uid} | {data.get('name', 'Unknown')} | {data.get('username', 'N/A')} | {data.get('total_generated', 0)}\n"
            
            with open("user_list.txt", "w", encoding="utf-8") as f:
                f.write(txt_content)
                
            with open("user_list.txt", "rb") as f:
                bot.send_document(chat_id, f, caption="📄 <b>All Users List</b>", parse_mode='HTML')
            os.remove("user_list.txt")

        elif call.data == "admin_ban":
            bot.edit_message_text("✍️ <b>Suspend User:</b>\n\nযাকে সাসপেন্ড করতে চান তার User ID টাইপ করে সেন্ড করুন:", chat_id, call.message.message_id, reply_markup=get_back_button())
            bot.register_next_step_handler(call.message, process_ban)
            
        elif call.data == "admin_unban":
            bot.edit_message_text("✍️ <b>Activate User:</b>\n\nযাকে অ্যাক্টিভ করতে চান তার User ID সেন্ড করুন:", chat_id, call.message.message_id, reply_markup=get_back_button())
            bot.register_next_step_handler(call.message, process_unban)
            
        elif call.data == "admin_send_promo":
            bot.clear_step_handler_by_chat_id(chat_id)
            msg = bot.edit_message_text("📢 <b>Premium Broadcast:</b>\n\nনোটিশ বা প্রোমোশনাল পোস্টের টেক্সট বা ছবি লিখে সেন্ড করুন:", chat_id, call.message.message_id, reply_markup=get_back_button())
            bot.register_next_step_handler(msg, process_promo_text)
            
        elif call.data == "admin_del_promo":
            deleted = 0
            for uid, msg_id in system_data['active_promos'].items():
                try: bot.delete_message(uid, msg_id); deleted += 1
                except: pass
            system_data['active_promos'].clear()
            bot.edit_message_text(f"✅ <b>Promo Deleted!</b>\n\n{deleted} জন ইউজারের ইনবক্স থেকে সর্বশেষ মেসেজ মুছে ফেলা হয়েছে।", chat_id, call.message.message_id, reply_markup=get_back_button())

if __name__ == "__main__":
    # --- Start Setup ---
    load_all_data_from_firebase()
    threading.Thread(target=run_web_server, daemon=True).start()
    threading.Thread(target=auto_check_mail, daemon=True).start()
    print("🚀 Pro Mail Bot (Dual Server + Native Touch Copy) is Live...")
    while True:
        try: bot.polling(none_stop=True, interval=0, timeout=20)
        except Exception: time.sleep(5)
