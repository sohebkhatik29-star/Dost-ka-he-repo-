import os
import io
import json
import asyncio
import time
import re
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.tl.custom import Message
from telethon.tl import functions, types

from config import (
    API_ID,
    API_HASH,
    BOT_TOKEN,
    ADMIN_IDS,
    CHECK_DELAY,
    MAX_LINKS_PER_BATCH,
    SESSION_STRING,
    BOT_USERNAME,
    OWNER_USERNAME,
    DEVELOPER_USERNAME,
    LOG_CHANNEL_ID,
    FSUB_CHANNEL_ID,
    validate_config
)
from checker import extract_telegram_links, check_single_link, parse_link

if not validate_config():
    print("❌ Cannot start bot. Please verify your environment variables.")
    exit(1)

# Ensure data directory exists
os.makedirs("data", exist_ok=True)
USERS_FILE = "data/users.json"
BANNED_FILE = "data/banned.json"
SAVED_SESSION_FILE = "data/session.txt"

def load_json_set(file_path: str) -> set:
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(data)
        except Exception:
            return set()
    return set()

def save_json_set(file_path: str, data_set: set):
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(list(data_set), f, indent=2)
    except Exception as e:
        print(f"Error saving {file_path}: {e}")

known_users = load_json_set(USERS_FILE)
banned_users = load_json_set(BANNED_FILE)

