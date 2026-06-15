import os
import asyncio
import logging
import json
from aiohttp import web
from dotenv import load_dotenv
from telethon import TelegramClient, events, Button
from telethon.tl.types import UpdateBotChatInviteRequester, InputPeerUser
from telethon.errors import UserIsBlockedError, FloodWaitError
from telethon.sessions import MemorySession

# ==========================================
# SETUP & CONFIGURATION
# ==========================================

logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', 
    level=logging.INFO
)
logger = logging.getLogger("ProTelegramBot")

load_dotenv()

API_ID = int(os.environ.get("API_ID", 0))
API_HASH = os.environ.get("API_HASH", "")
BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
PORT = int(os.environ.get("PORT", 10000))

raw_admins = os.environ.get("ADMIN_IDS", "0")
ADMIN_IDS = [int(admin.strip()) for admin in raw_admins.split(",") if admin.strip().isdigit()]

saved_messages = {}
# 🔥 BIG CHANGE: tracked_users ab list/set nahi, ek Dictionary (Map) hai!
# Format: {user_id: access_hash}
tracked_users = {}  
blocked_users = set()  

loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
client = TelegramClient(MemorySession(), API_ID, API_HASH, loop=loop)

# ==========================================
# ASYNC WEB SERVER (Keeps Render Alive)
# ==========================================
async def web_handler(request):
    return web.Response(text="🟢 Pro Bot Tracking System is Live & Running!")

async def start_web_server():
    try:
        app = web.Application()
        app.add_routes([web.get('/', web_handler)])
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', PORT)
        await site.start()
        logger.info(f"🌐 Async Web server started on port {PORT}")
    except Exception as e:
        logger.error(f"❌ Web server failed to start: {e}")

# ==========================================
# 💎 EXCLUSIVE ADMIN PANEL KEYBOARD 
# ==========================================

@client.on(events.NewMessage(pattern='/start', incoming=True))
async def start_handler(event):
    # Capture admin's own access_hash just in case
    user = await event.get_sender()
    tracked_users[user.id] = getattr(user, 'access_hash', 0)

    if event.sender_id in ADMIN_IDS:
        admin_keyboard = [
            [Button.text("📊 Users Stats"), Button.text("⚙️ System Status")],
            [Button.text("📢 Broadcast"), Button.text("🔢 Set Sequence")],
            [Button.text("📁 Backup"), Button.text("🔄 Restore")]
        ]
        await event.reply(
            "👨‍💻 **Welcome Admin!**\n\nAapka Control Panel tayyar hai. Niche diye gaye buttons ka use karein:", 
            buttons=admin_keyboard
        )
    else:
        await event.reply(
            "👋 **Hello! Welcome to the bot.**\nMain ek automated system hu.", 
            buttons=Button.clear()
        )

# ==========================================
# ADMIN COMMANDS & BUTTON HANDLERS
# ==========================================

@client.on(events.NewMessage(pattern=r'^(/status|⚙️ System Status)$', incoming=True))
async def bot_status(event):
    if event.sender_id in ADMIN_IDS:
        status_text = f"⚙️ **System Status:**\n👥 **Active Users:** `{len(tracked_users)}`\n🛑 **Blocked Users:** `{len(blocked_users)}`\n\n**Active Sequences:**\n"
        for step in sorted(saved_messages.keys()):
            status_text += f"🔹 Step {step} ➜ `{saved_messages[step]['type'].upper()}`\n"
        await event.reply(status_text if saved_messages else "⚙️ System Online. No sequences set.")

@client.on(events.NewMessage(pattern=r'^(/users|📊 Users Stats)$', incoming=True))
async def show_users_stats(event):
    if event.sender_id in ADMIN_IDS:
        stats_msg = (
            "📊 **Pro Bot User Statistics:**\n\n"
            f"🟢 **Active Users:** `{len(tracked_users)}`\n"
            f"🔴 **Blocked Users:** `{len(blocked_users)}`\n\n"
            f"💡 *Pro Tip:* Use 📁 Backup frequently to save these users."
        )
        await event.reply(stats_msg)

