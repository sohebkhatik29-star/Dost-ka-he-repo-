import re
from typing import List, Tuple, Dict, Any
from telethon import types, functions, errors


def parse_link(url: str) -> Tuple[str, str]:
    """
    Parses a Telegram URL and identifies whether it is an 'invite' or 'public' link.
    Returns (link_type, identifier)
    """
    url = url.strip()

    # Private Invite Links (e.g. t.me/+hash or t.me/joinchat/hash)
    inv_match = re.search(r'(?:t\.me|telegram\.me)/(?:\+|joinchat/)([a-zA-Z0-9_-]+)', url)
    if inv_match:
        return ('invite', inv_match.group(1))

    # Public Channel / Group Links (e.g. t.me/username)
    pub_match = re.search(r'(?:t\.me|telegram\.me)/([a-zA-Z0-9_]{5,})', url)
    if pub_match:
        username = pub_match.group(1)
        # Reserved system keywords filter
        reserved = {'joinchat', 'addstickers', 'addtheme', 'setlanguage', 'share', 's', 'c', 'bg', 'socks', 'proxy'}
        if username.lower() not in reserved:
            return ('public', username)

    return (None, None)


def extract_telegram_links(text: str) -> List[str]:
    """
    Extracts all unique Telegram links from a raw text string.
    """
    if not text:
        return []

    # Regex pattern to capture t.me and telegram.me links
    pattern = r'https?://(?:t\.me|telegram\.me)/(?:\+[a-zA-Z0-9_-]+|joinchat/[a-zA-Z0-9_-]+|[a-zA-Z0-9_]{5,})'
    found_links = re.findall(pattern, text)

    # Clean and remove duplicates while preserving original order
    seen = set()
    cleaned_links = []
    for link in found_links:
        # Standardize link format
        clean_url = link.strip()
        if clean_url not in seen:
            seen.add(clean_url)
            cleaned_links.append(clean_url)

    return cleaned_links


def extract_links_from_message(message) -> List[str]:
    """
    Extracts Telegram links from text, formatted hyperlinks, and inline buttons of a message.
    """
    extracted_links = []

    # 1. Extract from plain text or caption
    raw_text = message.message or message.text or ""
    extracted_links.extend(extract_telegram_links(raw_text))

    # 2. Extract from formatted Entities (Hyperlinks)
    entities = getattr(message, 'entities', None) or getattr(message, 'caption_entities', None)
    if entities:
        for entity in entities:
            if isinstance(entity, types.MessageEntityTextUrl) and entity.url:
                extracted_links.extend(extract_telegram_links(entity.url))
            elif isinstance(entity, types.MessageEntityUrl):
                # Extract segment from text corresponding to Url entity
                try:
                    offset = entity.offset
                    length = entity.length
                    url_str = raw_text[offset:offset + length]
                    extracted_links.extend(extract_telegram_links(url_str))
                except Exception:
                    pass

    # 3. Extract from Inline Keyboard Buttons
    reply_markup = getattr(message, 'reply_markup', None)
    if reply_markup and isinstance(reply_markup, types.ReplyInlineMarkup):
        for row in reply_markup.rows:
            for button in row.buttons:
                if isinstance(button, types.KeyboardButtonUrl) and button.url:
                    extracted_links.extend(extract_telegram_links(button.url))

    # Deduplicate extracted links while keeping order
    seen = set()
    final_links = []
    for link in extracted_links:
        if link not in seen:
            seen.add(link)
            final_links.append(link)

    return final_links


async def check_single_link(client, url: str) -> Dict[str, Any]:
    """
    Validates a single Telegram link via MTProto RPC requests safely.
    Returns dict: {'url': str, 'status': 'working'|'expired', 'title': str, 'members': int, 'request_needed': bool}
    """
    link_type, identifier = parse_link(url)

    result = {
        'url': url,
        'status': 'expired',
        'title': None,
        'members': 0,
        'request_needed': False
    }

    if not link_type or not identifier:
        return result

    try:
        # --- CASE 1: Private Invite Link (t.me/+... or t.me/joinchat/...) ---
        if link_type == 'invite':
            invite_res = await client(functions.messages.CheckChatInviteRequest(hash=identifier))

            if isinstance(invite_res, types.ChatInvite):
                result['status'] = 'working'
                result['title'] = getattr(invite_res, 'title', 'Private Group/Channel')
                result['members'] = getattr(invite_res, 'participants_count', 0)
                result['request_needed'] = getattr(invite_res, 'request_needed', False)

            elif isinstance(invite_res, types.ChatInviteAlready):
                # Account is already a member of this chat
                chat = getattr(invite_res, 'chat', None)
                result['status'] = 'working'
                if chat:
                    result['title'] = getattr(chat, 'title', 'Private Group/Channel')
                    result['members'] = getattr(chat, 'participants_count', 0)
                else:
                    result['title'] = 'Private Joined Chat'

            elif isinstance(invite_res, types.ChatInvitePeek):
                chat = getattr(invite_res, 'chat', None)
                result['status'] = 'working'
                if chat:
                    result['title'] = getattr(chat, 'title', 'Private Group/Channel')
                    result['members'] = getattr(chat, 'participants_count', 0)

        # --- CASE 2: Public Channel/Group/User Link (t.me/username) ---
        elif link_type == 'public':
            entity = await client.get_entity(identifier)

            # Accept Channels, Supergroups, Chats, and Bots
            if isinstance(entity, (types.Channel, types.Chat, types.User)):
                result['status'] = 'working'
                result['title'] = getattr(entity, 'title', None) or getattr(entity, 'first_name', identifier)
                result['members'] = getattr(entity, 'participants_count', 0)

    except (errors.InviteHashExpiredError, errors.InviteHashInvalidError):
        result['status'] = 'expired'

    except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError):
        result['status'] = 'expired'

    except errors.ChannelPrivateError:
        # Link exists but account doesn't have access / banned
        result['status'] = 'expired'

    except errors.FloodWaitError as e:
        # Wait if rate limited by Telegram API
        await asyncio.sleep(e.seconds + 1)
        result['status'] = 'expired'

    except Exception:
        result['status'] = 'expired'

    return result
