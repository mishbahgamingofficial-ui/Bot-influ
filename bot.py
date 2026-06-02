from pyrogram import Client
from pyrogram.handlers import ChatJoinRequestHandler

API_ID = 24223583      # apna api_id
API_HASH = "your_api_hash"
BOT_TOKEN = "your_bot_token"

app = Client(
    "joinbot",
    api_id=API_ID,
    api_hash=API_HASH,
    bot_token=BOT_TOKEN
)

async def join_request_handler(client, join_request):
    try:
        await client.send_message(
            join_request.from_user.id,
            "Hi 👋\n\nThanks for joining our channel!"
        )
    except Exception as e:
        print(e)

app.add_handler(ChatJoinRequestHandler(join_request_handler))

app.run()
