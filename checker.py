import re
import time
import asyncio
from typing import List, Dict, Any, Tuple
from telethon import TelegramClient
from telethon.tl import functions, types
from telethon.errors import (
    InviteHashExpiredError,
    InviteHashInvalidError,
    ChannelPrivateError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    FloodWaitError,
    BotMethodInvalidError,
    RPCError,
)

# Persistent link verification cache (only for definitively verified links)
LINK_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL = 300  # 5 minutes TTL for verified links

# Non-chat Telegram system paths to ignore
IGNORED_SYSTEM_PATHS = {
    'c', 's', 'share', 'iv', 'proxy', 'socks', 'addstickers',
    'addemoji', 'addtheme', 'invoice', 'boost', 'setlanguage',
    'login', 'passport', 'terms', 'privacy', 'addlist', 'joinchat'
}


def get_cached_result(url: str) -> Dict[str, Any]:
    cached = LINK_CACHE.get(url)
    if cached and (time.time() - cached.get('_cached_at', 0) < CACHE_TTL):
        res = dict(cached)
        res.pop('_cached_at', None)
        return res
    return None


def set_cached_result(url: str, res: Dict[str, Any]):
    if res and res.get('definitive') is True and res.get('status') in ('working', 'expired'):
        copy_res = dict(res)
        copy_res['_cached_at'] = time.time()
        LINK_CACHE[url] = copy_res


def extract_telegram_links(text: str) -> List[str]:
    """
    Extracts and normalizes all unique Telegram invite, public, and folder links from raw text.
    Preserves exact order and removes identical duplicates.
    """
    if not text:
        return []

    found_links = []
    seen = set()

    # 1. Handle tg:// URLs (e.g. tg://join?invite=... and tg://resolve?domain=...)
    tg_invites = re.findall(r'tg://join\?invite=([a-zA-Z0-9_\-]+)', text, re.IGNORECASE)
    for inv in tg_invites:
        canonical = f"https://t.me/+{inv}"
        if canonical not in seen:
            seen.add(canonical)
            found_links.append(canonical)

    tg_domains = re.findall(r'tg://resolve\?domain=([a-zA-Z0-9_]{4,32})', text, re.IGNORECASE)
    for d in tg_domains:
        if d.lower() not in IGNORED_SYSTEM_PATHS and not d.isdigit():
            canonical = f"https://t.me/{d}"
            if canonical not in seen:
                seen.add(canonical)
                found_links.append(canonical)

    # 2. Addlist / Folder links (https://t.me/addlist/slug)
    addlist_matches = re.findall(r'(?:https?://)?(?:www\.)?t(?:elegram)?\.(?:me|dog)/addlist/([a-zA-Z0-9_\-]+)', text, re.IGNORECASE)
    for slug in addlist_matches:
        canonical = f"https://t.me/addlist/{slug}"
        if canonical not in seen:
            seen.add(canonical)
            found_links.append(canonical)

    # 3. Regex search for standard URLs
    url_pattern = re.compile(
        r'(?:https?://)?(?:www\.)?(?:t(?:elegram)?\.(?:me|dog)|telegram\.org)/([^\s\n\(\)\[\]\{\}<>"\',;]+)',
        re.IGNORECASE
    )

    for match in url_pattern.finditer(text):
        raw_path = match.group(1).strip().rstrip('.,;:!?"\')]}')
        if not raw_path:
            continue

        clean_path = raw_path.split('?')[0].split('#')[0].strip('/')
        if not clean_path:
            continue

        path_first_part = clean_path.split('/')[0].lower()

        # Handle Addlist folder
        if path_first_part == 'addlist':
            parts = clean_path.split('/')
            if len(parts) >= 2 and parts[1]:
                slug = parts[1]
                canonical_url = f"https://t.me/addlist/{slug}"
                if canonical_url not in seen:
                    seen.add(canonical_url)
                    found_links.append(canonical_url)
            continue

        # Ignore non-chat system routes
        if path_first_part in IGNORED_SYSTEM_PATHS:
            continue

        # Private invite: +hash or joinchat/hash
        if clean_path.startswith('+'):
            invite_hash = clean_path[1:]
            if re.match(r'^[a-zA-Z0-9_\-]+$', invite_hash) and len(invite_hash) >= 4:
                canonical_url = f"https://t.me/+{invite_hash}"
                if canonical_url not in seen:
                    seen.add(canonical_url)
                    found_links.append(canonical_url)
        elif clean_path.startswith('joinchat/'):
            invite_hash = clean_path[len('joinchat/'):]
            if re.match(r'^[a-zA-Z0-9_\-]+$', invite_hash) and len(invite_hash) >= 4:
                canonical_url = f"https://t.me/+{invite_hash}"
                if canonical_url not in seen:
                    seen.add(canonical_url)
                    found_links.append(canonical_url)
        else:
            # Public username
            username = clean_path.split('/')[0]
            if re.match(r'^[a-zA-Z0-9_]{4,32}$', username) and not username.isdigit() and username.lower() not in IGNORED_SYSTEM_PATHS:
                canonical_url = f"https://t.me/{username}"
                if canonical_url not in seen:
                    seen.add(canonical_url)
                    found_links.append(canonical_url)

    # 4. Extract standalone @usernames
    at_matches = re.findall(r'(?:^|[\s\n\(\[\{<])@([a-zA-Z0-9_]{4,32})', text)
    for uname in at_matches:
        if uname.lower() in IGNORED_SYSTEM_PATHS or uname.isdigit():
            continue
        canonical_url = f"https://t.me/{uname}"
        if canonical_url not in seen:
            seen.add(canonical_url)
            found_links.append(canonical_url)

    return found_links


