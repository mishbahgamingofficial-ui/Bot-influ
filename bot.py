import os
import asyncio
import logging
import threadi
import json
from httpserver import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dote
from telethon import TelegramClient, events
from telet.tl.types import UpdateBotChatInviteRequester
from telethon.errors import UserIsBlockedError  # Blocked users pakadne ke liye

# Setup Advanced Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger("MyTelegramBot")

load_dotenv()

# Fetch Environment Variables
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", 10000))

# Multi-Admin IDs
raw_admins = os.environ.get("ADMIN_IDS", "0")
ADMIN_IDS = [int(admin.strip()) for admin in raw_admins.split(",") if admin.strip().isdigit()]

# In-Memory Database (Resets on Render spin-down, use /backup & /restore!)
saved_messages = {}
tracked_users = set()  # Active Users
blocked_users = set()  # Blocked Users

# Create Event Loop for Telethon
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
client = TelegramClient('bot_session', API_ID, API_HASH, loop=loop)

# Background Web Server for Render
class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot Tracking System is Live!")
    def log_message(self, format, *args):
        pass

def start_web_server():
    try:
        server = HTTPServer(("0.0.0.0", PORT), KeepAliveHandler)
        server.serve_forever()
    except Exception as e:
        logger.error(f"❌ Web server failed to start: {e}")

# ==========================================
# ADMIN COMMANDS PANEL
# ==========================================

@client.on(events.NewMessage(pattern=r'^/setmsg(\d+)(?:\s+(.+))?', incoming=True))
async def set_message_step(event):
    global saved_messages
    if event.sender_id not in ADMIN_IDS:
        return
    
    step_num = int(event.pattern_match.group(1))
    extra_text = event.pattern_match.group(2)
    
    if event.is_reply:
        reply_msg = await event.get_reply_message()
        saved_messages[step_num] = {'type': 'forward', 'msg_id': reply_msg.id, 'from_chat': event.chat_id}
        await event.reply(f"✅ **Message {step_num} save ho gaya!**")
    elif extra_text and extra_text.strip():
        saved_messages[step_num] = {'type': 'text', 'text': extra_text.strip()}
        await event.reply(f"✅ **Message {step_num} (Text) save ho gaya!**")
    else:
        if step_num in saved_messages:
            del saved_messages[step_num]
            await event.reply(f"✅ Message {step_num} remove ho gaya.")

# NAYA COMMAND: /users (Active aur Blocked users dekhne ke liye)
@client.on(events.NewMessage(pattern='/users', incoming=True))
async def show_users_stats(event):
    if event.sender_id not in ADMIN_IDS:
        return
    
    stats_msg = (
        "📊 **Bot User Statistics:**\n\n"
        f"🟢 **Active Users:** {len(tracked_users)}\n"
        f"🔴 **Blocked Users:** {len(blocked_users)}\n\n"
        f"💡 *Note:* Blocked users ka list tabhi update hota hai jab aap `/broadcast` chalate hain aur bot unhe message nahi bhej pata."
    )
    await event.reply(stats_msg)

# ==========================================
# BACKUP & RESTORE SYSTEM (With Blocked Support)
# ==========================================

@client.on(events.NewMessage(pattern='/backup', incoming=True))
async def backup_data(event):
    if event.sender_id not in ADMIN_IDS:
        return
    
    # Dono lists ko ek sath dictionary me save karenge
    backup_dict = {
        "tracked": list(tracked_users),
        "blocked": list(blocked_users)
    }
    
    with open("users_backup.json", "w") as f:
        json.dump(backup_dict, f)
        
    await client.send_file(
        event.chat_id, 
        "users_backup.json", 
        caption="📁 **Here is your updated User Database Backup.**\n\nIsme Active aur Blocked dono users saved hain."
    )
    os.remove("users_backup.json")

@client.on(events.NewMessage(pattern='/restore', incoming=True))
async def restore_data(event):
    global tracked_users, blocked_users
    if event.sender_id not in ADMIN_IDS:
        return
        
    if event.is_reply:
        reply_msg = await event.get_reply_message()
        if reply_msg.file and reply_msg.file.name.endswith('.json'):
            file_path = await client.download_media(reply_msg.media)
            try:
                with open(file_path, "r") as f:
                    data = json.load(f)
                    
                    # Check karo ki naya dict format hai ya purana list format
                    if isinstance(data, dict):
                        tracked_users.update(data.get("tracked", []))
                        blocked_users.update(data.get("blocked", []))
                    elif isinstance(data, list):
                        tracked_users.update(data)  # Purane backup ke liye compatibility
                        
                await event.reply(f"✅ **Database Restored!**\n\n🟢 Active: {len(tracked_users)}\n🔴 Blocked: {len(blocked_users)}")
            except Exception as e:
                await event.reply(f"❌ Failed to restore: {e}")
            finally:
                if os.path.exists(file_path):
                    os.remove(file_path)
        else:
            await event.reply("❌ Please reply to a valid `.json` backup file.")

