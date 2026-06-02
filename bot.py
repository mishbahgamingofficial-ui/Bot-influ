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
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0))

# ==========================================
# STARTING ME SAB KHALI (No Defaults)
# ==========================================
welcome_text = None
stored_file_id = None
stored_file_caption = None

# Create Event Loop for Telethon
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
client = TelegramClient('bot_session', API_ID, API_HASH, loop=loop)

# Background Web Server (Render Health Check Trick)
class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is Live and waiting for Admin commands!")
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

@client.on(events.NewMessage(pattern=r'^/setmsg(?: (.*))?', incoming=True))
async def change_message(event):
    global welcome_text
    if event.sender_id == ADMIN_ID:
        new_text = event.pattern_match.group(1)
        if new_text and new_text.strip():
            welcome_text = new_text.strip()
            await event.reply("✅ **Text message set ho gaya hai!**\nAb users ko ye message jayega.")
        else:
            welcome_text = None
            await event.reply("✅ **Text message remove kar diya gaya hai!**\nAb alag se koi text nahi jayega.")
    else:
        await event.reply("❌ Aap is bot ke admin nahi hain!")

@client.on(events.NewMessage(pattern='/setfile', incoming=True))
async def change_file(event):
    global stored_file_id, stored_file_caption
    if event.sender_id == ADMIN_ID:
        if event.is_reply:
            reply_msg = await event.get_reply_message()
            if reply_msg.media:
                stored_file_id = reply_msg.media
                # Original video/file ka caption yahan copy hoga
                stored_file_caption = reply_msg.text or "" 
                await event.reply("✅ **Nayi file (aur uska original caption) set ho gayi hai!**")
            else:
                await event.reply("❌ Jis message ka aapne reply kiya hai usme koi media/file nahi hai!")
        else:
            # Agar bina reply ke likha toh file hat jayegi
            stored_file_id = None
            stored_file_caption = None
            await event.reply("✅ **File remove kar di gayi hai!**\nAb users ko koi file nahi jayegi.")
    else:
        await event.reply("❌ Aap is bot ke admin nahi hain!")

@client.on(events.NewMessage(pattern='/status', incoming=True))
async def bot_status(event):
    if event.sender_id == ADMIN_ID:
        msg_status = welcome_text if welcome_text else "❌ Koi alag text message set nahi hai"
        file_status = "✅ File set hai (with original caption)" if stored_file_id else "❌ Koi file set nahi hai"
        status_msg = f"⚙️ **Current Bot Status:**\n\n💬 **Message:**\n{msg_status}\n\n📂 **File:** {file_status}"
        await event.reply(status_msg)

# ==========================================
# USER JOIN HANDLERS
# ==========================================
async def send_welcome_package(user_id, first_name):
    try:
        # 1. Agar admin ne text set kiya hai, tabhi bhejo
        if welcome_text:
            await client.send_message(user_id, welcome_text)
        
        # 2. Agar admin ne file set ki hai, tabhi bhejo (original caption ke sath)
        if stored_file_id:
            await client.send_file(user_id, stored_file_id, caption=stored_file_caption)
            
    except Exception as e:
        logger.error(f"❌ Failed to send package to {user_id}: {e}")

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
    logger.info("🤖 Bot Started! Waiting for Admin setup...")
    await client.start(bot_token=BOT_TOKEN)
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        loop.run_until_complete(main())
    except Exception as e:
        pass
