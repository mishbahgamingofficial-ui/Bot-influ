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
from telethon.tl.types import UpdateBotChatInviteRequester, InputPeerUser
from telethon.errors import (
    UserIsBlockedError,
    FloodWaitError,
    PeerIdInvalidError
)

# ------------------ ENVIRONMENT ------------------
load_dotenv()
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("ProBot")

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

# ------------------ DATABASE ------------------
DB_PATH = Path("bot_data.db")

tracked_users: dict[int, int] = {}      # user_id -> access_hash
blocked_users: set[int] = set()
saved_messages: dict[int, dict] = {}    # step -> {"type","text"/"msg_id","from_chat"}
welcome_enabled = True

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
    cur.execute("SELECT user_id, access_hash, blocked FROM users")
    for uid, hsh, blk in cur.fetchall():
        tracked_users[uid] = hsh
        if blk:
            blocked_users.add(uid)
    cur.execute("SELECT step, msg_type, text, msg_id, from_chat FROM messages")
    for step, mtype, text, mid, fchat in cur.fetchall():
        saved_messages[step] = {
            "type": mtype,
            "text": text,
            "msg_id": mid,
            "from_chat": fchat
        }
    cur.execute("SELECT value FROM settings WHERE key='welcome_enabled'")
    row = cur.fetchone()
    if row:
        welcome_enabled = row[0] == "1"
    conn.close()
    logger.info(f"Loaded {len(tracked_users)} users, {len(blocked_users)} blocked, {len(saved_messages)} messages")

def save_user(uid: int, access_hash: int, blocked: bool = False):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute(
        "INSERT OR REPLACE INTO users (user_id, access_hash, blocked, joined_date) VALUES (?,?,?,?)",
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
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)", (key, value))
    conn.commit()
    conn.close()

# ------------------ CLIENT ------------------
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
            await event.reply("❌ Admin only command.")
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
            logger.warning(f"Cannot get access_hash for {uid}")
            return

    tracked_users[uid] = access_hash
    save_user(uid, access_hash, blocked=False)
    blocked_users.discard(uid)

    if not welcome_enabled or uid in ADMIN_IDS:
        return

    try:
        for step in sorted(saved_messages.keys()):
            item = saved_messages[step]
            if item['type'] == 'forward':
                await client.forward_messages(uid, item['msg_id'], item['from_chat'])
            else:
                await client.send_message(uid, item['text'])
            await asyncio.sleep(0.5)
        logger.info(f"Welcome sent to {uid}")
    except UserIsBlockedError:
        blocked_users.add(uid)
        save_user(uid, access_hash, blocked=True)
        logger.info(f"User {uid} blocked during welcome")
    except FloodWaitError as e:
        logger.warning(f"Flood wait {e.seconds}s")
        await asyncio.sleep(e.seconds)
    except Exception as e:
        logger.error(f"Welcome error for {uid}: {e}")

@client.on(events.ChatAction())
async def chat_action(event):
    if event.user_joined or event.user_added:
        user = await event.get_user()
        await send_welcome_sequence(user)

@client.on(events.Raw(types=UpdateBotChatInviteRequester))
async def join_request(event):
    try:
        user = await client.get_entity(event.user_id)
        await send_welcome_sequence(user)
    except Exception as e:
        logger.error(f"Join request error: {e}")

# ------------------ ADMIN PANEL ------------------
@client.on(events.NewMessage(pattern='/start'))
async def start(event):
    if event.sender_id in ADMIN_IDS:
        btns = [
            [Button.text("📊 Stats"), Button.text("⚙️ Status")],
            [Button.text("📢 Broadcast"), Button.text("🔢 Set Sequence")],
            [Button.text("📁 Backup"), Button.text("🔄 Restore")],
            [Button.text("🔇 Toggle Welcome"), Button.text("🧹 Cleanup")]
        ]
        await event.reply("👨‍💻 **Admin Panel**", buttons=btns)
    else:
        await event.reply("👋 Hello! I'm a welcome & broadcast bot.", buttons=Button.clear())

# ------------------ STATS / STATUS ------------------
@client.on(events.NewMessage(pattern=r'^(/stats|📊 Stats)$'))
@admin_only
async def stats(event):
    msg = (
        f"**📊 Statistics**\n"
        f"👥 Active: `{len(tracked_users)}`\n"
        f"🚫 Blocked: `{len(blocked_users)}`\n"
        f"📨 Sequence steps: `{len(saved_messages)}`\n"
        f"🔊 Welcome: `{'ON' if welcome_enabled else 'OFF'}`"
    )
    await event.reply(msg)

