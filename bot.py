import os
import io
import asyncio
import time
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.tl.custom import Message

from config import (
    API_ID,
    API_HASH,
    BOT_TOKEN,
    ADMIN_IDS,
    CHECK_DELAY,
    MAX_LINKS_PER_BATCH,
    SESSION_STRING,
    validate_config
)
from checker import extract_telegram_links, check_single_link

if not validate_config():
    print("❌ Cannot start bot. Please verify your environment variables.")
    exit(1)

# 1. Main Bot Client (Handles telegram interface, commands, buttons)
bot_client = TelegramClient('tg_bot_session', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# 2. MTProto User Client (Required for checking private invite links)
user_session_str = SESSION_STRING
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
login_states = {} # For interactive login flow: step, phone, phone_code_hash, client

def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


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


# --- COMMAND HANDLERS ---

@bot_client.on(events.NewMessage(pattern=r'^/start$'))
async def start_handler(event: Message):
    sender_id = event.sender_id
    if not is_admin(sender_id):
        await event.reply("⛔ **Access Denied**\n\nThis bot is private and accessible only to authorized administrators.", buttons=Button.clear())
        return

    is_logged_in = await ensure_user_client()
    status_badge = "🟢 **Engine Active (Ready to check)**" if is_logged_in else "🟡 **Needs 1-Time Account Connect**"

    welcome_text = (
        "💎 **Welcome to Telegram Bulk Invite Link Checker Bot**\n\n"
        "A smart and fast bot to clean up your Telegram invite links and keep only active ones.\n\n"
        f"📡 **Status:** {status_badge}\n\n"
        "⚡ **Features:**\n"
        "• Bulk link extraction from messy text/chats\n"
        "• Deep MTProto validation (Active, Expired, Revoked)\n"
        "• Duplicate link removal\n"
        "• Live progress tracking\n"
        "• Export active links as Text or .TXT File\n\n"
        "📥 **How to use:**\n"
        "Send or forward any message with Telegram links here!"
    )
    
    buttons = []
    if not is_logged_in:
        buttons.append([Button.inline("🔑 Connect Account (1-Click Setup)", data=b"start_login_flow")])
    buttons.append([Button.inline("ℹ️ Bot Info & Limits", data=b"help_info"), Button.inline("⚙️ Check Status", data=b"bot_status")])
    
    await event.reply(welcome_text, buttons=buttons)


# --- INTERACTIVE 1-CLICK LOGIN FLOW ---

@bot_client.on(events.CallbackQuery(data=b"start_login_flow"))
async def start_login_callback(event):
    sender_id = event.sender_id
    if not is_admin(sender_id):
        return await event.answer("Access Denied", alert=True)

    login_states[sender_id] = {'step': 'awaiting_phone'}
    
    prompt_text = (
        "📱 **Connect Telegram Account (1-Time Step)**\n\n"
        "Telegram API requires an account connection to verify private invite links (`t.me/+...`).\n\n"
        "👉 **Abhi apna Phone Number reply karein** (Country code ke sath):\n\n"
        "Example: `+919876543210`"
    )
    await event.edit(prompt_text, buttons=[[Button.inline("❌ Cancel", data=b"cancel_login")]])


@bot_client.on(events.CallbackQuery(data=b"cancel_login"))
async def cancel_login_callback(event):
    sender_id = event.sender_id
    login_states.pop(sender_id, None)
    await event.edit("❌ Connection cancelled. You can connect anytime by typing `/login`.")


@bot_client.on(events.NewMessage(pattern=r'^/login(?:\s+(.+))?$'))
async def login_cmd_handler(event: Message):
    global user_client
    sender_id = event.sender_id
    if not is_admin(sender_id):
        return

    args = event.pattern_match.group(1)
    if not args:
        login_states[sender_id] = {'step': 'awaiting_phone'}
        return await event.reply(
            "📱 **Please enter your phone number with country code:**\n\n"
            "Example: `+919876543210`"
        )

    await process_phone_submission(event, sender_id, args.strip())


async def process_phone_submission(event: Message, sender_id: int, phone_raw: str):
    global user_client
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
            f"Apne Telegram app me official Telegram notification check karein aur **OTP code yahan reply karein**:\n\n"
            f"Example: `12345`"
        )
    except Exception as e:
        login_states.pop(sender_id, None)
        await event.reply(f"❌ Failed to request code: `{str(e)}`\nPlease verify your phone number format (e.g. `+919876543210`).")


