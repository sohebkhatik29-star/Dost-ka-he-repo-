import os
import io
import time
import asyncio
from typing import Dict, Any, List
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.tl import types, functions

# Modular imports
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
    validate_config
)
from logging_service import log_bot_startup, log_new_user_start, log_link_check_activity
from fsub import (
    check_fsub_membership,
    send_fsub_prompt,
    get_fsub_channel_link,
    load_fsub_config,
    save_fsub_config
)
from checker import (
    extract_telegram_links,
    extract_links_from_message,
    check_single_link,
    parse_link
)
from admin import (
    is_admin,
    is_banned,
    register_user,
    ban_user,
    unban_user,
    get_stats,
    get_admin_dashboard_buttons,
    known_users,
    banned_users,
    SAVED_SESSION_FILE
)

if not validate_config():
    print("❌ Cannot start bot. Please verify your environment variables.")
    exit(1)

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

# Job Store
active_jobs: Dict[str, Dict[str, Any]] = {}
user_latest_job: Dict[int, str] = {}
admin_action_states: Dict[int, Dict[str, Any]] = {}


def create_checking_job(user_id: int, links: list) -> str:
    job_id = f"j_{int(time.time())}_{os.urandom(3).hex()}"
    active_jobs[job_id] = {
        'job_id': job_id,
        'user_id': user_id,
        'created_at': time.time(),
        'pending_links': links,
        'results': None,
        'is_running': False
    }
    user_latest_job[user_id] = job_id
    # Prune old jobs older than 12 hours
    now = time.time()
    for jid in list(active_jobs.keys()):
        if now - active_jobs[jid].get('created_at', 0) > 43200:
            active_jobs.pop(jid, None)
    return job_id


async def ensure_user_client() -> bool:
    """Checks if the user MTProto client is connected and authorized."""
    global user_client
    if user_client:
        try:
            if not user_client.is_connected():
                await asyncio.wait_for(user_client.connect(), timeout=6.0)
            if await asyncio.wait_for(user_client.is_user_authorized(), timeout=6.0):
                return True
        except Exception as e:
            print(f"ensure_user_client notice: {e}")
            return False
    return False


# --- BOT COMMANDS ---

@bot_client.on(events.NewMessage(pattern=r'^/start$'))
async def start_handler(event):
    sender_id = event.sender_id
    if is_banned(sender_id):
        return await event.respond("🚫 You are banned from using this bot.")

    is_new = register_user(sender_id)
    if is_new:
        sender_obj = await event.get_sender()
        if sender_obj:
            asyncio.create_task(log_new_user_start(bot_client, sender_obj, len(known_users)))

    input_user = await event.get_input_sender()
    if not await check_fsub_membership(bot_client, sender_id, input_user):
        return await send_fsub_prompt(event, bot_client, sender_id)

    welcome_text = (
        "👋 **Welcome to Bulk Telegram Invite Link Checker!**\n\n"
        "⚡ **Features:**\n"
        "• Check hundreds of Telegram invite & public links simultaneously.\n"
        "• Automatic duplicate removal & link extraction.\n"
        "• Fast & accurate validation via MTProto engine.\n"
        "• Export working links directly as Text or `.txt` file.\n\n"
        "📥 **How to Use:**\n"
        "Simply **send, paste, or forward** any message containing Telegram links, or upload a `.txt` file!"
    )

    buttons = [
        [Button.inline("ℹ️ Help & Guide", data=b"show_help")],
        [Button.url("👑 Owner", f"https://t.me/{OWNER_USERNAME.lstrip('@')}"),
         Button.url("👨‍💻 Developer", f"https://t.me/{DEVELOPER_USERNAME.lstrip('@')}")]
    ]

    await event.respond(welcome_text, buttons=buttons, link_preview=False)


