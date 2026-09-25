import re
import asyncio
from typing import List, Dict, Any, Tuple
from telethon import TelegramClient
from telethon.tl import functions, types
from telethon.errors import (
    InviteHashExpiredError,
    InviteHashInvalidError,
    InviteRequestSentError,
    ChannelPrivateError,
    UsernameInvalidError,
    UsernameNotOccupiedError,
    FloodWaitError,
    RPCError,
)
from config import CHECK_DELAY

# Regex pattern matching any Telegram invite or public link inside raw text
TG_LINK_REGEX = re.compile(
    r'(?:https?://)?(?:www\.)?(?:t(?:elegram)?\.(?:me|dog))/(?:joinchat/|\+|)([a-zA-Z0-9_\-]+)',
    re.IGNORECASE
)

def extract_telegram_links(text: str) -> List[str]:
    """
    Extracts all Telegram invite and chat links from a raw text message
    and removes duplicates while preserving original order.
    """
    if not text:
        return []
    
    found_links = []
    seen = set()
    
    # Also catch full URLs directly
    raw_urls = re.findall(r'(https?://t(?:elegram)?\.(?:me|dog)/(?:joinchat/|\+)?[a-zA-Z0-9_\-]+)', text)
    # Also catch t.me/ without http
    simple_urls = re.findall(r'(?:^|[\s\n\(\[\{])(t(?:elegram)?\.(?:me|dog)/(?:joinchat/|\+)?[a-zA-Z0-9_\-]+)', text)
    
    all_candidates = raw_urls + simple_urls
    
    for url in all_candidates:
        url = url.strip().rstrip('.,;:!?"\')]}')
        if not url.startswith('http://') and not url.startswith('https://'):
            url = 'https://' + url
        
        # Standardize telegram.dog/telegram.me to t.me
        standardized = re.sub(r'https?://(?:www\.)?telegram\.(?:me|dog)/', 'https://t.me/', url)
        
        # Avoid non-chat endpoints like t.me/c/ (internal message links), t.me/s/, etc.
        if '/c/' in standardized or '/s/' in standardized or standardized.endswith('t.me/') or standardized.endswith('t.me'):
            continue
            
        if standardized not in seen:
            seen.add(standardized)
            found_links.append(standardized)
            
    return found_links


def parse_link(url: str) -> Tuple[str, str]:
    """
    Identifies if a link is a private invite hash (t.me/+xyz or t.me/joinchat/xyz)
    or a public username (t.me/username).
    Returns (type, identifier):
      - ('invite', 'XYZ123')
      - ('public', 'username')
    """
    # Check for private invite hash: t.me/+hash or t.me/joinchat/hash
    invite_match = re.search(r't\.me/(?:\+|joinchat/)([a-zA-Z0-9_\-]+)', url)
    if invite_match:
        return ('invite', invite_match.group(1))
    
    # Check for public channel/group/bot username
    public_match = re.search(r't\.me/([a-zA-Z0-9_]{4,})', url)
    if public_match:
        return ('public', public_match.group(1))
        
    return ('unknown', url)


