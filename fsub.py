import os
import json
from typing import Tuple, Optional
from telethon import TelegramClient, events, Button
from telethon.tl import functions, types
from telethon.errors import (
    UserNotParticipantError,
    ChannelPrivateError,
    ChatAdminRequiredError,
    ChannelInvalidError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    RPCError,
)
from config import FSUB_CHANNEL_ID

FSUB_FILE = "data/fsub_config.json"
os.makedirs("data", exist_ok=True)


def load_fsub_config() -> dict:
    if os.path.exists(FSUB_FILE):
        try:
            with open(FSUB_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    # Default fallback
    return {
        "channel_id": FSUB_CHANNEL_ID if FSUB_CHANNEL_ID != 0 else None,
        "invite_link": None,
        "title": "Official Channel",
        "is_active": True if (FSUB_CHANNEL_ID and FSUB_CHANNEL_ID != 0) else False
    }


def save_fsub_config(cfg: dict):
    try:
        with open(FSUB_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as e:
        print(f"Error saving fsub config: {e}")


async def get_fsub_channel_link(bot_client: TelegramClient) -> Tuple[Optional[str], str]:
    """
    Returns (invite_url, channel_title) for Force Sub.
    Ensures only real Telegram Channels are used.
    """
    cfg = load_fsub_config()
    if not cfg.get("is_active"):
        return None, "Disabled"

    custom_link = cfg.get("invite_link")
    title = cfg.get("title") or "Official Channel"
    cid = cfg.get("channel_id")

    if custom_link:
        return custom_link, title

    if cid:
        try:
            entity = await bot_client.get_entity(cid)
            if isinstance(entity, (types.Channel, types.Chat)):
                if getattr(entity, 'username', None):
                    return f"https://t.me/{entity.username}", getattr(entity, 'title', title)
                # Try export invite link
                try:
                    res = await bot_client(functions.messages.ExportChatInviteRequest(peer=entity))
                    if hasattr(res, 'link'):
                        cfg["invite_link"] = res.link
                        save_fsub_config(cfg)
                        return res.link, getattr(entity, 'title', title)
                except Exception:
                    pass
        except Exception:
            pass

    return None, title


async def check_fsub_membership(bot_client: TelegramClient, user_id: int, input_user=None) -> bool:
    """
    Verifies if user has joined the official channel.
    If no valid channel is configured or FSUB is disabled, returns True.
    """
    cfg = load_fsub_config()
    if not cfg.get("is_active"):
        return True

    channel_id = cfg.get("channel_id")
    if not channel_id or channel_id == 0:
        return True

    try:
        channel_entity = await bot_client.get_entity(channel_id)
        if isinstance(channel_entity, types.User):
            # Guard against invalid user entities
            return True

        target_user = input_user if input_user is not None else user_id
        participant = await bot_client(functions.channels.GetParticipantRequest(
            channel=channel_entity,
            participant=target_user
        ))
        
        # Valid member or admin
        if isinstance(participant.participant, (
            types.ChannelParticipant,
            types.ChannelParticipantSelf,
            types.ChannelParticipantAdmin,
            types.ChannelParticipantCreator
        )):
            return True
        elif isinstance(participant.participant, (types.ChannelParticipantBanned, types.ChannelParticipantLeft)):
            return False
        return True
    except UserNotParticipantError:
        return False
    except (ChannelPrivateError, ChannelInvalidError, ChatAdminRequiredError):
        # Bot not admin or channel inaccessible -> don't block user
        return True
    except RPCError as e:
        if "USER_NOT_PARTICIPANT" in str(e).upper():
            return False
        return True
    except Exception as e:
        print(f"FSUB Check Notice: {e}")
        return True


async def send_fsub_prompt(event, bot_client: TelegramClient, user_id: int):
    """Sends clean Force Subscribe prompt with direct Join button and Try Again."""
    invite_link, title = await get_fsub_channel_link(bot_client)

    buttons = []
    if invite_link:
        buttons.append([Button.url(f"📢 Join {title}", invite_link)])
    
    buttons.append([Button.inline("🔄 Try Again", data=b"check_fsub_status")])

    text = (
        "🔒 **Access Restricted: Join Channel First**\n\n"
        f"Bot use karne ke liye pehle hamare official channel **{title}** ko join karein.\n\n"
        "👉 **Neeche button par click karke channel join karein, phir '🔄 Try Again' dabayein.**"
    )

    try:
        if hasattr(event, 'edit'):
            await event.edit(text, buttons=buttons, link_preview=False)
        else:
            await event.respond(text, buttons=buttons, link_preview=False)
    except Exception:
        await bot_client.send_message(user_id, text, buttons=buttons, link_preview=False)