@bot_client.on(events.NewMessage(pattern=r'^/help$'))
async def help_handler(event):
    sender_id = event.sender_id
    if is_banned(sender_id):
        return

    help_text = (
        "📖 **Help & Instructions:**\n\n"
        "1️⃣ **Sending Links:**\n"
        "• Send raw links (e.g. `https://t.me/+AbCdEfGh`)\n"
        "• Forward messages from other channels (embedded hyperlinks are supported!)\n"
        "• Upload a `.txt` file containing Telegram links.\n\n"
        "2️⃣ **Checking & Progress:**\n"
        "• Click **Start Checking** to begin verification.\n"
        "• Live progress updates keep you informed.\n\n"
        "3️⃣ **Results:**\n"
        "• Receive only active, verified working links.\n"
        "• Download full report or copy in Telegram."
    )
    await event.respond(help_text)


@bot_client.on(events.CallbackQuery(data=b"show_help"))
async def callback_help(event):
    help_text = (
        "📖 **Help & Instructions:**\n\n"
        "1️⃣ **Sending Links:**\n"
        "• Send raw links (e.g. `https://t.me/+AbCdEfGh`)\n"
        "• Forward messages with formatted hyperlinks.\n"
        "• Upload a `.txt` file.\n\n"
        "2️⃣ **Click Start Checking** to get real-time validation!"
    )
    await event.edit(help_text, buttons=[[Button.inline("🔙 Back", data=b"back_to_start")]])


@bot_client.on(events.CallbackQuery(data=b"back_to_start"))
async def callback_back_to_start(event):
    welcome_text = (
        "👋 **Bulk Telegram Invite Link Checker**\n\n"
        "📥 Send, paste, or forward any message containing Telegram links to begin!"
    )
    buttons = [
        [Button.inline("ℹ️ Help & Guide", data=b"show_help")],
        [Button.url("👑 Owner", f"https://t.me/{OWNER_USERNAME.lstrip('@')}"),
         Button.url("👨‍💻 Developer", f"https://t.me/{DEVELOPER_USERNAME.lstrip('@')}")]
    ]
    await event.edit(welcome_text, buttons=buttons, link_preview=False)


@bot_client.on(events.CallbackQuery(data=b"check_fsub_status"))
async def fsub_check_status_callback(event):
    sender_id = event.sender_id
    input_user = await event.get_input_sender()
    is_member = await check_fsub_membership(bot_client, sender_id, input_user)
    if is_member:
        await event.answer("✅ Channel membership verified! Welcome!", alert=True)
        welcome_text = (
            "✅ **Membership Verified!**\n\n"
            "Aapka swagat hai! Ab aap links send karke checking shuru kar sakte hain."
        )
        await event.edit(welcome_text, buttons=[[Button.inline("ℹ️ Help & Guide", data=b"show_help")]])
    else:
        await event.answer("❌ Aapne abhi tak channel join nahi kiya hai! Pehle join karein.", alert=True)


# --- ADMIN COMMANDS & DASHBOARD ---

@bot_client.on(events.NewMessage(pattern=r'^/admin$'))
async def admin_handler(event):
    sender_id = event.sender_id
    if not is_admin(sender_id):
        return await event.respond("⛔ This command is restricted to Bot Administrators.")

    stats = get_stats()
    text = (
        "👑 **ADMINISTRATOR CONTROL PANEL**\n\n"
        f"👥 **Total Registered Users:** `{stats['total_users']}`\n"
        f"🚫 **Banned Users:** `{stats['banned_users']}`\n"
        f"🛡️ **Admin Accounts:** `{stats['admin_count']}`\n\n"
        "Select an administrative action below:"
    )
    await event.respond(text, buttons=get_admin_dashboard_buttons())


