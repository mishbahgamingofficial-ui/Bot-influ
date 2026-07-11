import os
import re
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

# Safely parse Admin IDs
def parse_admin_ids(env_string):
    ids = []
    for uid in env_string.split(","):
        uid = uid.strip()
        if uid.lstrip('-').isdigit():
            ids.append(int(uid))
    return ids

ADMIN_IDS = parse_admin_ids(os.environ.get("ADMIN_IDS", "0"))

# ------------------ DATABASE & STATE ------------------
DB_PATH = Path("bot_data.db")

tracked_users: dict[int, int] = {}      
blocked_users: set[int] = set()
saved_messages: dict[int, dict] = {}    
button_forwards: dict[str, list] = {}   
welcome_enabled = True
recently_welcomed: set[int] = set()

user_chat_state: dict[int, bool] = {}
admin_chat_state: dict[int, dict] = {}
auto_replies: dict[str, str] = {}

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
        """)
        return conn

def load_from_db():
    global tracked_users, blocked_users, saved_messages, button_forwards, welcome_enabled, auto_replies
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
            
        cur.execute("SELECT keyword, response FROM auto_replies")
        for kw, resp in cur.fetchall():
            auto_replies[kw] = resp
            
    logger.info(f"Loaded {len(tracked_users)} users, {len(auto_replies)} auto-replies.")

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
    full_name = f"{first_name} {last_name}".strip()
    
    safe_name = full_name.replace('[', '').replace(']', '').replace('*', '').replace('_', '').replace('`', '')
    if not safe_name.strip():
        safe_name = "Unknown User"
        
    linked_name = f"[{safe_name}](tg://user?id={user.id})"
    
    if user.username:
        username_display = f"@{user.username}"
    else:
        username_display = f"❌ No Username"
    
    text = (
        f"🚨 **New User Alert!**\n\n"
        f"👤 **Name:** {linked_name}\n"
        f"🔗 **Username:** {username_display}\n"
        f"🆔 **ID:** `{user.id}` *(Tap to copy)*\n"
        f"💬 **Direct Chat:** Send `/send {user.id}`\n"
        f"📅 **Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
    )
    
    for admin_id in ADMIN_IDS:
        try:
            await client.send_message(admin_id, text)
        except Exception as e:
            pass

# ------------------ WELCOME SYSTEM & MENU ------------------
async def remove_welcome_cooldown(uid):
    await asyncio.sleep(30)
    recently_welcomed.discard(uid)

async def send_user_menu(entity):
    btns = [
        [Button.text("I Want Number HACK", resize=True)],
        [Button.text("How To Start COLOUR TRADING", resize=True)],
        [Button.text("Contact to Admin", resize=True)]
    ]
    try:
        await client.send_message(
            entity,
            "👋 Welcome! Tap a button below to receive the information.",
            buttons=btns
        )
    except Exception:
        pass

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
    except Exception:
        pass

# ------------------ START (Keyboard for users) ------------------
@client.on(events.NewMessage(pattern="(?i)^/start$"))
async def start(event):
    user_chat_state[event.sender_id] = False 
    admin_chat_state.pop(event.sender_id, None)
    
    if event.sender_id in ADMIN_IDS:
        btns = [
            [Button.text("📊 Stats"), Button.text("⚙️ Status")],
            [Button.text("📢 Broadcast"), Button.text("✉️ Send Message")],
            [Button.text("🔢 Set Sequence"), Button.text("🔇 Toggle Welcome")],
            [Button.text("📁 Backup"), Button.text("🔄 Restore")],
            [Button.text("🔘 Set Button"), Button.text("🗑 Clear Button")],
            [Button.text("🧹 Cleanup")]
        ]
        await event.reply("👨‍💻 **Admin Panel**\n*(To view Auto-Replies, send /listreplies)*", buttons=btns)
    else:
        user = await event.get_sender()
        if user:
            await send_welcome_sequence(user)
            await send_user_menu(user)

# ------------------ FOOLPROOF BUTTON HANDLERS & HACKER ANIMATION ------------------
@client.on(events.NewMessage(func=lambda e: e.text and "number hack" in e.text.lower()))
async def hack_button_handler(event):
    await send_button_forward(event, "hack")

@client.on(events.NewMessage(func=lambda e: e.text and "colour trading" in e.text.lower()))
async def prediction_button_handler(event):
    await send_button_forward(event, "prediction")

@client.on(events.NewMessage(func=lambda e: e.text and "contact to admin" in e.text.lower()))
async def contact_admin_handler(event):
    if event.sender_id in ADMIN_IDS:
        return
        
    user_chat_state[event.sender_id] = True
    await event.reply("📝 **Admin Support**\n\nAb aap apna message ya screenshot yahan bhej sakte hain. Seedha admin ko deliver ho jayega! 👇")

async def send_button_forward(event, key):
    uid = event.sender_id
    config_list = button_forwards.get(key)
    
    if not config_list:
        await event.reply("⚠️ This option is not configured yet. Please contact admin.")
        return

    # 🔥 THE HACKER TERMINAL ANIMATION 🔥
    if key == "hack":
        status_msg = await event.reply("💻 `[root@bot]~ Connecting to VIP Server...`")
        await asyncio.sleep(0.6)
        
        await status_msg.edit("🛠️ `[root@bot]~ Bypassing Firewall [████░░░░] 45%`")
        await asyncio.sleep(0.6)
        
        await status_msg.edit("📂 `[root@bot]~ Extracting Data  [████████] 100%`")
        await asyncio.sleep(0.6)
        
        await status_msg.edit("✅ **ACCESS GRANTED!** Sending files... 🚀")
        await asyncio.sleep(0.4)
    else:
        status_msg = await event.reply("⏳ Sending requested information...")

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
            await status_msg.edit("❌ The configured messages are unavailable or deleted. Admin needs to reset them.")
        else:
            await asyncio.sleep(1)
            await status_msg.delete() 
            
    except UserIsBlockedError:
        await status_msg.edit("❌ You have blocked the bot. Please unblock and try again.")
    except FloodWaitError as e:
        await status_msg.edit(f"⏳ Too many requests, please wait {e.seconds} seconds.")
    except Exception as e:
        logger.error(f"Button {key} error for {uid}: {e}")
        await status_msg.edit("❌ Something went wrong. Please try again later.")


# ------------------ DIRECT MESSAGE (SMART BUTTON SYSTEM) ------------------
@client.on(events.NewMessage(pattern=r"^✉️ Send Message$"))
@admin_only
async def btn_send_dm_start(event):
    admin_id = event.sender_id
    prompt = await event.reply("👤 **Kisko message bhejna hai?**\n\n👉 Niche us user ki **ID** type karke bhejo:\n*(Cancel karne ke liye /cancel likho)*")
    
    admin_chat_state[admin_id] = {
        "step": "waiting_for_id",
        "target_id": None,
        "delete_msgs": [event.id, prompt.id]
    }

@client.on(events.NewMessage(pattern=r"^/cancel$"))
@admin_only
async def cancel_state(event):
    if event.sender_id in admin_chat_state:
        del admin_chat_state[event.sender_id]
        await event.reply("🚫 **Action cancelled.**")

@client.on(events.NewMessage(func=lambda e: e.sender_id in ADMIN_IDS and e.sender_id in admin_chat_state))
async def handle_admin_chat_state(event):
    admin_id = event.sender_id
    state = admin_chat_state[admin_id]
    
    if event.text and event.text.startswith('/'):
        return 
        
    if state["step"] == "waiting_for_id":
        text = event.text.strip()
        if text.lstrip('-').isdigit():
            target_id = int(text)
            state["target_id"] = target_id
            state["step"] = "waiting_for_msg"
            state["delete_msgs"].append(event.id)
            try:
                await client.delete_messages(admin_id, state["delete_msgs"])
            except Exception:
                pass
            prompt = await event.respond(f"✅ Target ID Set: `{target_id}`\n\n✍️ **Ab apna Message, Photo ya Video bhejo:**\n*(Cancel karne ke liye /cancel likho)*")
            state["delete_msgs"] = [prompt.id] 
        else:
            await event.reply("❌ Invalid ID! Sirf numbers allow hain. Phir se target ID type karo:")
            
    elif state["step"] == "waiting_for_msg":
        target_id = state["target_id"]
        try:
            try:
                await client.delete_messages(admin_id, state["delete_msgs"])
            except Exception:
                pass
            access_hash = tracked_users.get(target_id, 0)
            peer = InputPeerUser(target_id, access_hash) if access_hash else target_id
            
            if event.text:
                await client.send_message(peer, event.text, file=event.media)
            elif event.media:
                await client.send_message(peer, file=event.media)
                
            user_chat_state[target_id] = True 
            await event.respond(f"✅ **Message successfully delivered to `{target_id}`!**")
            del admin_chat_state[admin_id]
        except Exception as e:
            await event.respond(f"❌ **Error:** {e}")
            del admin_chat_state[admin_id]
            
    raise events.StopPropagation 

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
        await event.reply(f"✅ Direct message sent to `{target_uid}`!")
    except Exception as e:
        await event.reply(f"❌ Failed to send message: {e}")

# 🔥 ------------------ AUTO-RESPONDER COMMANDS ------------------ 🔥
@client.on(events.NewMessage(pattern=r"^/setreply\s+(.+?)\s*\|\s*(.+)"))
@admin_only
async def set_auto_reply(event):
    keyword = event.pattern_match.group(1).strip().lower()
    response = event.pattern_match.group(2).strip()
    
    auto_replies[keyword] = response
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("INSERT OR REPLACE INTO auto_replies (keyword, response) VALUES (?, ?)", (keyword, response))
        
    await event.reply(f"✅ **Auto-Reply Set!**\n\n🔑 **Keyword:** `{keyword}`\n🤖 **Response:**\n{response}")

@client.on(events.NewMessage(pattern=r"^/delreply\s+(.+)"))
@admin_only
async def del_auto_reply(event):
    keyword = event.pattern_match.group(1).strip().lower()
    if keyword in auto_replies:
        del auto_replies[keyword]
        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute("DELETE FROM auto_replies WHERE keyword=?", (keyword,))
        await event.reply(f"🗑 **Auto-reply deleted** for keyword: `{keyword}`")
    else:
        await event.reply(f"⚠️ Keyword `{keyword}` not found.")

@client.on(events.NewMessage(pattern=r"^/listreplies$"))
@admin_only
async def list_auto_replies(event):
    if not auto_replies:
        await event.reply("📭 No auto-replies are set right now.")
        return
        
    msg = "🤖 **Active Auto-Replies:**\n\n"
    for kw, resp in auto_replies.items():
        short_resp = resp[:30] + "..." if len(resp) > 30 else resp
        msg += f"🔹 **{kw}** ➡ {short_resp}\n"
    await event.reply(msg)

# ------------------ STATS / STATUS / BROADCAST / SETTINGS ------------------
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
        f"🤖 Auto-Replies: `{len(auto_replies)}`\n"
        f"🔊 Welcome: `{'ON' if welcome_enabled else 'OFF'}`\n"
        f"🔘 Hack btn: `{hack_msgs}` | 🔘 Prediction btn: `{pred_msgs}`"
    )
    await event.reply(msg)

@client.on(events.NewMessage(pattern=r"^(/status|⚙️ Status)$"))
@admin_only
async def status(event):
    steps = "\n".join(f"  {s}: {d['type']}" for s, d in sorted(saved_messages.items()))
    await event.reply(f"⚙️ **Status**\nUsers: `{len(tracked_users)}`\nBlocked: `{len(blocked_users)}`\nSequence:\n{steps if steps else 'none'}")

@client.on(events.NewMessage(pattern=r"^/broadcast"))
@admin_only
async def broadcast(event):
    if not tracked_users: return await event.reply("❌ No users.")
    text = event.text.replace("/broadcast", "").strip()
    if not text and not event.is_reply: return await event.reply("❌ Reply to a message or provide text.")

    status_msg = await event.reply(f"📢 Broadcasting to {len(tracked_users)}...")
    success = fail = skip = 0

    for uid, old_hash in list(tracked_users.items()):
        if uid in blocked_users:
            skip += 1
            continue
        try:
            peer = await client.get_input_entity(uid)
            if event.is_reply:
                reply_msg = await event.get_reply_message()
                await client.forward_messages(peer, reply_msg.id, reply_msg.chat_id)
            else:
                await client.send_message(peer, text)
            success += 1
            await asyncio.sleep(0.3)
        except Exception:
            fail += 1

    await status_msg.edit(f"✅ **Broadcast done**\n✅ Delivered: `{success}`\n❌ Failed: `{fail}`\n⏭ Skipped: `{skip}`")

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
            return await event.reply(f"🗑 Step {step} removed.")
        else:
            return await event.reply("❌ Please provide text or reply to a message.")

    saved_messages[step] = data
    save_message_step(step, data)
    await event.reply(f"✅ Step {step} saved as `{data['type']}`.")

@client.on(events.NewMessage(pattern=r"^/setbutton\s+(hack|prediction)$"))
@admin_only
async def set_button(event):
    btn_key = event.pattern_match.group(1).lower()
    if not event.is_reply: return await event.reply("❌ Please reply to the message you want to forward.")
    reply_msg = await event.get_reply_message()
    chat_id = get_chat_id(reply_msg, event)
    delete_button_config(btn_key)
    button_forwards[btn_key] = []
    add_button_config(btn_key, reply_msg.id, chat_id)
    button_forwards[btn_key].append({"msg_id": reply_msg.id, "from_chat": chat_id})
    await event.reply(f"✅ Button **'{btn_key}'** set! (1 message)\n\n👉 **Tip:** Use `/addbutton {btn_key}` to add more.")

@client.on(events.NewMessage(pattern=r"^/addbutton\s+(hack|prediction)$"))
@admin_only
async def add_button(event):
    btn_key = event.pattern_match.group(1).lower()
    if not event.is_reply: return await event.reply("❌ Please reply to the message.")
    if btn_key not in button_forwards: button_forwards[btn_key] = []
    reply_msg = await event.get_reply_message()
    chat_id = get_chat_id(reply_msg, event)
    add_button_config(btn_key, reply_msg.id, chat_id)
    button_forwards[btn_key].append({"msg_id": reply_msg.id, "from_chat": chat_id})
    await event.reply(f"✅ Additional message attached! Total: {len(button_forwards[btn_key])}")

@client.on(events.NewMessage(pattern=r"^/clearbutton\s+(hack|prediction)$"))
@admin_only
async def clear_button(event):
    btn_key = event.pattern_match.group(1).lower()
    if btn_key in button_forwards:
        del button_forwards[btn_key]
        delete_button_config(btn_key)
        await event.reply(f"🗑 Button **'{btn_key}'** cleared completely.")
    else:
        await event.reply(f"⚠️ No configuration found.")

def get_chat_id(reply_msg, event):
    if hasattr(reply_msg, "chat_id") and reply_msg.chat_id: return reply_msg.chat_id
    elif hasattr(reply_msg, "peer_id"):
        pid = reply_msg.peer_id
        if isinstance(pid, PeerUser): return pid.user_id
        elif isinstance(pid, PeerChannel): return pid.channel_id
    return event.chat_id

@client.on(events.NewMessage(pattern=r"^(/togglewelcome|🔇 Toggle Welcome)$"))
@admin_only
async def toggle_welcome(event):
    global welcome_enabled
    welcome_enabled = not welcome_enabled
    set_setting("welcome_enabled", "1" if welcome_enabled else "0")
    await event.reply(f"🔊 Welcome sequence is now **{'ON' if welcome_enabled else 'OFF'}**.")

@client.on(events.NewMessage(pattern=r"^(/backup|📁 Backup)$"))
@admin_only
async def backup(event):
    data = {
        "tracked": tracked_users,
        "blocked": list(blocked_users),
        "messages": {str(k): v for k, v in saved_messages.items()},
        "button_forwards": button_forwards,
        "welcome": welcome_enabled,
        "auto_replies": auto_replies
    }
    file = "backup.json"
    with open(file, "w") as f: json.dump(data, f)
    await client.send_file(event.chat_id, file, caption="📁 Full backup")
    os.remove(file)

@client.on(events.NewMessage(pattern=r"^/restore"))
@admin_only
async def restore(event):
    if not event.is_reply: return await event.reply("❌ Reply to a `.json` backup file.")
    rep = await event.get_reply_message()
    if not rep.file or not rep.file.name.endswith(".json"): return await event.reply("❌ Invalid file.")
    path = await client.download_media(rep.media)
    try:
        with open(path, "r") as f: data = json.load(f)
        global tracked_users, blocked_users, saved_messages, button_forwards, welcome_enabled, auto_replies
        tracked_users = {int(k): int(v) for k, v in data.get("tracked", {}).items()}
        blocked_users = set(int(u) for u in data.get("blocked", []))
        saved_messages = {int(k): v for k, v in data.get("messages", {}).items()}
        button_forwards = data.get("button_forwards", {})
        welcome_enabled = data.get("welcome", True)
        auto_replies = data.get("auto_replies", {})

        with sqlite3.connect(str(DB_PATH)) as conn:
            conn.execute("DELETE FROM users")
            conn.execute("DELETE FROM messages")
            conn.execute("DELETE FROM button_msgs")
            conn.execute("DELETE FROM auto_replies")
            
            for uid, hsh in tracked_users.items():
                conn.execute("INSERT OR REPLACE INTO users (user_id, access_hash, blocked) VALUES (?,?,?)", (uid, hsh, int(uid in blocked_users)))
            for step, d in saved_messages.items():
                conn.execute("INSERT OR REPLACE INTO messages (step, msg_type, text, msg_id, from_chat) VALUES (?,?,?,?,?)", (step, d.get("type"), d.get("text"), d.get("msg_id"), d.get("from_chat")))
            for key, cfg_list in button_forwards.items():
                if isinstance(cfg_list, dict): cfg_list = [cfg_list]
                for cfg in cfg_list: conn.execute("INSERT INTO button_msgs (btn_key, msg_id, from_chat) VALUES (?,?,?)", (key, cfg["msg_id"], cfg["from_chat"]))
            for kw, resp in auto_replies.items():
                conn.execute("INSERT OR REPLACE INTO auto_replies (keyword, response) VALUES (?,?)", (kw, resp))
                
        set_setting("welcome_enabled", "1" if welcome_enabled else "0")
        await event.reply(f"✅ Restored: {len(tracked_users)} users")
    except Exception as e:
        await event.reply(f"❌ Restore failed: {e}")
    finally:
        if os.path.exists(path): os.remove(path)

@client.on(events.NewMessage(pattern=r"^(📢 Broadcast|🔢 Set Sequence|🔘 Set Button|🗑 Clear Button)$"))
@admin_only
async def helper_buttons(event):
    text = event.text
    if text == "📢 Broadcast": await event.reply("📢 **Broadcast Kaise Karein?**\n\n👉 `/broadcast <text>` ya reply with message.")
    elif text == "🔢 Set Sequence": await event.reply("🔢 **Sequence Kaise Set Karein?**\n\n👉 `/setmsg1 <text>` ya reply karein.")
    elif text == "🔘 Set Button": await event.reply("🔘 **Button Set Kaise Karein?**\n👉 **Step 1:** `/setbutton hack` (reply karke pehla msg daalo)\n👉 **Step 2:** `/addbutton hack` (reply karke dusra msg add karo)")
    elif text == "🗑 Clear Button": await event.reply("🗑 **Button Clear Kaise Karein?**\n👉 `/clearbutton hack` ya `/clearbutton prediction` se button config hataayein.")

@client.on(events.NewMessage(pattern=r"^(📁 Backup|🔄 Restore)$"))
@admin_only
async def backup_restore_buttons(event):
    if event.text == "📁 Backup":
        await backup(event)
    elif event.text == "🔄 Restore":
        if os.path.exists("auto_backup.json"):
            try:
                with open("auto_backup.json", "r") as f: data = json.load(f)
                global tracked_users, blocked_users, saved_messages, button_forwards, welcome_enabled, auto_replies
                tracked_users = {int(k): int(v) for k, v in data.get("tracked", {}).items()}
                blocked_users = set(int(u) for u in data.get("blocked", []))
                saved_messages = {int(k): v for k, v in data.get("messages", {}).items()}
                button_forwards = data.get("button_forwards", {})
                welcome_enabled = data.get("welcome", True)
                auto_replies = data.get("auto_replies", {})
                
                with sqlite3.connect(str(DB_PATH)) as conn:
                    conn.execute("DELETE FROM users")
                    conn.execute("DELETE FROM messages")
                    conn.execute("DELETE FROM button_msgs")
                    conn.execute("DELETE FROM auto_replies")
                    
                    for uid, hsh in tracked_users.items(): conn.execute("INSERT OR REPLACE INTO users (user_id, access_hash, blocked) VALUES (?,?,?)", (uid, hsh, int(uid in blocked_users)))
                    for step, d in saved_messages.items(): conn.execute("INSERT OR REPLACE INTO messages (step, msg_type, text, msg_id, from_chat) VALUES (?,?,?,?,?)", (step, d.get("type"), d.get("text"), d.get("msg_id"), d.get("from_chat")))
                    for key, cfg_list in button_forwards.items():
                        if isinstance(cfg_list, dict): cfg_list = [cfg_list]
                        for cfg in cfg_list: conn.execute("INSERT INTO button_msgs (btn_key, msg_id, from_chat) VALUES (?,?,?)", (key, cfg["msg_id"], cfg["from_chat"]))
                    for kw, resp in auto_replies.items():
                        conn.execute("INSERT OR REPLACE INTO auto_replies (keyword, response) VALUES (?,?)", (kw, resp))
                        
                set_setting("welcome_enabled", "1" if welcome_enabled else "0")
                await event.reply(f"✅ Restored from auto‑backup. Users: {len(tracked_users)}")
            except Exception as e:
                await event.reply(f"❌ Auto‑restore failed: {e}")
        else:
            await event.reply("🔄 **Restore Kaise Karein?**\n1. Pehle `/backup` se `.json` file banayein.\n2. Us file par reply karke `/restore` command bhejein.")

@client.on(events.NewMessage(pattern=r"^(/cleanup|🧹 Cleanup)$"))
@admin_only
async def cleanup(event):
    with sqlite3.connect(str(DB_PATH)) as conn:
        conn.execute("DELETE FROM users WHERE blocked=1")
    for uid in list(blocked_users):
        tracked_users.pop(uid, None)
    blocked_users.clear()
    await event.reply("🧹 Blocked users removed from DB and memory.")

# ------------------ TWO-WAY SEAMLESS CHAT & AUTO-REPLY ------------------
@client.on(events.NewMessage(incoming=True, func=lambda e: e.is_private))
async def seamless_chat_handler(event):
    if event.sender_id in admin_chat_state: return

    text = event.raw_text.lower() if event.raw_text else ""
    
    # Ignore Commands
    if text.startswith('/') or "✉️ send message" in text or "📢 broadcast" in text or "📊 stats" in text or "⚙️ status" in text or "🔢 set sequence" in text or "🔇 toggle welcome" in text or "📁 backup" in text or "🔄 restore" in text or "🔘 set button" in text or "🗑 clear button" in text or "🧹 cleanup" in text:
        return
    if "number hack" in text or "colour trading" in text or "contact to admin" in text:
        return

    if event.sender_id not in ADMIN_IDS:
        # --- USER TO ADMIN ---
        if user_chat_state.get(event.sender_id):
            
            # 🔥 Check for Auto-Replies FIRST
            bot_auto_replied = False
            for kw, resp in auto_replies.items():
                if kw in text:
                    try:
                        await client.send_message(event.sender_id, resp)
                        bot_auto_replied = True
                        break # Only trigger one keyword per message
                    except Exception:
                        pass

            user = await event.get_sender()
            name = user.first_name or "User"
            
            # Tag the admin message if bot already answered it
            auto_tag = "🤖 *(Bot Auto-Replied)*\n" if bot_auto_replied else ""
            
            caption = (
                f"💬 **NEW SUPPORT MESSAGE** 💬\n\n"
                f"{auto_tag}"
                f"👤 **From:** [{name}](tg://user?id={event.sender_id})\n"
                f"🆔 `{event.sender_id}`\n"
            )
            
            if event.raw_text: caption += f"\n📝 **Message:**\n{event.raw_text}"
            caption += "\n\n👇 *(Reply to this message to answer)*"
                
            for admin_id in ADMIN_IDS:
                try: await client.send_message(admin_id, caption, file=event.media)
                except Exception: pass
                    
            try:
                if not bot_auto_replied:
                    feedback_msg = await event.reply("✅ *Message delivered to Admin.*")
                    await asyncio.sleep(3)
                    await feedback_msg.delete()
            except Exception:
                pass
                
        else:
            try: await event.delete()
            except Exception: pass
            warning_msg = await event.respond("⚠️ **Direct messages are disabled.**\n👉 Please use the **'Contact to Admin'** button below to speak with us.")
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
                    admin_feedback = await event.reply("✅ **Sent!**")
                    await asyncio.sleep(2)
                    try: await admin_feedback.delete()
                    except Exception: pass
                        
                except Exception as e:
                    await event.reply(f"❌ **Error:** {e}")

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
                "auto_replies": auto_replies
            }
            with open("auto_backup.tmp", "w") as f:
                json.dump(data, f)
            os.replace("auto_backup.tmp", "auto_backup.json")
            logger.info("Auto backup saved safely.")
        except Exception as e:
            pass

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
    try: client.loop.run_until_complete(main())
    except KeyboardInterrupt: logger.info("Bot stopped.")
    except Exception as e: logger.error(f"Fatal error: {e}")
