import os
import asyncio
import logging
import json
from aiohttp import web
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import UpdateBotChatInviteRequester
from telethon.errors import UserIsBlockedError, FloodWaitError
from telethon.sessions import MemorySession

# Setup Advanced Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger("ProTelegramBot")

load_dotenv()

# Fetch Environment Variables safely
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", 10000))

# Multi-Admin IDs
raw_admins = os.environ.get("ADMIN_IDS", "0")
ADMIN_IDS = [int(admin.strip()) for admin in raw_admins.split(",") if admin.strip().isdigit()]

# In-Memory Database
saved_messages = {}
tracked_users = set()  # Active Users
blocked_users = set()  # Blocked Users

# ==========================================
# 🔥 PYTHON 3.14 FIX: Manually Create Loop Early
# ==========================================
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

# MemorySession ensures Render doesn't throw SQLite database lock errors!
client = TelegramClient(MemorySession(), API_ID, API_HASH, loop=loop)

# ==========================================
# ASYNC WEB SERVER (Keeps Render Alive)
# ==========================================
async def web_handler(request):
    return web.Response(text="🟢 Pro Bot Tracking System is Live & Running!")

async def start_web_server():
    try:
        app = web.Application()
        app.add_routes([web.get('/', web_handler)])
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        logger.info(f"🌐 Async Web server started on port {PORT}")
    except Exception as e:
        logger.error(f"❌ Web server failed to start: {e}")

# ==========================================
# ADMIN COMMANDS PANEL
# ==========================================

@client.on(events.NewMessage(pattern=r'^/setmsg(\d+)(?:\s+(.+))?', incoming=True))
async def set_message_step(event):
    if event.sender_id not in ADMIN_IDS:
        return
    
    step_num = int(event.pattern_match.group(1))
    extra_text = event.pattern_match.group(2)
    
    if event.is_reply:
        reply_msg = await event.get_reply_message()
        saved_messages[step_num] = {'type': 'forward', 'msg_id': reply_msg.id, 'from_chat': event.chat_id}
        await event.reply(f"✅ **Message Sequence `{step_num}` Saved! (Forward)**")
    elif extra_text and extra_text.strip():
        saved_messages[step_num] = {'type': 'text', 'text': extra_text.strip()}
        await event.reply(f"✅ **Message Sequence `{step_num}` Saved! (Text)**")
    else:
        if step_num in saved_messages:
            del saved_messages[step_num]
            await event.reply(f"🗑 **Message Sequence `{step_num}` Removed.**")
        else:
            await event.reply("⚠️ Pura command likhein. Ya toh reply karein, ya aage text likhein.")

@client.on(events.NewMessage(pattern='/users', incoming=True))
async def show_users_stats(event):
    if event.sender_id not in ADMIN_IDS:
        return
    
    stats_msg = (
        "📊 **Pro Bot User Statistics:**\n\n"
        f"🟢 **Active Users:** `{len(tracked_users)}`\n"
        f"🔴 **Blocked Users:** `{len(blocked_users)}`\n\n"
        f"💡 *Pro Tip:* Use `/backup` frequently to save these users."
    )
    await event.reply(stats_msg)

# ==========================================
# BACKUP & RESTORE SYSTEM
# ==========================================

@client.on(events.NewMessage(pattern='/backup', incoming=True))
async def backup_data(event):
    if event.sender_id not in ADMIN_IDS:
        return
    
    backup_dict = {
        "tracked": list(tracked_users),
        "blocked": list(blocked_users)
    }
    
    with open("users_backup.json", "w") as f:
        json.dump(backup_dict, f)
        
    await client.send_file(
        event.chat_id, 
        "users_backup.json", 
        caption="📁 **Database Backup Completed.**\nSafe rakhna is file ko!"
    )
    os.remove("users_backup.json")

@client.on(events.NewMessage(pattern='/restore', incoming=True))
async def restore_data(event):
    if event.sender_id not in ADMIN_IDS:
        return
        
    if event.is_reply:
        reply_msg = await event.get_reply_message()
        if reply_msg.file and reply_msg.file.name.endswith('.json'):
            file_path = await client.download_media(reply_msg.media)
            try:
                with open(file_path, "r") as f:
                    data = json.load(f)
                    
                    if isinstance(data, dict):
                        tracked_users.update(data.get("tracked", []))
                        blocked_users.update(data.get("blocked", []))
                    elif isinstance(data, list):
                        tracked_users.update(data)
                        
                await event.reply(f"✅ **Database Restored Successfully!**\n\n🟢 Active: `{len(tracked_users)}`\n🔴 Blocked: `{len(blocked_users)}`")
            except Exception as e:
                await event.reply(f"❌ Restore Failed. Corrupt file? Error: {e}")
            finally:
                if os.path.exists(file_path):
                    os.remove(file_path)
        else:
            await event.reply("❌ Reply to a valid `.json` backup file bro.")