@client.on(events.NewMessage(pattern=r'^(📢 Broadcast|🔢 Set Sequence|🔄 Restore)$', incoming=True))
async def button_instructions(event):
    if event.sender_id not in ADMIN_IDS:
        return
    text = event.text
    if text == "📢 Broadcast":
        await event.reply("📢 **Broadcast Kaise Karein?**\n\n👉 `/broadcast Hello everyone!`\n\nYa fir kisi image/message par reply karke `/broadcast` likhein.")
    elif text == "🔢 Set Sequence":
        await event.reply("🔢 **Sequence Kaise Set Karein?**\n\nType karein: `/setmsg1 <aapka text>`\nYa kisi message/file par reply karke `/setmsg1` likhein.")
    elif text == "🔄 Restore":
        await event.reply("🔄 **Restore Kaise Karein?**\n\nPehle apni `users_backup.json` file yaha bhejein, phir us file par reply karke `/restore` likhein.")

# ==========================================
# SET MESSAGE SEQUENCE LOGIC
# ==========================================

@client.on(events.NewMessage(pattern=r'^/setmsg(\d+)(?:\s+(.+))?', incoming=True))
async def set_message_step(event):
    if event.sender_id not in ADMIN_IDS:
        return
    step_num = int(event.pattern_match.group(1))
    extra_text = event.pattern_match.group(2)
    
    if event.is_reply:
        reply_msg = await event.get_reply_message()
        saved_messages[step_num] = {'type': 'forward', 'msg_id': reply_msg.id, 'from_chat': event.chat_id}
        await event.reply(f"✅ **Message Sequence `{step_num}` Saved! (Forward)**")
    elif extra_text and extra_text.strip():
        saved_messages[step_num] = {'type': 'text', 'text': extra_text.strip()}
        await event.reply(f"✅ **Message Sequence `{step_num}` Saved! (Text)**")
    else:
        if step_num in saved_messages:
            del saved_messages[step_num]
            await event.reply(f"🗑 **Message Sequence `{step_num}` Removed.**")

# ==========================================
# BACKUP & RESTORE SYSTEM (UPDATED FOR HASH)
# ==========================================

@client.on(events.NewMessage(pattern=r'^(/backup|📁 Backup)$', incoming=True))
async def backup_data(event):
    if event.sender_id not in ADMIN_IDS:
        return
    
    backup_dict = {
        "tracked": tracked_users,  # Ab dictionary save hogi
        "blocked": list(blocked_users)
    }
    
    with open("users_backup.json", "w") as f:
        json.dump(backup_dict, f)
        
    await client.send_file(
        event.chat_id, 
        "users_backup.json", 
        caption="📁 **Database Backup Completed.**\nIs naye backup me Access Hashes bhi saved hain!"
    )
    os.remove("users_backup.json")

@client.on(events.NewMessage(pattern='/restore', incoming=True))
async def restore_data(event):
    if event.sender_id not in ADMIN_IDS:
        return
        
    if event.is_reply:
        reply_msg = await event.get_reply_message()
        if reply_msg.file and reply_msg.file.name.endswith('.json'):
            file_path = await client.download_media(reply_msg.media)
            try:
                with open(file_path, "r") as f:
                    data = json.load(f)
                    
                    if isinstance(data, dict):
                        # Restore logic changed to handle dictionaries
                        tracked_data = data.get("tracked", {})
                        if isinstance(tracked_data, dict):
                            # Naya format: {id: hash}
                            for uid, hsh in tracked_data.items():
                                tracked_users[int(uid)] = int(hsh)
                        elif isinstance(tracked_data, list):
                            # Purana format fallback (might still fail broadcast, but won't crash code)
                            for uid in tracked_data:
                                if uid not in tracked_users:
                                    tracked_users[int(uid)] = 0
                                    
                        blocked_users.update(data.get("blocked", []))
                        
                await event.reply(f"✅ **Database Restored!**\n\n🟢 Active: `{len(tracked_users)}`\n🔴 Blocked: `{len(blocked_users)}`")
            except Exception as e:
                await event.reply(f"❌ Restore Failed: {e}")
            finally:
                if os.path.exists(file_path):
                    os.remove(file_path)
        else:
            await event.reply("❌ Please reply to a valid `.json` backup file.")

