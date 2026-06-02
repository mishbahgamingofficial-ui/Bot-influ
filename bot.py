import os
from pyrogram import Client
from pyrogram.handlers import ChatJoinRequestHandler

API_ID = int(os.environ["API_ID"])
API_HASH = os.environ["API_HASH"]
BOT_TOKEN = os.environ["BOT_TOKEN"]

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

app.add_handler(ChatJoinRequestHandler(join_request_handler))

print("Bot Started...")
app.run()
