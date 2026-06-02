import os
import asyncio
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from dotenv import load_dotenv
from telethon import TelegramClient, events
from telethon.tl.types import UpdateBotChatInviteRequester

# 1. Setup Advanced Logging
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger("MyTelegramBot")

load_dotenv()

# 2. Fetch Environment Variables
API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", 10000))
ADMIN_ID = int(os.environ.get("ADMIN_ID", 0)) # Apni Telegram ID yahan daalna env me

# 3. Global Variables (Default Messages & Files)
# Ye bot restart hone par default par aa jayega, par live bot me admin kabhi bhi badal sakta hai
welcome_text = "⏳ **Admin is busy right now!**\n\nPlease wait for approval... Thank you for your patience! 😊"
target_file = 'YOUR_FILE_NAME.pdf'  # Local file name jo GitHub par hai
stored_file_id = None                # Admin agar nayi file bhejega toh uski Telegram ID yahan save hogi

# Create Event Loop for Telethon
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

# Initialize Telegram Client
client = TelegramClient('bot_session', API_ID, API_HASH, loop=loop)

# 4. Background Web Server (Render Health Check)
class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is Live and Running with Admin Panel!")
    def log_message(self, format, *args):
        pass

def start_web_server():
    try:
        server = HTTPServer(("0.0.0.0", PORT), KeepAliveHandler)
        logger.info(f"🌐 Background web server listening on PORT {PORT}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"❌ Web server failed to start: {e}")

# 5. ADMIN COMMANDS PANEL
# Command to change text message: /setmsg <new message>
@client.on(events.NewMessage(pattern='/setmsg (.+)', incoming=True))
async def change_message(event):
    global welcome_text
    if event.sender_id == ADMIN_ID:
        welcome_text = event.pattern_match.group(1)
        await event.reply("✅ **Welcome message successfully update ho gaya hai!**")
        logger.info("Admin updated the welcome text.")
    else:
        await event.reply("❌ Aap is bot ke admin nahi hain!")

# Command to change file: Send any file/document and reply /setfile to it
@client.on(events.NewMessage(pattern='/setfile', incoming=True))
async def change_file(event):
    global stored_file_id
    if event.sender_id == ADMIN_ID:
        if event.is_reply:
            reply_msg = await event.get_reply_message()
            if reply_msg.file:
                # Telegram File ID ko save kar rahe hain taaki Render delete na kar paye
                stored_file_id = reply_msg.media
                await event.reply("✅ **Nayi file successfully set ho gayi hai! Ab users ko yahi file jayegi.**")
                logger.info("Admin updated the target file using Telegram Media ID.")
            else:
                await event.reply("❌ Jis message ka aapne reply kiya hai usme koi file nahi hai!")
        else:
            await event.reply("ℹ️ **Kaise use karein:** Pehle bot ko wo file bhejin, phir us file par reply karke `/setfile` likhein.")
    else:
        await event.reply("❌ Aap is bot ke admin nahi hain!")

# Command to check current settings: /status
@client.on(events.NewMessage(pattern='/status', incoming=True))
async def bot_status(event):
    if event.sender_id == ADMIN_ID:
        file_status = "Custom Admin File (Telegram ID)" if stored_file_id else f"Default Local File ({target_file})"
        status_msg = (
            "⚙️ **Current Bot Status:**\n\n"
            f"💬 **Message:**\n{welcome_text}\n\n"
            f"📂 **File Type:** {file_status}"
        )
        await event.reply(status_msg)

# 6. USER JOIN HANDLERS (Sending Text + File)
async def send_welcome_package(user_id, first_name):
    try:
        # 1. Send Text Message
        await client.send_message(user_id, welcome_text)
        
        # 2. Send File (Check if Admin changed it via Telegram ID or using local file)
        file_to_send = stored_file_id if stored_file_id else target_file
        await client.send_file(user_id, file_to_send, caption="Here is your requested file! 📂")
        
        logger.info(f"✅ Success: Sent Message + File to {first_name} (ID: {user_id})")
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

# 7. Main Bot Function
async def main():
    threading.Thread(target=start_web_server, daemon=True).start()
    logger.info("🤖 Starting the Telegram Bot with Admin Controls...")
    await client.start(bot_token=BOT_TOKEN)
    logger.info("✅ Bot is online! Admins can use /setmsg and /setfile commands in bot DM.")
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("🛑 Bot stopped manually.")
    except Exception as e:
        logger.critical(f"💀 Fatal error: {e}")