# ==========================================
# BROADCAST SYSTEM (With Auto Block Detection)
# ==========================================

@client.on(events.NewMessage(pattern='/broadcast', incoming=True))
async def broadcast_to_all(event):
    global tracked_users, blocked_users
    if event.sender_id not in ADMIN_IDS:
        return
        
    if not tracked_users:
        await event.reply("❌ Active database khali hai! Pehle `/restore` karein.")
        return
        
    status_msg = await event.reply(f"📢 **Total {len(tracked_users)} Active Users par broadcast shuru ho raha hai...**")
    
    success_count = 0
    fail_count = 0
    
    # list() use kiya taaki loop ke beech me set size change hone se crash na ho
    for user_id in list(tracked_users):
        try:
            if event.is_reply:
                reply_msg = await event.get_reply_message()
                await client.forward_messages(user_id, reply_msg.id, event.chat_id)
            else:
                broadcast_text = event.text.replace('/broadcast', '').strip()
                if broadcast_text:
                    await client.send_message(user_id, broadcast_text)
                else:
                    await status_msg.edit("❌ Galti: Ya toh reply karke `/broadcast` likhein, ya aage text likhein!")
                    return
            
            success_count += 1
            # Agar koi pehle blocked me tha par ab active ho gaya, toh blocked se hata do
            if user_id in blocked_users:
                blocked_users.remove(user_id)
                
            await asyncio.sleep(0.5) 
            
        except UserIsBlockedError:
            # 🔥 YAHA PE MAGIC HAI: Agar user ne bot ko block kiya hai
            fail_count += 1
            blocked_users.add(user_id)          # Blocked list me daalo
            if user_id in tracked_users:
                tracked_users.remove(user_id)   # Active list se hatao
                
        except Exception:
            fail_count += 1
            
    await status_msg.edit(f"📢 **Broadcast Complete!**\n\n✅ Safal hua: {success_count} bando ko\n❌ Fail hua (Ya Blocked): {fail_count} bando ka\n\n👉 Live stats dekhne ke liye `/users` check karein.")

@client.on(events.NewMessage(pattern='/status', incoming=True))
async def bot_status(event):
    if event.sender_id in ADMIN_IDS:
        status_text = f"⚙️ **Bot Status:**\n👥 **Tracked Active Users:** {len(tracked_users)}\n🛑 **Blocked Users:** {len(blocked_users)}\n\n**Sequences Active:**\n"
        for step in sorted(saved_messages.keys()):
            status_text += f"🔹 Step {step} -> `{saved_messages[step]['type'].upper()}`\n"
        await event.reply(status_text if saved_messages else "⚙️ Bot sequence khali hai.")

# ==========================================
# AUTOMATIC USER JOIN SENDER + TRACKING
# ==========================================
async def send_welcome_package(user_id, first_name):
    global tracked_users, blocked_users
    try:
        # Naya banda aaya matlab active hai, agar pehle blocked me tha toh wahan se hata do
        tracked_users.add(user_id)
        if user_id in blocked_users:
            blocked_users.remove(user_id)
        
        # Send Sequence to User
        if saved_messages:
            for step in sorted(saved_messages.keys()):
                item = saved_messages[step]
                if item['type'] == 'forward':
                    await client.forward_messages(user_id, item['msg_id'], item['from_chat'])
                elif item['type'] == 'text':
                    await client.send_message(user_id, item['text'])
                await asyncio.sleep(1)
            
        logger.info(f"✅ Sequence sent & User Tracked: {first_name}")
    except Exception as e:
        logger.error(f"❌ Failed to execute: {e}")

@client.on(events.ChatAction())
async def handle_chat_action(event):
    if event.user_joined or event.user_added:
        user = await event.get_user()
        await send_welcome_package(user.id, user.first_name)

@client.on(events.Raw)
async def raw_join_request_handler(event):
    if isinstance(event, UpdateBotChatInviteRequester):
        await send_welcome_package(event.user_id, "Join Requester")

# Main Bot Function
async def main():
    threading.Thread(target=start_web_server, daemon=True).start()
    logger.info("🤖 Bot Starting...")
    await client.start(bot_token=BOT_TOKEN)
    logger.info("✅ Bot is online with Active/Block tracking system!")
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        loop.run_until_complete(main())
    except Exception:
        pass