# ==========================================
# ANTI-BAN BROADCAST SYSTEM
# ==========================================

@client.on(events.NewMessage(pattern='/broadcast', incoming=True))
async def broadcast_to_all(event):
    if event.sender_id not in ADMIN_IDS:
        return
        
    if not tracked_users:
        await event.reply("❌ No active users! Restore data first using `/restore`.")
        return
        
    status_msg = await event.reply(f"📢 **Starting Broadcast to {len(tracked_users)} users...**\n⏳ Kripya pratiksha karein...")
    
    success_count = 0
    fail_count = 0
    
    # Snapshot of tracked users to safely modify sets during loop
    current_users = list(tracked_users)
    
    for user_id in current_users:
        try:
            if event.is_reply:
                reply_msg = await event.get_reply_message()
                await client.forward_messages(user_id, reply_msg.id, event.chat_id)
            else:
                broadcast_text = event.text.replace('/broadcast', '').strip()
                if broadcast_text:
                    await client.send_message(user_id, broadcast_text)
                else:
                    await status_msg.edit("❌ You must provide text or reply to a message!")
                    return
            
            success_count += 1
            if user_id in blocked_users:
                blocked_users.remove(user_id)
                
            await asyncio.sleep(0.3) # Keep limits safe
            
        except FloodWaitError as e:
            # 🛑 ANTI-BAN: Agar Telegram limit lagata hai, toh bot automatically wait karega
            logger.warning(f"FloodWaitError: Sleeping for {e.seconds} seconds.")
            await asyncio.sleep(e.seconds)
            
        except UserIsBlockedError:
            fail_count += 1
            blocked_users.add(user_id)
            if user_id in tracked_users:
                tracked_users.remove(user_id)
                
        except Exception as e:
            fail_count += 1
            logger.error(f"Broadcast error for {user_id}: {e}")
            
    await status_msg.edit(f"📢 **Broadcast Finished!**\n\n✅ Delivered: `{success_count}`\n🔴 Blocked/Failed: `{fail_count}`")

@client.on(events.NewMessage(pattern='/status', incoming=True))
async def bot_status(event):
    if event.sender_id in ADMIN_IDS:
        status_text = f"⚙️ **System Status:**\n👥 **Active:** `{len(tracked_users)}`\n🛑 **Blocked:** `{len(blocked_users)}`\n\n**Active Sequences:**\n"
        for step in sorted(saved_messages.keys()):
            status_text += f"🔹 Step {step} ➜ `{saved_messages[step]['type'].upper()}`\n"
        await event.reply(status_text if saved_messages else "⚙️ System Online. No sequences set.")

# ==========================================
# AUTOMATIC WELCOME & TRACKING
# ==========================================
async def send_welcome_package(user_id, first_name):
    try:
        tracked_users.add(user_id)
        if user_id in blocked_users:
            blocked_users.remove(user_id)
        
        if saved_messages:
            for step in sorted(saved_messages.keys()):
                item = saved_messages[step]
                if item['type'] == 'forward':
                    await client.forward_messages(user_id, item['msg_id'], item['from_chat'])
                elif item['type'] == 'text':
                    await client.send_message(user_id, item['text'])
                await asyncio.sleep(0.5)
            
        logger.info(f"✅ Sequence delivered to: {first_name} ({user_id})")
    except UserIsBlockedError:
        logger.warning(f"User {user_id} blocked the bot instantly.")
    except Exception as e:
        logger.error(f"Failed welcome sequence: {e}")

@client.on(events.ChatAction())
async def handle_chat_action(event):
    if event.user_joined or event.user_added:
        user = await event.get_user()
        await send_welcome_package(user.id, user.first_name)

@client.on(events.Raw)
async def raw_join_request_handler(event):
    if isinstance(event, UpdateBotChatInviteRequester):
        await send_welcome_package(event.user_id, "Join Requester")

# ==========================================
# MAIN EXECUTION
# ==========================================
async def main():
    # Start web server alongside the bot asynchronously
    await start_web_server()
    logger.info("🤖 Starting Telegram Bot...")
    
    await client.start(bot_token=BOT_TOKEN)
    logger.info("✅ Bot Online and running cleanly!")
    
    # Run the client until it's disconnected
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        # Use the loop we safely created at the very top
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Bot manually stopped.")
    except Exception as e:
        logger.error(f"Critical Runtime Error: {e}")