@bot_client.on(events.NewMessage(pattern=r'^/fsub(?:\s+(.+))?$'))
async def fsub_command_handler(event):
    sender_id = event.sender_id
    if not is_admin(sender_id):
        return

    arg = (event.pattern_match.group(1) or "").strip()
    cfg = load_fsub_config()

    if not arg:
        status_str = "🟢 Active" if cfg.get("is_active") else "🔴 Disabled"
        curr_chan = cfg.get("title", "None")
        return await event.respond(
            f"🔒 **Force Subscribe Settings**\n\n"
            f"• **Status:** {status_str}\n"
            f"• **Channel:** `{curr_chan}`\n"
            f"• **Invite Link:** {cfg.get('invite_link') or 'Auto'}\n\n"
            "**Usage Commands:**\n"
            "`/fsub off` - Disable Force Subscribe\n"
            "`/fsub on` - Enable Force Subscribe\n"
            "`/fsub @channel_username` - Set public channel\n"
            "`/fsub -100xxxxxxxxxx` - Set private channel ID"
        )

    if arg.lower() in ("off", "disable", "false", "0"):
        cfg["is_active"] = False
        save_fsub_config(cfg)
        return await event.respond("🔴 **Force Subscribe has been DISABLED.** Users can now use the bot freely.")

    if arg.lower() in ("on", "enable", "true", "1"):
        cfg["is_active"] = True
        save_fsub_config(cfg)
        return await event.respond("🟢 **Force Subscribe has been ENABLED.**")

    # Set new channel
    try:
        channel_val = int(arg) if (arg.startswith('-100') or arg.isdigit()) else arg
        entity = await bot_client.get_entity(channel_val)

        if isinstance(entity, types.User):
            return await event.respond("❌ **Error:** That is a personal user account, not a channel!")

        if isinstance(entity, (types.Channel, types.Chat)):
            cfg["channel_id"] = entity.id if isinstance(entity, types.Chat) else entity.id
            if not str(cfg["channel_id"]).startswith("-100"):
                cfg["channel_id"] = int(f"-100{cfg['channel_id']}")
            cfg["title"] = getattr(entity, 'title', 'Official Channel')
            cfg["is_active"] = True

            # Try to get invite link
            if getattr(entity, 'username', None):
                cfg["invite_link"] = f"https://t.me/{entity.username}"
            else:
                try:
                    exp = await bot_client(functions.messages.ExportChatInviteRequest(peer=entity))
                    cfg["invite_link"] = exp.link
                except Exception:
                    cfg["invite_link"] = None

            save_fsub_config(cfg)
            return await event.respond(
                f"✅ **Force Subscribe Channel Updated!**\n\n"
                f"📢 **Channel:** `{cfg['title']}`\n"
                f"🆔 **ID:** `{cfg['channel_id']}`\n"
                f"🔗 **Link:** {cfg['invite_link'] or 'Auto-generated'}"
            )
    except Exception as e:
        return await event.respond(f"❌ **Failed to set channel:** `{e}`\nMake sure bot is Admin in the channel.")


@bot_client.on(events.CallbackQuery(data=b"admin_main"))
async def admin_main_callback(event):
    if not is_admin(event.sender_id):
        return
    stats = get_stats()
    text = (
        "👑 **ADMINISTRATOR CONTROL PANEL**\n\n"
        f"👥 **Total Registered Users:** `{stats['total_users']}`\n"
        f"🚫 **Banned Users:** `{stats['banned_users']}`\n"
        f"🛡️ **Admin Accounts:** `{stats['admin_count']}`\n\n"
        "Select an administrative action below:"
    )
    await event.edit(text, buttons=get_admin_dashboard_buttons())


@bot_client.on(events.CallbackQuery(data=b"close_admin_panel"))
async def close_admin_callback(event):
    await event.delete()


