import os
import io
import json
import asyncio
import time
import re
import uuid
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
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
active_jobs = {} # Independent storage for checking jobs by job_id
login_states = {} # For admin interactive login flow

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS

def is_banned(user_id: int) -> bool:
    return user_id in banned_users


async def ensure_user_client():
    """Checks if the user MTProto client is connected and authorized without hanging."""
    global user_client
    if user_client:
        try:
            if not user_client.is_connected():
                await asyncio.wait_for(user_client.connect(), timeout=5.0)
            if await asyncio.wait_for(user_client.is_user_authorized(), timeout=5.0):
                return True
        except Exception as e:
            print(f"ensure_user_client note: {e}")
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


async def log_link_check_activity(user, total_links: int, working_links: list, expired_links: list, restricted_links: list = None):
    """Sends log to log channel with ALL links with title, members, and clickable join URL."""
    try:
        user_id = user.id
        first_name = user.first_name or "Unknown"
        username = f"@{user.username}" if user.username else "No Username"
        restricted_links = restricted_links or []
        
        stat_lines = [
            f"• Total Links: `{total_links}`",
            f"• ✅ Working: `{len(working_links)}`",
            f"• ❌ Expired: `{len(expired_links)}`"
        ]
        if restricted_links:
            stat_lines.append(f"• 🚫 Restricted: `{len(restricted_links)}`")

        header = (
            "🔗 **#LINK_CHECK_LOG**\n\n"
            f"👤 **User:** [{first_name}](tg://user?id={user_id}) ({username})\n"
            f"🆔 **User ID:** `{user_id}`\n\n"
            f"📊 **Statistics:**\n"
            + "\n".join(stat_lines) + "\n\n"
        )

        all_lines = [header]

        if working_links:
            all_lines.append(f"✅ **Working Links ({len(working_links)} Total):**\n\n")
            for item in working_links:
                url = item['url']
                title = item.get('title')
                members = item.get('members', 0) or 0
                if title:
                    m_str = f" ({members} members)" if members > 0 else ""
                    all_lines.append(f"• {title}{m_str}\n  {url}\n\n")
                else:
                    all_lines.append(f"• {url}\n\n")

        if restricted_links:
            all_lines.append(f"🚫 **Restricted / TOS Violated ({len(restricted_links)} Total):**\n\n")
            for item in restricted_links:
                url = item['url']
                all_lines.append(f"• {url}\n")
            all_lines.append("\n")

        if expired_links:
            all_lines.append(f"❌ **Expired Links ({len(expired_links)} Total):**\n\n")
            for item in expired_links:
                url = item['url']
                all_lines.append(f"• {url}\n")
            all_lines.append("\n")

        # Combine lines safely into chunks (under 3500 chars) to prevent Telegram length limit
        chunks = []
        current_chunk = ""
        for line in all_lines:
            if len(current_chunk) + len(line) > 3500:
                chunks.append(current_chunk)
                current_chunk = line
            else:
                current_chunk += line

        if current_chunk.strip():
            chunks.append(current_chunk)

        for chunk in chunks:
            await bot_client.send_message(LOG_CHANNEL_ID, chunk, link_preview=False)
            await asyncio.sleep(0.4)
    except Exception as e:
        print(f"Log Error (Link Activity): {e}")


# --- FORCE SUBSCRIBE VERIFICATION ---

FSUB_CONFIG_FILE = "data/fsub_config.json"
active_fsub_cache = None

