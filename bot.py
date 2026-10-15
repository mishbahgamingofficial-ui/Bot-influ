import os
import re
import time
import json
import asyncio
import logging
import sqlite3
from datetime import datetime
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetParticipantRequest
from telethon.tl.types import (
    InputPeerUser,
    UpdateBotChatInviteRequester,
    PeerUser,
    PeerChannel,
)
from telethon.errors import (
    UserIsBlockedError,
    FloodWaitError,
    UserNotParticipantError,
    PeerIdInvalidError,
    ChatInvalidError,
    ChannelInvalidError,
    MessageIdInvalidError,
)

# ------------------ ENVIRONMENT ------------------
load_dotenv()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("ProBot")

API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", 10000))
SESSION_STRING = os.environ.get("SESSION_STRING", "")

START_TIME = time.time()

def parse_admin_ids(env_string):
    ids = []
    for uid in env_string.split(","):
        uid = uid.strip()
        if uid.lstrip('-').isdigit():
            ids.append(int(uid))
    return ids

ROOT_ADMINS = parse_admin_ids(os.environ.get("ADMIN_IDS", "0"))
ADMIN_IDS = list(ROOT_ADMINS)

# ------------------ DATABASE & STATE ------------------
DB_PATH = Path("bot_data.db")

tracked_users: dict[int, int] = {}      
blocked_users: set[int] = set()
banned_users: set[int] = set()
user_join_dates: dict[int, str] = {}
saved_messages: dict[int, dict] = {}    
button_forwards: dict[str, list] = {}   

welcome_enabled = True
animations_enabled = True
broadcast_cancel_flag = False

# Default "👇" रखा है जैसा तेरे इस कोड में था (Admin Panel से बदल भी सकते हो)
user_menu_text = "👇"
support_username = "@SAHILOFFICIAL_0"
force_channel = ""
banner_config = {"msg_id": 0, "chat_id": 0}
click_stats = {"prediction": 0, "hack": 0, "support": 0}

recently_welcomed: set[int] = set()
user_chat_state: dict[int, bool] = {}
admin_chat_state: dict[int, dict] = {}
auto_replies: dict[str, str] = {}
admin_msg_map: dict[tuple, list] = {} 

ADMIN_KB_LABELS = {
    "📊 stats", "⚙️ status", "📢 broadcast", "✉️ send message",
    "🔘 set button", "🗑 clear button", "🔢 set sequence", "🖼️ menu & banner",
    "🔒 force join", "🤖 auto-replies", "👑 manage admins", "🔇 toggle welcome",
    "📁 backup", "🔄 restore", "🧹 cleanup", "🔍 find user", "panel", "menu", "admin"
}

CANCEL_BTN = [[Button.inline("❌ Cancel Operation", data=b"adm_cancel")]]
SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

def get_uptime():
    seconds = int(time.time() - START_TIME)
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days > 0:
        return f"{days}d {hours}h {minutes}m"
    elif hours > 0:
        return f"{hours}h {minutes}m {seconds}s"
    return f"{minutes}m {seconds}s"

def format_eta(seconds: float) -> str:
    if seconds <= 0 or seconds > 86400:
        return "Calculating..."
    m, s = divmod(int(seconds), 60)
    if m > 0:
        return f"{m}m {s}s"
    return f"{s}s"

# ------------------ ANIMATION ENGINE ------------------
async def play_animation(event_or_msg, frames: list[tuple[str, float]], is_edit: bool = False, use_reply: bool = True):
    """Plays a multi-frame cyberpunk animation smoothly."""
    if is_edit:
        msg = event_or_msg
    else:
        msg = await (event_or_msg.reply(frames[0][0]) if use_reply else event_or_msg.respond(frames[0][0]))
        
    if not animations_enabled:
        return msg
        
    start_idx = 0 if is_edit else 1
    for i in range(start_idx, len(frames)):
        text_frame, delay = frames[i]
        await asyncio.sleep(delay)
        try:
            await msg.edit(text_frame)
        except Exception:
            pass
    return msg