@client.on(events.NewMessage(pattern=r'^(/status|⚙️ Status)$'))
@admin_only
async def status(event):
    steps = "\n".join(f"  {s}: {d['type']}" for s, d in sorted(saved_messages.items()))
    await event.reply(
        f"⚙️ **Status**\n"
        f"Users: `{len(tracked_users)}`\n"
        f"Blocked: `{len(blocked_users)}`\n"
        f"Sequence:\n{steps if steps else 'none'}"
    )

# ------------------ BROADCAST ------------------
@client.on(events.NewMessage(pattern=r'^/broadcast'))
@admin_only
async def broadcast(event):
    if not tracked_users:
        await event.reply("❌ No users.")
        return

    text = event.text.replace('/broadcast', '').strip()
    if not text and not event.is_reply:
        await event.reply("❌ Reply to a message or provide text.")
        return

    status_msg = await event.reply(f"📢 Broadcasting to {len(tracked_users)}...")
    success = fail = skip = 0
    for uid, access_hash in list(tracked_users.items()):
        if uid in blocked_users:
            skip += 1
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
            await asyncio.sleep(0.3)
        except FloodWaitError as e:
            logger.info(f"FloodWait {e.seconds}s")
            await asyncio.sleep(e.seconds)
        except (UserIsBlockedError, PeerIdInvalidError, ValueError):
            blocked_users.add(uid)
            save_user(uid, access_hash, blocked=True)
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
@client.on(events.NewMessage(pattern=r'^/setmsg(\d+)(?:\s+(.+))?'))
@admin_only
async def setmsg(event):
    step = int(event.pattern_match.group(1))
    extra = event.pattern_match.group(2)
    if event.is_reply:
        reply_msg = await event.get_reply_message()
        data = {"type": "forward", "msg_id": reply_msg.id, "from_chat": event.chat_id}
    elif extra:
        data = {"type": "text", "text": extra.strip()}
    else:
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

# ------------------ WELCOME TOGGLE ------------------
@client.on(events.NewMessage(pattern=r'^(/togglewelcome|🔇 Toggle Welcome)$'))
@admin_only
async def toggle_welcome(event):
    global welcome_enabled
    welcome_enabled = not welcome_enabled
    set_setting("welcome_enabled", "1" if welcome_enabled else "0")
    await event.reply(f"🔊 Welcome sequence is now **{'ON' if welcome_enabled else 'OFF'}**.")

# ------------------ BACKUP / RESTORE (manual) ------------------
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

@client.on(events.NewMessage(pattern=r'^/restore'))
@admin_only
async def restore(event):
    if not event.is_reply:
        await event.reply("❌ Reply to a `.json` backup file.")
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
        saved_messages = {int(k): v for k, v in data.get("messages", {}).items()}
        welcome_enabled = data.get("welcome", True)

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
        await event.reply(f"✅ Restored: {len(tracked_users)} users")
    except Exception as e:
        await event.reply(f"❌ Restore failed: {e}")
    finally:
        if os.path.exists(path):
            os.remove(path)

# ------------------ BUTTON HANDLERS (Backup/Restore now do actions) ------------------
@client.on(events.NewMessage(pattern=r'^(📢 Broadcast|🔢 Set Sequence)$'))
@admin_only
async def helper_buttons(event):
    if event.text == "📢 Broadcast":
        await event.reply("📢 **Broadcast Kaise Karein?**\n\n👉 `/broadcast <text>` ya reply with message.")
    elif event.text == "🔢 Set Sequence":
        await event.reply("🔢 **Sequence Kaise Set Karein?**\n\n👉 `/setmsg1 <text>` ya reply karein.")

@client.on(events.NewMessage(pattern=r'^(📁 Backup|🔄 Restore)$'))
@admin_only
async def backup_restore_buttons(event):
    text = event.text
    if text == "📁 Backup":
        await backup(event)
    elif text == "🔄 Restore":
        # Try auto‑restore from hourly backup
        if os.path.exists("auto_backup.json"):
            try:
                with open("auto_backup.json", "r") as f:
                    data = json.load(f)
                global tracked_users, blocked_users, saved_messages, welcome_enabled
                tracked_users = {int(k): int(v) for k, v in data.get("tracked", {}).items()}
                blocked_users = set(int(u) for u in data.get("blocked", []))
                saved_messages = {int(k): v for k, v in data.get("messages", {}).items()}
                welcome_enabled = data.get("welcome", True)

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
@client.on(events.NewMessage(pattern=r'^(/cleanup|🧹 Cleanup)$'))
@admin_only
async def cleanup(event):
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("DELETE FROM users WHERE blocked=1")
    conn.commit()
    conn.close()
    for uid in blocked_users:
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
    logger.info("Bot running...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped.")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
