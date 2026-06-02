import os
import asyncio
from dotenv import load_dotenv
from pyrogram import Client, filters
from pyrogram.handlers import ChatJoinRequestHandler, MessageHandler

load_dotenv()

API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

app = Client(
    "join_bot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

async def join_request_handler(client, join_request):
    """Handle new join requests to the channel"""
    try:
        await client.send_message(
            join_request.from_user.id,
            "👋 Hi!\n\nThanks for joining our channel!"
        )
        print(f"Join request message sent to {join_request.from_user.id}")
    except Exception as e:
        print(f"Error sending join message: {e}")

async def message_handler(client, message):
    """Handle regular messages in the channel"""
    try:
        # Respond to /start command
        if message.text and message.text.startswith("/start"):
            await message.reply_text("👋 Welcome to our bot!")
            print(f"Replied to /start command from {message.from_user.id}")
        
        # Respond to /help command
        elif message.text and message.text.startswith("/help"):
            await message.reply_text("📋 Available commands:\n/start - Start the bot\n/help - Show this message")
            print(f"Replied to /help command from {message.from_user.id}")
    
    except Exception as e:
        print(f"Error handling message: {e}")

async def main():
    # Add handlers
    app.add_handler(ChatJoinRequestHandler(join_request_handler))
    app.add_handler(MessageHandler(message_handler, filters.command))
    
    print("Bot Started...")
    async with app:
        await app.start()
        print("✅ Bot is running and listening for messages...")
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