# 1. Main Bot Client
bot_client = TelegramClient('tg_bot_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# 2. MTProto User Client (for private invite links checking)
user_session_str = SESSION_STRING
if not user_session_str and os.path.exists(SAVED_SESSION_FILE):
    try:
        with open(SAVED_SESSION_FILE, "r") as f:
            user_session_str = f.read().strip()
    except Exception:
        pass

user_client = None

if user_session_str:
    try:
        user_client = TelegramClient(StringSession(user_session_str), API_ID, API_HASH)
    except Exception as e:
        print(f"⚠️ Error loading SESSION_STRING: {e}")
elif os.path.exists('user_session.session'):
    user_client = TelegramClient('user_session', API_ID, API_HASH)

# In-memory stores
user_sessions = {}
login_states = {} # For admin interactive login flow

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def is_banned(user_id: int) -> bool:
    return user_id in banned_users


async def ensure_user_client():
    """Checks if the user MTProto client is connected and authorized."""
    global user_client
    if user_client:
        try:
            if not user_client.is_connected():
                await user_client.connect()
            if await user_client.is_user_authorized():
                return True
        except Exception:
            return False
    return False


# --- LOG CHANNEL DISPATCHER ---

async def log_bot_startup():
    """Sends log when bot starts online."""
    try:
        await asyncio.sleep(2)
        log_msg = (
            "🟢 **#BOT_STARTED / ONLINE**\n\n"
            f"🤖 **Bot:** {BOT_USERNAME}\n"
            f"👥 **Total Registered Users:** `{len(known_users)}`\n"
            f"⏰ **Boot Time:** `{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}`\n\n"
            "🚀 *Bot is active and running 24/7!*"
        )
        await bot_client.send_message(LOG_CHANNEL_ID, log_msg)
    except Exception as e:
        print(f"Startup log notice: {e}")


async def log_new_user_start(user):
    """Sends log ONLY when a brand new user starts the bot for the first time."""
    try:
        user_id = user.id
        first_name = user.first_name or "Unknown"
        last_name = user.last_name or ""
        full_name = f"{first_name} {last_name}".strip()
        username = f"@{user.username}" if user.username else "No Username"
        total_count = len(known_users)

        log_msg = (
            "🆕 **#NEW_USER_STARTED**\n\n"
            f"👤 **User:** [{full_name}](tg://user?id={user_id})\n"
            f"🆔 **User ID:** `{user_id}`\n"
            f"🔗 **Username:** {username}\n"
            f"👥 **Total Registered Users:** `{total_count}`\n"
            f"📅 **Time:** `{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}`"
        )
        await bot_client.send_message(LOG_CHANNEL_ID, log_msg)
    except Exception as e:
        print(f"Log Error (New User): {e}")


async def log_link_check_activity(user, total_links: int, working_links: list, expired_links: list):
    """Sends log to log channel whenever any user checks links."""
    try:
        user_id = user.id
        first_name = user.first_name or "Unknown"
        username = f"@{user.username}" if user.username else "No Username"
        
        log_msg = (
            "🔗 **#LINK_CHECK_LOG**\n\n"
            f"👤 **User:** [{first_name}](tg://user?id={user_id}) ({username})\n"
            f"🆔 **User ID:** `{user_id}`\n\n"
            f"📊 **Statistics:**\n"
            f"• Total Links: `{total_links}`\n"
            f"• ✅ Working: `{len(working_links)}`\n"
            f"• ❌ Expired: `{len(expired_links)}`\n\n"
        )

        if working_links:
            log_msg += "✅ **Working Links:**\n"
            for item in working_links[:10]:
                log_msg += f"• `{item['url']}`\n"
            if len(working_links) > 10:
                log_msg += f"... and `{len(working_links) - 10}` more\n"
            log_msg += "\n"

        if expired_links:
            log_msg += "❌ **Expired Links:**\n"
            for item in expired_links[:5]:
                log_msg += f"• `{item['url']}`\n"
            if len(expired_links) > 5:
                log_msg += f"... and `{len(expired_links) - 5}` more\n"

        await bot_client.send_message(LOG_CHANNEL_ID, log_msg, link_preview=False)
    except Exception as e:
        print(f"Log Error (Link Activity): {e}")


# --- FORCE SUBSCRIBE VERIFICATION ---

async def get_fsub_invite_link():
    """Returns a direct join link for the Force Sub channel."""
    try:
        chat = await bot_client.get_entity(FSUB_CHANNEL_ID)
        if getattr(chat, 'username', None):
            return f"https://t.me/{chat.username}"
        inv = await bot_client(functions.messages.ExportChatInviteRequest(peer=chat))
        return inv.link
    except Exception:
        return "https://t.me/Aysha_sama"


async def check_fsub_membership(user_id: int) -> bool:
    """Checks if a user is a member of the Force Sub channel."""
    if is_admin(user_id):
        return True
    try:
        chat = await bot_client.get_entity(FSUB_CHANNEL_ID)
        participant = await bot_client(functions.channels.GetParticipantRequest(
            channel=chat,
            participant=user_id
        ))
        if participant:
            return True
    except Exception as e:
        err_str = str(e).lower()
        if "user_not_participant" in err_str:
            return False
        # If bot is not in channel or peer error, don't block user
        return True
    return True


async def send_fsub_prompt(event, user_id: int):
    """Prompts the user to join the Force Sub channel before using the bot."""
    try:
        channel_link = await get_fsub_invite_link()
        fsub_text = (
            "🔒 **Access Restricted: Join Channel First**\n\n"
            "Bot use karne ke liye pehle hamare official channel ko join karein.\n\n"
            "👉 **Neeche button par click karke channel join karein**, phir **'🔄 Try Again'** dabayein."
        )
        buttons = [
            [Button.url("📢 Join Channel", channel_link)],
            [Button.inline("🔄 Try Again", data=b"check_fsub_again")]
        ]
        if isinstance(event, Message):
            await event.reply(fsub_text, buttons=buttons)
        else:
            await event.edit(fsub_text, buttons=buttons)
    except Exception:
        await send_start_view(event, user_id)


@bot_client.on(events.CallbackQuery(data=b"check_fsub_again"))
async def check_fsub_again_callback(event):
    try:
        user_id = event.sender_id
        if is_banned(user_id):
            return await event.answer("You are banned from using this bot.", alert=True)

        is_member = await check_fsub_membership(user_id)
        if is_member:
            await event.answer("✅ Verification successful! Welcome.", alert=True)
            await send_start_view(event, user_id)
        else:
            await event.answer("❌ Aapne abhi tak channel join nahi kiya hai! Pehle join karein.", alert=True)
    except Exception as e:
        print(f"Error check_fsub_again: {e}")
        await send_start_view(event, event.sender_id)


# --- START & MENU SYSTEM ---

async def send_start_view(target, user_id: int):
    try:
        admin_user = is_admin(user_id)
        is_logged_in = await ensure_user_client()

        welcome_text = (
            "💎 **Welcome to Telegram Bulk Invite Link Checker Bot**\n\n"
            "A smart and lightning-fast bot to verify Telegram invite links and filter active ones.\n\n"
            "⚡ **Features:**\n"
            "• Bulk link extraction from messy text/chats\n"
            "• Deep MTProto validation (Active, Expired, Revoked)\n"
            "• Duplicate link removal automatically\n"
            "• Live progress tracking\n"
            "• Export active links as Text or .TXT File\n\n"
            "📥 **How to use:**\n"
            "Simply send or forward any message containing Telegram links here!"
        )
        
        buttons = []
        
        if admin_user and not is_logged_in:
            buttons.append([Button.inline("🔑 Admin: Connect Engine (1-Click)", data=b"start_login_flow")])
        
        buttons.append([
            Button.inline("ℹ️ Help / How to Use", data=b"menu_help"),
            Button.inline("📖 About Bot", data=b"menu_about")
        ])
        
        row2 = [Button.inline("⚙️ Bot Status", data=b"menu_status")]
        if admin_user:
            row2.append(Button.inline("🛡️ Admin Panel", data=b"menu_admin_stats"))
        buttons.append(row2)

        if isinstance(target, Message):
            await target.reply(welcome_text, buttons=buttons)
        else:
            await target.edit(welcome_text, buttons=buttons)
    except Exception as e:
        print(f"Error in send_start_view: {e}")


@bot_client.on(events.NewMessage(pattern=r'^/start(?:\s+.*)?$'))
async def start_handler(event: Message):
    try:
        sender = await event.get_sender()
        sender_id = event.sender_id

        if is_banned(sender_id):
            return await event.reply("⛔ You are banned from using this bot.")

        # ONLY send log when user starts for the VERY FIRST TIME
        if sender_id not in known_users:
            known_users.add(sender_id)
            save_json_set(USERS_FILE, known_users)
            if sender:
                asyncio.create_task(log_new_user_start(sender))

        # Check Force Sub
        is_member = await check_fsub_membership(sender_id)
        if not is_member:
            return await send_fsub_prompt(event, sender_id)

        await send_start_view(event, sender_id)
    except Exception as e:
        print(f"Error in start_handler: {e}")
        try:
            await send_start_view(event, event.sender_id)
        except Exception:
            pass


@bot_client.on(events.CallbackQuery(data=b"back_to_start"))
async def back_callback(event):
    await send_start_view(event, event.sender_id)


@bot_client.on(events.NewMessage(pattern=r'^/help(?:\s+.*)?$'))
async def help_cmd_handler(event: Message):
    if is_banned(event.sender_id):
        return
    await send_help_view(event)


@bot_client.on(events.CallbackQuery(data=b"menu_help"))
async def help_callback(event):
    await send_help_view(event)


async def send_help_view(target):
    help_text = (
        "ℹ️ **How to Use Telegram Link Checker Bot:**\n\n"
        "1️⃣ **Send or Forward Links:**\n"
        "Send any message or forward a chat containing Telegram invite links. You can send messy text — the bot automatically extracts all links.\n\n"
        "2️⃣ **Duplicate Cleaning:**\n"
        "Duplicate links are automatically filtered out.\n\n"
        "3️⃣ **Start Verification:**\n"
        "Click `Start Checking` to run deep MTProto validation.\n\n"
        "4️⃣ **Export Working Links:**\n"
        "Choose `Get as Text` or download a clean `.txt` file containing only active links!\n\n"
        "📋 **Supported Links:**\n"
        "• Private Invites: `https://t.me/+...` or `t.me/joinchat/...`\n"
        "• Public Links: `https://t.me/username`\n\n"
        f"⚡ **Batch Limit:** Up to **{MAX_LINKS_PER_BATCH} links** per batch.\n"
        f"⏱️ **Safe Delay:** **{CHECK_DELAY}s** per link to prevent rate-limits."
    )
    buttons = [[Button.inline("⬅️ Back to Menu", data=b"back_to_start")]]
    if isinstance(target, Message):
        await target.reply(help_text, buttons=buttons)
    else:
        await target.edit(help_text, buttons=buttons)


@bot_client.on(events.NewMessage(pattern=r'^/about(?:\s+.*)?$'))
async def about_cmd_handler(event: Message):
    if is_banned(event.sender_id):
        return
    await send_about_view(event)


@bot_client.on(events.CallbackQuery(data=b"menu_about"))
async def about_callback(event):
    await send_about_view(event)


async def send_about_view(target):
    about_text = (
        "📖 **About This Bot:**\n\n"
        f"🤖 **Bot Username:** {BOT_USERNAME}\n"
        f"👑 **Owner:** {OWNER_USERNAME}\n"
        f"💻 **Developer:** {DEVELOPER_USERNAME}\n"
        f"⚡ **Engine:** MTProto Ultra Deep Validator v2.0\n"
        f"🛡️ **Protection:** Anti-Flood & Rate-Limit Safe\n\n"
        "✨ *Built for fast, bulk Telegram link filtering and active group/channel discovery.*"
    )
    buttons = [
        [
            Button.url("👑 Owner", f"https://t.me/{OWNER_USERNAME.replace('@', '')}"),
            Button.url("💻 Developer", f"https://t.me/{DEVELOPER_USERNAME.replace('@', '')}")
        ],
        [Button.inline("⬅️ Back to Menu", data=b"back_to_start")]
    ]
    if isinstance(target, Message):
        await target.reply(about_text, buttons=buttons)
    else:
        await target.edit(about_text, buttons=buttons)


@bot_client.on(events.CallbackQuery(data=b"menu_status"))
async def status_callback(event):
    is_user_auth = await ensure_user_client()
    user_auth_str = "✅ Active (High Speed)" if is_user_auth else "🟡 Online"
    
    status_text = (
        "⚙️ **Bot System Status:**\n\n"
        f"🤖 **Bot Name:** {BOT_USERNAME}\n"
        f"🟢 **Server Status:** ONLINE 24/7\n"
        f"🔑 **Verification Engine:** `{user_auth_str}`\n"
        f"📦 **Max Links Per Batch:** `{MAX_LINKS_PER_BATCH}`\n"
        f"⏱️ **Check Delay:** `{CHECK_DELAY}s`\n"
    )
    buttons = [[Button.inline("⬅️ Back to Menu", data=b"back_to_start")]]
    await event.edit(status_text, buttons=buttons)


@bot_client.on(events.CallbackQuery(data=b"menu_admin_stats"))
async def admin_stats_callback(event):
    if not is_admin(event.sender_id):
        return await event.answer("Access Denied", alert=True)

    is_user_auth = await ensure_user_client()
    stats_text = (
        "📊 **Admin Control Panel & Statistics:**\n\n"
        f"👥 **Total Unique Users:** `{len(known_users)}`\n"
        f"⛔ **Banned Users:** `{len(banned_users)}`\n"
        f"👑 **Admins:** `{len(ADMIN_IDS)}`\n"
        f"🔑 **Engine Status:** `{'🟢 Connected' if is_user_auth else '🟡 Disconnected'}`\n"
        f"📢 **Log Channel:** `{LOG_CHANNEL_ID}`\n"
        f"🔒 **Force Sub Channel:** `{FSUB_CHANNEL_ID}`\n\n"
        "🛠️ **Admin Commands:**\n"
        "• `/ban <user_id>` — Ban a user\n"
        "• `/unban <user_id>` — Unban a user\n"
        "• `/banned` — View banned list\n"
        "• `/login` — Connect engine"
    )
    buttons = []
    if not is_user_auth:
        buttons.append([Button.inline("🔑 Connect Engine", data=b"start_login_flow")])
    buttons.append([Button.inline("⬅️ Back to Menu", data=b"back_to_start")])
    await event.edit(stats_text, buttons=buttons)


# --- ADMIN BAN & UNBAN COMMANDS ---

@bot_client.on(events.NewMessage(pattern=r'^/ban(?:\s+(\d+))?$'))
async def ban_user_handler(event: Message):
    if not is_admin(event.sender_id):
        return

    target_id_str = event.pattern_match.group(1)
    if not target_id_str:
        return await event.reply("⚠️ **Usage:** `/ban <user_id>`\nExample: `/ban 123456789`")

    target_id = int(target_id_str)
    if is_admin(target_id):
        return await event.reply("❌ You cannot ban an Admin/Owner!")

    banned_users.add(target_id)
    save_json_set(BANNED_FILE, banned_users)
    await event.reply(f"🚫 **User Banned:** `{target_id}` has been banned from using this bot.")


@bot_client.on(events.NewMessage(pattern=r'^/unban(?:\s+(\d+))?$'))
async def unban_user_handler(event: Message):
    if not is_admin(event.sender_id):
        return

    target_id_str = event.pattern_match.group(1)
    if not target_id_str:
        return await event.reply("⚠️ **Usage:** `/unban <user_id>`\nExample: `/unban 123456789`")

    target_id = int(target_id_str)
    if target_id in banned_users:
        banned_users.remove(target_id)
        save_json_set(BANNED_FILE, banned_users)
        await event.reply(f"✅ **User Unbanned:** `{target_id}` has been unbanned.")
    else:
        await event.reply(f"ℹ️ User `{target_id}` is not in the banned list.")


@bot_client.on(events.NewMessage(pattern=r'^/banned(?:\s+.*)?$'))
async def list_banned_handler(event: Message):
    if not is_admin(event.sender_id):
        return

    if not banned_users:
        return await event.reply("✅ No users are currently banned.")

    banned_list_text = "⛔ **Banned Users List:**\n\n"
    for uid in banned_users:
        banned_list_text += f"• `{uid}`\n"
    await event.reply(banned_list_text)


# --- ADMIN LOGIN FLOW ---

@bot_client.on(events.CallbackQuery(data=b"start_login_flow"))
async def start_login_callback(event):
    sender_id = event.sender_id
    if not is_admin(sender_id):
        return await event.answer("⚠️ Admin only action.", alert=True)

    login_states[sender_id] = {'step': 'awaiting_phone'}
    prompt_text = (
        "📱 **Connect Admin Account Engine (1-Time Setup)**\n\n"
        "Telegram API requires an account connection to verify private invite links (`t.me/+...`) for all users.\n\n"
        "👉 **Abhi apna Phone Number reply karein** (Country code ke sath):\n\n"
        "Example: `+919876543210`"
    )
    await event.edit(prompt_text, buttons=[[Button.inline("❌ Cancel", data=b"cancel_login")]])


@bot_client.on(events.CallbackQuery(data=b"cancel_login"))
async def cancel_login_callback(event):
    sender_id = event.sender_id
    if not is_admin(sender_id):
        return await event.answer("Access Denied", alert=True)
    login_states.pop(sender_id, None)
    await event.edit("❌ Connection cancelled.")


@bot_client.on(events.NewMessage(pattern=r'^/login(?:\s+(.+))?$'))
async def login_cmd_handler(event: Message):
    sender_id = event.sender_id
    if not is_admin(sender_id):
        return

    args = event.pattern_match.group(1)
    if not args:
        login_states[sender_id] = {'step': 'awaiting_phone'}
        return await event.reply("📱 **Please enter your phone number with country code:**\nExample: `+919876543210`")

    await process_phone_submission(event, sender_id, args.strip())


async def process_phone_submission(event: Message, sender_id: int, phone_raw: str):
    global user_client
    if not is_admin(sender_id):
        return

    phone = phone_raw.replace(" ", "").replace("-", "")
    if not phone.startswith("+") and phone.isdigit() and len(phone) == 10:
        phone = "+91" + phone
    elif not phone.startswith("+"):
        phone = "+" + phone

    await event.reply(f"⏳ Sending Telegram verification code to `{phone}`...")

    try:
        new_client = TelegramClient(StringSession(), API_ID, API_HASH)
        await new_client.connect()
        send_code = await new_client.send_code_request(phone)
        
        login_states[sender_id] = {
            'step': 'awaiting_otp',
            'phone': phone,
            'phone_code_hash': send_code.phone_code_hash,
            'client': new_client
        }

        await event.reply(
            f"📩 **Telegram OTP Sent!**\n\n"
            f"Apne Telegram app me official Telegram notification check karein aur **OTP code reply karein**:\n\n"
            f"Example: `12345`"
        )
    except Exception as e:
        login_states.pop(sender_id, None)
        await event.reply(f"❌ Failed to request code: `{str(e)}`\nPlease verify your phone number format (e.g. `+919876543210`).")


async def process_otp_submission(event: Message, sender_id: int, otp_raw: str):
    global user_client, user_session_str
    if not is_admin(sender_id):
        return False

    state = login_states.get(sender_id)
    if not state or state.get('step') != 'awaiting_otp':
        return False

    otp_code = otp_raw.strip().replace(" ", "")
    client_inst = state['client']

    try:
        await client_inst.sign_in(phone=state['phone'], code=otp_code, phone_code_hash=state['phone_code_hash'])
        session_str = client_inst.session.save()
        user_client = client_inst
        user_session_str = session_str
        login_states.pop(sender_id, None)

        try:
            with open(SAVED_SESSION_FILE, "w") as f:
                f.write(session_str)
        except Exception:
            pass

        await event.reply(
            "🎉 **SUCCESS: Engine Connected!**\n\n"
            "✅ Private invite link checking is now **100% Active for ALL users**.\n\n"
            "🚀 Koi bhi user ab links bhej kar direct check kar sakta hai!"
        )
        return True

    except Exception as e:
        error_msg = str(e)
        if "password" in error_msg.lower() or "2fa" in error_msg.lower():
            state['step'] = 'awaiting_password'
            await event.reply("🔒 **Two-Step Verification (2FA) Enabled!**\n\nApna 2FA Password yahan reply karein:")
            return True
        else:
            await event.reply(f"❌ Invalid OTP or Sign-in failed: `{error_msg}`\nSend OTP again or send `/login` to restart.")
            return True


async def process_password_submission(event: Message, sender_id: int, password_raw: str):
    global user_client, user_session_str
    if not is_admin(sender_id):
        return False

    state = login_states.get(sender_id)
    if not state or state.get('step') != 'awaiting_password':
        return False

    password = password_raw.strip()
    client_inst = state['client']

    try:
        await client_inst.sign_in(password=password)
        session_str = client_inst.session.save()
        user_client = client_inst
        user_session_str = session_str
        login_states.pop(sender_id, None)

        try:
            with open(SAVED_SESSION_FILE, "w") as f:
                f.write(session_str)
        except Exception:
            pass

        await event.reply("🎉 **SUCCESS: 2FA Login Completed!**\n\n✅ Engine is now 100% active for all users.")
        return True
    except Exception as e:
        await event.reply(f"❌ Incorrect 2FA Password: `{str(e)}`\nTry again:")
        return True


# --- MESSAGE & LINK RECEIVER HANDLER ---

@bot_client.on(events.NewMessage)
async def message_handler(event: Message):
    sender_id = event.sender_id
    if is_banned(sender_id):
        return

    text_content = (event.text or "").strip()
    if not text_content:
        return

    # Check admin login state
    if is_admin(sender_id):
        state = login_states.get(sender_id)
        if state:
            step = state.get('step')
            if step == 'awaiting_phone':
                await process_phone_submission(event, sender_id, text_content)
                return
            elif step == 'awaiting_otp':
                otp_cleaned = text_content.replace('/otp', '').strip()
                if otp_cleaned.isdigit() or len(otp_cleaned) in (5, 6):
                    await process_otp_submission(event, sender_id, otp_cleaned)
                    return
            elif step == 'awaiting_password':
                pwd_cleaned = text_content.replace('/password', '').strip()
                await process_password_submission(event, sender_id, pwd_cleaned)
                return

    # Ignore commands
    if text_content.startswith('/'):
        return

    # Handle document upload
    if event.file and event.file.name and event.file.name.endswith('.txt'):
        try:
            file_bytes = await event.download_media(bytes)
            text_content = file_bytes.decode('utf-8', errors='ignore')
        except Exception as e:
            await event.reply(f"⚠️ Could not read document: {str(e)}")
            return

    # Check Force Sub
    if not await check_fsub_membership(sender_id):
        return await send_fsub_prompt(event, sender_id)

    # Extract unique links
    links = extract_telegram_links(text_content)
    if not links:
        return

    if len(links) > MAX_LINKS_PER_BATCH:
        await event.reply(
            f"⚠️ **Limit Exceeded:** You sent `{len(links)}` links.\n"
            f"Maximum allowed per batch is `{MAX_LINKS_PER_BATCH}` links. Please split your list."
        )
        return

    user_sessions[sender_id] = {
        'pending_links': links,
        'results': None,
        'started_at': None
    }

    buttons = [
        [Button.inline(f"🚀 Start Checking ({len(links)} Links)", data=b"start_check")],
        [Button.inline("❌ Cancel", data=b"cancel_check")]
    ]
    await event.reply(
        f"🔗 **Detected {len(links)} Unique Telegram Links**\n\n"
        "Duplicate links have been automatically removed.\n"
        "Click **Start Checking** to begin verification.",
        buttons=buttons
    )


# --- CHECKING & PROGRESS WORKFLOW ---

@bot_client.on(events.CallbackQuery(data=b"cancel_check"))
async def cancel_callback(event):
    sender_id = event.sender_id
    user_sessions.pop(sender_id, None)
    await event.edit("❌ **Operation Cancelled.** Send new links anytime.")


@bot_client.on(events.CallbackQuery(data=b"start_check"))
async def start_check_callback(event):
    sender_id = event.sender_id
    if is_banned(sender_id):
        return await event.answer("You are banned from using this bot.", alert=True)

    if not await check_fsub_membership(sender_id):
        return await send_fsub_prompt(event, sender_id)

    session = user_sessions.get(sender_id)
    if not session or not session.get('pending_links'):
        return await event.edit("⚠️ No links found in queue. Please send your links again.")

    links = session['pending_links']
    total_count = len(links)
    
    # Check if MTProto engine is connected
    is_user_auth = await ensure_user_client()
    
    # Check if batch contains private links
    has_private_links = any(parse_link(u)[0] == 'invite' for u in links)

    if has_private_links and not is_user_auth:
        if is_admin(sender_id):
            buttons = [
                [Button.inline("🔑 Connect Engine Now (1-Click)", data=b"start_login_flow")],
                [Button.inline("❌ Cancel", data=b"cancel_check")]
            ]
            return await event.edit(
                "⚠️ **Admin Notice: MTProto Engine Disconnected**\n\n"
                "Server restart hone par Telegram engine disconnect ho gaya hai.\n\n"
                "👉 **Neeche button dabakar 5 second me connect karein:**",
                buttons=buttons
            )
        else:
            return await event.edit(
                "⚠️ **Bot Engine Initializing...**\n\n"
                "Bot is currently establishing MTProto connection. Please try again in 1 minute!"
            )

    active_client = user_client if is_user_auth else bot_client

    await event.edit(
        f"🔄 **Starting Verification...**\n\n"
        f"📊 Total Links: `{total_count}`\n"
        f"⏳ Validating links safely..."
    )

    working_list = []
    expired_list = []
    error_list = []
    last_update_time = time.time()
    
    for idx, url in enumerate(links, start=1):
        res = await check_single_link(active_client, url)
        
        if res['status'] == 'working':
            working_list.append(res)
        elif res['status'] == 'expired':
            expired_list.append(res)
        else:
            error_list.append(res)

        current_time = time.time()
        is_last = (idx == total_count)
        if is_last or (current_time - last_update_time >= 2.5) or (idx % 4 == 0):
            try:
                progress_text = (
                    f"🔍 **Checking Links in Progress...**\n\n"
                    f"**Progress:** `{idx} / {total_count}` (`{int((idx/total_count)*100)}%`)\n\n"
                    f"✅ **Working:** `{len(working_list)}`\n"
                    f"❌ **Expired / Invalid:** `{len(expired_list)}`\n"
                    f"⚠️ **Could Not Check:** `{len(error_list)}`\n\n"
                    f"⏳ *Please wait while MTProto validates links safely...*"
                )
                await event.edit(progress_text)
                last_update_time = current_time
            except Exception:
                pass

        await asyncio.sleep(CHECK_DELAY)

    session['results'] = {
        'total': total_count,
        'working': working_list,
        'expired': expired_list,
        'error': error_list
    }

    # Dispatch Activity Log to Log Channel
    sender_obj = await event.get_sender()
    if sender_obj:
        asyncio.create_task(log_link_check_activity(sender_obj, total_count, working_list, expired_list))

    working_pct = (len(working_list) / total_count * 100) if total_count > 0 else 0
    expired_pct = (len(expired_list) / total_count * 100) if total_count > 0 else 0

    summary_text = (
        "🏁 **CHECK COMPLETE**\n\n"
        "📊 **Statistics:**\n"
        f"• **Total Links:** `{total_count}`\n"
        f"• ✅ **Working Links:** `{len(working_list)}` ({working_pct:.1f}%)\n"
        f"• ❌ **Expired / Invalid:** `{len(expired_list)}` ({expired_pct:.1f}%)\n"
    )
    
    if error_list:
        summary_text += f"• ⚠️ **Could Not Check:** `{len(error_list)}`\n"

    summary_text += "\n**Choose how to receive working links:**"

    result_buttons = [
        [Button.inline(f"📋 Get as Text ({len(working_list)})", data=b"get_as_text")],
        [Button.inline("📁 Get as File (.txt)", data=b"get_as_file")],
        [Button.inline("🔄 Check New Links", data=b"back_to_start")]
    ]

    await event.edit(summary_text, buttons=result_buttons)


# --- RESULT DELIVERY HANDLERS ---

@bot_client.on(events.CallbackQuery(data=b"get_as_text"))
async def get_text_callback(event):
    sender_id = event.sender_id
    session = user_sessions.get(sender_id)
    if not session or not session.get('results'):
        return await event.answer("No completed results found. Please check again.", alert=True)

    working = session['results']['working']
    if not working:
        return await event.answer("❌ No working links were found in this batch!", alert=True)

    await event.answer("Sending working links...")

    lines = []
    for item in working:
        url = item['url']
        title = item.get('title')
        members = item.get('members', 0)
        if title:
            lines.append(f"• [{title}]({url}) ({members} members)\n  `{url}`")
        else:
            lines.append(f"`{url}`")

    header = f"✅ **Working Links ({len(working)} Total):**\n\n"
    current_chunk = header
    chunks = []

    for line in lines:
        if len(current_chunk) + len(line) + 2 > 3500:
            chunks.append(current_chunk)
            current_chunk = line + "\n\n"
        else:
            current_chunk += line + "\n\n"
            
    if current_chunk.strip():
        chunks.append(current_chunk)

    for c in chunks:
        await bot_client.send_message(sender_id, c, link_preview=False)


@bot_client.on(events.CallbackQuery(data=b"get_as_file"))
async def get_file_callback(event):
    sender_id = event.sender_id
    session = user_sessions.get(sender_id)
    if not session or not session.get('results'):
        return await event.answer("No completed results found. Please check again.", alert=True)

    working = session['results']['working']
    total = session['results']['total']
    if not working:
        return await event.answer("❌ No working links were found in this batch!", alert=True)

    await event.answer("Generating .txt file...")

    file_content = f"# TELEGRAM WORKING INVITE LINKS REPORT\n"
    file_content += f"# Generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n"
    file_content += f"# Total Checked: {total} | Active/Working: {len(working)}\n"
    file_content += "# " + "="*50 + "\n\n"
    
    for item in working:
        url = item['url']
        title = item.get('title') or 'N/A'
        members = item.get('members', 0)
        file_content += f"{url} | Title: {title} | Members: {members}\n"

    file_bytes = io.BytesIO(file_content.encode('utf-8'))
    file_bytes.name = f"working_links_{int(time.time())}.txt"

    caption = (
        f"📄 **Working Links Export**\n\n"
        f"✅ Total Working: `{len(working)} / {total}`\n"
        f"All verified invite links are listed inside."
    )

    await bot_client.send_file(sender_id, file=file_bytes, caption=caption)


# --- MAIN STARTUP & FREE TIER HEALTH SERVER ---

async def health_check_handler(request):
    from aiohttp import web
    return web.Response(text="TG Link Checker Bot is running 24/7!")

async def start_web_server():
    from aiohttp import web
    port = int(os.environ.get("PORT", 8080))
    app = web.Application()
    app.router.add_get("/", health_check_handler)
    app.router.add_get("/health", health_check_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"🌐 Health-check web server listening on port {port} (100% Free Render Support)")

async def main():
    try:
        await start_web_server()
    except Exception as e:
        print(f"Web server notice: {e}")
        
    print("="*60)
    print("🚀 Telegram Bulk Invite Link Checker Bot is STARTING (PUBLIC MODE)...")
    print(f"👤 Configured Admin IDs: {ADMIN_IDS}")
    print(f"📢 Log Channel: {LOG_CHANNEL_ID}")
    print(f"🔒 Force Sub Channel: {FSUB_CHANNEL_ID}")
    print("="*60)
    
    asyncio.create_task(log_bot_startup())
    await bot_client.run_until_disconnected()

if __name__ == '__main__':
    bot_client.loop.run_until_complete(main())
