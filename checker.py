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
CACHE_TTL = 7200 # 2 hours

def get_cached_result(url: str) -> Dict[str, Any]:
    cached = LINK_CACHE.get(url)
    if cached and (time.time() - cached.get('_cached_at', 0) < CACHE_TTL):
        res = dict(cached)
        res.pop('_cached_at', None)
        return res
    return None

def set_cached_result(url: str, res: Dict[str, Any]):
    if res and res.get('status') in ('working', 'expired'):
        copy_res = dict(res)
        copy_res['_cached_at'] = time.time()
        LINK_CACHE[url] = copy_res


def extract_telegram_links(text: str) -> List[str]:
    """
    Extracts all unique Telegram invite and chat links from a raw text message.
    """
    if not text:
        return []
    
    found_links = []
    seen = set()
    
    # Catch full URLs directly
    raw_urls = re.findall(r'(https?://t(?:elegram)?\.(?:me|dog)/(?:joinchat/|\+)?[a-zA-Z0-9_\-]+)', text)
    simple_urls = re.findall(r'(?:^|[\s\n\(\[\{])(t(?:elegram)?\.(?:me|dog)/(?:joinchat/|\+)?[a-zA-Z0-9_\-]+)', text)
    
    all_candidates = raw_urls + simple_urls
    
    for url in all_candidates:
        url = url.strip().rstrip('.,;:!?"\')]}')
        if not url.startswith('http://') and not url.startswith('https://'):
            url = 'https://' + url
        
        standardized = re.sub(r'https?://(?:www\.)?telegram\.(?:me|dog)/', 'https://t.me/', url)
        
        # Avoid internal message links (t.me/c/...) or empty domains
        if '/c/' in standardized or '/s/' in standardized or standardized.endswith('t.me/') or standardized.endswith('t.me'):
            continue
            
        if standardized not in seen:
            seen.add(standardized)
            found_links.append(standardized)
            
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


async def check_single_link(client: TelegramClient, url: str, retry_count: int = 0) -> Dict[str, Any]:
    """
    Checks the validity of a single Telegram link using MTProto API with caching and smart FloodWait recovery.
    """
    # Check cache first (instant response, zero rate-limit)
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

    try:
        if link_type == 'invite':
            # MTProto CheckChatInviteRequest
            result = await client(functions.messages.CheckChatInviteRequest(hash=identifier))
            
            if isinstance(result, types.ChatInvite):
                res = {
                    'url': url,
                    'status': 'working',
                    'reason': 'Active invite link',
                    'title': getattr(result, 'title', 'Private Chat'),
                    'members': getattr(result, 'participants_count', 0),
                    'is_channel': getattr(result, 'channel', False),
                    'is_group': not getattr(result, 'channel', False),
                    'request_needed': getattr(result, 'request_needed', False)
                }
                set_cached_result(url, res)
                return res
            elif isinstance(result, types.ChatInviteAlready):
                chat = result.chat
                title = getattr(chat, 'title', 'Active Chat (Member)')
                participants = getattr(chat, 'participants_count', 0)
                res = {
                    'url': url,
                    'status': 'working',
                    'reason': 'Active (Already member)',
                    'title': title,
                    'members': participants,
                    'is_channel': isinstance(chat, types.Channel) and chat.broadcast,
                    'is_group': isinstance(chat, types.Chat) or (isinstance(chat, types.Channel) and chat.megagroup),
                    'request_needed': False
                }
                set_cached_result(url, res)
                return res
            elif isinstance(result, types.ChatInvitePeek):
                chat = result.chat
                res = {
                    'url': url,
                    'status': 'working',
                    'reason': 'Active invite',
                    'title': getattr(chat, 'title', 'Telegram Chat'),
                    'members': getattr(chat, 'participants_count', 0),
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
                    'reason': 'Active',
                    'title': 'Telegram Chat',
                    'members': 0,
                    'is_channel': False,
                    'is_group': True,
                    'request_needed': False
                }
                set_cached_result(url, res)
                return res
                
        elif link_type == 'public':
            entity = await client.get_entity(identifier)
            if isinstance(entity, (types.Channel, types.Chat)):
                res = {
                    'url': url,
                    'status': 'working',
                    'reason': 'Active public chat',
                    'title': getattr(entity, 'title', identifier),
                    'members': getattr(entity, 'participants_count', 0),
                    'is_channel': isinstance(entity, types.Channel) and entity.broadcast,
                    'is_group': isinstance(entity, types.Chat) or (isinstance(entity, types.Channel) and entity.megagroup),
                    'request_needed': False
                }
                set_cached_result(url, res)
                return res
            elif isinstance(entity, types.User):
                res = {
                    'url': url,
                    'status': 'working',
                    'reason': 'User/Bot profile',
                    'title': f"{entity.first_name or ''} {entity.last_name or ''}".strip() or entity.username,
                    'members': 1,
                    'is_channel': False,
                    'is_group': False,
                    'request_needed': False
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
            'status': 'expired',
            'reason': 'Private channel restricted',
            'title': None,
            'members': 0,
            'is_channel': False,
            'is_group': False,
            'request_needed': False
        }
        set_cached_result(url, res)
        return res

    except FloodWaitError as e:
        wait_seconds = min(e.seconds, 10)
        if retry_count < 2 and e.seconds <= 15:
            print(f"⚠️ FloodWait ({e.seconds}s): Waiting {wait_seconds}s and retrying...")
            await asyncio.sleep(wait_seconds)
            return await check_single_link(client, url, retry_count=retry_count + 1)
        return {
            'url': url,
            'status': 'error',
            'reason': f'Telegram FloodWait ({e.seconds}s cooldown)',
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
            'reason': 'Engine disconnected (User login required)',
            'title': None,
            'members': 0,
            'is_channel': False,
            'is_group': False,
            'request_needed': False
        }

    except RPCError as e:
        error_msg = str(e)
        if any(w in error_msg for w in ("INVITE_HASH_EXPIRED", "INVITE_HASH_INVALID", "CHAT_INVALID", "PEER_ID_INVALID")):
            res = {
                'url': url,
                'status': 'expired',
                'reason': 'Invite link expired or invalid',
                'title': None,
                'members': 0,
                'is_channel': False,
                'is_group': False,
                'request_needed': False
            }
            set_cached_result(url, res)
            return res
        return {
            'url': url,
            'status': 'error',
            'reason': f'Telegram Error: {error_msg}',
            'title': None,
            'members': 0,
            'is_channel': False,
            'is_group': False,
            'request_needed': False
        }

    except Exception as e:
        return {
            'url': url,
            'status': 'error',
            'reason': f'{str(e)}',
            'title': None,
            'members': 0,
            'is_channel': False,
            'is_group': False,
            'request_needed': False
        }

    res = {
        'url': url,
        'status': 'expired',
        'reason': 'Unknown response',
        'title': None,
        'members': 0,
        'is_channel': False,
        'is_group': False,
        'request_needed': False
    }
    set_cached_result(url, res)
    return res
