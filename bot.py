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

# 3. Create Event Loop for Telethon
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

# 4. Initialize Telegram Client
client = TelegramClient('bot_session', API_ID, API_HASH, loop=loop)

# 5. Background Web Server (Render Health Check Trick)
class KeepAliveHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is Live and Running!")
        
    def log_message(self, format, *args):
        # Keeps the Render logs clean by hiding constant ping messages
        pass

def start_web_server():
    try:
        server = HTTPServer(("0.0.0.0", PORT), KeepAliveHandler)
        logger.info(f"🌐 Background web server listening on PORT {PORT}")
        server.serve_forever()
    except Exception as e:
        logger.error(f"❌ Web server failed to start: {e}")

# 6. Event Listener: When a user directly joins or is added
@client.on(events.ChatAction())
async def handle_join_request(event):
    """Detects when a user joins the channel and sends a DM."""
    try:
        if event.user_joined or event.user_added:
            user = await event.get_user()
            
            welcome_msg = (
                "⏳ **Admin is busy right now!**\n\n"
                "Please wait for approval... Thank you for your patience! 😊"
            )
            
            await client.send_message(user.id, welcome_msg)
            logger.info(f"✅ Success: Sent DM to joined user {user.first_name} (ID: {user.id})")
            
    except Exception as e:
        logger.error(f"❌ Failed to handle user join: {e}")

# 7. Event Listener: When a user sends a "Join Request" (Pending Approval)
@client.on(events.Raw)
async def raw_join_request_handler(event):
    """Detects when a user clicks 'Request to Join' via an invite link."""
    if isinstance(event, UpdateBotChatInviteRequester):
        try:
            user_id = event.user_id
            
            welcome_msg = (
                "⏳ **Admin is busy right now!**\n\n"
                "Your request has been received. Please wait for approval... Thank you for your patience! 😊"
            )
            
            await client.send_message(user_id, welcome_msg)
            logger.info(f"✅ Success: Sent DM to Join Requester (ID: {user_id})")
            
        except Exception as e:
            logger.error(f"❌ Failed to send DM to Join Requester: {e}")

# 8. Main Bot Function
async def main():
    # Start the web server in a background thread
    threading.Thread(target=start_web_server, daemon=True).start()
    
    logger.info("🤖 Starting the Telegram Bot...")
    await client.start(bot_token=BOT_TOKEN)
    
    logger.info("✅ Bot is online, watching the channel, and ready to send DMs!")
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("🛑 Bot stopped manually by user.")
    except Exception as e:
        logger.critical(f"💀 Fatal error occurred: {e}")
