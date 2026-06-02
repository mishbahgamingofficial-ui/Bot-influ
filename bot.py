import os
import asyncio
from dotenv import load_dotenv
from telethon import TelegramClient, events

load_dotenv()

API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

# 1. Create and set an event loop explicitly before creating the client
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)

# 2. Pass the loop directly to the TelegramClient
client = TelegramClient('bot_session', API_ID, API_HASH, loop=loop)

@client.on(events.ChatAction())
async def handle_join_request(event):
    """Handle user join requests"""
    try:
        # Check if it's a user joining the channel
        if event.user_joined or event.user_added:
            user = await event.get_user()
            # Send welcome message to the user
            await client.send_message(
                user.id,
                "⏳ Admin is busy right now!\n\nPlease wait for approval... Thank you for your patience! 😊"
            )
            print(f"✅ Auto-reply sent to {user.first_name} (ID: {user.id})")
    except Exception as e:
        print(f"❌ Error: {e}")

async def main():
    print("🤖 Bot Starting...")
    await client.start(bot_token=BOT_TOKEN)
    print("✅ Bot is running and listening for join requests...")
    await client.run_until_disconnected()

if __name__ == "__main__":
    # 3. Use the loop we created at the top to run the program
    loop.run_until_complete(main())