def extract_links_from_message(message) -> List[str]:
    """
    Extracts all Telegram links from a Telethon Message object,
    including plain text, formatted hyperlinks in message.entities (MessageEntityTextUrl),
    and button URLs.
    """
    if not message:
        return []

    text_parts = []
    if hasattr(message, 'text') and message.text:
        text_parts.append(message.text)

    if hasattr(message, 'entities') and message.entities:
        for ent in message.entities:
            url = getattr(ent, 'url', None)
            if url:
                text_parts.append(url)

    if hasattr(message, 'buttons') and message.buttons:
        for row in message.buttons:
            for btn in row:
                url = getattr(btn, 'url', None)
                if url:
                    text_parts.append(url)

    combined_text = "\n".join(text_parts)
    return extract_telegram_links(combined_text)


def parse_link(url: str) -> Tuple[str, str]:
    """
    Identifies link type ('invite', 'addlist', 'public') and returns the clean identifier/hash.
    """
    # Chat Folder / Addlist
    addlist_match = re.search(r't\.me/addlist/([a-zA-Z0-9_\-]+)', url)
    if addlist_match:
        return ('addlist', addlist_match.group(1))

    # Bare addlist without slug -> Invalid / Expired
    if re.search(r't\.me/addlist/?$', url):
        return ('unknown', url)

    # Private Invite: https://t.me/+hash or https://t.me/joinchat/hash
    invite_match = re.search(r't\.me/(?:\+|joinchat/)([a-zA-Z0-9_\-]+)', url)
    if invite_match:
        return ('invite', invite_match.group(1))

    # Public Chat: https://t.me/username
    public_match = re.search(r't\.me/([a-zA-Z0-9_]{4,32})', url)
    if public_match:
        uname = public_match.group(1)
        if uname.lower() not in IGNORED_SYSTEM_PATHS:
            return ('public', uname)

    return ('unknown', url)