@bot_client.on(events.CallbackQuery(data=b"admin_stats"))
async def admin_stats_callback(event):
    if not is_admin(event.sender_id):
        return
    stats = get_stats()
    user_auth = await ensure_user_client()
    engine_status = "🟢 Connected & Authorized" if user_auth else "🔴 Disconnected / Bot Token Only"
    fsub_cfg = load_fsub_config()

    text = (
        "📊 **DETAILED SYSTEM STATISTICS**\n\n"
        f"👥 **Registered Users:** `{stats['total_users']}`\n"
        f"🚫 **Banned Users:** `{stats['banned_users']}`\n"
        f"🛡️ **Active Admins:** `{stats['admin_count']}`\n"
        f"🔑 **MTProto Engine:** {engine_status}\n"
        f"🔒 **Force Subscribe:** `{'ON' if fsub_cfg.get('is_active') else 'OFF'}` ({fsub_cfg.get('title', 'None')})\n"
        f"⏱️ **Check Delay:** `{CHECK_DELAY}s`\n"
        f"📦 **Max Batch Limit:** `{MAX_LINKS_PER_BATCH}` links"
    )
    buttons = [[Button.inline("🔙 Back to Admin", data=b"admin_main")]]
    await event.edit(text, buttons=buttons)


@bot_client.on(events.CallbackQuery(data=b"admin_fsub_menu"))
async def admin_fsub_menu_callback(event):
    if not is_admin(event.sender_id):
        return
    cfg = load_fsub_config()
    status_text = "🟢 ACTIVE" if cfg.get("is_active") else "🔴 DISABLED"

    text = (
        "🔒 **FORCE SUBSCRIBE CONFIGURATION**\n\n"
        f"• **Status:** {status_text}\n"
        f"• **Channel Title:** `{cfg.get('title', 'Not Set')}`\n"
        f"• **Channel ID:** `{cfg.get('channel_id', 'Not Set')}`\n"
        f"• **Link:** {cfg.get('invite_link') or 'Auto'}\n\n"
        "Choose an option below:"
    )

    toggle_btn = Button.inline("🔴 Turn OFF" if cfg.get("is_active") else "🟢 Turn ON", data=b"admin_fsub_toggle")
    buttons = [
        [toggle_btn],
        [Button.inline("🔙 Back to Admin", data=b"admin_main")]
    ]
    await event.edit(text, buttons=buttons)


@bot_client.on(events.CallbackQuery(data=b"admin_fsub_toggle"))
async def admin_fsub_toggle_callback(event):
    if not is_admin(event.sender_id):
        return
    cfg = load_fsub_config()
    cfg["is_active"] = not cfg.get("is_active", False)
    save_fsub_config(cfg)
    await event.answer(f"Force Sub is now {'ENABLED' if cfg['is_active'] else 'DISABLED'}", alert=True)
    await admin_fsub_menu_callback(event)


# --- MESSAGE ROUTING & BATCH LINK CHECKING ---

@bot_client.on(events.NewMessage)
async def incoming_message_handler(event):
    sender_id = event.sender_id
    if is_banned(sender_id):
        return

    text_content = (event.text or "").strip()
    if text_content.startswith('/'):
        return

    # Check Force Sub
    input_user = await event.get_input_sender()
    if not await check_fsub_membership(bot_client, sender_id, input_user):
        return await send_fsub_prompt(event, bot_client, sender_id)

    # Document upload handling
    if event.file and event.file.name and event.file.name.endswith('.txt'):
        try:
            file_bytes = await event.download_media(bytes)
            text_content = file_bytes.decode('utf-8', errors='ignore')
        except Exception as e:
            return await event.respond(f"⚠️ Could not read document: {str(e)}")

    # Extract all Telegram links (plain text, formatted hyperlinks/entities, and buttons)
    links = extract_links_from_message(event.message)
    if not links and text_content:
        links = extract_telegram_links(text_content)

    # If still no links, check if user replied to another message
    if not links and event.is_reply:
        try:
            reply_msg = await event.get_reply_message()
            if reply_msg:
                links = extract_links_from_message(reply_msg)
        except Exception:
            pass

    if not links:
        return

    if len(links) > MAX_LINKS_PER_BATCH:
        return await event.respond(
            f"⚠️ **Limit Exceeded:** You sent `{len(links)}` links.\n"
            f"Maximum allowed per batch is `{MAX_LINKS_PER_BATCH}` links. Please split your list."
        )

    job_id = create_checking_job(sender_id, links)

    buttons = [
        [Button.inline(f"🚀 Start Checking ({len(links)} Links)", data=f"chk_start:{job_id}".encode())],
        [Button.inline("❌ Cancel", data=f"chk_cancel:{job_id}".encode())]
    ]
    await event.respond(
        f"🔗 **Detected {len(links)} Unique Telegram Links**\n\n"
        "Duplicate links have been automatically removed.\n"
        "Click **Start Checking** to begin verification.",
        buttons=buttons
    )