async def process_otp_submission(event: Message, sender_id: int, otp_raw: str):
    global user_client, user_session_str
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

        await event.reply(
            "🎉 **SUCCESS: Account Connected!**\n\n"
            "✅ Private invite link checking is now **100% Active & Accurate**.\n\n"
            "🚀 Ab aap koi bhi links bhej kar **Start Checking** dabayein!"
        )

        # If user had pending links, auto-prompt to check
        session = user_sessions.get(sender_id)
        if session and session.get('pending_links'):
            links = session['pending_links']
            buttons = [
                [Button.inline(f"🚀 Check {len(links)} Pending Links Now", data=b"start_check")],
                [Button.inline("❌ Cancel", data=b"cancel_check")]
            ]
            await event.reply(f"🔗 Aapki `{len(links)}` links queue mein hain. Check start karein?", buttons=buttons)
        return True

    except Exception as e:
        error_msg = str(e)
        if "password" in error_msg.lower() or "2fa" in error_msg.lower():
            state['step'] = 'awaiting_password'
            await event.reply(
                "🔒 **Two-Step Verification (2FA) Enabled!**\n\n"
                "Apna 2FA Password yahan reply karein:"
            )
            return True
        else:
            await event.reply(f"❌ Invalid OTP or Sign-in failed: `{error_msg}`\nSend OTP again or send `/login` to restart.")
            return True


async def process_password_submission(event: Message, sender_id: int, password_raw: str):
    global user_client, user_session_str
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

        await event.reply(
            "🎉 **SUCCESS: 2FA Login Completed!**\n\n"
            "✅ Private invite link checking is now **100% Active & Accurate**.\n\n"
            "🚀 Ab aap links bhej kar check start kar sakte hain!"
        )
        return True
    except Exception as e:
        await event.reply(f"❌ Incorrect 2FA Password: `{str(e)}`\nTry again:")
        return True


# --- HELP & STATUS ---

@bot_client.on(events.CallbackQuery(data=b"help_info"))
async def help_callback(event):
    if not is_admin(event.sender_id):
        return await event.answer("Access Denied", alert=True)
        
    help_text = (
        "📋 **Supported Links & Limits:**\n\n"
        "• Private Invites: `https://t.me/+...` or `t.me/joinchat/...`\n"
        "• Public Chats/Channels: `https://t.me/username`\n"
        f"• Global Limit: Up to **{MAX_LINKS_PER_BATCH} links** per batch.\n"
        f"• Safe Rate-Limit Delay: **{CHECK_DELAY}s** per link.\n\n"
        "Send your links directly as text anytime."
    )
    await event.edit(help_text, buttons=[[Button.inline("⬅️ Back", data=b"back_to_start")]])


@bot_client.on(events.CallbackQuery(data=b"bot_status"))
async def status_callback(event):
    if not is_admin(event.sender_id):
        return await event.answer("Access Denied", alert=True)
    
    is_user_auth = await ensure_user_client()
    user_auth_str = "✅ Connected (Active)" if is_user_auth else "❌ Not Connected (Tap 'Connect Account')"
    
    status_text = (
        "✅ **Bot Status: ONLINE**\n\n"
        f"👤 **Configured Admins:** `{len(ADMIN_IDS)}`\n"
        f"🔑 **MTProto Engine:** `{user_auth_str}`\n"
        f"⏱️ **Delay:** `{CHECK_DELAY}s`\n"
        f"📦 **Max Batch Size:** `{MAX_LINKS_PER_BATCH}`\n"
    )
    buttons = []
    if not is_user_auth:
        buttons.append([Button.inline("🔑 Connect Account", data=b"start_login_flow")])
    buttons.append([Button.inline("⬅️ Back", data=b"back_to_start")])
    await event.edit(status_text, buttons=buttons)


@bot_client.on(events.CallbackQuery(data=b"back_to_start"))
async def back_callback(event):
    if not is_admin(event.sender_id):
        return await event.answer("Access Denied", alert=True)
        
    is_logged_in = await ensure_user_client()
    status_badge = "🟢 **Engine Active**" if is_logged_in else "🟡 **Account Connect Needed**"

    welcome_text = (
        "💎 **Telegram Bulk Invite Link Checker Bot**\n\n"
        f"📡 **Status:** {status_badge}\n\n"
        "Send or forward any text message with Telegram invite links to start checking."
    )
    buttons = []
    if not is_logged_in:
        buttons.append([Button.inline("🔑 Connect Account", data=b"start_login_flow")])
    buttons.append([Button.inline("ℹ️ Bot Info / Limits", data=b"help_info"), Button.inline("⚙️ Check Status", data=b"bot_status")])
    await event.edit(welcome_text, buttons=buttons)


