import time
import asyncio
from typing import List, Dict, Any
from telethon import TelegramClient
from config import LOG_CHANNEL_ID

async def log_bot_startup(bot_client: TelegramClient, admin_ids: list):
    """Sends log when bot comes online."""
    try:
        await asyncio.sleep(2)
        log_msg = (
            "🤖 **#BOT_STARTED_ONLINE**\n\n"
            f"✅ **Status:** Running 24/7\n"
            f"👑 **Admins:** `{len(admin_ids)} Active`\n"
            f"📢 **Log Channel:** Connected\n"
            f"📅 **Time:** `{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}`"
        )
        await bot_client.send_message(LOG_CHANNEL_ID, log_msg)
    except Exception as e:
        print(f"Log Error (Startup): {e}")


async def log_new_user_start(bot_client: TelegramClient, user, total_users_count: int):
    """Sends log when a new user starts the bot."""
    try:
        user_id = user.id
        first_name = user.first_name or "Unknown"
        last_name = user.last_name or ""
        full_name = f"{first_name} {last_name}".strip()
        username = f"@{user.username}" if user.username else "No Username"

        log_msg = (
            "🆕 **#NEW_USER_STARTED**\n\n"
            f"👤 **User:** [{full_name}](tg://user?id={user_id})\n"
            f"🆔 **User ID:** `{user_id}`\n"
            f"🔗 **Username:** {username}\n"
            f"👥 **Total Registered Users:** `{total_users_count}`\n"
            f"📅 **Time:** `{time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}`"
        )
        await bot_client.send_message(LOG_CHANNEL_ID, log_msg)
    except Exception as e:
        print(f"Log Error (New User): {e}")


async def log_link_check_activity(bot_client: TelegramClient, user, total_links: int, working_links: list, expired_links: list):
    """Sends detailed link verification activity to the Log Channel."""
    try:
        user_id = user.id
        first_name = user.first_name or "Unknown"
        username = f"@{user.username}" if user.username else "No Username"
        
        header = (
            "🔗 **#LINK_CHECK_LOG**\n\n"
            f"👤 **User:** [{first_name}](tg://user?id={user_id}) ({username})\n"
            f"🆔 **User ID:** `{user_id}`\n\n"
            f"📊 **Statistics:**\n"
            f"• Total Links: `{total_links}`\n"
            f"• ✅ Working: `{len(working_links)}`\n"
            f"• ❌ Expired: `{len(expired_links)}`\n\n"
        )

        all_lines = [header]

        if working_links:
            all_lines.append(f"✅ **Working Links ({len(working_links)} Total):**\n\n")
            for item in working_links:
                url = item['url']
                title = item.get('title')
                members = item.get('members', 0)
                if title:
                    all_lines.append(f"• {title} ({members} members)\n  {url}\n\n")
                else:
                    all_lines.append(f"• {url}\n\n")

        if expired_links:
            all_lines.append(f"❌ **Expired Links ({len(expired_links)} Total):**\n\n")
            for item in expired_links:
                url = item['url']
                all_lines.append(f"• {url}\n")
            all_lines.append("\n")

        # Split into safe chunks (<3500 chars) to prevent Telegram flood/length errors
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