@bot_client.on(events.CallbackQuery(pattern=r'^chk_cancel:(.+)$'))
async def cancel_job_callback(event):
    job_id = event.pattern_match.group(1).decode() if isinstance(event.pattern_match.group(1), bytes) else event.pattern_match.group(1)
    active_jobs.pop(job_id, None)
    await event.edit("❌ **Operation Cancelled.** Send new links anytime.")


@bot_client.on(events.CallbackQuery(pattern=r'^(?:chk_start:(.+)|start_check)$'))
async def start_check_callback(event):
    sender_id = event.sender_id
    if is_banned(sender_id):
        return await event.answer("You are banned from using this bot.", alert=True)

    input_user = await event.get_input_sender()
    if not await check_fsub_membership(bot_client, sender_id, input_user):
        return await send_fsub_prompt(event, bot_client, sender_id)

    raw_match = event.pattern_match.group(1)
    job_id = None
    if raw_match:
        job_id = raw_match.decode() if isinstance(raw_match, bytes) else str(raw_match)

    job = active_jobs.get(job_id) if job_id else None
    if not job:
        fallback_jid = user_latest_job.get(sender_id)
        if fallback_jid:
            job = active_jobs.get(fallback_jid)
            job_id = fallback_jid

    # Auto fallback re-extraction if bot restarted
    if not job or not job.get('pending_links'):
        try:
            msg = await event.get_message()
            extracted = []
            if msg:
                extracted = extract_links_from_message(msg)
                if not extracted and msg.is_reply:
                    reply_msg = await msg.get_reply_message()
                    if reply_msg:
                        extracted = extract_links_from_message(reply_msg)
            if extracted:
                job_id = create_checking_job(sender_id, extracted)
                job = active_jobs[job_id]
        except Exception as e:
            print(f"Fallback extraction notice: {e}")

    if not job or not job.get('pending_links'):
        return await event.answer("⚠️ No links found. Please send your links again.", alert=True)

    if job.get('is_running'):
        return await event.answer("⏳ Verification is already running for this batch!", alert=True)

    job['is_running'] = True
    links = job['pending_links']
    total_count = len(links)

    # Check MTProto connection
    is_user_auth = await ensure_user_client()
    active_client = user_client if is_user_auth else bot_client

    await event.edit(
        f"🔄 **Starting Verification...**\n\n"
        f"📊 Total Links: `{total_count}`\n"
        f"⏳ Validating links safely with MTProto..."
    )

    working_list = []
    expired_list = []
    last_update_time = time.time()

    for idx, url in enumerate(links, start=1):
        res = await check_single_link(active_client, url)

        status = res.get('status')
        if status == 'working':
            working_list.append(res)
        else:
            expired_list.append(res)

        current_time = time.time()
        is_last = (idx == total_count)
        if is_last or (current_time - last_update_time >= 2.5) or (idx % 4 == 0):
            try:
                progress_text = (
                    f"🔍 **Checking Links in Progress...**\n\n"
                    f"**Progress:** `{idx} / {total_count}` (`{int((idx/total_count)*100)}%`)\n\n"
                    f"✅ **Working:** `{len(working_list)}`\n"
                    f"❌ **Expired / Invalid:** `{len(expired_list)}`\n\n"
                    f"⏳ *Please wait while MTProto validates links safely...*"
                )
                await event.edit(progress_text)
                last_update_time = current_time
            except Exception:
                pass

        await asyncio.sleep(CHECK_DELAY)

    job['is_running'] = False
    job['results'] = {
        'total': total_count,
        'working': working_list,
        'expired': expired_list
    }

    # Dispatch Activity Log to Log Channel
    sender_obj = await event.get_sender()
    if sender_obj:
        asyncio.create_task(log_link_check_activity(bot_client, sender_obj, total_count, working_list, expired_list))

    working_pct = (len(working_list) / total_count * 100) if total_count > 0 else 0
    expired_pct = (len(expired_list) / total_count * 100) if total_count > 0 else 0

    summary_text = (
        "🏁 **CHECK COMPLETE**\n\n"
        "📊 **Statistics:**\n"
        f"• **Total Links:** `{total_count}`\n"
        f"• ✅ **Working Links:** `{len(working_list)}` ({working_pct:.1f}%)\n"
        f"• ❌ **Expired / Invalid:** `{len(expired_list)}` ({expired_pct:.1f}%)\n\n"
        "**Choose how to receive working links:**"
    )

    result_buttons = [
        [Button.inline(f"📋 Get as Text ({len(working_list)})", data=f"chk_text:{job_id}".encode())],
        [Button.inline("📁 Get as File (.txt)", data=f"chk_file:{job_id}".encode())],
        [Button.inline("🔄 Check New Links", data=b"back_to_start")]
    ]

    await event.edit(summary_text, buttons=result_buttons)


