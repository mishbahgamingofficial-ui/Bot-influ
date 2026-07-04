import os
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
from telethon.tl.types import (
    InputPeerUser,
    UpdateBotChatInviteRequester,
    PeerUser,
    PeerChannel,
)
from telethon.errors import (
    UserIsBlockedError,
    FloodWaitError,
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

# Safely parse Admin IDs, allowing for negative IDs (groups/channels)
def parse_admin_ids(env_string):
    ids = []
    for uid in env_string.split(","):
        uid = uid.strip()
        if uid.lstrip('-').isdigit():
            ids.append(int(uid))
    return ids

ADMIN_IDS = parse_admin_ids(os.environ.get("ADMIN_IDS", "0"))

# ------------------ DATABASE ------------------
DB_PATH = Path("bot_data.db")

tracked_users: dict[int, int] = {}      
blocked_users: set[int] = set()
saved_messages: dict[int, dict] = {}    
button_forwards: dict[str, list] = {}   
welcome_enabled = True
recently_welcomed: set[int] = set()

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
        """)
        return conn

def load_from_db():
    global tracked_users, blocked_users, saved_messages, button_forwards, welcome_enabled
    with sqlite3.connect(str(DB_PATH)) as conn:
        cur = conn.cursor()
        cur.execute("SELECT user_id, access_hash, blocked FROM users")
        for uid, hsh, blk in cur.fetchall():
            tracked_users[uid] = hsh
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
            
        cur.execute("SELECT value FROM settings WHERE key='welcome_enabled'")
        row = cur.fetchone()
        if row:
            welcome_enabled = row[0] == "1"
            
    logger.info(f"Loaded {len(tracked_users)} users, {len(blocked_users)} blocked, "
                f"{len(saved_messages)} messages, {len(button_forwards)} buttons configured")

def save_user(uid: int, access_hash: int, blocked: bool = False):
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO users (user_id, access_hash, blocked, joined_date) VALUES (?,?,?,?)",
            (uid, access_hash, int(blocked), datetime.now().isoformat()),
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

# ------------------ CLIENT ------------------
client = TelegramClient(
    StringSession(SESSION_STRING) if SESSION_STRING else "bot_session",
    API_ID,
    API_HASH,
)

# ------------------ WEB SERVER ------------------
async def web_handler(request):
    return web.Response(text="🟢 Bot is alive")

async def start_web():
    app = web.Application()
    app.add_routes([web.get("/", web_handler)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server on port {PORT}")

# ------------------ DECORATORS ------------------
def admin_only(func):
    async def wrapper(event):
        if event.sender_id not in ADMIN_IDS:
            await event.reply("❌ Admin only command.")
            return
        await func(event)
    return wrapper

# ------------------ ADMIN ALERTS ------------------
async def notify_admins_new_user(user):
    first_name = user.first_name or ""
    last_name = user.last_name or ""
    full_name = f"{first_name} {last_name}".strip() or "Unknown"
    username = f"@{user.username}" if user.username else "None"
    
    text = (
        f"🚨 **New User Alert!**\n\n"
        f"👤 **Name:** {full_name}\n"
        f"🔗 **Username:** {username}\n"
        f"🆔 **ID:** `{user.id}`\n"
        f"📅 **Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    
    for admin_id in ADMIN_IDS:
        try:
            await client.send_message(admin_id, text)
        except Exception as e:
            logger.error(f"Failed to alert admin {admin_id}: {e}")

# ------------------ WELCOME SYSTEM ------------------
async def remove_welcome_cooldown(uid):
    await asyncio.sleep(30)
    recently_welcomed.discard(uid)

async def send_user_menu(entity):
    btns = [
        [Button.text("I Want Number HACK", resize=True)],
        [Button.text("How To Start COLOUR TRADING", resize=True)],
    ]
    try:
        await client.send_message(
            entity,
            "👋 Welcome! Tap a button below to receive the information.",
            buttons=btns
        )
    except Exception as e:
        logger.error(f"Failed to send user menu: {e}")

async def send_welcome_sequence(user):
    uid = user.id
    if uid in recently_welcomed:
        return
    recently_welcomed.add(uid)

    if getattr(user, "is_bot", False):
        return

    try:
        full_user = await client.get_input_entity(uid)
        if not isinstance(full_user, InputPeerUser):
            return
        access_hash = full_user.access_hash
    except Exception as e:
        logger.warning(f"Cannot get entity for {uid}: {e}")
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
                except (MessageIdInvalidError, ChatInvalidError, ChannelInvalidError):
                    continue
                await client.forward_messages(full_user, item["msg_id"], item["from_chat"])
            else:
                await client.send_message(full_user, item["text"])
            await asyncio.sleep(0.5)
        except UserIsBlockedError:
            blocked_users.add(uid)
            save_user(uid, access_hash, blocked=True)
            return
        except PeerIdInvalidError:
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
        if user:
            await send_welcome_sequence(user)
            if user.id not in ADMIN_IDS:
                await send_user_menu(user)

@client.on(events.Raw(types=UpdateBotChatInviteRequester))
async def join_request(event):
    try:
        user = await client.get_entity(event.user_id)
        await send_welcome_sequence(user)
        if event.user_id not in ADMIN_IDS:
            await send_user_menu(user)
    except Exception as e:
        logger.error(f"Join request error: {e}")

# ------------------ START (Keyboard for users) ------------------
@client.on(events.NewMessage(pattern="(?i)^/start$"))
async def start(event):
    if event.sender_id in ADMIN_IDS:
        btns = [
            [Button.text("📊 Stats"), Button.text("⚙️ Status")],
            [Button.text("📢 Broadcast"), Button.text("🔢 Set Sequence")],
            [Button.text("📁 Backup"), Button.text("🔄 Restore")],
            [Button.text("🔇 Toggle Welcome"), Button.text("🧹 Cleanup")],
            [Button.text("🔘 Set Button"), Button.text("🗑 Clear Button")],
        ]
        await event.reply("👨‍💻 **Admin Panel**", buttons=btns)
    else:
        user = await event.get_sender()
        if user:
            await send_welcome_sequence(user)
            await send_user_menu(user)

# ------------------ FOOLPROOF BUTTON HANDLERS ------------------
# Yeh Lambda function 100% fail-proof hai, case or spaces ka issue nahi aayega!
@client.on(events.NewMessage(func=lambda e: e.text and "number hack" in e.text.lower()))
async def hack_button_handler(event):
    await send_button_forward(event, "hack")

@client.on(events.NewMessage(func=lambda e: e.text and "colour trading" in e.text.lower()))
async def prediction_button_handler(event):
    await send_button_forward(event, "prediction")

async def send_button_forward(event, key):
    uid = event.sender_id
    config_list = button_forwards.get(key)
    
    if not config_list:
        await event.reply("⚠️ This option is not configured yet. Please contact admin.")
        return

    # Visual loading status added so you know bot is responding!
    status_msg = await event.reply("⏳ Sending requested information...")

    try:
        sent_count = 0
        
        for config in config_list:
            admin_id = config["from_chat"]
            # Fetch hash directly from memory/DB to bypass Render server resets!
            admin_hash = tracked_users.get(admin_id, 0)
            
            # Smart peer reconstruction
            if admin_id > 0 and admin_hash:
                from_peer = InputPeerUser(admin_id, admin_hash)
            else:
                from_peer = admin_id 

            try:
                await client.forward_messages(uid, config["msg_id"], from_peer)
                sent_count += 1
            except Exception as inner_e:
                logger.warning(f"Could not forward msg {config['msg_id']} for {uid}: {inner_e}")
                
            await asyncio.sleep(0.3) 
            
        if sent_count == 0:
            await status_msg.edit("❌ The configured messages are unavailable or deleted. Admin needs to reset them.")
        else:
            await status_msg.delete() # Successful, hide the loading message
            
    except UserIsBlockedError:
        await status_msg.edit("❌ You have blocked the bot. Please unblock and try again.")
    except PeerIdInvalidError:
        await status_msg.edit("❌ Could not send you the message. Please start the bot again.")
    except FloodWaitError as e:
        await status_msg.edit(f"⏳ Too many requests, please wait {e.seconds} seconds.")
    except Exception as e:
        logger.error(f"Button {key} error for {uid}: {e}")
        await status_msg.edit("❌ Something went wrong. Please try again later.")

# ------------------ STATS / STATUS ------------------
@client.on(events.NewMessage(pattern=r"^(/stats|📊 Stats)$"))
@admin_only
async def stats(event):
    hack_msgs = len(button_forwards.get('hack', []))
    pred_msgs = len(button_forwards.get('prediction', []))
    
    msg = (
        f"**📊 Statistics**\n"
        f"👥 Active: `{len(tracked_users)}`\n"
        f"🚫 Blocked: `{len(blocked_users)}`\n"
        f"📨 Sequence steps: `{len(saved_messages)}`\n"
        f"🔊 Welcome: `{'ON' if welcome_enabled else 'OFF'}`\n"
        f"🔘 Hack button: `{hack_msgs} messages`\n"
        f"🔘 Prediction button: `{pred_msgs} messages`"
    )
    await event.reply(msg)

@client.on(events.NewMessage(pattern=r"^(/status|⚙️ Status)$"))
@admin_only
async def status(event):
    steps = "\n".join(f"  {s}: {d['type']}" for s, d in sorted(saved_messages.items()))
    hack_msgs = len(button_forwards.get('hack', []))
    pred_msgs = len(button_forwards.get('prediction', []))
    
    btn_status = (
        f"🔘 Hack: {hack_msgs} msgs\n"
        f"🔘 Prediction: {pred_msgs} msgs"
    )
    await event.reply(
        f"⚙️ **Status**\n"
        f"Users: `{len(tracked_users)}`\n"
        f"Blocked: `{len(blocked_users)}`\n"
        f"Sequence:\n{steps if steps else 'none'}\n"
        f"{btn_status}"
    )

# ------------------ BROADCAST ------------------
@client.on(events.NewMessage(pattern=r"^/broadcast"))
@admin_only
async def broadcast(event):
    if not tracked_users:
        await event.reply("❌ No users.")
        return

    text = event.text.replace("/broadcast", "").strip()
    if not text and not event.is_reply:
        await event.reply("❌ Reply to a message or provide text.")
        return

    status_msg = await event.reply(f"📢 Broadcasting to {len(tracked_users)}...")
    success = fail = skip = 0

    for uid, old_hash in list(tracked_users.items()):
        if uid in blocked_users:
            skip += 1
            continue
        try:
            peer = await client.get_input_entity(uid)
        except Exception:
            fail += 1
            continue

        try:
            if event.is_reply:
                reply_msg = await event.get_reply_message()
                await client.forward_messages(peer, reply_msg.id, reply_msg.chat_id)
            else:
                await client.send_message(peer, text)
            success += 1
            await asyncio.sleep(0.3)
        except FloodWaitError as e:
            await asyncio.sleep(e.seconds)
        except (UserIsBlockedError, PeerIdInvalidError):
            blocked_users.add(uid)
            save_user(uid, old_hash, blocked=True)
            fail += 1
        except Exception as e:
            logger.error(f"Broadcast to {uid}: {e}")
            fail += 1

    await status_msg.edit(
        f"✅ **Broadcast done**\n"
        f"✅ Delivered: `{success}`\n"
        f"❌ Failed: `{fail}`\n"
        f"⏭ Skipped: `{skip}`"
    )

# ------------------ SEQUENCE ------------------
@client.on(events.NewMessage(pattern=r"^/setmsg(\d+)(?:\s+(.+))?"))
@admin_only
async def setmsg(event):
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
            await event.reply(f"🗑 Step {step} removed.")
            return
        else:
            await event.reply("❌ Please provide text or reply to a message.")
            return

    saved_messages[step] = data
    save_message_step(step, data)
    await event.reply(f"✅ Step {step} saved as `{data['type']}`.")

# ------------------ BUTTON CONFIGURATION ------------------
@client.on(events.NewMessage(pattern=r"^/setbutton\s+(hack|prediction)$"))
@admin_only
async def set_button(event):
    btn_key = event.pattern_match.group(1).lower()
    if not event.is_reply:
        await event.reply("❌ Please reply to the message you want to forward for this button.")
        return

    reply_msg = await event.get_reply_message()
    chat_id = get_chat_id(reply_msg, event)

    # Overwrite the button entirely
    delete_button_config(btn_key)
    button_forwards[btn_key] = []
    
    add_button_config(btn_key, reply_msg.id, chat_id)
    button_forwards[btn_key].append({"msg_id": reply_msg.id, "from_chat": chat_id})
    await event.reply(f"✅ Button **'{btn_key}'** set! (1 message)\n\n👉 **Tip:** Reply to another message with `/addbutton {btn_key}` to add a second message to this button!")

@client.on(events.NewMessage(pattern=r"^/addbutton\s+(hack|prediction)$"))
@admin_only
async def add_button(event):
    btn_key = event.pattern_match.group(1).lower()
    if not event.is_reply:
        await event.reply("❌ Please reply to the message you want to attach.")
        return

    if btn_key not in button_forwards:
        button_forwards[btn_key] = []

    reply_msg = await event.get_reply_message()
    chat_id = get_chat_id(reply_msg, event)
    
    add_button_config(btn_key, reply_msg.id, chat_id)
    button_forwards[btn_key].append({"msg_id": reply_msg.id, "from_chat": chat_id})
    
    total_msgs = len(button_forwards[btn_key])
    await event.reply(f"✅ Additional message attached! Button **'{btn_key}'** will now send {total_msgs} messages in a row.")

@client.on(events.NewMessage(pattern=r"^/clearbutton\s+(hack|prediction)$"))
@admin_only
async def clear_button(event):
    btn_key = event.pattern_match.group(1).lower()
    if btn_key in button_forwards:
        del button_forwards[btn_key]
        delete_button_config(btn_key)
        await event.reply(f"🗑 Button **'{btn_key}'** cleared completely.")
    else:
        await event.reply(f"⚠️ No configuration found for button '{btn_key}'.")

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

# ------------------ WELCOME TOGGLE ------------------
@client.on(events.NewMessage(pattern=r"^(/togglewelcome|🔇 Toggle Welcome)$"))
@admin_only
async def toggle_welcome(event):
    global welcome_enabled
    welcome_enabled = not welcome_enabled
    set_setting("welcome_enabled", "1" if welcome_enabled else "0")
    await event.reply(f"🔊 Welcome sequence is now **{'ON' if welcome_enabled else 'OFF'}**.")

# ------------------ BACKUP / RESTORE ------------------
@client.on(events.NewMessage(pattern=r"^(/backup|📁 Backup)$"))
@admin_only
async def backup(event):
    data = {
        "tracked": tracked_users,
        "blocked": list(blocked_users),
        "messages": {str(k): v for k, v in saved_messages.items()},
        "button_forwards": button_forwards,
        "welcome": welcome_enabled,
    }
    file = "backup.json"
    with open(file, "w") as f:
        json.dump(data, f)
    await client.send_file(event.chat_id, file, caption="📁 Full backup")
    os.remove(file)

@client.on(events.NewMessage(pattern=r"^/restore"))
@admin_only
async def restore(event):
    if not event.is_reply:
        await event.reply("❌ Reply to a `.json` backup file.")
        return
    rep = await event.get_reply_message()
    if not rep.file or not rep.file.name.endswith(".json"):
        await event.reply("❌ Invalid file.")
        return
    path = await client.download_media(rep.media)
    try:
        with open(path, "r") as f:
            data = json.load(f)
        global tracked_users, blocked_users, saved_messages, button_forwards, welcome_enabled
        tracked_users = {int(k): int(v) for k, v in data.get("tracked", {}).items()}
        blocked_users = set(int(u) for u in data.get("blocked", []))
        saved_messages = {int(k): v for k, v in data.get("messages", {}).items()}
        button_forwards = data.get("button_forwards", {})
        welcome_enabled = data.get("welcome", True)

        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute("DELETE FROM users")
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM button_msgs")
            
            for uid, hsh in tracked_users.items():
                conn.execute("INSERT OR REPLACE INTO users (user_id, access_hash, blocked) VALUES (?,?,?)", 
                             (uid, hsh, int(uid in blocked_users)))
            for step, d in saved_messages.items():
                conn.execute("INSERT OR REPLACE INTO messages (step, msg_type, text, msg_id, from_chat) VALUES (?,?,?,?,?)",
                             (step, d.get("type"), d.get("text"), d.get("msg_id"), d.get("from_chat")))
            for key, cfg_list in button_forwards.items():
                if isinstance(cfg_list, dict): 
                    cfg_list = [cfg_list]
                    button_forwards[key] = cfg_list
                for cfg in cfg_list:
                    conn.execute("INSERT INTO button_msgs (btn_key, msg_id, from_chat) VALUES (?,?,?)",
                                 (key, cfg["msg_id"], cfg["from_chat"]))
                             
        set_setting("welcome_enabled", "1" if welcome_enabled else "0")
        await event.reply(f"✅ Restored: {len(tracked_users)} users")
    except Exception as e:
        await event.reply(f"❌ Restore failed: {e}")
    finally:
        if os.path.exists(path):
            os.remove(path)

# ------------------ OTHER ADMIN BUTTON HELPERS ------------------
@client.on(events.NewMessage(pattern=r"^(📢 Broadcast|🔢 Set Sequence|🔘 Set Button|🗑 Clear Button)$"))
@admin_only
async def helper_buttons(event):
    text = event.text
    if text == "📢 Broadcast":
        await event.reply("📢 **Broadcast Kaise Karein?**\n\n👉 `/broadcast <text>` ya reply with message.")
    elif text == "🔢 Set Sequence":
        await event.reply("🔢 **Sequence Kaise Set Karein?**\n\n👉 `/setmsg1 <text>` ya reply karein.")
    elif text == "🔘 Set Button":
        await event.reply(
            "🔘 **Button Set Kaise Karein?**\n\n"
            "👉 **Step 1:** `/setbutton hack` (reply karke pehla msg daalo)\n"
            "👉 **Step 2 (Optional):** `/addbutton hack` (reply karke dusra msg add karo)"
        )
    elif text == "🗑 Clear Button":
        await event.reply(
            "🗑 **Button Clear Kaise Karein?**\n\n"
            "👉 `/clearbutton hack` ya `/clearbutton prediction` se button config hataayein."
        )

@client.on(events.NewMessage(pattern=r"^(📁 Backup|🔄 Restore)$"))
@admin_only
async def backup_restore_buttons(event):
    if event.text == "📁 Backup":
        await backup(event)
    elif event.text == "🔄 Restore":
        if os.path.exists("auto_backup.json"):
            try:
                with open("auto_backup.json", "r") as f:
                    data = json.load(f)
                global tracked_users, blocked_users, saved_messages, button_forwards, welcome_enabled
                tracked_users = {int(k): int(v) for k, v in data.get("tracked", {}).items()}
                blocked_users = set(int(u) for u in data.get("blocked", []))
                saved_messages = {int(k): v for k, v in data.get("messages", {}).items()}
                button_forwards = data.get("button_forwards", {})
                welcome_enabled = data.get("welcome", True)
                
                with sqlite3.connect(str(DB_PATH)) as conn:
                    conn.execute("DELETE FROM users")
                    conn.execute("DELETE FROM messages")
                    conn.execute("DELETE FROM button_msgs")
                    
                    for uid, hsh in tracked_users.items():
                        conn.execute("INSERT OR REPLACE INTO users (user_id, access_hash, blocked) VALUES (?,?,?)", 
                                     (uid, hsh, int(uid in blocked_users)))
                    for step, d in saved_messages.items():
                        conn.execute("INSERT OR REPLACE INTO messages (step, msg_type, text, msg_id, from_chat) VALUES (?,?,?,?,?)",
                                     (step, d.get("type"), d.get("text"), d.get("msg_id"), d.get("from_chat")))
                    for key, cfg_list in button_forwards.items():
                        if isinstance(cfg_list, dict):
                            cfg_list = [cfg_list]
                            button_forwards[key] = cfg_list
                        for cfg in cfg_list:
                            conn.execute("INSERT INTO button_msgs (btn_key, msg_id, from_chat) VALUES (?,?,?)",
                                         (key, cfg["msg_id"], cfg["from_chat"]))
                                     
                set_setting("welcome_enabled", "1" if welcome_enabled else "0")
                await event.reply(f"✅ Restored from auto‑backup. Users: {len(tracked_users)}")
            except Exception as e:
                await event.reply(f"❌ Auto‑restore failed: {e}")
        else:
            await event.reply(
                "🔄 **Restore Kaise Karein?**\n\n"
                "1. Pehle `/backup` se `.json` file banayein.\n"
                "2. Us file par reply karke `/restore` command bhejein.\n\n"
                "👉 Ya button dabayein toh automatic `auto_backup.json` restore hoga agar available ho."
            )

# ------------------ CLEANUP ------------------
@client.on(events.NewMessage(pattern=r"^(/cleanup|🧹 Cleanup)$"))
@admin_only
async def cleanup(event):
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("DELETE FROM users WHERE blocked=1")
    for uid in list(blocked_users):
        tracked_users.pop(uid, None)
    blocked_users.clear()
    await event.reply("🧹 Blocked users removed from DB and memory.")

# ------------------ AUTO BACKUP ------------------
async def periodic_backup():
    while True:
        await asyncio.sleep(3600)
        try:
            data = {
                "tracked": tracked_users,
                "blocked": list(blocked_users),
                "messages": {str(k): v for k, v in saved_messages.items()},
                "button_forwards": button_forwards,
                "welcome": welcome_enabled,
            }
            with open("auto_backup.tmp", "w") as f:
                json.dump(data, f)
            os.replace("auto_backup.tmp", "auto_backup.json")
            logger.info("Auto backup saved safely.")
        except Exception as e:
            logger.error(f"Auto backup error: {e}")

# ------------------ MAIN ------------------
async def main():
    await client.start(bot_token=BOT_TOKEN)
    init_db()
    load_from_db()
    await start_web()
    asyncio.create_task(periodic_backup())
    logger.info("Bot running...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        client.loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
