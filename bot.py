import os
import sys
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
    UpdateBotChatInviteRequester,
    InputPeerUser,
    PeerUser
)
from telethon.errors import (
    UserIsBlockedError,
    FloodWaitError,
    PeerIdInvalidError
)

# ------------------ SETUP ------------------
load_dotenv()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("ProBot")

# Environment config
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", 10000))
ADMIN_IDS = [
    int(uid.strip())
    for uid in os.environ.get("ADMIN_IDS", "0").split(",")
    if uid.strip().isdigit()
]
SESSION_STRING = os.environ.get("SESSION_STRING", "")

# Persistent DB
DB_PATH = Path("bot_data.db")

# Runtime caches (loaded from DB on start)
tracked_users: dict[int, int] = {}      # user_id -> access_hash
blocked_users: set[int] = set()
saved_messages: dict[int, dict] = {}    # step -> {"type","text"/"msg_id","from_chat"}
welcome_enabled = True                  # toggle for welcome sequence

# ------------------ DATABASE LAYER ------------------
def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            user_id INTEGER PRIMARY KEY,
            access_hash INTEGER NOT NULL,
            blocked INTEGER DEFAULT 0,
            joined_date TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS messages (
            step INTEGER PRIMARY KEY,
            msg_type TEXT,
            text TEXT,
            msg_id INTEGER,
            from_chat INTEGER
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        )
    """)
    conn.commit()
    return conn

def load_from_db():
    global tracked_users, blocked_users, saved_messages, welcome_enabled
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()

    # Users
    cur.execute("SELECT user_id, access_hash, blocked FROM users")
    for uid, access_hash, blocked in cur.fetchall():
        tracked_users[uid] = access_hash
        if blocked:
            blocked_users.add(uid)

    # Messages
    cur.execute("SELECT step, msg_type, text, msg_id, from_chat FROM messages")
    for step, mtype, text, msg_id, from_chat in cur.fetchall():
        saved_messages[step] = {
            "type": mtype,
            "text": text,
            "msg_id": msg_id,
            "from_chat": from_chat
        }

    # Settings
    cur.execute("SELECT value FROM settings WHERE key='welcome_enabled'")
    row = cur.fetchone()
    if row:
        welcome_enabled = row[0] == "1"

    conn.close()
    logger.info(f"Loaded {len(tracked_users)} users, {len(blocked_users)} blocked, {len(saved_messages)} messages from DB.")

def save_user(uid: int, access_hash: int, blocked: bool = False):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        """INSERT OR REPLACE INTO users (user_id, access_hash, blocked, joined_date)
           VALUES (?, ?, ?, ?)""",
        (uid, access_hash, int(blocked), datetime.now().isoformat())
    )
    conn.commit()
    conn.close()

def save_message_step(step: int, data: dict):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "INSERT OR REPLACE INTO messages (step, msg_type, text, msg_id, from_chat) VALUES (?,?,?,?,?)",
        (step, data.get("type"), data.get("text"), data.get("msg_id"), data.get("from_chat"))
    )
    conn.commit()
    conn.close()

def set_setting(key: str, value: str):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()

# ------------------ TELEGRAM CLIENT ------------------
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

client = TelegramClient(
    StringSession(SESSION_STRING) if SESSION_STRING else None,
    API_ID,
    API_HASH,
    loop=loop
).start(bot_token=BOT_TOKEN)

# ------------------ WEB SERVER (Render keep‑alive) ------------------
async def web_handler(request):
    return web.Response(text="🟢 Bot is alive")

async def start_web():
    app = web.Application()
    app.add_routes([web.get('/', web_handler)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', PORT)
    await site.start()
    logger.info(f"Web server on port {PORT}")

# ------------------ DECORATORS ------------------
def admin_only(func):
    async def wrapper(event):
        if event.sender_id not in ADMIN_IDS:
            await event.reply("❌ **Admin only command!**")
            return
        await func(event)
    return wrapper

# ------------------ WELCOME SYSTEM ------------------
async def send_welcome_sequence(user):
    uid = user.id
    access_hash = getattr(user, 'access_hash', 0)
    if access_hash == 0:
        try:
            full = await client.get_entity(uid)
            access_hash = getattr(full, 'access_hash', 0)
        except:
            logger.warning(f"Cannot fetch access_hash for {uid}, skipping welcome.")
            return

    # Store user (even if blocked, we’ll flag later)
    tracked_users[uid] = access_hash
    save_user(uid, access_hash, blocked=False)
    if uid in blocked_users:
        blocked_users.discard(uid)

    if not welcome_enabled:
        return

    if uid in ADMIN_IDS:
        return  # Admins don’t need the welcome spiel

    try:
        if saved_messages:
            for step in sorted(saved_messages.keys()):
                item = saved_messages[step]
                if item['type'] == 'forward':
                    await client.forward_messages(uid, item['msg_id'], item['from_chat'])
                else:
                    await client.send_message(uid, item['text'])
                await asyncio.sleep(0.5)
        logger.info(f"Welcome sequence sent to {uid}")
    except UserIsBlockedError:
        blocked_users.add(uid)
        save_user(uid, access_hash, blocked=True)
        logger.info(f"User {uid} blocked bot during welcome.")
    except FloodWaitError as e:
        logger.warning(f"Flood wait {e.seconds}s for user {uid}")
        await asyncio.sleep(e.seconds)
    except Exception as e:
        logger.error(f"Welcome error for {uid}: {e}")

# Event: user joins group (bot must be admin)
@client.on(events.ChatAction())
async def on_chat_action(event):
    if event.user_joined or event.user_added:
        user = await event.get_user()
        await send_welcome_sequence(user)

# Event: join request (if enabled)
@client.on(events.Raw(types=UpdateBotChatInviteRequester))
async def on_join_request(event):
    try:
        user = await client.get_entity(event.user_id)
        await send_welcome_sequence(user)
    except Exception as e:
        logger.error(f"Join request error: {e}")

# ------------------ ADMIN PANEL ------------------
@client.on(events.NewMessage(pattern='/start'))
async def start_cmd(event):
    if event.sender_id in ADMIN_IDS:
        buttons = [
            [Button.text("📊 Stats"), Button.text("⚙️ Status")],
            [Button.text("📢 Broadcast"), Button.text("🔢 Set Sequence")],
            [Button.text("📁 Backup"), Button.text("🔄 Restore")],
            [Button.text("🔇 Toggle Welcome"), Button.text("🧹 Cleanup")]
        ]
        await event.reply("👨‍💻 **Admin Panel**", buttons=buttons)
    else:
        await event.reply("👋 Hello! I'm a welcome & broadcast bot.", buttons=Button.clear())

# ------------------ STATS / STATUS ------------------
@client.on(events.NewMessage(pattern=r'^(/stats|📊 Stats)$'))
@admin_only
async def stats(event):
    msg = (
        f"**📊 Bot Statistics**\n"
        f"👥 Active users: `{len(tracked_users)}`\n"
        f"🚫 Blocked: `{len(blocked_users)}`\n"
        f"📨 Welcome messages: `{len(saved_messages)}` steps\n"
        f"🔊 Welcome enabled: `{welcome_enabled}`"
    )
    await event.reply(msg)

@client.on(events.NewMessage(pattern=r'^(/status|⚙️ Status)$'))
@admin_only
async def status(event):
    steps = "\n".join(f"  {s}: {d['type']}" for s, d in sorted(saved_messages.items()))
    await event.reply(
        f"⚙️ **System Status**\n"
        f"Users: `{len(tracked_users)}`\n"
        f"Blocked: `{len(blocked_users)}`\n"
        f"Sequence:\n{steps if steps else 'none'}"
    )

# ------------------ BROADCAST (with throttling) ------------------
@client.on(events.NewMessage(pattern=r'^/broadcast'))
@admin_only
async def broadcast(event):
    if not tracked_users:
        await event.reply("❌ No users to broadcast to.")
        return

    text = event.text.replace('/broadcast', '').strip()
    if not text and not event.is_reply:
        await event.reply("❌ Reply to a message or provide text.")
        return

    status_msg = await event.reply(f"📢 Broadcasting to {len(tracked_users)} users...")

    success, fail, skipped = 0, 0, 0
    for uid, access_hash in list(tracked_users.items()):
        if uid in blocked_users:
            skipped += 1
            continue
        if access_hash == 0:
            fail += 1
            continue

        try:
            peer = InputPeerUser(uid, access_hash)
            if event.is_reply:
                reply_msg = await event.get_reply_message()
                await client.forward_messages(peer, reply_msg.id, event.chat_id)
            else:
                await client.send_message(peer, text)

            success += 1
            await asyncio.sleep(0.3)   # throttle

        except FloodWaitError as e:
            logger.info(f"Flood wait {e.seconds}s")
            await asyncio.sleep(e.seconds)
        except (UserIsBlockedError, PeerIdInvalidError, ValueError):
            blocked_users.add(uid)
            save_user(uid, access_hash, blocked=True)
            fail += 1
        except Exception as e:
            logger.error(f"Broadcast to {uid} failed: {e}")
            fail += 1

    await status_msg.edit(
        f"✅ **Broadcast finished**\n"
        f"✅ Delivered: `{success}`\n"
        f"❌ Failed: `{fail}`\n"
        f"⏭ Skipped (blocked): `{skipped}`"
    )

# ------------------ SEQUENCE MANAGEMENT ------------------
@client.on(events.NewMessage(pattern=r'^/setmsg(\d+)(?:\s+(.+))?'))
@admin_only
async def set_message(event):
    step = int(event.pattern_match.group(1))
    extra = event.pattern_match.group(2)

    if event.is_reply:
        reply_msg = await event.get_reply_message()
        data = {"type": "forward", "msg_id": reply_msg.id, "from_chat": event.chat_id}
    elif extra:
        data = {"type": "text", "text": extra.strip()}
    else:
        # delete step if no data
        if step in saved_messages:
            del saved_messages[step]
            conn = sqlite3.connect(str(DB_PATH))
            conn.execute("DELETE FROM messages WHERE step=?", (step,))
            conn.commit()
            conn.close()
            await event.reply(f"🗑 Step {step} removed.")
            return
        else:
            await event.reply("❌ Step not found.")
            return

    saved_messages[step] = data
    save_message_step(step, data)
    await event.reply(f"✅ Step {step} saved as `{data['type']}`.")

# ------------------ TOGGLE WELCOME ------------------
@client.on(events.NewMessage(pattern=r'^(/togglewelcome|🔇 Toggle Welcome)$'))
@admin_only
async def toggle_welcome(event):
    global welcome_enabled
    welcome_enabled = not welcome_enabled
    set_setting("welcome_enabled", "1" if welcome_enabled else "0")
    state = "ON" if welcome_enabled else "OFF"
    await event.reply(f"🔊 Welcome sequence is now **{state}**.")

# ------------------ BACKUP / RESTORE ------------------
@client.on(events.NewMessage(pattern=r'^(/backup|📁 Backup)$'))
@admin_only
async def backup(event):
    data = {
        "tracked": tracked_users,
        "blocked": list(blocked_users),
        "messages": {str(k): v for k, v in saved_messages.items()},
        "welcome": welcome_enabled
    }
    file = "backup.json"
    with open(file, "w") as f:
        json.dump(data, f)
    await client.send_file(event.chat_id, file, caption="📁 Full backup")
    os.remove(file)

@client.on(events.NewMessage(pattern='/restore'))
@admin_only
async def restore(event):
    if not event.is_reply:
        await event.reply("❌ Reply to a backup `.json` file.")
        return
    rep = await event.get_reply_message()
    if not rep.file or not rep.file.name.endswith('.json'):
        await event.reply("❌ Invalid file.")
        return
    path = await client.download_media(rep.media)
    try:
        with open(path, "r") as f:
            data = json.load(f)
        global tracked_users, blocked_users, saved_messages, welcome_enabled
        tracked_users = {int(k): int(v) for k, v in data.get("tracked", {}).items()}
        blocked_users = set(int(u) for u in data.get("blocked", []))
        saved_messages = {
            int(k): v for k, v in data.get("messages", {}).items()
        }
        welcome_enabled = data.get("welcome", True)

        # Sync to DB
        conn = sqlite3.connect(str(DB_PATH))
        conn.execute("DELETE FROM users")
        conn.execute("DELETE FROM messages")
        conn.commit()
        for uid, hsh in tracked_users.items():
            save_user(uid, hsh, blocked=(uid in blocked_users))
        for step, d in saved_messages.items():
            save_message_step(step, d)
        set_setting("welcome_enabled", "1" if welcome_enabled else "0")
        conn.close()

        await event.reply(f"✅ Restored: {len(tracked_users)} users, {len(blocked_users)} blocked, {len(saved_messages)} messages.")
    except Exception as e:
        await event.reply(f"❌ Restore failed: {e}")
    finally:
        if os.path.exists(path):
            os.remove(path)

# ------------------ CLEANUP BLOCKED ------------------
@client.on(events.NewMessage(pattern=r'^(/cleanup|🧹 Cleanup)$'))
@admin_only
async def cleanup(event):
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.cursor()
    cur.execute("DELETE FROM users WHERE blocked=1")
    conn.commit()
    conn.close()
    # Also clear from memory
    for uid in blocked_users:
        tracked_users.pop(uid, None)
    blocked_users.clear()
    await event.reply("🧹 Blocked users removed from database and memory.")

# ------------------ PERIODIC AUTO‑BACKUP ------------------
async def periodic_backup():
    while True:
        await asyncio.sleep(3600)  # every hour
        try:
            data = {
                "tracked": tracked_users,
                "blocked": list(blocked_users),
                "messages": {str(k): v for k, v in saved_messages.items()},
                "welcome": welcome_enabled
            }
            with open("auto_backup.json", "w") as f:
                json.dump(data, f)
            logger.info("Auto backup saved.")
        except Exception as e:
            logger.error(f"Auto backup error: {e}")

# ------------------ MAIN ------------------
async def main():
    init_db()
    load_from_db()
    await start_web()
    asyncio.create_task(periodic_backup())
    logger.info("Bot is running...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user.")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