def init_db():
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                access_hash INTEGER NOT NULL,
                blocked INTEGER DEFAULT 0,
                joined_date TEXT
            );
            CREATE TABLE IF NOT EXISTS messages (
                step INTEGER PRIMARY KEY,
                msg_type TEXT,
                text TEXT,
                msg_id INTEGER,
                from_chat INTEGER
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS button_msgs (
                btn_key TEXT,
                msg_id INTEGER NOT NULL,
                from_chat INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS auto_replies (
                keyword TEXT PRIMARY KEY,
                response TEXT
            );
            CREATE TABLE IF NOT EXISTS admins (
                user_id INTEGER PRIMARY KEY
            );
            CREATE TABLE IF NOT EXISTS banned (
                user_id INTEGER PRIMARY KEY
            );
        """)
        return conn

def load_from_db():
    global tracked_users, blocked_users, banned_users, user_join_dates, saved_messages, button_forwards
    global welcome_enabled, animations_enabled, user_menu_text, support_username, force_channel, banner_config, click_stats, auto_replies, ADMIN_IDS
    
    with sqlite3.connect(str(DB_PATH)) as conn:
        cur = conn.cursor()
        
        cur.execute("SELECT user_id FROM admins")
        for (uid,) in cur.fetchall():
            if uid not in ADMIN_IDS:
                ADMIN_IDS.append(uid)

        cur.execute("SELECT user_id FROM banned")
        for (uid,) in cur.fetchall():
            banned_users.add(uid)

        cur.execute("SELECT user_id, access_hash, blocked, joined_date FROM users")
        for uid, hsh, blk, jdate in cur.fetchall():
            tracked_users[uid] = hsh
            if jdate:
                user_join_dates[uid] = jdate[:10]
            if blk:
                blocked_users.add(uid)
                
        cur.execute("SELECT step, msg_type, text, msg_id, from_chat FROM messages")
        for step, mtype, text, mid, fchat in cur.fetchall():
            saved_messages[step] = {
                "type": mtype, "text": text, "msg_id": mid, "from_chat": fchat
            }
            
        cur.execute("SELECT btn_key, msg_id, from_chat FROM button_msgs")
        button_forwards.clear()
        for key, mid, fchat in cur.fetchall():
            if key not in button_forwards:
                button_forwards[key] = []
            button_forwards[key].append({"msg_id": mid, "from_chat": fchat})
            
        cur.execute("SELECT key, value FROM settings")
        for k, v in cur.fetchall():
            if k == 'welcome_enabled':
                welcome_enabled = (v == "1")
            elif k == 'animations_enabled':
                animations_enabled = (v == "1")
            elif k == 'user_menu_text':
                user_menu_text = v
            elif k == 'support_username':
                support_username = v
            elif k == 'force_channel':
                force_channel = v
            elif k == 'banner_config':
                try: banner_config = json.loads(v)
                except: pass
            elif k == 'click_stats':
                try: click_stats = json.loads(v)
                except: pass
            
        cur.execute("SELECT keyword, response FROM auto_replies")
        for kw, resp in cur.fetchall():
            auto_replies[kw] = resp
            
    logger.info(f"Loaded {len(tracked_users)} users, {len(auto_replies)} auto-replies, {len(ADMIN_IDS)} admins.")

def save_user(uid: int, access_hash: int, blocked: bool = False):
    jdate = user_join_dates.get(uid) or datetime.now().strftime("%Y-%m-%d")
    user_join_dates[uid] = jdate
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (user_id, access_hash, blocked, joined_date) VALUES (?,?,?,?)",
            (uid, access_hash, int(blocked), jdate),
        )

def save_message_step(step: int, data: dict):
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO messages (step, msg_type, text, msg_id, from_chat) VALUES (?,?,?,?,?)",
            (step, data.get("type"), data.get("text"), data.get("msg_id"), data.get("from_chat")),
        )

def add_button_config(key: str, msg_id: int, from_chat: int):
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute(
            "INSERT INTO button_msgs (btn_key, msg_id, from_chat) VALUES (?,?,?)",
            (key, msg_id, from_chat),
        )

def delete_button_config(key: str):
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("DELETE FROM button_msgs WHERE btn_key=?", (key,))

def set_setting(key: str, value: str):
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))

def increment_click(key: str):
    if key in click_stats:
        click_stats[key] = click_stats.get(key, 0) + 1
        set_setting("click_stats", json.dumps(click_stats))

def get_chat_id(reply_msg, event):
    if hasattr(reply_msg, "chat_id") and reply_msg.chat_id:
        return reply_msg.chat_id
    elif hasattr(reply_msg, "peer_id"):
        pid = reply_msg.peer_id
        if isinstance(pid, PeerUser):
            return pid.user_id
        elif isinstance(pid, PeerChannel):
            return pid.channel_id
    return event.chat_id

# ------------------ CLIENT ------------------
client = TelegramClient(
    StringSession(SESSION_STRING) if SESSION_STRING else "bot_session",
    API_ID,
    API_HASH,
)

# ------------------ WEB SERVER ------------------
async def web_handler(request):
    return web.Response(text=f"🟢 Bot is alive | Uptime: {get_uptime()} | Users: {len(tracked_users)}")

async def start_web():
    app = web.Application()
    app.add_routes([web.get("/", web_handler)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server on port {PORT}")

# ------------------ DECORATORS & KEYBOARDS ------------------
def admin_only(func):
    async def wrapper(event):
        if event.sender_id not in ADMIN_IDS:
            await event.reply("❌ `[Access Denied] Admin privileges required.`")
            return
        await func(event)
    return wrapper

def get_admin_keyboard():
    return [
        [Button.text("📊 Stats", resize=True), Button.text("⚙️ Status", resize=True)],
        [Button.text("📢 Broadcast", resize=True), Button.text("✉️ Send Message", resize=True)],
        [Button.text("🔘 Set Button", resize=True), Button.text("🗑 Clear Button", resize=True)],
        [Button.text("🔢 Set Sequence", resize=True), Button.text("🖼️ Menu & Banner", resize=True)],
        [Button.text("🔒 Force Join", resize=True), Button.text("🤖 Auto-Replies", resize=True)],
        [Button.text("👑 Manage Admins", resize=True), Button.text("🔍 Find User", resize=True)],
        [Button.text("📁 Backup", resize=True), Button.text("🔄 Restore", resize=True)],
        [Button.text("🔇 Toggle Welcome", resize=True), Button.text("🧹 Cleanup", resize=True)]
    ]

# ------------------ FORCE JOIN CHECKER ------------------
async def is_user_joined(uid: int) -> bool:
    if not force_channel or uid in ADMIN_IDS:
        return True
    try:
        clean_ch = force_channel.lstrip("@")
        await client(GetParticipantRequest(channel=clean_ch, participant=uid))
        return True
    except UserNotParticipantError:
        return False
    except Exception as e:
        logger.warning(f"Force join check error ({force_channel}): {e}")
        return True

async def send_force_join_prompt(event, key: str):
    clean_ch = force_channel.lstrip("@")
    btns = [
        [Button.url("📢 Join VIP Channel", f"https://t.me/{clean_ch}")],
        [Button.inline("🔄 Verify & Unlock", data=f"usr_verify_{key}".encode())]
    ]
    msg_text = (
        "🚨 **SECURITY CLEARANCE REQUIRED** 🚨\n"
        "➖➖➖➖➖➖➖➖➖➖➖➖\n"
        "1. Official VIP Channel - **Not Joined ❌**\n\n"
        "👉 *Click **Join VIP Channel** below, then tap **Verify & Unlock** to get your VIP package immediately!*"
    )
    await event.reply(msg_text, buttons=btns)

@client.on(events.CallbackQuery(pattern=b"^usr_verify_(hack|prediction)$"))
async def user_verify_force_join(event):
    key = event.pattern_match.group(1).decode("utf-8")
    if not await is_user_joined(event.sender_id):
        return await event.answer("❌ You haven't joined the channel yet! Join first and try again.", alert=True)
    await event.answer("✅ Verified! Unlocking package...")
    try:
        await event.delete()
    except Exception:
        pass
    await send_button_forward(event, key, is_callback=True)

# ------------------ ADMIN ALERTS (WITH 1-CLICK BUTTONS) ------------------
async def notify_admins_new_user(user):
    first_name = user.first_name or ""
    last_name = user.last_name or ""
    full_name = f"{first_name} {last_name}".strip()
    
    safe_name = full_name.replace('[', '').replace(']', '').replace('*', '').replace('_', '').replace('`', '')
    if not safe_name.strip():
        safe_name = "Unknown User"
        
    linked_name = f"[{safe_name}](tg://user?id={user.id})"
    username_display = f"@{user.username}" if user.username else "❌ `[No Username]`"
    
    text = (
        f"🚨 **NEW USER CONNECTION** 🚨\n"
        f"➖➖➖➖➖➖➖➖➖➖➖➖\n"
        f"👤 **Name:** {linked_name}\n"
        f"🔗 **Username:** {username_display}\n"
        f"🆔 **UID:** `{user.id}` *(Tap to copy)*\n"
        f"📊 **Total Users:** `{len(tracked_users)}`\n"
        f"➖➖➖➖➖➖➖➖➖➖➖➖"
    )
    btns = [
        [
            Button.inline("✉️ Send DM", data=f"adm_quickdm_{user.id}".encode()),
            Button.inline("🚫 Ban User", data=f"adm_quickban_{user.id}".encode())
        ]
    ]
    for admin_id in ADMIN_IDS:
        try:
            await client.send_message(admin_id, text, buttons=btns)
        except Exception:
            pass

# ------------------ WELCOME SYSTEM & KEYBOARD MENU ------------------
async def remove_welcome_cooldown(uid):
    await asyncio.sleep(30)
    recently_welcomed.discard(uid)

async def send_user_menu(entity):
    # 🔥 EXACT REPLY KEYBOARD BUTTONS AS REQUESTED 🔥
    btns = [
        [Button.text("⚡️ I Want Number HACK", resize=True)],
        [Button.text("📊 How To Start COLOUR TRADING", resize=True)],
        [Button.text("🎧 Contact Support", resize=True)]
    ]
    try:
        safe_text = user_menu_text or "👇"
        if banner_config.get("msg_id") and banner_config.get("chat_id"):
            try:
                b_msg = await client.get_messages(banner_config["chat_id"], ids=banner_config["msg_id"])
                if b_msg and b_msg.media:
                    await client.send_file(entity, b_msg.media, caption=safe_text, buttons=btns)
                    return
            except Exception as banner_err:
                logger.warning(f"Banner load failed, falling back to text: {banner_err}")
                
        await client.send_message(entity, safe_text, buttons=btns)
    except Exception as e:
        logger.error(f"Menu error: {e}")

async def send_welcome_sequence(user):
    uid = user.id
    if uid in recently_welcomed or uid in banned_users:
        return
    recently_welcomed.add(uid)

    if getattr(user, "is_bot", False):
        return

    try:
        full_user = await client.get_input_entity(uid)
        if not isinstance(full_user, InputPeerUser):
            return
        access_hash = full_user.access_hash
    except Exception:
        return

    is_new_user = uid not in tracked_users
    tracked_users[uid] = access_hash
    
    save_user(uid, access_hash, blocked=False)
    blocked_users.discard(uid)

    if is_new_user and uid not in ADMIN_IDS:
        asyncio.create_task(notify_admins_new_user(user))

    if not welcome_enabled or uid in ADMIN_IDS:
        asyncio.create_task(remove_welcome_cooldown(uid))
        return

    for step in sorted(saved_messages.keys()):
        item = saved_messages[step]
        try:
            if item["type"] == "forward":
                try:
                    await client.get_messages(item["from_chat"], ids=item["msg_id"])
                except Exception:
                    continue
                await client.forward_messages(full_user, item["msg_id"], item["from_chat"])
            else:
                await client.send_message(full_user, item["text"])
            await asyncio.sleep(0.5)
        except UserIsBlockedError:
            blocked_users.add(uid)
            save_user(uid, access_hash, blocked=True)
            return
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds)
        except Exception:
            continue
            
    asyncio.create_task(remove_welcome_cooldown(uid))

@client.on(events.ChatAction())
async def chat_action(event):
    if event.user_joined or event.user_added:
        user = await event.get_user()
        if user and user.id not in banned_users:
            await send_welcome_sequence(user)
            if user.id not in ADMIN_IDS:
                await send_user_menu(user)

@client.on(events.Raw(types=UpdateBotChatInviteRequester))
async def join_request(event):
    try:
        if event.user_id in banned_users:
            return
        user = await client.get_entity(event.user_id)
        await send_welcome_sequence(user)
        if event.user_id not in ADMIN_IDS:
            await send_user_menu(user)
    except Exception:
        pass

# ------------------ START COMMAND & PANEL OPENER ------------------
@client.on(events.NewMessage(pattern=r"(?i)^(/start|panel|menu|admin)$"))
async def start(event):
    if event.sender_id in banned_users:
        return await event.reply("🚫 **Access Denied:** You have been banned from using this bot.")

    raw_cmd = event.raw_text.strip().lower()
    if raw_cmd in ("panel", "menu", "admin") and event.sender_id not in ADMIN_IDS:
        return

    user_chat_state[event.sender_id] = False 
    admin_chat_state.pop(event.sender_id, None)
    
    if event.sender_id in blocked_users:
        blocked_users.discard(event.sender_id)
        access_hash = tracked_users.get(event.sender_id, 0)
        save_user(event.sender_id, access_hash, blocked=False)
    
    if event.sender_id in ADMIN_IDS:
        boot_msg = await event.reply("⚡ `[ROOT] Authenticating VIP Admin Console...`")
        await asyncio.sleep(0.3)
        try:
            await boot_msg.delete()
        except Exception:
            pass
        admin_panel_text = (
            "👑 **ZERO-SLASH VIP ADMIN CONSOLE** 👑\n"
            "➖➖➖➖➖➖➖➖➖➖➖➖\n"
            f"🟢 **Uptime:** `{get_uptime()}` | 👥 **Users:** `{len(tracked_users)}`\n"
            f"🎬 **Animations:** `{'🟢 ON' if animations_enabled else '🔴 OFF'}`\n\n"
            "👇 **Select an operation below to manage your bot seamlessly (Zero `/` needed):**"
        )
        await event.respond(admin_panel_text, buttons=get_admin_keyboard())
    else:
        user = await event.get_sender()
        if user:
            await send_welcome_sequence(user)
            await send_user_menu(user)

# ------------------ USER KEYBOARD BUTTON HANDLERS & ANIMATIONS ------------------
@client.on(events.NewMessage(func=lambda e: e.is_private and e.text and "number hack" in e.text.lower()))
async def hack_button_handler(event):
    if event.sender_id in banned_users:
        return
    increment_click("hack")
    if not await is_user_joined(event.sender_id):
        return await send_force_join_prompt(event, "hack")
    await send_button_forward(event, "hack")

@client.on(events.NewMessage(func=lambda e: e.is_private and e.text and "colour trading" in e.text.lower()))
async def prediction_button_handler(event):
    if event.sender_id in banned_users:
        return
    increment_click("prediction")
    if not await is_user_joined(event.sender_id):
        return await send_force_join_prompt(event, "prediction")
    await send_button_forward(event, "prediction")

@client.on(events.NewMessage(func=lambda e: e.is_private and e.text and "contact support" in e.text.lower()))
async def contact_admin_handler(event):
    if event.sender_id in banned_users or event.sender_id in ADMIN_IDS:
        return
    increment_click("support")
    user_chat_state[event.sender_id] = True
    
    sup_frames = [
        ("🔐 `[TUNNEL] Initializing 256-bit encrypted support line...`", 0.35),
        ("📡 `[TUNNEL] Connecting to VIP Admin Server [██████░░░░] 65%`", 0.35),
        ("🟢 `[CONNECTED] Secure Support Channel Established!`", 0.3)
    ]
    anim_msg = await play_animation(event, sup_frames, use_reply=True)
    try:
        await anim_msg.delete()
    except Exception:
        pass
        
    await event.reply(
        f"📝 **Support Section** 🟢\n"
        f"➖➖➖➖➖➖➖➖➖➖➖➖\n"
        f"👤 **Admin Contact ID** - {support_username}\n\n"
        f"✍️ *Aap yahan niche seedha apna message, photo ya screenshot bhej sakte hain, wo direct Admin ko deliver ho jayega!*"
    )

async def send_button_forward(event, key, is_callback: bool = False):
    uid = event.sender_id
    config_list = button_forwards.get(key)
    
    if not config_list:
        msg_txt = "⚠️ `[Notice] This module is not configured yet. Please check back later.`"
        if is_callback:
            await event.respond(msg_txt)
        else:
            await event.reply(msg_txt)
        return

    if key == "hack":
        hack_frames = [
            ("⚡ `[ROOT] Connecting to Dark-Node Proxy Server...`\n`[█░░░░░░░░░] 12%`", 0.35),
            ("🌐 `[BYPASS] Injecting packet spoofing into firewall...`\n`[███░░░░░░░] 34%`", 0.35),
            ("🔐 `[CRACK] Bypassing SHA-256 Security Matrix...`\n`[█████░░░░░] 58%`", 0.35),
            ("🧬 `[DECRYPT] Unlocking VIP Number Hack Payload...`\n`[████████░░] 84%`", 0.35),
            ("📂 `[VERIFIED] Checksum matched! Extracting files...`\n`[██████████] 100%`", 0.35),
            ("✅ **SUCCESS:** Package extracted securely. Sending now... 🚀", 0.4)
        ]
        status_msg = await play_animation(event, hack_frames, use_reply=not is_callback)
    elif key == "prediction":
        pred_frames = [
            ("🤖 `[QUANTUM-AI] Booting Neural Prediction Engine v4.2...`\n`[█░░░░░░░░░] 15%`", 0.35),
            ("📊 `[MARKET-SCAN] Fetching live colour trading history...`\n`[████░░░░░░] 42%`", 0.35),
            ("🧠 `[DEEP-LEARN] Analyzing 10,000+ winning patterns...`\n`[███████░░░] 71%`", 0.35),
            ("🔍 `[ACCURACY] Filtering 99.4% high-probability trend...`\n`[█████████░] 93%`", 0.35),
            ("🎯 `[SIGNAL LOCKED] High-Win Colour Prediction Ready!`\n`[██████████] 100%`", 0.35),
            ("🎯 **SIGNAL READY:** Forwarding prediction details... 💸", 0.4)
        ]
        status_msg = await play_animation(event, pred_frames, use_reply=not is_callback)
    else:
        status_msg = await (event.respond("⏳ `[sys] Processing request...`") if is_callback else event.reply("⏳ `[sys] Processing request...`"))

    try:
        sent_count = 0
        for config in config_list:
            admin_id = config["from_chat"]
            admin_hash = tracked_users.get(admin_id, 0)
            from_peer = InputPeerUser(admin_id, admin_hash) if (admin_id > 0 and admin_hash) else admin_id 
            try:
                await client.forward_messages(uid, config["msg_id"], from_peer)
                sent_count += 1
            except Exception as inner_e:
                logger.warning(f"Could not forward msg {config['msg_id']} for {uid}: {inner_e}")
            await asyncio.sleep(0.3) 
            
        if sent_count == 0:
            await status_msg.edit("❌ `[Error] Configured messages are unavailable or deleted.`")
        else:
            await asyncio.sleep(0.8)
            await status_msg.delete() 
            
    except UserIsBlockedError:
        blocked_users.add(uid)
        save_user(uid, tracked_users.get(uid, 0), blocked=True)
    except FloodWaitError as e:
        await status_msg.edit(f"⏳ Rate limit exceeded. Please wait {e.seconds} seconds.")
    except Exception as e:
        logger.error(f"Button {key} error for {uid}: {e}")
        await status_msg.edit("❌ An error occurred. Please try again later.")

# =====================================================================
# 👑 ZERO-SLASH INTERACTIVE ADMIN KEYBOARD HANDLERS (WITH ANIMATIONS)
# =====================================================================

@client.on(events.NewMessage(pattern=r"^(/stats|📊 Stats)$"))
@admin_only
async def stats_btn_handler(event):
    admin_chat_state.pop(event.sender_id, None)
    
    scan_frames = [
        ("📡 `[RADAR] Scanning SQLite Database & User Nodes...`\n`[███░░░░░░░] 30%`", 0.3),
        ("📊 `[METRICS] Aggregating live click analytics & uptime...`\n`[████████░░] 85%`", 0.3)
    ]
    scan_msg = await play_animation(event, scan_frames)
    
    hack_msgs = len(button_forwards.get('hack', []))
    pred_msgs = len(button_forwards.get('prediction', []))
    active_count = len(tracked_users) - len(blocked_users)
    banner_status = "🟢 Set" if banner_config.get("msg_id") else "🔴 None"
    force_status = f"🟢 @{force_channel}" if force_channel else "🔴 OFF"
    total_clicks = sum(click_stats.values())
    
    msg = (
        "📊 **VIP BOT ANALYTICS & METRICS**\n"
        "➖➖➖➖➖➖➖➖➖➖➖➖\n"
        f"⏱ **Server Uptime:** `{get_uptime()}`\n"
        f"👥 **Total Users:** `{len(tracked_users)}` *(🟢 {active_count} Active)*\n"
        f"🚫 **Blocked / Dead:** `{len(blocked_users)}` | **Banned:** `{len(banned_users)}`\n"
        f"👑 **Total Admins:** `{len(ADMIN_IDS)}`\n\n"
        f"📈 **BUTTON CLICK ANALYTICS ({total_clicks} Total):**\n"
        f" ├ ⚡ Number Hack: `{click_stats.get('hack', 0)}` clicks\n"
        f" ├ 📊 Colour Trading: `{click_stats.get('prediction', 0)}` clicks\n"
        f" └ 🎧 Contact Support: `{click_stats.get('support', 0)}` clicks\n\n"
        f"⚙️ **SYSTEM CONFIGURATION:**\n"
        f" ├ 🔊 Welcome: `{'🟢 ON' if welcome_enabled else '🔴 OFF'}` ({len(saved_messages)} steps)\n"
        f" ├ 🎬 Animations: `{'🟢 ON' if animations_enabled else '🔴 OFF'}`\n"
        f" ├ 🖼️ Menu Banner: `{banner_status}`\n"
        f" ├ 🔒 Force Join: `{force_status}`\n"
        f" ├ 🎧 Support ID: `{support_username}`\n"
        f" └ 📁 Files: Hack (`{hack_msgs}`) | Prediction (`{pred_msgs}`)\n"
        "➖➖➖➖➖➖➖➖➖➖➖➖"
    )
    await scan_msg.edit(msg)

@client.on(events.NewMessage(pattern=r"^(/status|⚙️ Status)$"))
@admin_only
async def status_btn_handler(event):
    admin_chat_state.pop(event.sender_id, None)
    diag_frames = [
        ("⚙️ `[DIAGNOSTICS] Pinging Telegram MTProto Data Centers...`", 0.3),
        ("🟢 `[DIAGNOSTICS] All systems nominal! Generating report...`", 0.25)
    ]
    diag_msg = await play_animation(event, diag_frames)
    
    steps = "\n".join(f"  • Step {s}: `{d['type'].upper()}`" for s, d in sorted(saved_messages.items()))
    await diag_msg.edit(
        f"⚙️ **SYSTEM HEALTH & DIAGNOSTICS**\n"
        f"➖➖➖➖➖➖➖➖➖➖➖➖\n"
        f"🟢 **Engine Status:** `ONLINE (100% Smooth)`\n"
        f"⏱ **Live Uptime:** `{get_uptime()}`\n"
        f"👥 **Tracked Peers:** `{len(tracked_users)}`\n"
        f"🤖 **Auto-Replies:** `{len(auto_replies)}` active\n"
        f"🎬 **Cyber Animations:** `{'Enabled' if animations_enabled else 'Disabled'}`\n\n"
        f"🔢 **Welcome Sequence Pipeline:**\n{steps if steps else '  • None configured'}"
    )

@client.on(events.NewMessage(pattern=r"^(/togglewelcome|🔇 Toggle Welcome)$"))
@admin_only
async def toggle_welcome_btn(event):
    global welcome_enabled
    admin_chat_state.pop(event.sender_id, None)
    welcome_enabled = not welcome_enabled
    set_setting("welcome_enabled", "1" if welcome_enabled else "0")
    await event.reply(f"🔊 Welcome protocol is now **{'🟢 ON' if welcome_enabled else '🔴 OFF'}**.")

@client.on(events.NewMessage(pattern=r"^(/cleanup|🧹 Cleanup)$"))
@admin_only
async def cleanup_btn(event):
    admin_chat_state.pop(event.sender_id, None)
    clean_frames = [
        ("🧹 `[VACUUM] Scanning database for dead/blocked accounts...`", 0.35),
        ("🗑️ `[PURGE] Wiping unreachable peers & optimizing SQLite...`", 0.35)
    ]
    c_msg = await play_animation(event, clean_frames)
    
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("DELETE FROM users WHERE blocked=1")
    removed = len(blocked_users)
    for uid in list(blocked_users):
        tracked_users.pop(uid, None)
    blocked_users.clear()
    await c_msg.edit(f"✨ **Database Vacuum Complete!**\n🗑 Removed `{removed}` dead/blocked accounts.\n👥 Active Clean Users: `{len(tracked_users)}`")

@client.on(events.NewMessage(pattern=r"^(/backup|📁 Backup)$"))
@admin_only
async def backup_btn(event):
    admin_chat_state.pop(event.sender_id, None)
    b_frames = [
        ("🔐 `[BACKUP] Serializing user hashes & settings...`", 0.3),
        ("📦 `[EXPORT] Compressing JSON archive & verifying integrity...`", 0.3)
    ]
    b_msg = await play_animation(event, b_frames)
    
    db_admins = [a for a in ADMIN_IDS if a not in ROOT_ADMINS]
    data = {
        "tracked": tracked_users,
        "blocked": list(blocked_users),
        "banned": list(banned_users),
        "join_dates": user_join_dates,
        "messages": {str(k): v for k, v in saved_messages.items()},
        "button_forwards": button_forwards,
        "welcome": welcome_enabled,
        "animations_enabled": animations_enabled,
        "user_menu_text": user_menu_text,
        "support_username": support_username,
        "force_channel": force_channel,
        "banner_config": banner_config,
        "click_stats": click_stats,
        "auto_replies": auto_replies,
        "db_admins": db_admins
    }
    file = "backup.json"
    with open(file, "w") as f:
        json.dump(data, f)
    try:
        await b_msg.delete()
    except Exception:
        pass
    await client.send_file(event.chat_id, file, caption=f"📁 `[VIP System Backup File]`\n👥 Total Users: `{len(tracked_users)}`\n⏱ Uptime: `{get_uptime()}`")
    os.remove(file)

# --- 🔍 FIND USER (VIP DOSSIER) ---
@client.on(events.NewMessage(pattern=r"^🔍 Find User$"))
@admin_only
async def find_user_btn(event):
    admin_id = event.sender_id
    await event.reply(
        "🔍 **VIP USER INSPECTOR**\n"
        "➖➖➖➖➖➖➖➖➖➖➖➖\n"
        "👉 **Jis user ki kundali (status/details) check karni hai uski Numeric UID bhejo:**",
        buttons=CANCEL_BTN
    )
    admin_chat_state[admin_id] = {"step": "inspect_user_id"}

# --- 1. INTERACTIVE BROADCAST MENU ---
@client.on(events.NewMessage(pattern=r"^📢 Broadcast$"))
@admin_only
async def broadcast_menu_btn(event):
    admin_chat_state.pop(event.sender_id, None)
    active_targets = len(tracked_users) - len(blocked_users)
    btns = [
        [Button.inline("📨 Normal Broadcast (Live Tracker)", data=b"adm_bc_normal")],
        [Button.inline("📌 Broadcast + Auto-Pin", data=b"adm_bc_pin")],
        [Button.inline("❌ Close", data=b"adm_cancel")]
    ]
    await event.reply(
        "📢 **VIP BROADCAST ENGINE v2.0**\n"
        "➖➖➖➖➖➖➖➖➖➖➖➖\n"
        f"👥 **Total Database:** `{len(tracked_users)}` users\n"
        f"🟢 **Reachable Targets:** `{active_targets}` users\n"
        f"⚡ **Live Update Rate:** `Every 3 Users`\n\n"
        "👇 *Select broadcast mode below:*",
        buttons=btns
    )

# --- 2. INTERACTIVE SEND DM ---
@client.on(events.NewMessage(pattern=r"^✉️ Send Message$"))
@admin_only
async def send_dm_btn(event):
    admin_id = event.sender_id
    prompt = await event.reply(
        "👤 **DIRECT VIP MESSAGING**\n"
        "➖➖➖➖➖➖➖➖➖➖➖➖\n"
        "👉 Niche target user ki **UID** type karke bhejo:",
        buttons=CANCEL_BTN
    )
    admin_chat_state[admin_id] = {"step": "waiting_for_id", "target_id": None, "prompt_id": prompt.id}

# --- 3. INTERACTIVE SET BUTTON MENU ---
@client.on(events.NewMessage(pattern=r"^🔘 Set Button$"))
@admin_only
async def set_button_menu_btn(event):
    admin_chat_state.pop(event.sender_id, None)
    hack_cnt = len(button_forwards.get("hack", []))
    pred_cnt = len(button_forwards.get("prediction", []))
    btns = [
        [
            Button.inline(f"⚡ Set Hack (Replace)", data=b"adm_sbtn_rep_hack"),
            Button.inline(f"➕ Add Hack ({hack_cnt})", data=b"adm_sbtn_add_hack")
        ],
        [
            Button.inline(f"📊 Set Trading (Replace)", data=b"adm_sbtn_rep_prediction"),
            Button.inline(f"➕ Add Trading ({pred_cnt})", data=b"adm_sbtn_add_prediction")
        ],
        [Button.inline("❌ Close", data=b"adm_cancel")]
    ]
    await event.reply(
        "🔘 **MODULE FILE MANAGER**\n"
        "➖➖➖➖➖➖➖➖➖➖➖➖\n"
        "• **Set (Replace):** Purani files hata kar nayi file lagayega.\n"
        "• **Add (+):** Purani files ke sath ek aur file jodega.\n\n"
        "👇 *Choose which keyboard button to configure:*",
        buttons=btns
    )

# --- 4. INTERACTIVE CLEAR BUTTON MENU ---
@client.on(events.NewMessage(pattern=r"^🗑 Clear Button$"))
@admin_only
async def clear_button_menu_btn(event):
    admin_chat_state.pop(event.sender_id, None)
    btns = [
        [Button.inline("🗑 Clear Number Hack Files", data=b"adm_cbtn_hack")],
        [Button.inline("🗑 Clear Colour Trading Files", data=b"adm_cbtn_prediction")],
        [Button.inline("❌ Close", data=b"adm_cancel")]
    ]
    await event.reply("🗑 **CLEAR MODULE FILES**\n➖➖➖➖➖➖➖➖➖➖➖➖\n👇 *Click to wipe files from a button:*", buttons=btns)

# --- 5. INTERACTIVE WELCOME SEQUENCE MENU ---
@client.on(events.NewMessage(pattern=r"^🔢 Set Sequence$"))
@admin_only
async def sequence_menu_btn(event):
    admin_chat_state.pop(event.sender_id, None)
    active_steps = ", ".join(f"Step {s}" for s in sorted(saved_messages.keys())) or "None"
    btns = [
        [
            Button.inline("➕ Set Step 1", data=b"adm_seq_set_1"),
            Button.inline("➕ Set Step 2", data=b"adm_seq_set_2"),
            Button.inline("➕ Set Step 3", data=b"adm_seq_set_3")
        ],
        [
            Button.inline("➕ Set Step 4", data=b"adm_seq_set_4"),
            Button.inline("➕ Set Step 5", data=b"adm_seq_set_5")
        ],
        [
            Button.inline("🗑 Clear Step 1", data=b"adm_seq_del_1"),
            Button.inline("🗑 Clear Step 2", data=b"adm_seq_del_2"),
            Button.inline("🗑 Clear Step 3", data=b"adm_seq_del_3")
        ],
        [Button.inline("🗑 Clear ALL Steps", data=b"adm_seq_del_all")],
        [Button.inline("❌ Close", data=b"adm_cancel")]
    ]
    await event.reply(
        "🔢 **WELCOME SEQUENCE BUILDER**\n"
        "➖➖➖➖➖➖➖➖➖➖➖➖\n"
        f"✅ **Active Steps:** `{active_steps}`\n\n"
        "👇 *Click a step below to set or delete it:*",
        buttons=btns
    )

# --- 6. INTERACTIVE MENU & BANNER CUSTOMIZER ---
@client.on(events.NewMessage(pattern=r"^🖼️ Menu & Banner$"))
@admin_only
async def menu_banner_btn(event):
    admin_chat_state.pop(event.sender_id, None)
    b_stat = "🟢 Active" if banner_config.get("msg_id") else "🔴 Not Set"
    anim_stat = "🟢 ON" if animations_enabled else "🔴 OFF"
    btns = [
        [Button.inline("✏️ Edit Menu Prompt Text", data=b"adm_mb_text")],
        [
            Button.inline("🖼️ Set Photo/Video Banner", data=b"adm_mb_setbanner"),
            Button.inline("🗑 Remove Banner", data=b"adm_mb_delbanner")
        ],
        [Button.inline("🎧 Change Support @Username", data=b"adm_mb_support")],
        [Button.inline(f"🎬 Toggle Animations ({anim_stat})", data=b"adm_mb_toggle_anim")],
        [Button.inline("👁 Preview User Keyboard Menu", data=b"adm_mb_preview")],
        [Button.inline("❌ Close", data=b"adm_cancel")]
    ]
    await event.reply(
        "🖼️ **MENU, BANNER & FX STUDIO**\n"
        "➖➖➖➖➖➖➖➖➖➖➖➖\n"
        f"💬 **Menu Text:** `{user_menu_text}`\n"
        f"🖼️ **Banner Status:** `{b_stat}`\n"
        f"🎧 **Support ID:** `{support_username}`\n"
        f"🎬 **Cyber Animations:** `{anim_stat}`\n\n"
        "👇 *Select what you want to customize:*",
        buttons=btns
    )

# --- 7. INTERACTIVE FORCE JOIN MENU ---
@client.on(events.NewMessage(pattern=r"^🔒 Force Join$"))
@admin_only
async def force_join_menu_btn(event):
    admin_chat_state.pop(event.sender_id, None)
    f_stat = f"🟢 @{force_channel}" if force_channel else "🔴 Disabled"
    btns = [
        [Button.inline("🔒 Set Force Join Channel", data=b"adm_fj_set")],
        [Button.inline("🔓 Disable Force Join", data=b"adm_fj_clear")],
        [Button.inline("❌ Close", data=b"adm_cancel")]
    ]
    await event.reply(
        "🔒 **FORCE SUBSCRIBE MANAGER**\n"
        "➖➖➖➖➖➖➖➖➖➖➖➖\n"
        f"📌 **Current Channel:** `{f_stat}`\n\n"
        "⚠️ *Make sure bot is Admin in that channel!*",
        buttons=btns
    )

# --- 8. INTERACTIVE AUTO-REPLIES MENU ---
@client.on(events.NewMessage(pattern=r"^🤖 Auto-Replies$"))
@admin_only
async def auto_replies_menu_btn(event):
    admin_chat_state.pop(event.sender_id, None)
    btns = [
        [Button.inline("➕ Add Auto-Reply", data=b"adm_ar_add")],
        [
            Button.inline(f"📋 View All ({len(auto_replies)})", data=b"adm_ar_list"),
            Button.inline("🗑 Delete Auto-Reply", data=b"adm_ar_del")
        ],
        [Button.inline("❌ Close", data=b"adm_cancel")]
    ]
    await event.reply(
        "🤖 **SMART AUTO-RESPONDER**\n"
        "➖➖➖➖➖➖➖➖➖➖➖➖\n"
        f"🔑 **Active Keywords:** `{len(auto_replies)}`\n\n"
        "👇 *Manage automatic support replies below:*",
        buttons=btns
    )

# --- 9. INTERACTIVE ADMIN & SECURITY MENU ---
@client.on(events.NewMessage(pattern=r"^👑 Manage Admins$"))
@admin_only
async def manage_admins_menu_btn(event):
    admin_chat_state.pop(event.sender_id, None)
    btns = [
        [
            Button.inline("➕ Add Admin", data=b"adm_sec_addadmin"),
            Button.inline("➖ Remove Admin", data=b"adm_sec_deladmin")
        ],
        [Button.inline(f"👑 List All Admins ({len(ADMIN_IDS)})", data=b"adm_sec_listadmins")],
        [
            Button.inline("🚫 Ban User", data=b"adm_sec_ban"),
            Button.inline(f"✅ Unban User ({len(banned_users)})", data=b"adm_sec_unban")
        ],
        [Button.inline("❌ Close", data=b"adm_cancel")]
    ]
    await event.reply(
        "👑 **ADMIN & SECURITY CENTER**\n"
        "➖➖➖➖➖➖➖➖➖➖➖➖\n"
        f"👑 **Admins:** `{len(ADMIN_IDS)}` | 🚫 **Banned Users:** `{len(banned_users)}`\n\n"
        "👇 *Select an operation below:*",
        buttons=btns
    )

# --- 10. INTERACTIVE RESTORE MENU ---
@client.on(events.NewMessage(pattern=r"^🔄 Restore$"))
@admin_only
async def restore_menu_btn(event):
    admin_chat_state.pop(event.sender_id, None)
    has_auto = os.path.exists("auto_backup.json")
    btns = [
        [Button.inline("📤 Upload Backup (.json) File", data=b"adm_res_upload")],
    ]
    if has_auto:
        btns.insert(0, [Button.inline("⚡ Instant Restore (auto_backup.json)", data=b"adm_res_auto")])
    btns.append([Button.inline("❌ Close", data=b"adm_cancel")])
    
    await event.reply(
        "🔄 **DATABASE RESTORE CENTER**\n"
        "➖➖➖➖➖➖➖➖➖➖➖➖\n"
        "👇 *Choose how you want to restore your data:*",
        buttons=btns
    )

# =====================================================================
# 🎯 INLINE CALLBACK HANDLERS FOR ADMIN SUB-MENUS
# =====================================================================
@client.on(events.CallbackQuery(pattern=b"^adm_"))
async def admin_callback_router(event):
    global banner_config, force_channel, animations_enabled, broadcast_cancel_flag
    if event.sender_id not in ADMIN_IDS:
        return await event.answer("❌ Access Denied!", alert=True)
        
    admin_id = event.sender_id
    data = event.data.decode("utf-8")

    if data == "adm_bc_stop":
        broadcast_cancel_flag = True
        await event.answer("🛑 Stopping broadcast...", alert=True)
        return

    if data == "adm_cancel":
        admin_chat_state.pop(admin_id, None)
        await event.answer("Operation Cancelled")
        try:
            await event.edit("🚫 **Operation Cancelled / Closed.**")
        except Exception:
            pass
        return

    if data == "adm_mb_toggle_anim":
        animations_enabled = not animations_enabled
        set_setting("animations_enabled", "1" if animations_enabled else "0")
        anim_stat = "🟢 ON" if animations_enabled else "🔴 OFF"
        await event.answer(f"🎬 Animations: {anim_stat}", alert=True)
        await event.edit(f"🎬 **Cyberpunk Animations are now `{anim_stat}`!**")
        return

    if data.startswith("adm_quickdm_"):
        target_uid = int(data.split("_")[-1])
        await event.answer()
        prompt = await event.respond(
            f"✅ **Target UID Locked:** `{target_uid}`\n"
            f"➖➖➖➖➖➖➖➖➖➖➖➖\n"
            f"✍️ **Ab apna Message, Photo ya Video bhejo:**",
            buttons=CANCEL_BTN
        )
        admin_chat_state[admin_id] = {"step": "waiting_for_msg", "target_id": target_uid, "prompt_id": prompt.id}
        return

    if data.startswith("adm_quickban_"):
        target_uid = int(data.split("_")[-1])
        if target_uid in ADMIN_IDS:
            return await event.answer("❌ Cannot ban an Admin!", alert=True)
        banned_users.add(target_uid)
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute("INSERT OR IGNORE INTO banned (user_id) VALUES (?)", (target_uid,))
        await event.answer(f"🚫 User {target_uid} Banned!", alert=True)
        await event.respond(f"🚫 **User `{target_uid}` has been BANNED.**")
        return

    if data == "adm_wipemsg":
        key = (event.chat_id, event.message_id)
        if key in admin_msg_map:
            target_msgs = admin_msg_map[key]
            for adm_id_item, msg_id_item in target_msgs:
                try:
                    await client.delete_messages(adm_id_item, [msg_id_item])
                except Exception:
                    pass
                admin_msg_map.pop((adm_id_item, msg_id_item), None)
            await event.answer("✅ Wiped from all Admin panels!")
        else:
            try:
                await event.delete()
            except Exception:
                pass
            await event.answer("🗑 Message deleted!")
        return

    if data in ("adm_bc_normal", "adm_bc_pin"):
        should_pin = (data == "adm_bc_pin")
        mode_str = "📌 Broadcast + Auto-Pin" if should_pin else "📨 Normal Broadcast"
        await event.answer()
        await event.edit(
            f"📡 **{mode_str} Ready!**\n"
            f"➖➖➖➖➖➖➖➖➖➖➖➖\n"
            f"👉 **Ab wo Message, Photo, Video ya Post bhejo (ya forward karo) jo sabhi `{len(tracked_users)}` users ko bhejna hai:**",
            buttons=CANCEL_BTN
        )
        admin_chat_state[admin_id] = {"step": "broadcast_send", "pin": should_pin}
        return

    if data.startswith("adm_sbtn_"):
        parts = data.split("_")
        mode = parts[2]
        btn_key = parts[3]
        await event.answer()
        action_label = "Replace All Files" if mode == "rep" else "Add Extra File"
        await event.edit(
            f"🔘 **Configure `{btn_key.upper()}` ({action_label})**\n"
            f"➖➖➖➖➖➖➖➖➖➖➖➖\n"
            f"👉 **Ab wo Message, File, Photo ya Video bhejo jo `{btn_key}` button dabane par user ko milegi:**",
            buttons=CANCEL_BTN
        )
        admin_chat_state[admin_id] = {"step": "set_btn_file", "mode": mode, "btn_key": btn_key}
        return

    if data.startswith("adm_cbtn_"):
        btn_key = data.split("_")[-1]
        button_forwards.pop(btn_key, None)
        delete_button_config(btn_key)
        await event.answer(f"🗑 {btn_key.upper()} files cleared!", alert=True)
        await event.edit(f"🗑 **All files for `{btn_key.upper()}` button have been wiped!**")
        return

    if data.startswith("adm_seq_set_"):
        step_num = int(data.split("_")[-1])
        await event.answer()
        await event.edit(
            f"🔢 **Configure Welcome Step {step_num}**\n"
            f"➖➖➖➖➖➖➖➖➖➖➖➖\n"
            f"👉 **Ab Step {step_num} ke liye Text, Photo, Video ya Forwarded Message bhejo:**",
            buttons=CANCEL_BTN
        )
        admin_chat_state[admin_id] = {"step": "set_seq_step", "seq_step": step_num}
        return

    if data.startswith("adm_seq_del_"):
        target = data.split("_")[-1]
        if target == "all":
            saved_messages.clear()
            with sqlite3.connect(str(DB_PATH)) as conn:
                conn.execute("DELETE FROM messages")
            await event.answer("🗑 All sequence steps cleared!", alert=True)
            await event.edit("🗑 **All Welcome Sequence steps have been wiped!**")
        else:
            step_num = int(target)
            saved_messages.pop(step_num, None)
            with sqlite3.connect(str(DB_PATH)) as conn:
                conn.execute("DELETE FROM messages WHERE step=?", (step_num,))
            await event.answer(f"🗑 Step {step_num} deleted!", alert=True)
            await event.edit(f"🗑 **Welcome Sequence Step {step_num} removed!**")
        return

    if data == "adm_mb_text":
        await event.answer()
        await event.edit(
            "✏️ **EDIT USER MENU PROMPT TEXT**\n"
            "➖➖➖➖➖➖➖➖➖➖➖➖\n"
            f"📌 **Current Text:**\n{user_menu_text}\n"
            "➖➖➖➖➖➖➖➖➖➖➖➖\n"
            "👉 **Ab naya Menu Text bhejo (default rakhne ke liye `👇` bhejo):**",
            buttons=CANCEL_BTN
        )
        admin_chat_state[admin_id] = {"step": "set_menu_text"}
        return

    if data == "adm_mb_setbanner":
        await event.answer()
        await event.edit(
            "🖼️ **SET VIP MENU BANNER**\n"
            "➖➖➖➖➖➖➖➖➖➖➖➖\n"
            "👉 **Ab koi bhi Photo, Video ya GIF bhejo jo `/start` keyboard menu ke sath jayegi:**",
            buttons=CANCEL_BTN
        )
        admin_chat_state[admin_id] = {"step": "set_banner_media"}
        return

    if data == "adm_mb_delbanner":
        banner_config = {"msg_id": 0, "chat_id": 0}
        set_setting("banner_config", json.dumps(banner_config))
        await event.answer("🗑 Banner Removed!", alert=True)
        await event.edit("🗑 **Menu Banner Removed Successfully!**")
        return

    if data == "adm_mb_support":
        await event.answer()
        await event.edit(
            "🎧 **CHANGE SUPPORT CONTACT**\n"
            "➖➖➖➖➖➖➖➖➖➖➖➖\n"
            f"📌 **Current ID:** `{support_username}`\n\n"
            "👉 **Ab naya `@username` type karke bhejo:**",
            buttons=CANCEL_BTN
        )
        admin_chat_state[admin_id] = {"step": "set_support_id"}
        return

    if data == "adm_mb_preview":
        await event.answer("Sending user keyboard preview...")
        await send_user_menu(admin_id)
        await event.respond("💡 *Tip: Apna Admin Keyboard wapas laane ke liye `panel` ya `/start` bhejein.*")
        return

    if data == "adm_fj_set":
        await event.answer()
        await event.edit(
            "🔒 **SET FORCE JOIN CHANNEL**\n"
            "➖➖➖➖➖➖➖➖➖➖➖➖\n"
            "👉 **Apne channel ka `@username` ya link (`https://t.me/...`) bhejo:**\n\n"
            "⚠️ *Yaad se pehle is bot ko us channel mein Admin bana dena!*",
            buttons=CANCEL_BTN
        )
        admin_chat_state[admin_id] = {"step": "set_force_ch"}
        return

    if data == "adm_fj_clear":
        force_channel = ""
        set_setting("force_channel", "")
        await event.answer("🔓 Force Join Disabled!", alert=True)
        await event.edit("🔓 **Force Join Disabled.** Sabhi users ab seedha files access kar sakte hain.")
        return

    if data == "adm_ar_add":
        await event.answer()
        await event.edit(
            "🤖 **ADD AUTO-REPLY (Step 1/2)**\n"
            "➖➖➖➖➖➖➖➖➖➖➖➖\n"
            "👉 **Sabse pehle Keyword bhejo** *(jaise `price`, `help`, `hack`)*:",
            buttons=CANCEL_BTN
        )
        admin_chat_state[admin_id] = {"step": "ar_step_keyword"}
        return

    if data == "adm_ar_list":
        await event.answer()
        if not auto_replies:
            return await event.edit("📭 **Abhi koi Auto-Replies set nahi hain.**")
        msg = "🤖 **ACTIVE AUTO-REPLIES:**\n➖➖➖➖➖➖➖➖➖➖➖➖\n"
        for kw, resp in auto_replies.items():
            short_resp = resp[:30] + "..." if len(resp) > 30 else resp
            msg += f"🔹 `{kw}` ➡ {short_resp}\n"
        await event.edit(msg)
        return

    if data == "adm_ar_del":
        await event.answer()
        if not auto_replies:
            return await event.edit("📭 **Delete karne ke liye koi Auto-Reply nahi hai.**")
        kw_btns = []
        for kw in list(auto_replies.keys())[:15]:
            kw_btns.append([Button.inline(f"🗑 Delete '{kw}'", data=f"adm_ardelkw_{kw}".encode())])
        kw_btns.append([Button.inline("❌ Cancel", data=b"adm_cancel")])
        await event.edit("🗑 **Tap any keyword below to delete it immediately:**", buttons=kw_btns)
        return

    if data.startswith("adm_ardelkw_"):
        kw = data.replace("adm_ardelkw_", "", 1)
        auto_replies.pop(kw, None)
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute("DELETE FROM auto_replies WHERE keyword=?", (kw,))
        await event.answer(f"🗑 Deleted: {kw}", alert=True)
        await event.edit(f"🗑 **Auto-Reply for `{kw}` deleted successfully!**")
        return

    if data == "adm_sec_addadmin":
        await event.answer()
        await event.edit(
            "👑 **ADD NEW ADMIN**\n"
            "➖➖➖➖➖➖➖➖➖➖➖➖\n"
            "👉 **Naye Admin ki Numeric Telegram UID bhejo:**",
            buttons=CANCEL_BTN
        )
        admin_chat_state[admin_id] = {"step": "sec_add_admin"}
        return

    if data == "adm_sec_deladmin":
        await event.answer()
        sub_admins = [a for a in ADMIN_IDS if a not in ROOT_ADMINS]
        if not sub_admins:
            return await event.edit("⚠️ **Koi Sub-Admin nahi hai (Root Admins ko remove nahi kiya ja sakta).**")
        adm_btns = [[Button.inline(f"🗑 Remove Admin {aid}", data=f"adm_secdelid_{aid}".encode())] for aid in sub_admins]
        adm_btns.append([Button.inline("❌ Cancel", data=b"adm_cancel")])
        await event.edit("👑 **Tap on an Admin ID below to remove their privileges:**", buttons=adm_btns)
        return

    if data.startswith("adm_secdelid_"):
        target_aid = int(data.split("_")[-1])
        if target_aid in ROOT_ADMINS:
            return await event.answer("❌ Cannot remove Root Admin!", alert=True)
        if target_aid in ADMIN_IDS:
            ADMIN_IDS.remove(target_aid)
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute("DELETE FROM admins WHERE user_id=?", (target_aid,))
        await event.answer("🗑 Admin Removed!", alert=True)
        await event.edit(f"🗑 **Admin `{target_aid}` removed successfully!**")
        return

    if data == "adm_sec_listadmins":
        await event.answer()
        msg = "👑 **CURRENT ADMINS:**\n➖➖➖➖➖➖➖➖➖➖➖➖\n"
        for aid in ADMIN_IDS:
            role = "⭐ (Root Admin)" if aid in ROOT_ADMINS else "🔹 (Sub Admin)"
            msg += f"{role} `{aid}`\n"
        await event.edit(msg)
        return

    if data == "adm_sec_ban":
        await event.answer()
        await event.edit(
            "🚫 **BAN A SPAMMER**\n"
            "➖➖➖➖➖➖➖➖➖➖➖➖\n"
            "👉 **Jis user ko Ban karna hai uski UID bhejo:**",
            buttons=CANCEL_BTN
        )
        admin_chat_state[admin_id] = {"step": "sec_ban_user"}
        return

    if data == "adm_sec_unban":
        await event.answer()
        if not banned_users:
            return await event.edit("✅ **Abhi koi bhi user Banned nahi hai.**")
        ub_btns = [[Button.inline(f"✅ Unban {uid}", data=f"adm_secunbanid_{uid}".encode())] for uid in list(banned_users)[:15]]
        ub_btns.append([Button.inline("❌ Cancel", data=b"adm_cancel")])
        await event.edit("🔓 **Tap a User ID below to Unban immediately (or send UID):**", buttons=ub_btns)
        admin_chat_state[admin_id] = {"step": "sec_unban_user"}
        return

    if data.startswith("adm_secunbanid_"):
        target_uid = int(data.split("_")[-1])
        banned_users.discard(target_uid)
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute("DELETE FROM banned WHERE user_id=?", (target_uid,))
        admin_chat_state.pop(admin_id, None)
        await event.answer(f"✅ Unbanned {target_uid}!", alert=True)
        await event.edit(f"✅ **User `{target_uid}` has been UNBANNED!**")
        return

    if data == "adm_res_auto":
        await event.answer("Restoring from auto_backup.json...")
        try:
            with open("auto_backup.json", "r") as f:
                bdata = json.load(f)
            restore_from_dict(bdata)
            await event.edit(f"✅ **Auto-Backup Restored Successfully!**\n👥 Loaded Users: `{len(tracked_users)}`")
        except Exception as e:
            await event.edit(f"❌ **Auto-Restore Failed:** {e}")
        return

    if data == "adm_res_upload":
        await event.answer()
        await event.edit(
            "📤 **UPLOAD BACKUP FILE**\n"
            "➖➖➖➖➖➖➖➖➖➖➖➖\n"
            "👉 **Ab apni `.json` backup file yahan chat mein bhejo:**",
            buttons=CANCEL_BTN
        )
        admin_chat_state[admin_id] = {"step": "restore_upload_json"}
        return

# =====================================================================
# 🚀 BROADCAST ENGINE 2.0 (EVERY 3 USERS UPDATE + ETA + STOP BUTTON)
# =====================================================================
async def execute_broadcast(event, should_pin: bool, manual_text: str = None, reply_msg_obj = None):
    global broadcast_cancel_flag
    if not tracked_users:
        return await event.reply("❌ Koi active user nahi hai.")
        
    broadcast_cancel_flag = False
    total_targets = len(tracked_users)
    stop_btn = [[Button.inline("🛑 Stop Broadcast", data=b"adm_bc_stop")]]
    
    launch_frames = [
        ("⚡ `[ENGINE] Locking target database & warming up threads...`", 0.3),
        (f"🚀 `[LAUNCH] Firing broadcast to {total_targets} peers...`", 0.3)
    ]
    status_msg = await play_animation(event, launch_frames)
    
    success = fail = skip = 0
    processed = 0
    bc_start_time = time.time()
    spin_idx = 0

    for uid, old_hash in list(tracked_users.items()):
        if broadcast_cancel_flag:
            break
            
        processed += 1
        if uid in blocked_users:
            skip += 1
        else:
            try:
                peer = await client.get_input_entity(uid)
                if reply_msg_obj:
                    sent = await client.forward_messages(peer, reply_msg_obj.id, reply_msg_obj.chat_id)
                elif manual_text is not None:
                    sent = await client.send_message(peer, manual_text)
                elif event.fwd_from:
                    sent = await client.forward_messages(peer, event.id, event.chat_id)
                else:
                    sent = await client.send_message(peer, event.text or "", file=event.media)
                    
                if should_pin and sent:
                    try:
                        msg_to_pin = sent[0].id if isinstance(sent, list) else sent.id
                        await client.pin_message(peer, msg_to_pin, notify=True)
                    except Exception:
                        pass
                        
                success += 1
                await asyncio.sleep(0.25)
            except FloodWaitError as e:
                await asyncio.sleep(e.seconds)
            except UserIsBlockedError:
                blocked_users.add(uid)
                save_user(uid, old_hash, blocked=True)
                fail += 1
            except Exception:
                fail += 1

        # 🔥 UPDATE EVERY 3 USERS (OR FINAL USER)
        if processed % 3 == 0 or processed == total_targets:
            pct = int((processed / total_targets) * 100)
            filled = pct // 10
            bar = "█" * filled + "░" * (10 - filled)
            spinner = SPINNER_FRAMES[spin_idx % len(SPINNER_FRAMES)]
            spin_idx += 1
            
            elapsed = max(time.time() - bc_start_time, 0.1)
            speed = processed / elapsed
            remaining_users = total_targets - processed
            eta_str = format_eta(remaining_users / speed) if speed > 0 else "..."
            
            try:
                await status_msg.edit(
                    f"{spinner} **LIVE VIP BROADCASTING...** `[{bar}] {pct}%`\n"
                    f"➖➖➖➖➖➖➖➖➖➖➖➖\n"
                    f"🔄 **Processed:** `{processed} / {total_targets}`\n"
                    f"✅ **Delivered:** `{success}` | ❌ **Failed:** `{fail}` | ⏭ **Skipped:** `{skip}`\n"
                    f"⚡ **Speed:** `{speed:.1f} users/s` | ⏳ **ETA:** `{eta_str}`",
                    buttons=stop_btn
                )
            except Exception:
                pass

    total_time = format_eta(time.time() - bc_start_time)
    header_title = "🛑 **BROADCAST ABORTED BY ADMIN**" if broadcast_cancel_flag else "✅ **VIP BROADCAST COMPLETED!**"
    await status_msg.edit(
        f"{header_title}\n"
        f"➖➖➖➖➖➖➖➖➖➖➖➖\n"
        f"👥 **Processed:** `{processed} / {total_targets}`\n"
        f"✅ **Delivered:** `{success}`\n"
        f"❌ **Failed/Blocked:** `{fail}`\n"
        f"⏭ **Skipped:** `{skip}`\n"
        f"⏱ **Total Time:** `{total_time}`\n"
        f"📌 **Auto-Pinned:** `{'Yes' if should_pin else 'No'}`",
        buttons=None
    )

# =====================================================================
# 🧠 MASTER INTERACTIVE STATE PROCESSOR (HANDLES ALL ADMIN INPUTS)
# =====================================================================
@client.on(events.NewMessage(func=lambda e: e.is_private and e.sender_id in ADMIN_IDS and e.sender_id in admin_chat_state))
async def handle_admin_chat_state(event):
    global user_menu_text, banner_config, support_username, force_channel
    admin_id = event.sender_id
    raw_txt = (event.raw_text or "").strip()

    if raw_txt.lower() in ADMIN_KB_LABELS or raw_txt.startswith("/"):
        return

    state = admin_chat_state[admin_id]
    step = state.get("step")

    # 0. Inspect User ID
    if step == "inspect_user_id":
        if not raw_txt.lstrip('-').isdigit():
            return await event.reply("❌ **Invalid UID!** Sirf numeric ID bhejo:", buttons=CANCEL_BTN)
        target_uid = int(raw_txt)
        admin_chat_state.pop(admin_id, None)
        
        scan_msg = await play_animation(event, [("🔍 `[DOSSIER] Fetching user profile & database records...`", 0.35)])
        in_db = target_uid in tracked_users
        is_blk = target_uid in blocked_users
        is_ban = target_uid in banned_users
        j_date = user_join_dates.get(target_uid, "Unknown")
        
        u_name = "Unknown"
        u_handle = "None"
        try:
            ent = await client.get_entity(target_uid)
            u_name = f"{getattr(ent, 'first_name', '') or ''} {getattr(ent, 'last_name', '') or ''}".strip() or "User"
            if getattr(ent, 'username', None):
                u_handle = f"@{ent.username}"
        except Exception:
            pass
            
        status_badge = "🚫 BANNED" if is_ban else ("🔴 BLOCKED BOT" if is_blk else ("🟢 ACTIVE VIP" if in_db else "⚪ NOT IN DB"))
        dossier_btns = [
            [Button.inline("✉️ Send Direct Message", data=f"adm_quickdm_{target_uid}".encode())],
            [
                Button.inline("✅ Unban User" if is_ban else "🚫 Ban User", data=f"adm_secunbanid_{target_uid}".encode() if is_ban else f"adm_quickban_{target_uid}".encode())
            ]
        ]
        await scan_msg.edit(
            f"🕵️‍♂️ **VIP USER DOSSIER**\n"
            f"➖➖➖➖➖➖➖➖➖➖➖➖\n"
            f"👤 **Name:** `{u_name}`\n"
            f"🔗 **Username:** `{u_handle}`\n"
            f"🆔 **UID:** `{target_uid}`\n"
            f"📡 **Status:** `{status_badge}`\n"
            f"📅 **Joined Date:** `{j_date}`\n"
            f"➖➖➖➖➖➖➖➖➖➖➖➖",
            buttons=dossier_btns
        )

    # 1. Direct Message: Waiting for UID
    elif step == "waiting_for_id":
        if raw_txt.lstrip('-').isdigit():
            target_id = int(raw_txt)
            state["target_id"] = target_id
            state["step"] = "waiting_for_msg"
            await event.respond(
                f"✅ **Target UID Locked:** `{target_id}`\n"
                f"➖➖➖➖➖➖➖➖➖➖➖➖\n"
                f"✍️ **Ab apna Message, Photo ya Video bhejo:**",
                buttons=CANCEL_BTN
            )
        else:
            await event.reply("❌ **Invalid UID!** Sirf numeric ID bhejo:", buttons=CANCEL_BTN)

    # 2. Direct Message: Waiting for Message Content
    elif step == "waiting_for_msg":
        target_id = state["target_id"]
        admin_chat_state.pop(admin_id, None)
        try:
            access_hash = tracked_users.get(target_id, 0)
            peer = InputPeerUser(target_id, access_hash) if access_hash else target_id
            if event.text:
                await client.send_message(peer, event.text, file=event.media)
            elif event.media:
                await client.send_message(peer, file=event.media)
            user_chat_state[target_id] = True
            await event.respond(f"✅ **VIP Message successfully delivered to `{target_id}`!**")
        except UserIsBlockedError:
            blocked_users.add(target_id)
            save_user(target_id, tracked_users.get(target_id, 0), blocked=True)
            await event.respond("❌ **Delivery Failed:** User blocked the bot.")
        except Exception as e:
            await event.respond(f"❌ **Delivery Failed:** Invalid ID or user hasn't started the bot (`{e}`).")

    # 3. Broadcast Execution
    elif step == "broadcast_send":
        should_pin = state.get("pin", False)
        admin_chat_state.pop(admin_id, None)
        await execute_broadcast(event, should_pin)

    # 4. Set / Add Module Button File
    elif step == "set_btn_file":
        mode = state["mode"]
        btn_key = state["btn_key"]
        admin_chat_state.pop(admin_id, None)
        if mode == "rep":
            delete_button_config(btn_key)
            button_forwards[btn_key] = []
        if btn_key not in button_forwards:
            button_forwards[btn_key] = []
        add_button_config(btn_key, event.id, event.chat_id)
        button_forwards[btn_key].append({"msg_id": event.id, "from_chat": event.chat_id})
        total_f = len(button_forwards[btn_key])
        more_btn = [[Button.inline(f"➕ Add Another File to {btn_key.upper()}", data=f"adm_sbtn_add_{btn_key}".encode())]]
        await event.reply(
            f"✅ **File Saved to `{btn_key.upper()}` Button!**\n"
            f"📁 **Total Active Files:** `{total_f}`",
            buttons=more_btn
        )

    # 5. Set Welcome Sequence Step
    elif step == "set_seq_step":
        seq_step = state["seq_step"]
        admin_chat_state.pop(admin_id, None)
        if event.media or event.fwd_from:
            data = {"type": "forward", "msg_id": event.id, "from_chat": event.chat_id}
        else:
            data = {"type": "text", "text": raw_txt}
        saved_messages[seq_step] = data
        save_message_step(seq_step, data)
        await event.reply(f"✅ **Welcome Sequence Step {seq_step} Saved Successfully!**")

    # 6. Edit User Menu Text
    elif step == "set_menu_text":
        if not raw_txt:
            return await event.reply("❌ Text khali nahi ho sakta! Phir se bhejo:", buttons=CANCEL_BTN)
        admin_chat_state.pop(admin_id, None)
        user_menu_text = raw_txt
        set_setting("user_menu_text", user_menu_text)
        await event.reply(f"✅ **User Menu Prompt Text Updated!**\n\nPreview:\n{user_menu_text}")

    # 7. Set Menu Banner Media
    elif step == "set_banner_media":
        if not event.media:
            return await event.reply("❌ **Error:** Kripya koi Photo, Video ya GIF bhejo!", buttons=CANCEL_BTN)
        admin_chat_state.pop(admin_id, None)
        banner_config = {"msg_id": event.id, "chat_id": event.chat_id}
        set_setting("banner_config", json.dumps(banner_config))
        await event.reply("🖼️ **VIP Menu Banner Updated Successfully!**")

    # 8. Set Support Username
    elif step == "set_support_id":
        if not raw_txt:
            return await event.reply("❌ Valid `@username` bhejo:", buttons=CANCEL_BTN)
        admin_chat_state.pop(admin_id, None)
        support_username = raw_txt if raw_txt.startswith("@") else f"@{raw_txt}"
        set_setting("support_username", support_username)
        await event.reply(f"✅ **Support Contact Updated:** `{support_username}`")

    # 9. Set Force Join Channel
    elif step == "set_force_ch":
        if not raw_txt:
            return await event.reply("❌ Channel `@username` bhejo:", buttons=CANCEL_BTN)
        admin_chat_state.pop(admin_id, None)
        ch = raw_txt
        if "t.me/" in ch:
            ch = ch.split("t.me/")[-1].split("/")[0]
        force_channel = ch.lstrip("@")
        set_setting("force_channel", force_channel)
        await event.reply(f"🔒 **Force Join Enabled for:** `@{force_channel}`")

    # 10. Auto-Reply Step 1 (Keyword)
    elif step == "ar_step_keyword":
        if not raw_txt:
            return await event.reply("❌ Keyword type karke bhejo:", buttons=CANCEL_BTN)
        kw = raw_txt.lower()
        state["keyword"] = kw
        state["step"] = "ar_step_response"
        await event.reply(
            f"🔑 **Keyword Locked:** `{kw}`\n"
            f"➖➖➖➖➖➖➖➖➖➖➖➖\n"
            f"👉 **Ab Step 2/2: Is keyword ka Auto-Reply message bhejo:**",
            buttons=CANCEL_BTN
        )

    # 11. Auto-Reply Step 2 (Response)
    elif step == "ar_step_response":
        if not raw_txt:
            return await event.reply("❌ Reply text khali nahi ho sakta!", buttons=CANCEL_BTN)
        kw = state["keyword"]
        admin_chat_state.pop(admin_id, None)
        auto_replies[kw] = raw_txt
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute("INSERT OR REPLACE INTO auto_replies (keyword, response) VALUES (?, ?)", (kw, raw_txt))
        await event.reply(f"✅ **Auto-Reply Saved!**\n🔑 `{kw}` ➡ {raw_txt}")

    # 12. Security: Add Admin
    elif step == "sec_add_admin":
        if not raw_txt.isdigit():
            return await event.reply("❌ Sirf numeric UID bhejo:", buttons=CANCEL_BTN)
        new_aid = int(raw_txt)
        admin_chat_state.pop(admin_id, None)
        if new_aid not in ADMIN_IDS:
            ADMIN_IDS.append(new_aid)
            with sqlite3.connect(str(DB_PATH)) as conn:
                conn.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (new_aid,))
        await event.reply(f"✅ **New Admin Added:** `{new_aid}`")

    # 13. Security: Ban User
    elif step == "sec_ban_user":
        if not raw_txt.isdigit():
            return await event.reply("❌ Sirf numeric UID bhejo:", buttons=CANCEL_BTN)
        b_uid = int(raw_txt)
        admin_chat_state.pop(admin_id, None)
        if b_uid in ADMIN_IDS:
            return await event.reply("❌ Admin ko ban nahi kar sakte!")
        banned_users.add(b_uid)
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute("INSERT OR IGNORE INTO banned (user_id) VALUES (?)", (b_uid,))
        await event.reply(f"🚫 **User `{b_uid}` has been BANNED!**")

    # 14. Security: Unban User
    elif step == "sec_unban_user":
        if not raw_txt.isdigit():
            return await event.reply("❌ Sirf numeric UID bhejo:", buttons=CANCEL_BTN)
        ub_uid = int(raw_txt)
        admin_chat_state.pop(admin_id, None)
        banned_users.discard(ub_uid)
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute("DELETE FROM banned WHERE user_id=?", (ub_uid,))
        await event.reply(f"✅ **User `{ub_uid}` has been UNBANNED!**")

    # 15. Restore via JSON Upload
    elif step == "restore_upload_json":
        if not event.file or not event.file.name.endswith(".json"):
            return await event.reply("❌ Kripya valid `.json` backup file bhejo:", buttons=CANCEL_BTN)
        admin_chat_state.pop(admin_id, None)
        path = await client.download_media(event.media)
        try:
            with open(path, "r") as f:
                bdata = json.load(f)
            extra_adm = restore_from_dict(bdata)
            await event.reply(f"✅ **Restore Successful!** Loaded `{len(tracked_users)}` users and `{extra_adm}` extra admins.")
        except Exception as e:
            await event.reply(f"❌ **Restore Failed:** {e}")
        finally:
            if os.path.exists(path):
                os.remove(path)

    raise events.StopPropagation

# =====================================================================
# 🛠 LEGACY SLASH COMMANDS (BACKWARD COMPATIBILITY)
# =====================================================================
@client.on(events.NewMessage(pattern=r"^/cancel$"))
@admin_only
async def cancel_state_cmd(event):
    admin_chat_state.pop(event.sender_id, None)
    await event.reply("🚫 `[Notice] Operation cancelled successfully.`")

@client.on(events.NewMessage(pattern=r"^/del$"))
@admin_only
async def global_delete_cmd(event):
    if not event.is_reply:
        return await event.reply("❌ `[Error] Please reply to the user's message with /del to wipe it.`")
    replied = await event.get_reply_message()
    key = (event.chat_id, replied.id)
    if key in admin_msg_map:
        target_msgs = admin_msg_map[key]
        for adm_id, msg_id in target_msgs:
            try: await client.delete_messages(adm_id, [msg_id])
            except Exception: pass
            admin_msg_map.pop((adm_id, msg_id), None)
        del_confirm = await event.respond("✅ `[sys] Message wiped globally from all Admin panels.`")
        try: await event.delete()
        except: pass
        await asyncio.sleep(3)
        try: await del_confirm.delete()
        except: pass
    else:
        warn = await event.reply("⚠️ `[Notice] Cannot wipe. Message not found in active global registry.`")
        await asyncio.sleep(3)
        try: await warn.delete()
        except: pass

@client.on(events.NewMessage(pattern=r"^/send\s+(\d+)(?:\s+(.+))?"))
@admin_only
async def send_dm_manual(event):
    target_uid = int(event.pattern_match.group(1))
    text_msg = event.pattern_match.group(2)
    try:
        peer = InputPeerUser(target_uid, tracked_users.get(target_uid, 0))
        if event.is_reply:
            reply_msg = await event.get_reply_message()
            await client.send_message(peer, text_msg or reply_msg.text or "", file=reply_msg.media)
        elif text_msg:
            await client.send_message(peer, text_msg.strip())
        user_chat_state[target_uid] = True
        await event.reply(f"✅ **Message successfully delivered to `{target_uid}`!**")
    except UserIsBlockedError:
        blocked_users.add(target_uid)
        save_user(target_uid, tracked_users.get(target_uid, 0), blocked=True)
        await event.reply("❌ `[Error] Delivery Failed: User blocked the bot.`")
    except Exception:
        await event.reply("❌ `[Error] Delivery Failed:` System issue ya invalid ID.")

@client.on(events.NewMessage(pattern=r"^/broadcast(_pin)?(?:\s+(.+))?"))
@admin_only
async def broadcast_slash(event):
    should_pin = bool(event.pattern_match.group(1))
    text = event.raw_text.replace("/broadcast_pin", "", 1).replace("/broadcast", "", 1).strip()
    if not text and not event.is_reply:
        return await event.reply("❌ Message text dein ya kisi message par reply karein.")
    reply_msg = await event.get_reply_message() if event.is_reply else None
    await execute_broadcast(event, should_pin, manual_text=text if not reply_msg else None, reply_msg_obj=reply_msg)

@client.on(events.NewMessage(pattern=r"^/setmsg(\d+)(?:\s+(.+))?"))
@admin_only
async def setmsg_slash(event):
    step = int(event.pattern_match.group(1))
    extra = event.pattern_match.group(2)
    if event.is_reply:
        reply_msg = await event.get_reply_message()
        data = {"type": "forward", "msg_id": reply_msg.id, "from_chat": event.chat_id}
    elif extra is not None:
        data = {"type": "text", "text": extra.strip()}
    else:
        if step in saved_messages:
            del saved_messages[step]
            with sqlite3.connect(str(DB_PATH)) as conn:
                conn.execute("DELETE FROM messages WHERE step=?", (step,))
            return await event.reply(f"🗑 Sequence step {step} hata diya gaya hai.")
        return await event.reply("❌ Text provide karein ya kisi message par reply karein.")
    saved_messages[step] = data
    save_message_step(step, data)
    await event.reply(f"✅ Sequence Step {step} save ho gaya hai.")

@client.on(events.NewMessage(pattern=r"^/setbutton\s+(hack|prediction)$"))
@admin_only
async def set_button_slash(event):
    btn_key = event.pattern_match.group(1).lower()
    if not event.is_reply:
        return await event.reply("❌ Please reply to the message you want to set for this button.")
    reply_msg = await event.get_reply_message()
    chat_id = get_chat_id(reply_msg, event)
    delete_button_config(btn_key)
    button_forwards[btn_key] = []
    add_button_config(btn_key, reply_msg.id, chat_id)
    button_forwards[btn_key].append({"msg_id": reply_msg.id, "from_chat": chat_id})
    await event.reply(f"✅ Button **'{btn_key}'** set ho gaya hai! (1 file)")

@client.on(events.NewMessage(pattern=r"^/addbutton\s+(hack|prediction)$"))
@admin_only
async def add_button_slash(event):
    btn_key = event.pattern_match.group(1).lower()
    if not event.is_reply:
        return await event.reply("❌ Please reply to the message.")
    if btn_key not in button_forwards:
        button_forwards[btn_key] = []
    reply_msg = await event.get_reply_message()
    chat_id = get_chat_id(reply_msg, event)
    add_button_config(btn_key, reply_msg.id, chat_id)
    button_forwards[btn_key].append({"msg_id": reply_msg.id, "from_chat": chat_id})
    await event.reply(f"✅ Additional file added! Total: `{len(button_forwards[btn_key])}`")

@client.on(events.NewMessage(pattern=r"^/clearbutton\s+(hack|prediction)$"))
@admin_only
async def clear_button_slash(event):
    btn_key = event.pattern_match.group(1).lower()
    if btn_key in button_forwards:
        del button_forwards[btn_key]
        delete_button_config(btn_key)
        await event.reply(f"🗑 Button **'{btn_key}'** clear kar diya gaya hai.")
    else:
        await event.reply("⚠️ Is button ki koi configuration nahi mili.")

@client.on(events.NewMessage(pattern=r"^/addadmin\s+(\d+)"))
@admin_only
async def add_admin_slash(event):
    new_admin_id = int(event.pattern_match.group(1))
    if new_admin_id in ADMIN_IDS:
        return await event.reply(f"⚠️ User `{new_admin_id}` pehle se hi admin hai.")
    ADMIN_IDS.append(new_admin_id)
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (new_admin_id,))
    await event.reply(f"✅ Naya admin add ho gaya hai: `{new_admin_id}`")

@client.on(events.NewMessage(pattern=r"^/deladmin\s+(\d+)"))
@admin_only
async def del_admin_slash(event):
    target_admin_id = int(event.pattern_match.group(1))
    if target_admin_id in ROOT_ADMINS:
        return await event.reply("❌ Root Admin ko remove nahi kiya ja sakta.")
    if target_admin_id in ADMIN_IDS:
        ADMIN_IDS.remove(target_admin_id)
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("DELETE FROM admins WHERE user_id=?", (target_admin_id,))
    await event.reply(f"🗑 Admin privileges hata di gayi hain: `{target_admin_id}`")

@client.on(events.NewMessage(pattern=r"^/listadmins"))
@admin_only
async def list_admins_slash(event):
    msg = "👑 **CURRENT ADMINS:**\n➖➖➖➖➖➖➖➖➖➖➖➖\n"
    for aid in ADMIN_IDS:
        role = "⭐ (Root Admin)" if aid in ROOT_ADMINS else "🔹 (Sub Admin)"
        msg += f"{role} `{aid}`\n"
    await event.reply(msg)

@client.on(events.NewMessage(pattern=r"^/setreply\s+(.+?)\s*\|\s*(.+)"))
@admin_only
async def set_auto_reply_slash(event):
    keyword = event.pattern_match.group(1).strip().lower()
    response = event.pattern_match.group(2).strip()
    auto_replies[keyword] = response
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("INSERT OR REPLACE INTO auto_replies (keyword, response) VALUES (?, ?)", (keyword, response))
    await event.reply(f"✅ **Auto-Reply Saved:** `{keyword}`")

@client.on(events.NewMessage(pattern=r"^/delreply\s+(.+)"))
@admin_only
async def del_auto_reply_slash(event):
    keyword = event.pattern_match.group(1).strip().lower()
    if keyword in auto_replies:
        del auto_replies[keyword]
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute("DELETE FROM auto_replies WHERE keyword=?", (keyword,))
        await event.reply(f"🗑 Auto-reply deleted: `{keyword}`")
    else:
        await event.reply("⚠️ Keyword nahi mila.")

@client.on(events.NewMessage(pattern=r"^/listreplies$"))
@admin_only
async def list_auto_replies_slash(event):
    if not auto_replies:
        return await event.reply("📭 Abhi koi auto-replies set nahi hain.")
    msg = "🤖 **ACTIVE AUTO-REPLIES:**\n➖➖➖➖➖➖➖➖➖➖➖➖\n"
    for kw, resp in auto_replies.items():
        msg += f"🔹 `{kw}` ➡ {resp[:25]}\n"
    await event.reply(msg)

@client.on(events.NewMessage(pattern=r"^/restore"))
@admin_only
async def restore_slash(event):
    if not event.is_reply:
        return await event.reply("❌ Please reply to a `.json` backup file.")
    rep = await event.get_reply_message()
    if not rep.file or not rep.file.name.endswith(".json"):
        return await event.reply("❌ Invalid file format.")
    path = await client.download_media(rep.media)
    try:
        with open(path, "r") as f:
            data = json.load(f)
        extra_adm = restore_from_dict(data)
        await event.reply(f"✅ **Restore Successful:** Loaded `{len(tracked_users)}` users and `{extra_adm}` extra admins.")
    except Exception as e:
        await event.reply(f"❌ Restore failed: {e}")
    finally:
        if os.path.exists(path):
            os.remove(path)

def restore_from_dict(data: dict):
    global tracked_users, blocked_users, banned_users, user_join_dates, saved_messages, button_forwards
    global welcome_enabled, animations_enabled, user_menu_text, support_username, force_channel, banner_config, click_stats, auto_replies, ADMIN_IDS
    
    tracked_users = {int(k): int(v) for k, v in data.get("tracked", {}).items()}
    blocked_users = set(int(u) for u in data.get("blocked", []))
    banned_users = set(int(u) for u in data.get("banned", []))
    user_join_dates = {int(k): str(v) for k, v in data.get("join_dates", {}).items()}
    saved_messages = {int(k): v for k, v in data.get("messages", {}).items()}
    button_forwards = data.get("button_forwards", {})
    welcome_enabled = data.get("welcome", True)
    animations_enabled = data.get("animations_enabled", True)
    user_menu_text = data.get("user_menu_text", user_menu_text)
    support_username = data.get("support_username", support_username)
    force_channel = data.get("force_channel", "")
    banner_config = data.get("banner_config", {"msg_id": 0, "chat_id": 0})
    click_stats = data.get("click_stats", {"prediction": 0, "hack": 0, "support": 0})
    auto_replies = data.get("auto_replies", {})
    db_admins = data.get("db_admins", [])

    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("DELETE FROM users")
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM button_msgs")
        conn.execute("DELETE FROM auto_replies")
        conn.execute("DELETE FROM admins")
        conn.execute("DELETE FROM banned")
        
        for uid, hsh in tracked_users.items():
            jdate = user_join_dates.get(uid, datetime.now().strftime("%Y-%m-%d"))
            conn.execute("INSERT OR REPLACE INTO users (user_id, access_hash, blocked, joined_date) VALUES (?,?,?,?)", (uid, hsh, int(uid in blocked_users), jdate))
        for uid in banned_users:
            conn.execute("INSERT OR IGNORE INTO banned (user_id) VALUES (?)", (uid,))
        for step, d in saved_messages.items():
            conn.execute("INSERT OR REPLACE INTO messages (step, msg_type, text, msg_id, from_chat) VALUES (?,?,?,?,?)", (step, d.get("type"), d.get("text"), d.get("msg_id"), d.get("from_chat")))
        for key, cfg_list in button_forwards.items():
            if isinstance(cfg_list, dict):
                cfg_list = [cfg_list]
            for cfg in cfg_list:
                conn.execute("INSERT INTO button_msgs (btn_key, msg_id, from_chat) VALUES (?,?,?)", (key, cfg["msg_id"], cfg["from_chat"]))
        for kw, resp in auto_replies.items():
            conn.execute("INSERT OR REPLACE INTO auto_replies (keyword, response) VALUES (?,?)", (kw, resp))
        for admin_id in db_admins:
            conn.execute("INSERT INTO admins (user_id) VALUES (?)", (admin_id,))
            if admin_id not in ADMIN_IDS:
                ADMIN_IDS.append(admin_id)
            
    set_setting("welcome_enabled", "1" if welcome_enabled else "0")
    set_setting("animations_enabled", "1" if animations_enabled else "0")
    set_setting("user_menu_text", user_menu_text)
    set_setting("support_username", support_username)
    set_setting("force_channel", force_channel)
    set_setting("banner_config", json.dumps(banner_config))
    set_setting("click_stats", json.dumps(click_stats))
    return len(db_admins)

# ------------------ TWO-WAY CHAT & AUTO-REPLY ------------------
@client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
async def seamless_chat_handler(event):
    if event.sender_id in banned_users or event.sender_id in admin_chat_state:
        return

    text = event.raw_text.strip().lower() if event.raw_text else ""
    
    # Ignore admin commands & labels
    if text.startswith('/') or text in ADMIN_KB_LABELS:
        return
    # Ignore main user keyboard buttons
    if "number hack" in text or "colour trading" in text or "contact support" in text:
        return

    if event.sender_id not in ADMIN_IDS:
        # --- USER TO ADMIN ---
        if user_chat_state.get(event.sender_id):
            bot_auto_replied = False
            for kw, resp in auto_replies.items():
                if kw in text:
                    try:
                        await client.send_message(event.sender_id, resp)
                        bot_auto_replied = True
                        break 
                    except Exception:
                        pass

            user = await event.get_sender()
            name = user.first_name or "User"
            auto_tag = "🤖 *(AI Auto-Responded)*\n" if bot_auto_replied else ""
            
            caption = (
                f"💬 **SUPPORT INTERCEPT** 💬\n"
                f"➖➖➖➖➖➖➖➖➖➖\n"
                f"{auto_tag}"
                f"👤 **From:** [{name}](tg://user?id={event.sender_id})\n"
                f"🆔 `{event.sender_id}`\n"
            )
            
            if event.raw_text:
                caption += f"\n📝 **Message:**\n{event.raw_text}"
            caption += "\n\n👇 *(Reply to answer, or use buttons below)*"
            
            intercept_btns = [
                [
                    Button.inline("🗑 Wipe Message", data=b"adm_wipemsg"),
                    Button.inline("🚫 Ban User", data=f"adm_quickban_{event.sender_id}".encode())
                ]
            ]
            
            sent_admin_msgs = []
            for admin_id in ADMIN_IDS:
                try: 
                    sent_msg = await client.send_message(admin_id, caption, file=event.media, buttons=intercept_btns)
                    sent_admin_msgs.append((admin_id, sent_msg.id))
                except Exception: 
                    pass
            
            if sent_admin_msgs:
                for adm_id, msg_id in sent_admin_msgs:
                    admin_msg_map[(adm_id, msg_id)] = sent_admin_msgs
                    
            try:
                if not bot_auto_replied:
                    feedback_msg = await event.reply("⚡ *Transmitting to Admin...*")
                    await asyncio.sleep(0.4)
                    await feedback_msg.edit("✅ *Message delivered to Admin.*")
                    await asyncio.sleep(2.5)
                    await feedback_msg.delete()
            except Exception:
                pass
        else:
            try: await event.delete()
            except Exception: pass
            warning_msg = await event.respond("⚠️ **Direct messages are disabled.**\n👉 Please use the **'🎧 Contact Support'** button below to chat with admin.")
            await asyncio.sleep(5)
            try: await warning_msg.delete()
            except Exception: pass
    else:
        # --- ADMIN TO USER ---
        if event.is_reply:
            replied_msg = await event.get_reply_message()
            replied_text = replied_msg.raw_text or ""
            
            match = re.search(r"🆔 `(\d+)`", replied_text)
            if match:
                target_uid = int(match.group(1))
                try:
                    access_hash = tracked_users.get(target_uid, 0)
                    peer = InputPeerUser(target_uid, access_hash) if access_hash else target_uid 
                        
                    if event.raw_text: await client.send_message(peer, event.raw_text, file=event.media)
                    elif event.media: await client.send_message(peer, file=event.media)
                        
                    user_chat_state[target_uid] = True
                    admin_feedback = await event.reply("⚡ *Encrypting & sending...*")
                    await asyncio.sleep(0.3)
                    await admin_feedback.edit("✅ **Sent!**")
                    await asyncio.sleep(2)
                    try: await admin_feedback.delete()
                    except Exception: pass
                        
                except UserIsBlockedError:
                    blocked_users.add(target_uid)
                    save_user(target_uid, tracked_users.get(target_uid, 0), blocked=True)
                    await event.reply("❌ `[Error] Transmission failed: User blocked the bot.`")
                except Exception as e:
                    await event.reply(f"❌ **Error:** {e}")

# ------------------ AUTO BACKUP (EVERY 6 HOURS & SEND TO ADMINS) ------------------
async def periodic_backup():
    while True:
        await asyncio.sleep(6 * 3600)
        try:
            db_admins = [a for a in ADMIN_IDS if a not in ROOT_ADMINS]
            data = {
                "tracked": tracked_users,
                "blocked": list(blocked_users),
                "banned": list(banned_users),
                "join_dates": user_join_dates,
                "messages": {str(k): v for k, v in saved_messages.items()},
                "button_forwards": button_forwards,
                "welcome": welcome_enabled,
                "animations_enabled": animations_enabled,
                "user_menu_text": user_menu_text,
                "support_username": support_username,
                "force_channel": force_channel,
                "banner_config": banner_config,
                "click_stats": click_stats,
                "auto_replies": auto_replies,
                "db_admins": db_admins
            }
            file = "auto_backup.json"
            with open(file, "w") as f:
                json.dump(data, f)
            
            caption = f"📁 `[Automatic 6-Hour Database Backup]`\n📊 Active Users: `{len(tracked_users)}` | Admins: `{len(ADMIN_IDS)}`"
            for admin_id in ROOT_ADMINS:
                try:
                    await client.send_file(admin_id, file, caption=caption)
                except Exception as e:
                    logger.error(f"Failed to send periodic backup to admin {admin_id}: {e}")
                    
            logger.info("Automatic 6-hour backup generated and sent to admins.")
        except Exception as e:
            logger.error(f"Periodic backup error: {e}")

# ------------------ MAIN ------------------
async def main():
    await client.start(bot_token=BOT_TOKEN)
    init_db()
    load_from_db()
    await start_web()
    asyncio.create_task(periodic_backup())
    logger.info("Bot is running smoothly (Keyboard Edition + Zero-Slash Admin)...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    try: client.loop.run_until_complete(main())
    except KeyboardInterrupt: logger.info("Bot stopped.")
    except Exception as e: logger.error(f"Fatal error: {e}")
