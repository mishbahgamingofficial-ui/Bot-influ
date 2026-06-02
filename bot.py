import os
import asyncio
from dotenv import load_dotenv
from pyrogram import Client
from pyrogram.handlers import ChatJoinRequestHandler

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
    try:
        await client.send_message(
            join_request.from_user.id,
            "👋 Hi!\n\nThanks for joining our channel!"
        )
        print(f"Message sent to {join_request.from_user.id}")
    except Exception as e:
        print(f"Error: {e}")

async def main():
    app.add_handler(ChatJoinRequestHandler(join_request_handler))
    print("Bot Started...")
    async with app:
        await app.start()
        print("Bot is running...")
        await asyncio.Event().wait()

if __name__ == "__main__":
    asyncio.run(main())