# --- MESSAGE & LINK RECEIVER HANDLER ---

@client_on_msg = bot_client.on(events.NewMessage)
async def message_handler(event: Message):
    sender_id = event.sender_id
    if not is_admin(sender_id):
        return

    text_content = (event.text or "").strip()
    if not text_content:
        return

    # Check if user is in an active login flow
    state = login_states.get(sender_id)
    if state:
        step = state.get('step')
        if step == 'awaiting_phone':
            await process_phone_submission(event, sender_id, text_content)
            return
        elif step == 'awaiting_otp':
            # Digits only or with /otp
            otp_cleaned = text_content.replace('/otp', '').strip()
            if otp_cleaned.isdigit() or len(otp_cleaned) in (5, 6):
                await process_otp_submission(event, sender_id, otp_cleaned)
                return
        elif step == 'awaiting_password':
            pwd_cleaned = text_content.replace('/password', '').strip()
            await process_password_submission(event, sender_id, pwd_cleaned)
            return

    # Handle document upload
    if event.file and event.file.name and event.file.name.endswith('.txt'):
        try:
            file_bytes = await event.download_media(bytes)
            text_content = file_bytes.decode('utf-8', errors='ignore')
        except Exception as e:
            await event.reply(f"⚠️ Could not read document: {str(e)}")
            return

    # Ignore commands
    if text_content.startswith('/'):
        return

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

    is_logged_in = await ensure_user_client()
    
    if not is_logged_in:
        buttons = [
            [Button.inline("🔑 Connect Account to Check (1-Click)", data=b"start_login_flow")],
            [Button.inline(f"🚀 Try Checking Anyway ({len(links)} Links)", data=b"start_check")],
            [Button.inline("❌ Cancel", data=b"cancel_check")]
        ]
        await event.reply(
            f"🔗 **Detected {len(links)} Unique Telegram Links**\n\n"
            "⚠️ **Note:** Telegram account is not connected yet.\n"
            "To check private invite links (`t.me/+...`), tap **Connect Account** below.",
            buttons=buttons
        )
    else:
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
    if not is_admin(sender_id):
        return await event.answer("Access Denied", alert=True)
        
    user_sessions.pop(sender_id, None)
    await event.edit("❌ **Operation Cancelled.** Send new links anytime.")


@bot_client.on(events.CallbackQuery(data=b"start_check"))
async def start_check_callback(event):
    sender_id = event.sender_id
    if not is_admin(sender_id):
        return await event.answer("Access Denied", alert=True)
        
    session = user_sessions.get(sender_id)
    if not session or not session.get('pending_links'):
        return await event.edit("⚠️ No links found in queue. Please send your links again.")

    links = session['pending_links']
    total_count = len(links)
    
    # Determine which client to use: Prefer user_client if logged in, fallback to bot_client
    is_user_auth = await ensure_user_client()
    active_client = user_client if is_user_auth else bot_client

    if not is_user_auth:
        await event.edit(
            f"🔄 **Starting Verification (Public entities only)...**\n\n"
            f"⚠️ *Note: User session is not logged in. For private links (t.me/+...), tap 'Connect Account'.*\n"
            f"📊 Total Links: `{total_count}`"
        )
    else:
        await event.edit(
            f"🔄 **Starting Verification (Full MTProto Engine)...**\n\n"
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
        summary_text += f"• ⚠️ **Could Not Check (Rate/Session):** `{len(error_list)}`\n"
        if not is_user_auth:
            summary_text += "\n*(💡 Private links require connecting your Telegram account)*\n"

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
    if not is_admin(sender_id):
        return await event.answer("Access Denied", alert=True)

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
    if not is_admin(sender_id):
        return await event.answer("Access Denied", alert=True)

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
    print("🚀 Telegram Bulk Invite Link Checker Bot is STARTING...")
    print(f"👤 Configured Admin IDs: {ADMIN_IDS}")
    print(f"⏱️ Safe Check Delay: {CHECK_DELAY}s")
    print("="*60)
    await bot_client.run_until_disconnected()

if __name__ == '__main__':
    bot_client.loop.run_until_complete(main())