async def check_single_link(client: TelegramClient, url: str, max_retries: int = 3) -> Dict[str, Any]:
    """
    Checks the validity of a Telegram link using MTProto API.
    Strict Binary Truth:
    - Working ONLY if link is genuinely verified active.
    - Expired if link is dead, revoked, restricted, deleted, or invalid.
    """
    cached = get_cached_result(url)
    if cached:
        return cached

    link_type, identifier = parse_link(url)

    if link_type == 'unknown':
        res = {
            'url': url,
            'status': 'expired',
            'reason': 'Invalid link format',
            'title': None,
            'members': 0,
            'is_channel': False,
            'is_group': False,
            'request_needed': False,
            'definitive': True
        }
        set_cached_result(url, res)
        return res

    for attempt in range(1, max_retries + 1):
        try:
            if link_type == 'invite':
                # MTProto CheckChatInviteRequest
                result = await client(functions.messages.CheckChatInviteRequest(hash=identifier))

                if isinstance(result, types.ChatInvite):
                    title = getattr(result, 'title', None) or 'Telegram Private Chat'
                    participants = getattr(result, 'participants_count', 0) or 0
                    is_chan = getattr(result, 'channel', False) or getattr(result, 'broadcast', False)
                    req_needed = getattr(result, 'request_needed', False)

                    res = {
                        'url': url,
                        'status': 'working',
                        'reason': 'Active invite link' + (' (Join Request Required)' if req_needed else ''),
                        'title': title,
                        'members': participants,
                        'is_channel': is_chan,
                        'is_group': not is_chan,
                        'request_needed': req_needed,
                        'definitive': True
                    }
                    set_cached_result(url, res)
                    return res

                elif isinstance(result, types.ChatInviteAlready):
                    chat = getattr(result, 'chat', None)
                    if isinstance(chat, types.ChannelForbidden):
                        res = {
                            'url': url,
                            'status': 'expired',
                            'reason': 'Channel restricted / inaccessible',
                            'title': getattr(chat, 'title', 'Restricted Channel'),
                            'members': 0,
                            'is_channel': True,
                            'is_group': False,
                            'request_needed': False,
                            'definitive': True
                        }
                        set_cached_result(url, res)
                        return res

                    title = getattr(chat, 'title', 'Telegram Chat') if chat else 'Active Chat'
                    participants = getattr(chat, 'participants_count', 0) if chat else 0
                    is_chan = isinstance(chat, types.Channel) and getattr(chat, 'broadcast', False)
                    is_grp = isinstance(chat, types.Chat) or (isinstance(chat, types.Channel) and getattr(chat, 'megagroup', False))

                    res = {
                        'url': url,
                        'status': 'working',
                        'reason': 'Active invite link',
                        'title': title,
                        'members': participants,
                        'is_channel': is_chan,
                        'is_group': is_grp or not is_chan,
                        'request_needed': False,
                        'definitive': True
                    }
                    set_cached_result(url, res)
                    return res

                elif isinstance(result, types.ChatInvitePeek):
                    chat = getattr(result, 'chat', None)
                    title = getattr(chat, 'title', 'Telegram Chat') if chat else 'Active Chat'
                    participants = getattr(chat, 'participants_count', 0) if chat else 0
                    is_chan = getattr(chat, 'broadcast', False) if chat else False

                    res = {
                        'url': url,
                        'status': 'working',
                        'reason': 'Active invite link',
                        'title': title,
                        'members': participants,
                        'is_channel': is_chan,
                        'is_group': not is_chan,
                        'request_needed': False,
                        'definitive': True
                    }
                    set_cached_result(url, res)
                    return res

                elif isinstance(result, (types.Channel, types.Chat)):
                    title = getattr(result, 'title', 'Active Chat')
                    participants = getattr(result, 'participants_count', 0) or 0
                    is_chan = isinstance(result, types.Channel) and getattr(result, 'broadcast', False)
                    res = {
                        'url': url,
                        'status': 'working',
                        'reason': 'Active invite link',
                        'title': title,
                        'members': participants,
                        'is_channel': is_chan,
                        'is_group': not is_chan,
                        'request_needed': False,
                        'definitive': True
                    }
                    set_cached_result(url, res)
                    return res

                else:
                    title = getattr(result, 'title', None) or 'Telegram Private Chat'
                    participants = getattr(result, 'participants_count', 0) or 0
                    res = {
                        'url': url,
                        'status': 'working',
                        'reason': 'Active invite link',
                        'title': title,
                        'members': participants,
                        'is_channel': False,
                        'is_group': True,
                        'request_needed': False,
                        'definitive': True
                    }
                    set_cached_result(url, res)
                    return res

            elif link_type == 'addlist':
                try:
                    result = await client(functions.chatlists.CheckChatlistInviteRequest(slug=identifier))
                    title = getattr(result, 'title', 'Chat Folder / Addlist')
                    chats_count = len(getattr(result, 'chats', [])) if hasattr(result, 'chats') else 0
                    res = {
                        'url': url,
                        'status': 'working',
                        'reason': f'Active Chat List ({chats_count} chats)',
                        'title': title,
                        'members': chats_count,
                        'is_channel': False,
                        'is_group': False,
                        'request_needed': False,
                        'definitive': True
                    }
                    set_cached_result(url, res)
                    return res
                except (InviteHashExpiredError, InviteHashInvalidError, RPCError) as add_err:
                    add_err_str = str(add_err).upper()
                    if any(k in add_err_str for k in ("INVITE_HASH_EXPIRED", "INVITE_HASH_INVALID", "SLUG_INVALID", "CHATLIST_EXPIRED")):
                        res = {
                            'url': url,
                            'status': 'expired',
                            'reason': 'Folder invite expired or invalid',
                            'title': None,
                            'members': 0,
                            'is_channel': False,
                            'is_group': False,
                            'request_needed': False,
                            'definitive': True
                        }
                        set_cached_result(url, res)
                        return res
                    else:
                        res = {
                            'url': url,
                            'status': 'working',
                            'reason': 'Active Chat Folder',
                            'title': 'Telegram Chat Folder',
                            'members': 0,
                            'is_channel': False,
                            'is_group': False,
                            'request_needed': False,
                            'definitive': True
                        }
                        set_cached_result(url, res)
                        return res

            elif link_type == 'public':
                resolved = await client(functions.contacts.ResolveUsernameRequest(username=identifier))

                if resolved.chats:
                    chat = resolved.chats[0]
                    if isinstance(chat, types.ChannelForbidden):
                        res = {
                            'url': url,
                            'status': 'expired',
                            'reason': 'Channel restricted / inaccessible',
                            'title': getattr(chat, 'title', identifier),
                            'members': 0,
                            'is_channel': True,
                            'is_group': False,
                            'request_needed': False,
                            'definitive': True
                        }
                        set_cached_result(url, res)
                        return res

                    title = getattr(chat, 'title', identifier)
                    participants = getattr(chat, 'participants_count', 0) or 0
                    is_chan = getattr(chat, 'broadcast', False)
                    is_grp = getattr(chat, 'megagroup', False) or isinstance(chat, types.Chat)

                    res = {
                        'url': url,
                        'status': 'working',
                        'reason': 'Active public chat',
                        'title': title,
                        'members': participants,
                        'is_channel': is_chan,
                        'is_group': is_grp or not is_chan,
                        'request_needed': False,
                        'definitive': True
                    }
                    set_cached_result(url, res)
                    return res

                elif resolved.users:
                    user = resolved.users[0]
                    full_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or user.username or identifier
                    res = {
                        'url': url,
                        'status': 'working',
                        'reason': 'Active user / bot profile',
                        'title': full_name,
                        'members': 1,
                        'is_channel': False,
                        'is_group': False,
                        'request_needed': False,
                        'definitive': True
                    }
                    set_cached_result(url, res)
                    return res
                else:
                    res = {
                        'url': url,
                        'status': 'expired',
                        'reason': 'Username not found',
                        'title': None,
                        'members': 0,
                        'is_channel': False,
                        'is_group': False,
                        'request_needed': False,
                        'definitive': True
                    }
                    set_cached_result(url, res)
                    return res

        except (InviteHashExpiredError, InviteHashInvalidError):
            res = {
                'url': url,
                'status': 'expired',
                'reason': 'Invite link expired or revoked',
                'title': None,
                'members': 0,
                'is_channel': False,
                'is_group': False,
                'request_needed': False,
                'definitive': True
            }
            set_cached_result(url, res)
            return res

        except (UsernameInvalidError, UsernameNotOccupiedError):
            res = {
                'url': url,
                'status': 'expired',
                'reason': 'Username does not exist',
                'title': None,
                'members': 0,
                'is_channel': False,
                'is_group': False,
                'request_needed': False,
                'definitive': True
            }
            set_cached_result(url, res)
            return res

        except ChannelPrivateError:
            res = {
                'url': url,
                'status': 'expired',
                'reason': 'Chat is private or inaccessible',
                'title': None,
                'members': 0,
                'is_channel': False,
                'is_group': False,
                'request_needed': False,
                'definitive': True
            }
            set_cached_result(url, res)
            return res

        except FloodWaitError as e:
            # Telegram rate limit
            wait_s = min(e.seconds, 5)
            print(f"⚠️ FloodWait ({e.seconds}s) on {url}. Waiting {wait_s}s (attempt {attempt}/{max_retries})...")
            await asyncio.sleep(wait_s + 0.5)
            if attempt < max_retries:
                continue

            # If all retries exhausted on FloodWait, do NOT assume working! Return expired to protect accuracy.
            res = {
                'url': url,
                'status': 'expired',
                'reason': 'Rate limit timeout',
                'title': None,
                'members': 0,
                'is_channel': False,
                'is_group': False,
                'request_needed': False,
                'definitive': False
            }
            return res

        except BotMethodInvalidError:
            res = {
                'url': url,
                'status': 'expired',
                'reason': 'Engine disconnected',
                'title': None,
                'members': 0,
                'is_channel': False,
                'is_group': False,
                'request_needed': False,
                'definitive': False
            }
            return res

        except RPCError as e:
            err_str = str(e).upper()

            # 1. Join request is pending or already a participant -> 100% Valid link!
            if any(k in err_str for k in ("INVITE_REQUEST_SENT", "USER_ALREADY_PARTICIPANT")):
                res = {
                    'url': url,
                    'status': 'working',
                    'reason': 'Active invite link',
                    'title': 'Active Private Channel',
                    'members': 0,
                    'is_channel': True,
                    'is_group': False,
                    'request_needed': "INVITE_REQUEST_SENT" in err_str,
                    'definitive': True
                }
                set_cached_result(url, res)
                return res

            # 2. User is banned from the channel -> Invite link itself is 100% valid!
            if "USER_BANNED_IN_CHANNEL" in err_str:
                res = {
                    'url': url,
                    'status': 'working',
                    'reason': 'Active invite link',
                    'title': 'Active Channel',
                    'members': 0,
                    'is_channel': True,
                    'is_group': False,
                    'request_needed': False,
                    'definitive': True
                }
                set_cached_result(url, res)
                return res

            # 3. Definite expired/invalid hashes
            if any(k in err_str for k in (
                "INVITE_HASH_EXPIRED", "INVITE_HASH_INVALID", "CHAT_INVALID",
                "CHANNEL_RESTRICTED", "CHAT_RESTRICTED", "CHAT_FORBIDDEN",
                "PEER_ID_INVALID", "CHANNEL_PRIVATE", "USER_DEACTIVATED"
            )):
                res = {
                    'url': url,
                    'status': 'expired',
                    'reason': 'Invite link expired or invalid',
                    'title': None,
                    'members': 0,
                    'is_channel': False,
                    'is_group': False,
                    'request_needed': False,
                    'definitive': True
                }
                set_cached_result(url, res)
                return res

            # 4. Flood in RPCError
            if "FLOOD" in err_str:
                await asyncio.sleep(1.5 * attempt)
                if attempt < max_retries:
                    continue

            # 5. Other temporary RPC errors
            if attempt < max_retries:
                await asyncio.sleep(1.0 * attempt)
                continue

            res = {
                'url': url,
                'status': 'expired',
                'reason': 'Inaccessible or invalid link',
                'title': None,
                'members': 0,
                'is_channel': False,
                'is_group': False,
                'request_needed': False,
                'definitive': False
            }
            return res

        except Exception as e:
            if attempt < max_retries:
                await asyncio.sleep(1.0 * attempt)
                continue

            res = {
                'url': url,
                'status': 'expired',
                'reason': 'Inaccessible or invalid link',
                'title': None,
                'members': 0,
                'is_channel': False,
                'is_group': False,
                'request_needed': False,
                'definitive': False
            }
            return res

    res = {
        'url': url,
        'status': 'expired',
        'reason': 'Inaccessible or invalid link',
        'title': None,
        'members': 0,
        'is_channel': False,
        'is_group': False,
        'request_needed': False,
        'definitive': False
    }
    return res
