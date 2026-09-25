import os
import io
import asyncio
import time
from telethon import TelegramClient, events, Button
from telethon.tl.custom import Message

from config import (
    API_ID,
    API_HASH,
    BOT_TOKEN,
    ADMIN_IDS,
    CHECK_DELAY,
    MAX_LINKS_PER_BATCH,
    validate_config
)
from checker import extract_telegram_links, check_single_link

# Ensure credentials are present before starting
if not validate_config():
    print("❌ Cannot start bot. Please verify your environment variables.")
    exit(1)

# Initialize Telethon Client as Bot
client = TelegramClient('tg_link_checker_bot', API_ID, API_HASH).start(bot_token=BOT_TOKEN)

# In-memory session store for pending batches and results
# session_data[user_id] = { 'pending_links': [...], 'results': {...}, 'last_time': ... }
user_sessions = {}


def is_admin(user_id: int) -> bool:
    """Checks if the user ID is in the configured ADMIN_IDS list."""
    return user_id in ADMIN_IDS


# --- COMMAND HANDLERS ---

@client.on(events.NewMessage(pattern=r'^/start$'))
async def start_handler(event: Message):
    sender_id = event.sender_id
    
    if not is_admin(sender_id):
        await event.reply(
            "⛔ **Access Denied**\n\n"
            "This bot is private and accessible only to authorized administrators.",
            buttons=Button.clear()
        )
        return

    welcome_text = (
        "💎 **Welcome to Telegram Bulk Invite Link Checker Bot**\n\n"
        "A smart and fast bot to clean up your Telegram invite links and keep only active ones.\n\n"
        "⚡ **Features:**\n"
        "• Bulk link extraction from messy text/chats\n"
        "• Deep MTProto validation (Active, Expired, Revoked, Rate-limits)\n"
        "• Duplicate link removal\n"
        "• Live progress tracking\n"
        "• Export active links as Text or .TXT File\n\n"
        "📥 **How to use:**\n"
        "Simply send or forward a message containing your Telegram invite links here!"
    )
    
    buttons = [
        [Button.inline("ℹ️ Bot Info / Limits", data=b"help_info")],
        [Button.inline("⚙️ Check Status", data=b"bot_status")]
    ]
    
    await event.reply(welcome_text, buttons=buttons)


@client.on(events.CallbackQuery(data=b"help_info"))
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


@client.on(events.CallbackQuery(data=b"bot_status"))
async def status_callback(event):
    if not is_admin(event.sender_id):
        return await event.answer("Access Denied", alert=True)
        
    status_text = (
        "✅ **Bot Status: ONLINE & READY**\n\n"
        f"👤 **Configured Admins:** `{len(ADMIN_IDS)}`\n"
        f"⏱️ **Delay:** `{CHECK_DELAY}s`\n"
        f"📦 **Max Batch Size:** `{MAX_LINKS_PER_BATCH}`\n"
    )
    await event.edit(status_text, buttons=[[Button.inline("⬅️ Back", data=b"back_to_start")]])


@client.on(events.CallbackQuery(data=b"back_to_start"))
async def back_callback(event):
    if not is_admin(event.sender_id):
        return await event.answer("Access Denied", alert=True)
        
    welcome_text = (
        "💎 **Telegram Bulk Invite Link Checker Bot**\n\n"
        "Send or forward any text message with Telegram invite links to start checking."
    )
    buttons = [
        [Button.inline("ℹ️ Bot Info / Limits", data=b"help_info")],
        [Button.inline("⚙️ Check Status", data=b"bot_status")]
    ]
    await event.edit(welcome_text, buttons=buttons)


# --- MESSAGE & LINK RECEIVER HANDLER ---

@client.on(events.NewMessage)
async def message_handler(event: Message):
    # Ignore slash commands handled above
    if event.text and event.text.startswith('/'):
        return
        
    sender_id = event.sender_id
    
    if not is_admin(sender_id):
        return
        
    text_content = event.text or ""
    
    # If user sent a .txt document file, read its text
    if event.file and event.file.name and event.file.name.endswith('.txt'):
        try:
            file_bytes = await event.download_media(bytes)
            text_content = file_bytes.decode('utf-8', errors='ignore')
        except Exception as e:
            await event.reply(f"⚠️ Could not read document: {str(e)}")
            return

    if not text_content:
        return

    # Extract unique links
    links = extract_telegram_links(text_content)
    
    if not links:
        # If user typed random text with no links
        return

    if len(links) > MAX_LINKS_PER_BATCH:
        await event.reply(
            f"⚠️ **Limit Exceeded:** You sent `{len(links)}` links.\n"
            f"Maximum allowed per batch is `{MAX_LINKS_PER_BATCH}` links. Please split your list."
        )
        return

    # Store in user session
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

