import os
import asyncio
import logging
import threading
import re
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import UpdateBotChatInviteRequester

# Setup Advanced Logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger("MyTelegramBot")

load_dotenv()

# Fetch Environment Variables
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", 10000))

# Multi-Admin IDs (Comma separated like: 12345,67890)
raw_admins = os.environ.get("ADMIN_IDS", "0")
ADMIN_IDS = [int(admin.strip()) for admin in raw_admins.split(",") if admin.strip().isdigit()]

# Global dictionary to store sequence of messages
saved_messages = {}

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
        self.wfile.write(b"Bot Permanent Cloud DB System is Live!")
    def log_message(self, format, *args):
        pass

def start_web_server():
    try:
        server = HTTPServer(("0.0.0.0", PORT), KeepAliveHandler)
        server.serve_forever()
    except Exception as e:
        logger.error(f"❌ Web server failed to start: {e}")

# ==========================================
# AUTO-RESTORE SYSTEM (For Settings)
# ==========================================
async def restore_settings_from_history():
    global saved_messages
    logger.info("🔄 History se purani settings restore ki ja rahi hain...")
    for admin_id in ADMIN_IDS:
        try:
            async for msg in client.iter_messages(admin_id, limit=100):
                if msg.text and msg.text.startswith('/setmsg'):
                    match = re.match(r'^/setmsg(\d+)(?:\s+(.+))?', msg.text)
                    if match:
                        step_num = int(match.group(1))
                        extra_text = match.group(2)
                        if step_num in saved_messages:
                            continue
                        if msg.is_reply:
                            reply_msg = await msg.get_reply_message()
                            if reply_msg:
                                saved_messages[step_num] = {'type': 'forward', 'msg_id': reply_msg.id, 'from_chat': admin_id}
                        elif extra_text and extra_text.strip():
                            saved_messages[step_num] = {'type': 'text', 'text': extra_text.strip()}
        except Exception as e:
            logger.error(f"⚠️ History read error: {e}")

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

# 🔥 PERMANENT BROADCAST SYSTEM (Reads Admin DM History)
@client.on(events.NewMessage(pattern='/broadcast', incoming=True))
async def broadcast_to_all(event):
    if event.sender_id not in ADMIN_IDS:
        return
        
    status_msg = await event.reply("⏳ Cloud Database (Chat History) se bando ki list nikali ja rahi hai...")
    
    # Set use karenge taaki duplicate IDs remove ho jayein
    tracked_users = set()
    
    try:
        # Bot admin ke DM ke pichle 3000 messages me se saare users nikalega
        async for msg in client.iter_messages(event.chat_id, limit=3000):
            if msg.text and '#usertrack' in msg.text:
                match = re.search(r'ID:\s*(\d+)', msg.text)
                if match:
                    tracked_users.add(int(match.group(1)))
                    
        if not tracked_users:
            await status_msg.edit("❌ Database me koi bhi user nahi mila! Pehle naye bando ko request bhejne dein.")
            return
            
        await status_msg.edit(f"📢 **Total {len(tracked_users)} Users mile.** Broadcast shuru ho raha hai...")
        
        success_count = 0
        fail_count = 0
        
        for user_id in tracked_users:
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
                await asyncio.sleep(0.5) # Anti-flood gap
            except Exception:
                fail_count += 1
                
        await status_msg.edit(f"📢 **Broadcast Report:**\n\n✅ Sahi se gaya: {success_count} bando ko\n❌ Fail hua: {fail_count} bando ka")
    except Exception as e:
        await status_msg.edit(f"❌ Error: {e}")

@client.on(events.NewMessage(pattern='/status', incoming=True))
async def bot_status(event):
    if event.sender_id in ADMIN_IDS:
        status_text = "⚙️ **Bot Sequences Active:**\n"
        for step in sorted(saved_messages.keys()):
            status_text += f"🔹 Step {step} -> `{saved_messages[step]['type'].upper()}`\n"
        await event.reply(status_text if saved_messages else "⚙️ Bot sequence khali hai.")

# ==========================================
# AUTOMATIC USER JOIN SENDER + CLOUD LOGGING
# ==========================================
async def send_welcome_package(user_id, first_name):
    try:
        # 1. Send Sequence to User
        for step in sorted(saved_messages.keys()):
            item = saved_messages[step]
            if item['type'] == 'forward':
                await client.forward_messages(user_id, item['msg_id'], item['from_chat'])
            elif item['type'] == 'text':
                await client.send_message(user_id, item['text'])
            await asyncio.sleep(1)
            
        # 2. 🔥 CLOUD DB LOG: Send user details to primary admin's DM permanently
        if ADMIN_IDS:
            log_text = f"👤 **New User Interaction Log:**\nName: {first_name}\nID: {user_id}\n\n#usertrack"
            await client.send_message(ADMIN_IDS[0], log_text)
            
        logger.info(f"✅ Sequence sent & Logged for {first_name}")
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
    await restore_settings_from_history()
    logger.info("✅ Bot is online with Permanent Cloud DB!")
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        loop.run_until_complete(main())
    except Exception:
        pass
