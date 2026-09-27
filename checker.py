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
from config import CHECK_DELAY

# Persistent link verification cache (to avoid duplicate checks and FloodWait)
LINK_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL = 7200  # 2 hours

def get_cached_result(url: str) -> Dict[str, Any]:
    cached = LINK_CACHE.get(url)
    if cached and (time.time() - cached.get('_cached_at', 0) < CACHE_TTL):
        res = dict(cached)
        res.pop('_cached_at', None)
        return res
    return None

def set_cached_result(url: str, res: Dict[str, Any]):
    if res and res.get('status') in ('working', 'expired', 'restricted'):
        copy_res = dict(res)
        copy_res['_cached_at'] = time.time()
        LINK_CACHE[url] = copy_res


def normalize_telegram_link(url: str) -> str:
    """
    Normalizes Telegram links to a standard canonical URL format.
    - Strips query params, trailing slashes, punctuation
    - Converts joinchat/HASH to +HASH
    - Preserves case of private invite hashes (case-sensitive)
    - Lowercases public usernames (case-insensitive)
    """
    clean = url.strip().rstrip('.,;:!?"\')]}')
    if not clean.startswith('http://') and not clean.startswith('https://'):
        clean = 'https://' + clean

    clean = re.sub(r'https?://(?:www\.)?telegram\.(?:me|dog)/', 'https://t.me/', clean)
    clean = re.sub(r'https?://(?:www\.)?t\.me/', 'https://t.me/', clean)

    # Strip query parameters (?start=..., ?boost=...)
    clean = clean.split('?')[0].split('#')[0].rstrip('/')

    # Standardize joinchat/ -> +
    if 't.me/joinchat/' in clean:
        clean = clean.replace('t.me/joinchat/', 't.me/+')

    # If it's a public link (t.me/username), lowercase the username part
    m = re.match(r'^https://t\.me/([a-zA-Z0-9_]{4,})$', clean)
    if m:
        username = m.group(1).lower()
        clean = f'https://t.me/{username}'

    return clean


def extract_telegram_links(text: str) -> List[str]:
    """
    Extracts all unique Telegram invite and chat links from a raw text message.
    Preserves unique order and strictly deduplicates exact matches.
    """
    if not text:
        return []
    
    found_links = []
    seen = set()
    
    # Match full URLs and bare domain mentions
    pattern = r'(?:https?://)?(?:www\.)?(?:t(?:elegram)?\.(?:me|dog))/(?:joinchat/|\+)?[a-zA-Z0-9_\-]+'
    matches = re.findall(pattern, text)
    
    for raw_url in matches:
        norm = normalize_telegram_link(raw_url)
        
        # Filter out internal message links or empty root domains
        if '/c/' in norm or '/s/' in norm or norm.endswith('t.me') or norm.endswith('t.me/'):
            continue
            
        # Ensure it actually has an identifier
        parsed_type, identifier = parse_link(norm)
        if parsed_type == 'unknown' or not identifier:
            continue

        if norm not in seen:
            seen.add(norm)
            found_links.append(norm)
            
    return found_links


def parse_link(url: str) -> Tuple[str, str]:
    """
    Identifies link type ('invite' vs 'public') and returns identifier.
    """
    invite_match = re.search(r't\.me/(?:\+|joinchat/)([a-zA-Z0-9_\-]+)', url)
    if invite_match:
        return ('invite', invite_match.group(1))
    
    public_match = re.search(r't\.me/([a-zA-Z0-9_]{4,})', url)
    if public_match:
        return ('public', public_match.group(1))
        
    return ('unknown', url)


