import os
import asyncio
import logging
import threading
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

# DO YA ZYADA ADMINS KE LIYE LOGIC (Comma separated IDs like: 12345,67890)
raw_admins = os.environ.get("ADMIN_IDS", "0")
ADMIN_IDS = [int(admin.strip()) for admin in raw_admins.split(",") if admin.strip().isdigit()]

# Global dictionary to store sequence of messages
saved_messages = {}

# Create Event Loop for Telethon
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
client = TelegramClient('bot_session', API_ID, API_HASH, loop=loop)

# Background Web Server (Render Health Check)
class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot Multi-Admin System is Live!")
    def log_message(self, format, *args):
        pass

def start_web_server():
    try:
        server = HTTPServer(("0.0.0.0", PORT), KeepAliveHandler)
        server.serve_forever()
    except Exception as e:
        logger.error(f"❌ Web server failed to start: {e}")

# ==========================================
# ADVANCED DYNAMIC ADMIN COMMANDS (Multi-Admin Check)
# ==========================================

@client.on(events.NewMessage(pattern=r'^/setmsg(\d+)(?:\s+(.+))?', incoming=True))
async def set_message_step(event):
    global saved_messages
    # CHECK: Agar sender ki ID admin list me hai toh hi allow karega
    if event.sender_id not in ADMIN_IDS:
        await event.reply("❌ Aap is bot ke admin nahi hain!")
        return
    
    step_num = int(event.pattern_match.group(1))
    extra_text = event.pattern_match.group(2)
    
    if event.is_reply:
        reply_msg = await event.get_reply_message()
        saved_messages[step_num] = {
            'type': 'forward',
            'msg_id': reply_msg.id,
            'from_chat': event.chat_id
        }
        await event.reply(f"✅ **Message {step_num} set ho gaya hai (Forward Mode)!**")
        
    elif extra_text and extra_text.strip():
        saved_messages[step_num] = {
            'type': 'text',
            'text': extra_text.strip()
        }
        await event.reply(f"✅ **Message {step_num} set ho gaya hai (Text Mode)!**")
        
    else:
        if step_num in saved_messages:
            del saved_messages[step_num]
            await event.reply(f"✅ **Message {step_num} ko sequence se remove kar diya gaya hai!**")
        else:
            await event.reply(f"❌ Message {step_num} pehle se hi khali hai.")

@client.on(events.NewMessage(pattern='/status', incoming=True))
async def bot_status(event):
    if event.sender_id in ADMIN_IDS:
        if not saved_messages:
            await event.reply("⚙️ **Bot Status:**\n❌ Abhi koi bhi message sequence set nahi hai.")
            return
        
        status_text = "⚙️ **Current Bot Message Sequence:**\n\n"
        for step in sorted(saved_messages.keys()):
            m_type = saved_messages[step]['type']
            status_text += f"🔹 **Step {step}** -> Type: `{m_type.upper()}`\n"
        await event.reply(status_text)

# ==========================================
# AUTOMATIC USER JOIN SEQUENCE SENDER
# ==========================================
async def send_welcome_package(user_id, first_name):
    try:
        if not saved_messages:
            return
        
        for step in sorted(saved_messages.keys()):
            item = saved_messages[step]
            if item['type'] == 'forward':
                await client.forward_messages(user_id, item['msg_id'], item['from_chat'])
            elif item['type'] == 'text':
                await client.send_message(user_id, item['text'])
            await asyncio.sleep(1)
            
        logger.info(f"✅ Success: Sent full sequence to {first_name}")
    except Exception as e:
        logger.error(f"❌ Failed to send sequence to {user_id}: {e}")

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
    logger.info("🤖 Bot Started with Multi-Admin Support!")
    await client.start(bot_token=BOT_TOKEN)
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        loop.run_until_complete(main())
    except Exception as e:
        pass
