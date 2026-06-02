import os
import asyncio
from dotenv import load_dotenv
from telethon import TelegramClient, events
from http.server import BaseHTTPRequestHandler, HTTPServer
import threading

load_dotenv()

API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", 10000))

# Create and set event loop
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

client = TelegramClient('bot_session', API_ID, API_HASH, loop=loop)

# Simple HTTP handler to reply "OK" to Render's port scanner
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-type", "text/plain")
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

def run_health_check_server():
    server = HTTPServer(("0.0.0.0", PORT), HealthCheckHandler)
    print(f"🌐 Health check server listening on port {PORT}")
    server.serve_forever()

@client.on(events.ChatAction())
async def handle_join_request(event):
    try:
        if event.user_joined or event.user_added:
            user = await event.get_user()
            await client.send_message(
                user.id,
                "⏳ Admin is busy right now!\n\nPlease wait for approval... Thank you for your patience! 😊"
            )
            print(f"✅ Auto-reply sent to {user.first_name} (ID: {user.id})")
    except Exception as e:
        print        if event.user_joined or event.user_added:
            user = await event.get_user()
            await client.send_message(
                user.id,
                "⏳ Admin is busy right now!\n\nPlease wait for approval... Thank you for your patience! 😊"
            )
            print(f"✅ Auto-reply sent to {user.first_name} (ID: {user.id})")
    except Exception as e:
        print(f"❌ Error: {e}")

async def main():
    # Start the fake web server in a background thread so Render sees an open port
    threading.Thread(target=run_health_check_server, daemon=True).start()

    print("🤖 Bot Starting...")
    await client.start(bot_token=BOT_TOKEN)
    print("✅ Bot is running and listening for join requests...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    loop.run_until_complete(main())    await client.start(bot_token=BOT_TOKEN)
    print("✅ Bot is running and listening for join requests...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    # 3. Use the loop we created at the top to run the program
    loop.run_until_complete(main())