def get_active_fsub_config() -> dict:
    global active_fsub_cache
    if active_fsub_cache is not None:
        return active_fsub_cache
    if os.path.exists(FSUB_CONFIG_FILE):
        try:
            with open(FSUB_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                active_fsub_cache = data
                return data
        except Exception:
            pass
    active_fsub_cache = {
        "channel_id": FSUB_CHANNEL_ID,
        "access_hash": None,
        "title": "Official Channel",
        "username": "Aysha_sama",
        "invite_link": "https://t.me/Aysha_sama",
        "full_id": FSUB_CHANNEL_ID
    }
    return active_fsub_cache

def set_active_fsub_config(channel_id: int, access_hash: int, title: str, username: str = None, invite_link: str = None, full_id: int = None):
    global active_fsub_cache
    data = {
        "channel_id": channel_id,
        "access_hash": access_hash,
        "title": title or "Official Channel",
        "username": username,
        "invite_link": invite_link or (f"https://t.me/{username}" if username else "https://t.me/Aysha_sama"),
        "full_id": full_id or channel_id
    }
    active_fsub_cache = data
    try:
        os.makedirs("data", exist_ok=True)
        with open(FSUB_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"Error saving fsub config: {e}")

async def get_fsub_invite_link() -> str:
    """Returns a direct join link for the Force Sub channel."""
    cfg = get_active_fsub_config()
    if cfg.get("invite_link"):
        return cfg["invite_link"]
    if cfg.get("username"):
        return f"https://t.me/{cfg['username']}"
    return "https://t.me/Aysha_sama"


async def check_fsub_membership(user_id: int, input_user=None) -> bool:
    """Checks if a user is a member of the Force Sub channel safely."""
    if is_admin(user_id):
        return True
    
    cfg = get_active_fsub_config()
    ch_id = cfg.get("channel_id")
    if not ch_id:
        return True

    access_hash = cfg.get("access_hash")

    try:
        if access_hash:
            channel_input = types.InputChannel(channel_id=ch_id, access_hash=access_hash)
        else:
            channel_input = await bot_client.get_input_entity(ch_id)

        if input_user is None:
            try:
                input_user = await bot_client.get_input_entity(user_id)
            except Exception:
                input_user = user_id

        participant = await bot_client(functions.channels.GetParticipantRequest(
            channel=channel_input,
            participant=input_user
        ))
        
        p_obj = getattr(participant, 'participant', participant)
        if isinstance(p_obj, (types.ChannelParticipantBanned, types.ChannelParticipantLeft)):
            return False
        return True
    except Exception as e:
        err_name = type(e).__name__.lower()
        err_msg = str(e).lower()
        if "usernotparticipant" in err_name or "user_not_participant" in err_msg:
            return False
        print(f"FSub check caught for user {user_id}: {err_name} - {e}")
        return False


async def send_fsub_prompt(event, user_id: int):
    """Prompts the user to join the Force Sub channel before using the bot."""
    channel_link = await get_fsub_invite_link()
    cfg = get_active_fsub_config()
    channel_title = cfg.get("title", "Official Channel")
    
    fsub_text = (
        "🔒 **Access Restricted: Join Channel First**\n\n"
        f"Bot use karne ke liye pehle hamare channel **{channel_title}** ko join karein.\n\n"
        "👉 **Neeche button par click karke channel join karein**, phir **'🔄 Try Again'** dabayein."
    )
    buttons = [
        [Button.url(f"📢 Join {channel_title[:25]}", channel_link)],
        [Button.inline("🔄 Try Again", data=b"check_fsub_again")]
    ]
    if isinstance(event, events.CallbackQuery.Event):
        try:
            await event.edit(fsub_text, buttons=buttons)
        except Exception:
            await bot_client.send_message(user_id, fsub_text, buttons=buttons)
    else:
        await event.respond(fsub_text, buttons=buttons)


@bot_client.on(events.CallbackQuery(data=b"check_fsub_again"))
async def check_fsub_again_callback(event):
    try:
        user_id = event.sender_id
        if is_banned(user_id):
            return await event.answer("You are banned from using this bot.", alert=True)

        input_user = await event.get_input_sender()
        is_member = await check_fsub_membership(user_id, input_user)
        if is_member:
            await event.answer("✅ Verification successful! Welcome.", alert=True)
            try:
                await event.delete()
            except Exception:
                pass
            await send_start_view(event, user_id, as_new_message=True)
        else:
            await event.answer("❌ Aapne abhi tak channel join nahi kiya hai! Pehle join karein.", alert=True)
    except Exception as e:
        print(f"Error check_fsub_again: {e}")
        await event.answer("❌ Verification failed. Please join the channel first!", alert=True)


# --- START & MENU SYSTEM ---

async def send_start_view(target, user_id: int, as_new_message: bool = False):
    admin_user = is_admin(user_id)

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
    
    buttons = [
        [
            Button.inline("ℹ️ Help / How to Use", data=b"menu_help"),
            Button.inline("📖 About Bot", data=b"menu_about")
        ],
        [Button.inline("⚙️ Bot Status", data=b"menu_status")]
    ]
    
    if admin_user:
        buttons.append([Button.inline("👑 Admin Panel", data=b"menu_admin_panel")])

    try:
        if as_new_message:
            await bot_client.send_message(user_id, welcome_text, buttons=buttons)
        elif isinstance(target, events.CallbackQuery.Event):
            await target.edit(welcome_text, buttons=buttons)
        else:
            await target.respond(welcome_text, buttons=buttons)
    except Exception as e:
        print(f"Error in send_start_view send: {e}")
        await bot_client.send_message(user_id, welcome_text, buttons=buttons)


@bot_client.on(events.NewMessage(pattern=r'^/start(?:\s+.*)?$'))
async def start_handler(event):
    sender = await event.get_sender()
    sender_id = event.sender_id

    if is_banned(sender_id):
        return await event.respond("⛔ You are banned from using this bot.")

    # ONLY send log when user starts for the VERY FIRST TIME
    if sender_id not in known_users:
        known_users.add(sender_id)
        save_json_set(USERS_FILE, known_users)
        if sender:
            asyncio.create_task(log_new_user_start(sender))

    # Check Force Sub
    input_user = await event.get_input_sender()
    is_member = await check_fsub_membership(sender_id, input_user)
    if not is_member:
        return await send_fsub_prompt(event, sender_id)

    await send_start_view(event, sender_id)


@bot_client.on(events.CallbackQuery(data=b"back_to_start"))
async def back_callback(event):
    await send_start_view(event, event.sender_id)


@bot_client.on(events.NewMessage(pattern=r'^/help(?:\s+.*)?$'))
async def help_cmd_handler(event):
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
    if isinstance(target, events.CallbackQuery.Event):
        await target.edit(help_text, buttons=buttons)
    else:
        await target.respond(help_text, buttons=buttons)


@bot_client.on(events.NewMessage(pattern=r'^/about(?:\s+.*)?$'))
async def about_cmd_handler(event):
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
    if isinstance(target, events.CallbackQuery.Event):
        await target.edit(about_text, buttons=buttons)
    else:
        await target.respond(about_text, buttons=buttons)


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


@bot_client.on(events.CallbackQuery(data=b"menu_admin_panel"))
async def admin_panel_callback(event):
    sender_id = event.sender_id
    if not is_admin(sender_id):
        return await event.answer("⚠️ Access Denied: Admin only.", alert=True)
    try:
        await event.delete()
    except Exception:
        pass
    await send_admin_panel_view(sender_id)


@bot_client.on(events.NewMessage(pattern=r'^/admin(?:\s+.*)?$'))
async def admin_cmd_handler(event):
    sender_id = event.sender_id
    if not is_admin(sender_id):
        return
    await send_admin_panel_view(sender_id)


async def send_admin_panel_view(user_id: int):
    is_user_auth = await ensure_user_client()
    cfg = get_active_fsub_config()
    fsub_title = f"{cfg.get('title', 'Official Channel')} (`{cfg.get('full_id', cfg.get('channel_id'))}`)"

    text = (
        "👑 **Admin Control Panel**\n\n"
        f"👥 **Total Users:** `{len(known_users)}`\n"
        f"🔑 **Engine Status:** `{'🟢 Connected' if is_user_auth else '🔴 Disconnected'}`\n"
        f"📢 **Current Force Sub:** {fsub_title}\n\n"
        "👉 **Manage karne ke liye neeche button dabayein:**"
    )
    buttons = [
        [Button.inline("🔑 Admin Account Engine", data=b"admin_engine_panel")],
        [Button.inline("📢 Force Sub Channel", data=b"admin_fsub_panel")],
        [Button.inline("⬅️ Back to Menu", data=b"back_to_start_new")]
    ]
    await bot_client.send_message(user_id, text, buttons=buttons)


@bot_client.on(events.CallbackQuery(data=b"admin_engine_panel"))
async def admin_engine_panel_callback(event):
    sender_id = event.sender_id
    if not is_admin(sender_id):
        return await event.answer("Access Denied", alert=True)
    try:
        await event.delete()
    except Exception:
        pass

    is_user_auth = await ensure_user_client()
    status_str = "🟢 Connected & Active" if is_user_auth else "🔴 Disconnected"

    text = (
        "🔑 **Admin Account Engine (MTProto)**\n\n"
        f"⚡ **Engine Status:** `{status_str}`\n\n"
        "Ye engine private invite links (`t.me/+...`) ko 100% speed aur accuracy se verify karta hai.\n\n"
        "👉 **Aap jab chahein apna Phone Number connect ya change kar sakte hain:**"
    )
    buttons = [
        [Button.inline("📱 Change / Connect Phone Number", data=b"start_login_flow_new")],
        [Button.inline("⬅️ Back to Admin Panel", data=b"menu_admin_panel")]
    ]
    await bot_client.send_message(sender_id, text, buttons=buttons)


@bot_client.on(events.CallbackQuery(data=b"start_login_flow_new"))
async def start_login_flow_new_callback(event):
    sender_id = event.sender_id
    if not is_admin(sender_id):
        return await event.answer("Access Denied", alert=True)
    try:
        await event.delete()
    except Exception:
        pass

    login_states[sender_id] = {'step': 'awaiting_phone'}
    prompt_text = (
        "📱 **Connect / Change Phone Number**\n\n"
        "👉 **Apna naya Phone Number reply karein** (Country code ke sath):\n\n"
        "Example: `+919876543210`\n\n"
        "*(Ya `/session <StringSession>` bhej kar direct session login karein)*"
    )
    buttons = [[Button.inline("❌ Cancel", data=b"admin_engine_panel")]]
    await bot_client.send_message(sender_id, prompt_text, buttons=buttons)


@bot_client.on(events.CallbackQuery(data=b"admin_fsub_panel"))
async def admin_fsub_panel_callback(event):
    sender_id = event.sender_id
    if not is_admin(sender_id):
        return await event.answer("Access Denied", alert=True)
    try:
        await event.delete()
    except Exception:
        pass

    cfg = get_active_fsub_config()
    fsub_title = f"{cfg.get('title', 'Official Channel')} (`{cfg.get('full_id', cfg.get('channel_id'))}`)"

    login_states[sender_id] = {'step': 'awaiting_fsub_forward'}

    text = (
        "📢 **Force Subscribe Channel Setup**\n\n"
        f"📌 **Active Channel:** {fsub_title}\n\n"
        "👉 **Apne target channel se koi bhi message yahan FORWARD karein** (ya channel ka @username / ID bhejein).\n\n"
        "⚠️ **Zaroori Shart:** Bot ka us channel me **Admin** hona laazmi hai! Agar bot admin nahi hoga toh error aayega."
    )
    buttons = [
        [Button.inline("❌ Cancel", data=b"menu_admin_panel")]
    ]
    await bot_client.send_message(sender_id, text, buttons=buttons)


@bot_client.on(events.CallbackQuery(data=b"back_to_start_new"))
async def back_to_start_new_callback(event):
    sender_id = event.sender_id
    try:
        await event.delete()
    except Exception:
        pass
    await send_start_view(event, sender_id, as_new_message=True)


# --- ADMIN BAN & UNBAN COMMANDS ---

@bot_client.on(events.NewMessage(pattern=r'^/ban(?:\s+(\d+))?$'))
async def ban_user_handler(event):
    if not is_admin(event.sender_id):
        return

    target_id_str = event.pattern_match.group(1)
    if not target_id_str:
        return await event.respond("⚠️ **Usage:** `/ban <user_id>`\nExample: `/ban 123456789`")

    target_id = int(target_id_str)
    if is_admin(target_id):
        return await event.respond("❌ You cannot ban an Admin/Owner!")

    banned_users.add(target_id)
    save_json_set(BANNED_FILE, banned_users)
    await event.respond(f"🚫 **User Banned:** `{target_id}` has been banned from using this bot.")


@bot_client.on(events.NewMessage(pattern=r'^/unban(?:\s+(\d+))?$'))
async def unban_user_handler(event):
    if not is_admin(event.sender_id):
        return

    target_id_str = event.pattern_match.group(1)
    if not target_id_str:
        return await event.respond("⚠️ **Usage:** `/unban <user_id>`\nExample: `/unban 123456789`")

    target_id = int(target_id_str)
    if target_id in banned_users:
        banned_users.remove(target_id)
        save_json_set(BANNED_FILE, banned_users)
        await event.respond(f"✅ **User Unbanned:** `{target_id}` has been unbanned.")
    else:
        await event.respond(f"ℹ️ User `{target_id}` is not in the banned list.")


@bot_client.on(events.NewMessage(pattern=r'^/banned(?:\s+.*)?$'))
async def list_banned_handler(event):
    if not is_admin(event.sender_id):
        return

    if not banned_users:
        return await event.respond("✅ No users are currently banned.")

    banned_list_text = "⛔ **Banned Users List:**\n\n"
    for uid in banned_users:
        banned_list_text += f"• `{uid}`\n"
    await event.respond(banned_list_text)


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


@bot_client.on(events.NewMessage(pattern=r'^/session(?:\s+(.+))?$'))
async def session_cmd_handler(event):
    global user_client, user_session_str
    sender_id = event.sender_id
    if not is_admin(sender_id):
        return

    session_arg = event.pattern_match.group(1)
    if not session_arg:
        return await event.respond(
            "🔑 **Connect Engine via StringSession (Instant, No OTP)**\n\n"
            "Usage: `/session <STRING_SESSION>`\n\n"
            "💡 Telethon StringSession yahan paste karein aur bina OTP ke engine turant connect ho jayega!"
        )

    clean_str = session_arg.strip()
    await event.respond("⏳ Connecting StringSession...")
    try:
        new_client = TelegramClient(StringSession(clean_str), API_ID, API_HASH)
        await new_client.connect()
        if await new_client.is_user_authorized():
            user_client = new_client
            user_session_str = clean_str
            with open(SAVED_SESSION_FILE, "w") as f:
                f.write(clean_str)
            me = await new_client.get_me()
            first_name = getattr(me, 'first_name', 'Account')
            await event.respond(
                f"🎉 **SUCCESS: Engine Connected!**\n\n"
                f"👤 Logged in as: **{first_name}**\n"
                "✅ Private invite links (`t.me/+...`) ab 100% active check honge sabhi users ke liye!"
            )
        else:
            await event.respond("❌ That StringSession is invalid or expired.")
    except Exception as e:
        await event.respond(f"❌ Failed to connect StringSession: `{e}`")


@bot_client.on(events.NewMessage(pattern=r'^/login(?:\s+(.+))?$'))
async def login_cmd_handler(event):
    sender_id = event.sender_id
    if not is_admin(sender_id):
        return

    args = event.pattern_match.group(1)
    if not args:
        login_states[sender_id] = {'step': 'awaiting_phone'}
        return await event.respond("📱 **Please enter your phone number with country code:**\nExample: `+919876543210`")

    await process_phone_submission(event, sender_id, args.strip())


async def process_phone_submission(event, sender_id: int, phone_raw: str):
    global user_client
    if not is_admin(sender_id):
        return

    phone = phone_raw.replace(" ", "").replace("-", "")
    if not phone.startswith("+") and phone.isdigit() and len(phone) == 10:
        phone = "+91" + phone
    elif not phone.startswith("+"):
        phone = "+" + phone

    await event.respond(f"⏳ Sending Telegram verification code to `{phone}`...")

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

        await event.respond(
            "📩 **Telegram OTP Sent to Telegram App!**\n\n"
            "⚠️ **IMPORTANT (Telegram Security Rule):**\n"
            "Telegram chat me direct continuous 5-digit number daalne par code expire ho jata hai!\n\n"
            "👉 **Isliye OTP ko SPACES ya DASHES ke sath reply karein:**\n"
            "Example: `1 2 3 4 5` ya `1-2-3-4-5`\n\n"
            "*(Space ya Dash zaroor daalein taaki Telegram use expire na kare!)*"
        )
    except Exception as e:
        login_states.pop(sender_id, None)
        await event.respond(f"❌ Failed to request code: `{str(e)}`\nPlease verify your phone number format (e.g. `+919876543210`).")


async def process_otp_submission(event, sender_id: int, otp_raw: str):
    global user_client, user_session_str
    if not is_admin(sender_id):
        return False

    state = login_states.get(sender_id)
    if not state or state.get('step') != 'awaiting_otp':
        return False

    # Extract all digits, supporting '1 2 3 4 5', '1-2-3-4-5', etc.
    otp_code = "".join(c for c in otp_raw if c.isdigit())
    if not otp_code:
        await event.respond("⚠️ Please enter valid digits. Example: `1 2 3 4 5`")
        return True

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

        await event.respond(
            "🎉 **SUCCESS: Engine Connected!**\n\n"
            "✅ Private invite link checking is now **100% Active for ALL users**.\n\n"
            "🚀 Koi bhi user ab links bhej kar direct check kar sakta hai!"
        )
        return True

    except Exception as e:
        error_msg = str(e)
        if "password" in error_msg.lower() or "2fa" in error_msg.lower():
            state['step'] = 'awaiting_password'
            await event.respond("🔒 **Two-Step Verification (2FA) Enabled!**\n\nApna 2FA Password yahan reply karein:")
            return True
        else:
            await event.respond(
                f"❌ Login failed: `{error_msg}`\n\n"
                "💡 **Tip:** Agar code expire ho gaya ho, to dobara `/login` karke new code lein aur **spaces ke sath** (`1 2 3 4 5`) bhejein."
            )
            return True


async def process_password_submission(event, sender_id: int, password_raw: str):
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

        await event.respond("🎉 **SUCCESS: 2FA Login Completed!**\n\n✅ Engine is now 100% active for all users.")
        return True
    except Exception as e:
        await event.respond(f"❌ Incorrect 2FA Password: `{str(e)}`\nTry again:")
        return True


async def process_fsub_forward_submission(event, sender_id: int):
    channel_entity = None

    # Check forward header
    if event.fwd_from:
        from_id = event.fwd_from.from_id
        if from_id:
            try:
                channel_entity = await bot_client.get_entity(from_id)
            except Exception as e:
                print(f"Error getting entity from fwd_from: {e}")

    # If text link or username provided
    text = (event.text or "").strip()
    if not channel_entity and text:
        clean_text = text
        if "t.me/" in clean_text:
            m = re.search(r't\.me/([a-zA-Z0-9_\-]+)', clean_text)
            if m:
                clean_text = m.group(1).replace("+", "").replace("joinchat/", "")
        if clean_text.startswith("@"):
            clean_text = clean_text[1:]
        try:
            target_arg = int(clean_text) if (clean_text.startswith("-100") or (clean_text.isdigit() and len(clean_text) > 8)) else clean_text
            channel_entity = await bot_client.get_entity(target_arg)
        except Exception as e:
            print(f"Error getting entity from text: {e}")

    if not channel_entity or not hasattr(channel_entity, 'id'):
        buttons = [
            [Button.inline("🔄 Try Again", data=b"admin_fsub_panel")],
            [Button.inline("⬅️ Back to Admin Panel", data=b"menu_admin_panel")]
        ]
        return await event.respond(
            "⚠️ **Channel detect nahi hua!**\n\n"
            "Channel se seedha **koi bhi message FORWARD karein** ya channel ka link/username (`@channel`) bhejien.\n\n"
            "*(Make sure forwarded message me channel ka naam show ho raha ho)*",
            buttons=buttons
        )

    channel_id = channel_entity.id
    access_hash = getattr(channel_entity, 'access_hash', 0)
    target_title = getattr(channel_entity, 'title', "Official Channel")
    target_username = getattr(channel_entity, 'username', None)
    full_channel_id = int(f"-100{channel_id}") if not str(channel_id).startswith("-100") else channel_id

    # Check if bot is ADMIN in this channel
    is_bot_admin = False
    try:
        if access_hash:
            chk_input = types.InputChannel(channel_id=channel_id, access_hash=access_hash)
        else:
            chk_input = channel_entity
        participant = await bot_client(functions.channels.GetParticipantRequest(
            channel=chk_input,
            participant="me"
        ))
        p = getattr(participant, 'participant', participant)
        if isinstance(p, (types.ChannelParticipantAdmin, types.ChannelParticipantCreator)):
            is_bot_admin = True
    except Exception as e:
        print(f"Bot admin check error: {e}")
        is_bot_admin = False

    if not is_bot_admin:
        login_states.pop(sender_id, None)
        buttons = [
            [Button.inline("🔄 Try Again", data=b"admin_fsub_panel")],
            [Button.inline("⬅️ Back to Admin Panel", data=b"menu_admin_panel")]
        ]
        return await event.respond(
            f"❌ **Error: Bot is NOT an Admin in this channel!**\n\n"
            f"📢 **Channel:** {target_title}\n"
            f"🆔 **Channel ID:** `{full_channel_id}`\n\n"
            f"⚠️ Pehle bot ko is channel me **Admin banayein** (Invite Users / Add Members permission ke sath), phir try karein!",
            buttons=buttons
        )

    # Bot is confirmed Admin! Generate/Export permanent invite link
    invite_link = None
    if target_username:
        invite_link = f"https://t.me/{target_username}"
    else:
        try:
            if access_hash:
                input_ch = types.InputChannel(channel_id=channel_id, access_hash=access_hash)
            else:
                input_ch = channel_entity
            exported_inv = await bot_client(functions.messages.ExportChatInviteRequest(peer=input_ch))
            invite_link = exported_inv.link
        except Exception as inv_err:
            print(f"Error exporting invite link: {inv_err}")
            invite_link = f"https://t.me/c/{channel_id}/1"

    # Save to config file and active cache
    set_active_fsub_config(
        channel_id=channel_id,
        access_hash=access_hash,
        title=target_title,
        username=target_username,
        invite_link=invite_link,
        full_id=full_channel_id
    )
    login_states.pop(sender_id, None)

    buttons = [
        [Button.inline("⬅️ Back to Admin Panel", data=b"menu_admin_panel")],
        [Button.inline("🏠 Main Menu", data=b"back_to_start_new")]
    ]
    await event.respond(
        f"🎉 **SUCCESS: Force Subscribe Channel Updated!**\n\n"
        f"📢 **Active Channel:** {target_title}\n"
        f"🆔 **Channel ID:** `{full_channel_id}`\n"
        f"🔗 **Join Link:** {invite_link}\n"
        f"🛡️ **Admin Rights:** Verified ✅\n\n"
        f"🚀 Ab sabhi users ko bot chalane se pehle **{target_title}** channel join karna zaroori hoga!",
        buttons=buttons
    )


# --- MESSAGE & LINK RECEIVER HANDLER ---

@bot_client.on(events.NewMessage)
async def message_handler(event):
    sender_id = event.sender_id
    if is_banned(sender_id):
        return

    # Check admin panel states first
    if is_admin(sender_id):
        state = login_states.get(sender_id)
        if state:
            step = state.get('step')
            if step == 'awaiting_fsub_forward':
                await process_fsub_forward_submission(event, sender_id)
                return
            elif step == 'awaiting_phone':
                text_content = (event.text or "").strip()
                if text_content:
                    await process_phone_submission(event, sender_id, text_content)
                return
            elif step == 'awaiting_otp':
                text_content = (event.text or "").strip()
                if text_content:
                    otp_cleaned = text_content.replace('/otp', '').strip()
                    await process_otp_submission(event, sender_id, otp_cleaned)
                return
            elif step == 'awaiting_password':
                text_content = (event.text or "").strip()
                if text_content:
                    pwd_cleaned = text_content.replace('/password', '').strip()
                    await process_password_submission(event, sender_id, pwd_cleaned)
                return

    text_content = (event.text or "").strip()
    if not text_content:
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
            await event.respond(f"⚠️ Could not read document: {str(e)}")
            return

    # Check Force Sub
    input_user = await event.get_input_sender()
    if not await check_fsub_membership(sender_id, input_user):
        return await send_fsub_prompt(event, sender_id)

    # Extract unique links
    links = extract_telegram_links(text_content)
    if not links:
        return

    if len(links) > MAX_LINKS_PER_BATCH:
        await event.respond(
            f"⚠️ **Limit Exceeded:** You sent `{len(links)}` links.\n"
            f"Maximum allowed per batch is `{MAX_LINKS_PER_BATCH}` links. Please split your list."
        )
        return

    job_id = uuid.uuid4().hex[:8]
    job_data = {
        'job_id': job_id,
        'user_id': sender_id,
        'pending_links': links,
        'results': None,
        'started_at': None
    }
    active_jobs[job_id] = job_data
    user_sessions[sender_id] = job_data

    buttons = [
        [Button.inline(f"🚀 Start Checking ({len(links)} Links)", data=f"chk_{job_id}".encode())],
        [Button.inline("❌ Cancel", data=f"ccl_{job_id}".encode())]
    ]
    await event.respond(
        f"🔗 **Detected {len(links)} Unique Telegram Links**\n\n"
        "Duplicate links have been automatically removed.\n"
        "Click **Start Checking** to begin verification.",
        buttons=buttons
    )


# --- CHECKING & PROGRESS WORKFLOW (JOB ISOLATED) ---

@bot_client.on(events.CallbackQuery(pattern=r'^(?:ccl_([a-zA-Z0-9]+)|cancel_check)$'))
async def cancel_callback(event):
    sender_id = event.sender_id
    job_id = None
    if event.pattern_match:
        try:
            job_id = event.pattern_match.group(1)
        except Exception:
            pass
    if job_id:
        active_jobs.pop(job_id, None)
    user_sessions.pop(sender_id, None)
    await event.edit("❌ **Operation Cancelled.** Send new links anytime.")


@bot_client.on(events.CallbackQuery(pattern=r'^(?:chk_([a-zA-Z0-9]+)|start_check)$'))
async def start_check_callback(event):
    sender_id = event.sender_id
    if is_banned(sender_id):
        return await event.answer("You are banned from using this bot.", alert=True)

    input_user = await event.get_input_sender()
    if not await check_fsub_membership(sender_id, input_user):
        return await send_fsub_prompt(event, sender_id)

    job_id = None
    if event.pattern_match:
        try:
            job_id = event.pattern_match.group(1)
        except Exception:
            pass

    job = active_jobs.get(job_id) if job_id else user_sessions.get(sender_id)
    if not job or not job.get('pending_links'):
        return await event.edit("⚠️ No links found in queue. Please send your links again.")

    actual_job_id = job.get('job_id') or job_id or uuid.uuid4().hex[:8]
    job['job_id'] = actual_job_id
    active_jobs[actual_job_id] = job

    links = job['pending_links']
    total_count = len(links)
    
    # Check if MTProto engine is connected
    is_user_auth = await ensure_user_client()
    
    # Check if batch contains private links
    has_private_links = any(parse_link(u)[0] == 'invite' for u in links)

    if has_private_links and not is_user_auth:
        if is_admin(sender_id):
            buttons = [
                [Button.inline("🔑 Connect Engine Now (1-Click)", data=b"start_login_flow")],
                [Button.inline("❌ Cancel", data=f"ccl_{actual_job_id}".encode())]
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
    restricted_list = []
    error_list = []
    last_update_time = time.time()
    
    for idx, url in enumerate(links, start=1):
        res = await check_single_link(active_client, url)
        
        status = res.get('status')
        if status == 'working':
            working_list.append(res)
        elif status == 'restricted':
            restricted_list.append(res)
        elif status == 'expired':
            expired_list.append(res)
        else:
            error_list.append(res)
            # If FloodWait occurred, pause safely to protect subsequent links
            fw = res.get('flood_wait_seconds', 0)
            if fw and fw > 0:
                wait_to_pause = min(fw, 12)
                await asyncio.sleep(wait_to_pause)

        current_time = time.time()
        is_last = (idx == total_count)
        if is_last or (current_time - last_update_time >= 2.5) or (idx % 4 == 0):
            try:
                progress_text = (
                    f"🔍 **Checking Links in Progress...**\n\n"
                    f"**Progress:** `{idx} / {total_count}` (`{int((idx/total_count)*100)}%`)\n\n"
                    f"✅ **Working:** `{len(working_list)}`\n"
                    f"❌ **Expired / Invalid:** `{len(expired_list)}`\n"
                )
                if restricted_list:
                    progress_text += f"🚫 **Restricted / TOS:** `{len(restricted_list)}`\n"
                if error_list:
                    progress_text += f"⚠️ **Could Not Check:** `{len(error_list)}`\n"
                progress_text += f"\n⏳ *Please wait while MTProto validates links safely...*"
                await event.edit(progress_text)
                last_update_time = current_time
            except Exception:
                pass

        await asyncio.sleep(CHECK_DELAY)

    job['results'] = {
        'total': total_count,
        'working': working_list,
        'expired': expired_list,
        'restricted': restricted_list,
        'error': error_list
    }
    user_sessions[sender_id] = job

    # Dispatch Activity Log to Log Channel
    sender_obj = await event.get_sender()
    if sender_obj:
        asyncio.create_task(log_link_check_activity(sender_obj, total_count, working_list, expired_list, restricted_list))

    working_pct = (len(working_list) / total_count * 100) if total_count > 0 else 0
    expired_pct = (len(expired_list) / total_count * 100) if total_count > 0 else 0

    summary_text = (
        "🏁 **CHECK COMPLETE**\n\n"
        "📊 **Statistics:**\n"
        f"• **Total Links:** `{total_count}`\n"
        f"• ✅ **Working Links:** `{len(working_list)}` ({working_pct:.1f}%)\n"
        f"• ❌ **Expired / Invalid:** `{len(expired_list)}` ({expired_pct:.1f}%)\n"
    )
    if restricted_list:
        summary_text += f"• 🚫 **Restricted / Unavailable:** `{len(restricted_list)}`\n"
    if error_list:
        summary_text += f"• ⚠️ **Could Not Check:** `{len(error_list)}`\n"

    summary_text += "\n**Choose how to receive working links:**"

    result_buttons = [
        [Button.inline(f"📋 Get as Text ({len(working_list)})", data=f"txt_{actual_job_id}".encode())],
        [Button.inline("📁 Get as File (.txt)", data=f"fil_{actual_job_id}".encode())],
        [Button.inline("🔄 Check New Links", data=b"back_to_start")]
    ]

    await event.edit(summary_text, buttons=result_buttons)


# --- RESULT DELIVERY HANDLERS ---

@bot_client.on(events.CallbackQuery(pattern=r'^(?:txt_([a-zA-Z0-9]+)|get_as_text)$'))
async def get_text_callback(event):
    sender_id = event.sender_id
    job_id = None
    if event.pattern_match:
        try:
            job_id = event.pattern_match.group(1)
        except Exception:
            pass

    job = active_jobs.get(job_id) if job_id else user_sessions.get(sender_id)
    if not job or not job.get('results'):
        return await event.answer("No completed results found for this batch. Please check again.", alert=True)

    working = job['results']['working']
    if not working:
        return await event.answer("❌ No working links were found in this batch!", alert=True)

    await event.answer("Sending working links...")

    lines = []
    for item in working:
        url = item['url']
        title = item.get('title')
        members = item.get('members', 0) or 0
        if title:
            m_str = f" ({members} members)" if members > 0 else ""
            lines.append(f"• {title}{m_str}\n  {url}")
        else:
            lines.append(f"{url}")

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


@bot_client.on(events.CallbackQuery(pattern=r'^(?:fil_([a-zA-Z0-9]+)|get_as_file)$'))
async def get_file_callback(event):
    sender_id = event.sender_id
    job_id = None
    if event.pattern_match:
        try:
            job_id = event.pattern_match.group(1)
        except Exception:
            pass

    job = active_jobs.get(job_id) if job_id else user_sessions.get(sender_id)
    if not job or not job.get('results'):
        return await event.answer("No completed results found for this batch. Please check again.", alert=True)

    working = job['results']['working']
    total = job['results']['total']
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
        members = item.get('members', 0) or 0
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