@client.on(events.CallbackQuery(data=b"cancel_check"))
async def cancel_callback(event):
    sender_id = event.sender_id
    if not is_admin(sender_id):
        return await event.answer("Access Denied", alert=True)
        
    user_sessions.pop(sender_id, None)
    await event.edit("❌ **Operation Cancelled.** Send new links anytime.")


@client.on(events.CallbackQuery(data=b"start_check"))
async def start_check_callback(event):
    sender_id = event.sender_id
    if not is_admin(sender_id):
        return await event.answer("Access Denied", alert=True)
        
    session = user_sessions.get(sender_id)
    if not session or not session.get('pending_links'):
        return await event.edit("⚠️ No links found in queue. Please send your links again.")

    links = session['pending_links']
    total_count = len(links)
    
    working_list = []
    expired_list = []
    error_list = []

    progress_msg = await event.edit(
        f"🔄 **Starting Verification...**\n\n"
        f"📊 Total Links: `{total_count}`\n"
        f"⏳ Initializing MTProto client..."
    )

    last_update_time = time.time()
    
    for idx, url in enumerate(links, start=1):
        res = await check_single_link(client, url)
        
        if res['status'] == 'working':
            working_list.append(res)
        elif res['status'] == 'expired':
            expired_list.append(res)
        else:
            error_list.append(res)

        # Update progress message smoothly every 3 links or at least every 2.5 seconds
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
                pass # Avoid flood on rapid edit

        # Rate-limiting safe delay
        await asyncio.sleep(CHECK_DELAY)

    # Save final results in session
    session['results'] = {
        'total': total_count,
        'working': working_list,
        'expired': expired_list,
        'error': error_list
    }

    # Final Summary Message (Sortlink Bot style from video)
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
        summary_text += f"• ⚠️ **Could Not Check (Rate/Network):** `{len(error_list)}`\n"

    summary_text += "\n**Choose how to receive working links:**"

    result_buttons = [
        [Button.inline(f"📋 Get as Text ({len(working_list)})", data=b"get_as_text")],
        [Button.inline("📁 Get as File (.txt)", data=b"get_as_file")],
        [Button.inline("🔄 Check New Links", data=b"back_to_start")]
    ]

    await event.edit(summary_text, buttons=result_buttons)


# --- RESULT DELIVERY HANDLERS ---

@client.on(events.CallbackQuery(data=b"get_as_text"))
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

    # Format links with title if available
    lines = []
    for item in working:
        url = item['url']
        title = item.get('title')
        members = item.get('members', 0)
        
        if title:
            lines.append(f"• [{title}]({url}) ({members} members)\n  `{url}`")
        else:
            lines.append(f"`{url}`")

    # Telegram message limit is 4096 characters.
    # Chunk them safely into messages of max ~3500 characters.
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
        await client.send_message(sender_id, c, link_preview=False)


@client.on(events.CallbackQuery(data=b"get_as_file"))
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

    # Build clean text file content
    file_content = f"# TELEGRAM WORKING INVITE LINKS REPORT\n"
    file_content += f"# Generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n"
    file_content += f"# Total Checked: {total} | Active/Working: {len(working)}\n"
    file_content += "# " + "="*50 + "\n\n"
    
    for item in working:
        url = item['url']
        title = item.get('title') or 'N/A'
        members = item.get('members', 0)
        file_content += f"{url} | Title: {title} | Members: {members}\n"

    # Convert string to in-memory bytes file
    file_bytes = io.BytesIO(file_content.encode('utf-8'))
    file_bytes.name = f"working_links_{int(time.time())}.txt"

    caption = (
        f"📄 **Working Links Export**\n\n"
        f"✅ Total Working: `{len(working)} / {total}`\n"
        f"All verified invite links are listed inside."
    )

    await client.send_file(sender_id, file=file_bytes, caption=caption)


# --- MAIN STARTUP ---

print("="*60)
print("🚀 Telegram Bulk Invite Link Checker Bot is STARTING...")
print(f"👤 Configured Admin IDs: {ADMIN_IDS}")
print(f"⏱️ Safe Check Delay: {CHECK_DELAY}s")
print("="*60)

# Run Telethon event loop until disconnected
client.run_until_disconnected()