async def check_single_link(client: TelegramClient, url: str) -> Dict[str, Any]:
    """
    Checks the validity of a single Telegram link using MTProto API.
    Returns a dictionary with status:
      - 'working': valid active channel/group
      - 'expired': invalid or expired invite link
      - 'error': temporary issue (could not check)
    """
    link_type, identifier = parse_link(url)
    
    if link_type == 'unknown':
        return {
            'url': url,
            'status': 'expired',
            'reason': 'Invalid link format',
            'title': None,
            'members': 0,
            'is_channel': False,
            'is_group': False,
            'request_needed': False
        }

    try:
        if link_type == 'invite':
            # MTProto CheckChatInviteRequest - does NOT join the chat, just inspects metadata
            result = await client(functions.messages.CheckChatInviteRequest(hash=identifier))
            
            if isinstance(result, types.ChatInvite):
                return {
                    'url': url,
                    'status': 'working',
                    'reason': 'Active invite link',
                    'title': result.title,
                    'members': result.participants_count if hasattr(result, 'participants_count') else 0,
                    'is_channel': getattr(result, 'channel', False),
                    'is_group': not getattr(result, 'channel', False),
                    'request_needed': getattr(result, 'request_needed', False)
                }
            elif isinstance(result, types.ChatInviteAlready):
                # Bot or account is already in this chat
                chat = result.chat
                title = getattr(chat, 'title', 'Already Joined Chat')
                participants = getattr(chat, 'participants_count', 0)
                return {
                    'url': url,
                    'status': 'working',
                    'reason': 'Active (Already member)',
                    'title': title,
                    'members': participants,
                    'is_channel': isinstance(chat, types.Channel) and chat.broadcast,
                    'is_group': isinstance(chat, types.Chat) or (isinstance(chat, types.Channel) and chat.megagroup),
                    'request_needed': False
                }
            elif isinstance(result, types.ChatInvitePeek):
                chat = result.chat
                return {
                    'url': url,
                    'status': 'working',
                    'reason': 'Active invite (Peekable)',
                    'title': getattr(chat, 'title', 'Telegram Chat'),
                    'members': getattr(chat, 'participants_count', 0),
                    'is_channel': getattr(chat, 'broadcast', False),
                    'is_group': True,
                    'request_needed': False
                }
            else:
                return {
                    'url': url,
                    'status': 'working',
                    'reason': 'Active',
                    'title': 'Telegram Chat',
                    'members': 0,
                    'is_channel': False,
                    'is_group': True,
                    'request_needed': False
                }
                
        elif link_type == 'public':
            # Check public username entity
            entity = await client.get_entity(identifier)
            if isinstance(entity, (types.Channel, types.Chat)):
                return {
                    'url': url,
                    'status': 'working',
                    'reason': 'Active public chat',
                    'title': getattr(entity, 'title', identifier),
                    'members': getattr(entity, 'participants_count', 0),
                    'is_channel': isinstance(entity, types.Channel) and entity.broadcast,
                    'is_group': isinstance(entity, types.Chat) or (isinstance(entity, types.Channel) and entity.megagroup),
                    'request_needed': False
                }
            elif isinstance(entity, types.User):
                return {
                    'url': url,
                    'status': 'working',
                    'reason': 'User/Bot profile',
                    'title': f"{entity.first_name or ''} {entity.last_name or ''}".strip() or entity.username,
                    'members': 1,
                    'is_channel': False,
                    'is_group': False,
                    'request_needed': False
                }

    except (InviteHashExpiredError, InviteHashInvalidError):
        return {
            'url': url,
            'status': 'expired',
            'reason': 'Invite link expired or revoked',
            'title': None,
            'members': 0,
            'is_channel': False,
            'is_group': False,
            'request_needed': False
        }
    except (UsernameInvalidError, UsernameNotOccupiedError):
        return {
            'url': url,
            'status': 'expired',
            'reason': 'Username does not exist',
            'title': None,
            'members': 0,
            'is_channel': False,
            'is_group': False,
            'request_needed': False
        }
    except ChannelPrivateError:
        return {
            'url': url,
            'status': 'expired',
            'reason': 'Private channel (Banned or restricted)',
            'title': None,
            'members': 0,
            'is_channel': False,
            'is_group': False,
            'request_needed': False
        }
    except FloodWaitError as e:
        # Pause and do NOT mark as expired! Mark as temporary error or wait
        print(f"⚠️ FloodWait encountered: sleeping for {e.seconds} seconds...")
        await asyncio.sleep(min(e.seconds, 5))
        return {
            'url': url,
            'status': 'error',
            'reason': f'Rate limited (FloodWait {e.seconds}s)',
            'title': None,
            'members': 0,
            'is_channel': False,
            'is_group': False,
            'request_needed': False
        }
    except RPCError as e:
        error_msg = str(e)
        if "INVITE_HASH_EXPIRED" in error_msg or "INVITE_HASH_INVALID" in error_msg:
            return {
                'url': url,
                'status': 'expired',
                'reason': 'Invite link expired/invalid',
                'title': None,
                'members': 0,
                'is_channel': False,
                'is_group': False,
                'request_needed': False
            }
        return {
            'url': url,
            'status': 'error',
            'reason': f'Telegram API error: {error_msg}',
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
            'reason': f'Network error: {str(e)}',
            'title': None,
            'members': 0,
            'is_channel': False,
            'is_group': False,
            'request_needed': False
        }

    return {
        'url': url,
        'status': 'expired',
        'reason': 'Unknown response',
        'title': None,
        'members': 0,
        'is_channel': False,
        'is_group': False,
        'request_needed': False
    }