# --- RESULT DELIVERY HANDLERS ---

@bot_client.on(events.CallbackQuery(pattern=r'^(?:chk_text:(.+)|get_as_text)$'))
async def get_text_callback(event):
    sender_id = event.sender_id
    raw_match = event.pattern_match.group(1)
    job_id = None
    if raw_match:
        job_id = raw_match.decode() if isinstance(raw_match, bytes) else str(raw_match)

    job = active_jobs.get(job_id) if job_id else None
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
        members = item.get('members', 0)
        req = item.get('request_needed', False)
        suffix = " (Join Request)" if req else ""
        if title:
            lines.append(f"• [{title}]({url}) ({members} members){suffix}\n  `{url}`")
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


@bot_client.on(events.CallbackQuery(pattern=r'^(?:chk_file:(.+)|get_as_file)$'))
async def get_file_callback(event):
    sender_id = event.sender_id
    raw_match = event.pattern_match.group(1)
    job_id = None
    if raw_match:
        job_id = raw_match.decode() if isinstance(raw_match, bytes) else str(raw_match)

    job = active_jobs.get(job_id) if job_id else None
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
        members = item.get('members', 0)
        req = " [Join Request]" if item.get('request_needed') else ""
        file_content += f"{url} | Title: {title} | Members: {members}{req}\n"

    file_bytes = io.BytesIO(file_content.encode('utf-8'))
    file_bytes.name = f"working_links_{int(time.time())}.txt"

    caption = (
        f"📄 **Working Links Export**\n\n"
        f"✅ Total Working: `{len(working)} / {total}`\n"
        f"All verified invite links are listed inside."
    )

    await bot_client.send_file(sender_id, file=file_bytes, caption=caption)


# --- HEALTH SERVER FOR RENDER 24/7 ---

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
    print(f"🌐 Health server listening on port {port}")


async def main():
    try:
        await start_web_server()
    except Exception as e:
        print(f"Web server notice: {e}")

    print("=" * 60)
    print("🚀 Modular Telegram Link Checker Bot is STARTING...")
    print(f"👑 Admin IDs: {ADMIN_IDS}")
    print(f"📢 Log Channel: Active")
    print(f"🔒 Force Sub Channel: Active")
    print("=" * 60)

    asyncio.create_task(log_bot_startup(bot_client, ADMIN_IDS))
    await bot_client.run_until_disconnected()


if __name__ == '__main__':
    bot_client.loop.run_until_complete(main())