async def check_single_link(client: TelegramClient, url: str, max_retries: int = 2) -> Dict[str, Any]:
    """
    Checks the validity of a single Telegram link with retry logic, FloodWait backoff,
    and accurate status categorization (working, expired, restricted, error).
    """
    # Check cache first
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
            'request_needed': False
        }
        set_cached_result(url, res)
        return res

    attempt = 0
    while attempt <= max_retries:
        attempt += 1
        try:
            if link_type == 'invite':
                # MTProto CheckChatInviteRequest
                result = await client(functions.messages.CheckChatInviteRequest(hash=identifier))
                
                # Check for Terms of Service restriction
                restricted = getattr(result, 'restricted', False)
                if restricted:
                    res = {
                        'url': url,
                        'status': 'restricted',
                        'reason': 'Channel restricted for Terms of Service violation',
                        'title': getattr(result, 'title', None) or 'Restricted Channel',
                        'members': (getattr(result, 'participants_count', 0) or 0),
                        'is_channel': getattr(result, 'channel', False),
                        'is_group': not getattr(result, 'channel', False),
                        'request_needed': False
                    }
                    set_cached_result(url, res)
                    return res

                if isinstance(result, types.ChatInvite):
                    p_count = getattr(result, 'participants_count', 0) or 0
                    title = getattr(result, 'title', None) or 'Private Chat'
                    res = {
                        'url': url,
                        'status': 'working',
                        'reason': 'Active invite link' + (' (Approval Required)' if getattr(result, 'request_needed', False) else ''),
                        'title': title,
                        'members': p_count,
                        'is_channel': getattr(result, 'channel', False),
                        'is_group': not getattr(result, 'channel', False),
                        'request_needed': getattr(result, 'request_needed', False)
                    }
                    set_cached_result(url, res)
                    return res

                elif isinstance(result, types.ChatInviteAlready):
                    chat = result.chat
                    restricted_chat = getattr(chat, 'restricted', False)
                    if restricted_chat:
                        res = {
                            'url': url,
                            'status': 'restricted',
                            'reason': 'Channel restricted by Telegram TOS',
                            'title': getattr(chat, 'title', 'Restricted Chat'),
                            'members': getattr(chat, 'participants_count', 0) or 0,
                            'is_channel': True,
                            'is_group': False,
                            'request_needed': False
                        }
                        set_cached_result(url, res)
                        return res

                    title = getattr(chat, 'title', None) or 'Active Chat'
                    participants = getattr(chat, 'participants_count', 0) or 0
                    res = {
                        'url': url,
                        'status': 'working',
                        'reason': 'Active (Already member)',
                        'title': title,
                        'members': participants,
                        'is_channel': isinstance(chat, types.Channel) and bool(chat.broadcast),
                        'is_group': isinstance(chat, types.Chat) or (isinstance(chat, types.Channel) and bool(chat.megagroup)),
                        'request_needed': False
                    }
                    set_cached_result(url, res)
                    return res

                elif isinstance(result, types.ChatInvitePeek):
                    chat = result.chat
                    title = getattr(chat, 'title', None) or 'Telegram Chat'
                    participants = getattr(chat, 'participants_count', 0) or 0
                    res = {
                        'url': url,
                        'status': 'working',
                        'reason': 'Active invite',
                        'title': title,
                        'members': participants,
                        'is_channel': getattr(chat, 'broadcast', False),
                        'is_group': True,
                        'request_needed': False
                    }
                    set_cached_result(url, res)
                    return res

                else:
                    res = {
                        'url': url,
                        'status': 'working',
                        'reason': 'Active invite',
                        'title': 'Telegram Chat',
                        'members': 0,
                        'is_channel': False,
                        'is_group': True,
                        'request_needed': False
                    }
                    set_cached_result(url, res)
                    return res
                    
            elif link_type == 'public':
                try:
                    resolved = await client(functions.contacts.ResolveUsernameRequest(username=identifier))
                    if resolved.chats:
                        chat = resolved.chats[0]
                        restricted = getattr(chat, 'restricted', False)
                        if restricted:
                            res = {
                                'url': url,
                                'status': 'restricted',
                                'reason': 'Channel restricted by Telegram TOS',
                                'title': getattr(chat, 'title', identifier),
                                'members': getattr(chat, 'participants_count', 0) or 0,
                                'is_channel': True,
                                'is_group': False,
                                'request_needed': False
                            }
                            set_cached_result(url, res)
                            return res

                        title = getattr(chat, 'title', None) or identifier
                        participants = getattr(chat, 'participants_count', 0) or 0
                        res = {
                            'url': url,
                            'status': 'working',
                            'reason': 'Active public chat',
                            'title': title,
                            'members': participants,
                            'is_channel': isinstance(chat, types.Channel) and bool(chat.broadcast),
                            'is_group': isinstance(chat, types.Chat) or (isinstance(chat, types.Channel) and bool(chat.megagroup)),
                            'request_needed': False
                        }
                        set_cached_result(url, res)
                        return res
                    elif resolved.users:
                        user = resolved.users[0]
                        first = getattr(user, 'first_name', '') or ''
                        last = getattr(user, 'last_name', '') or ''
                        name = f"{first} {last}".strip() or identifier
                        res = {
                            'url': url,
                            'status': 'working',
                            'reason': 'User/Bot profile',
                            'title': name,
                            'members': 1,
                            'is_channel': False,
                            'is_group': False,
                            'request_needed': False
                        }
                        set_cached_result(url, res)
                        return res
                except (UsernameNotOccupiedError, UsernameInvalidError):
                    res = {
                        'url': url,
                        'status': 'expired',
                        'reason': 'Username does not exist or was released',
                        'title': None,
                        'members': 0,
                        'is_channel': False,
                        'is_group': False,
                        'request_needed': False
                    }
                    set_cached_result(url, res)
                    return res

        except (InviteHashExpiredError, InviteHashInvalidError):
            # Definite permanent failure: link expired or revoked
            res = {
                'url': url,
                'status': 'expired',
                'reason': 'Invite link expired or revoked',
                'title': None,
                'members': 0,
                'is_channel': False,
                'is_group': False,
                'request_needed': False
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
                'request_needed': False
            }
            set_cached_result(url, res)
            return res

        except ChannelPrivateError:
            res = {
                'url': url,
                'status': 'restricted',
                'reason': 'Private channel or restricted access',
                'title': 'Private / Restricted Channel',
                'members': 0,
                'is_channel': True,
                'is_group': False,
                'request_needed': False
            }
            set_cached_result(url, res)
            return res

        except FloodWaitError as e:
            # Telegram FloodWait must be handled with appropriate backoff!
            wait_time = e.seconds
            print(f"⚠️ Telegram FloodWait: {wait_time}s required on link {url}")
            if attempt <= max_retries and wait_time <= 30:
                print(f"⏳ Waiting {wait_time + 1}s before retrying link...")
                await asyncio.sleep(wait_time + 1)
                continue
            else:
                return {
                    'url': url,
                    'status': 'error',
                    'reason': f'Telegram FloodWait ({wait_time}s cooldown)',
                    'flood_wait_seconds': wait_time,
                    'title': None,
                    'members': 0,
                    'is_channel': False,
                    'is_group': False,
                    'request_needed': False
                }

        except BotMethodInvalidError:
            return {
                'url': url,
                'status': 'error',
                'reason': 'Admin Account Engine disconnected (Re-login required in /admin)',
                'title': None,
                'members': 0,
                'is_channel': False,
                'is_group': False,
                'request_needed': False
            }

        except RPCError as e:
            err_code = getattr(e, 'code', 0)
            err_msg = getattr(e, 'message', str(e)).upper()
            
            # 1. Definite Working Link indicators in RPC errors:
            if "INVITE_REQUEST_SENT" in err_msg:
                # User already sent a join request to this approval-only channel. The link IS WORKING!
                res = {
                    'url': url,
                    'status': 'working',
                    'reason': 'Active invite link (Join Request Already Sent)',
                    'title': 'Private Channel (Request Pending)',
                    'members': 0,
                    'is_channel': True,
                    'is_group': False,
                    'request_needed': True
                }
                set_cached_result(url, res)
                return res

            if "USER_ALREADY_PARTICIPANT" in err_msg:
                res = {
                    'url': url,
                    'status': 'working',
                    'reason': 'Active invite (Already a Member)',
                    'title': 'Active Group / Channel',
                    'members': 0,
                    'is_channel': False,
                    'is_group': True,
                    'request_needed': False
                }
                set_cached_result(url, res)
                return res

            if any(term in err_msg for term in ("CHANNELS_TOO_MUCH", "USERS_TOO_MUCH", "USER_BANNED_IN_CHANNEL")):
                # The invite link itself is valid, but the user/group has hit account limits
                res = {
                    'url': url,
                    'status': 'working',
                    'reason': 'Active invite (Channel/Account capacity limit)',
                    'title': 'Active Channel',
                    'members': 0,
                    'is_channel': True,
                    'is_group': False,
                    'request_needed': False
                }
                set_cached_result(url, res)
                return res

            # 2. Definite Restricted / Terms of Service violation:
            if any(term in err_msg for term in ("CHANNEL_RESTRICTED", "CHAT_RESTRICTED", "USER_RESTRICTED", "BROADCAST_FORBIDDEN")):
                res = {
                    'url': url,
                    'status': 'restricted',
                    'reason': 'Channel violated Telegram Terms of Service / Restricted',
                    'title': 'Restricted Channel',
                    'members': 0,
                    'is_channel': True,
                    'is_group': False,
                    'request_needed': False
                }
                set_cached_result(url, res)
                return res

            # 3. Definite Expired / Invalid invite:
            if any(term in err_msg for term in ("INVITE_HASH_EXPIRED", "INVITE_HASH_INVALID", "CHAT_INVALID", "PEER_ID_INVALID")):
                res = {
                    'url': url,
                    'status': 'expired',
                    'reason': 'Invite link expired or revoked',
                    'title': None,
                    'members': 0,
                    'is_channel': False,
                    'is_group': False,
                    'request_needed': False
                }
                set_cached_result(url, res)
                return res

            # Temporary server-side error (DC_MIGRATE, RPC_CALL_FAIL, etc.): retry
            if attempt <= max_retries:
                await asyncio.sleep(1.0)
                continue
            
            return {
                'url': url,
                'status': 'error',
                'reason': f'Telegram Error: {err_msg}',
                'title': None,
                'members': 0,
                'is_channel': False,
                'is_group': False,
                'request_needed': False
            }

        except Exception as e:
            # Temporary network error / socket timeout: retry
            if attempt <= max_retries:
                await asyncio.sleep(1.0)
                continue
            return {
                'url': url,
                'status': 'error',
                'reason': f'{type(e).__name__}: {str(e)}',
                'title': None,
                'members': 0,
                'is_channel': False,
                'is_group': False,
                'request_needed': False
            }

    # Fallback if loop ends
    return {
        'url': url,
        'status': 'error',
        'reason': 'Verification timed out after retries',
        'title': None,
        'members': 0,
        'is_channel': False,
        'is_group': False,
        'request_needed': False
    }