# ==========================================
# ANTI-BAN BROADCAST SYSTEM (REAL FIX)
# ==========================================

@client.on(events.NewMessage(pattern=r'^/broadcast', incoming=True))
async def broadcast_to_all(event):
    if event.sender_id not in ADMIN_IDS:
        return
        
    if not tracked_users:
        await event.reply("❌ No active users! Restore data first using `/restore`.")
        return
        
    status_msg = await event.reply(f"📢 **Starting Broadcast to {len(tracked_users)} users...**\n⏳ Kripya pratiksha karein...")
    
    success_count = 0
    fail_count = 0
    
    # Iterate through dict: user_id and access_hash
    current_users = list(tracked_users.items())
    
    for uid, access_hash in current_users:
        uid = int(uid)
        try:
            # 🔥 THE REAL FIX: Proper Peer construction with real access_hash
            peer = InputPeerUser(uid, int(access_hash))

            if event.is_reply:
                reply_msg = await event.get_reply_message()
                await client.forward_messages(peer, reply_msg.id, event.chat_id)
            else:
                broadcast_text = event.text.replace('/broadcast', '').strip()
                if broadcast_text:
                    await client.send_message(peer, broadcast_text)
                else:
                    await status_msg.edit("❌ You must provide text or reply to a message!")
                    return
            
            success_count += 1
            if uid in blocked_users:
                blocked_users.remove(uid)
                
            await asyncio.sleep(0.3)
            
        except FloodWaitError as e:
            logger.warning(f"FloodWaitError: Sleeping for {e.seconds} seconds.")
            await asyncio.sleep(e.seconds)
            
        except (UserIsBlockedError, ValueError):
            # ValueError catch karega agar access_hash 0 wali file se purana data use ho raha ho
            fail_count += 1
            blocked_users.add(uid)
            if uid in tracked_users:
                del tracked_users[uid]
                
        except Exception as e:
            fail_count += 1
            logger.error(f"Broadcast error for ID {uid}: {e}")
            
    await status_msg.edit(f"📢 **Broadcast Finished!**\n\n✅ Delivered: `{success_count}`\n🔴 Blocked/Failed: `{fail_count}`")

# ==========================================
# AUTOMATIC WELCOME & TRACKING
# ==========================================
async def send_welcome_package(user):
    try:
        uid = user.id
        # Safely get access_hash
        access_hash = getattr(user, 'access_hash', 0)
        
        # Save into Dictionary
        tracked_users[uid] = access_hash
        
        if uid in blocked_users:
            blocked_users.remove(uid)
        
        if saved_messages:
            for step in sorted(saved_messages.keys()):
                item = saved_messages[step]
                if item['type'] == 'forward':
                    await client.forward_messages(uid, item['msg_id'], item['from_chat'])
                elif item['type'] == 'text':
                    await client.send_message(uid, item['text'])
                await asyncio.sleep(0.5)
            
        logger.info(f"✅ Sequence delivered & Hash Tracked for ID: {uid}")
    except UserIsBlockedError:
        logger.warning(f"User {uid} blocked the bot instantly.")
    except Exception as e:
        logger.error(f"Failed welcome sequence: {e}")

@client.on(events.ChatAction())
async def handle_chat_action(event):
    if event.user_joined or event.user_added:
        user = await event.get_user()
        await send_welcome_package(user)

@client.on(events.Raw)
async def raw_join_request_handler(event):
    if isinstance(event, UpdateBotChatInviteRequester):
        try:
            # Fetch complete user info to get access_hash
            user = await client.get_entity(event.user_id)
            await send_welcome_package(user)
        except Exception as e:
            logger.error(f"Failed to fetch join requester entity: {e}")

# ==========================================
# MAIN EXECUTION
# ==========================================
async def main():
    await start_web_server()
    logger.info("🤖 Starting Telegram Bot...")
    await client.start(bot_token=BOT_TOKEN)
    logger.info("✅ Bot Online and running cleanly!")
    await client.run_until_disconnected()

if __name__ == "__main__":
    try:
        loop.run_until_complete(main())
    except KeyboardInterrupt:
        logger.info("Bot manually stopped.")
    except Exception as e:
        logger.error(f"Critical Runtime Error: {e}")
                             
