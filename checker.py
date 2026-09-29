"""
FIX for the "working links shown as expired" bug.

Isko apni file me PURANE `check_single_link` ki jagah paste kar (purana function poora delete kar de).
Baaki sab functions (extract_telegram_links, parse_link, clean_title, get_cached_result,
set_cached_result, LINK_CACHE, IGNORED_SYSTEM_PATHS) waise hi rahenge.

Result dict ka format PURANE jaisa hi he, isliye bot ka baaki code change nahi karna padega.
"""
import asyncio
from typing import Any, Dict, List, Tuple

from telethon import TelegramClient
from telethon.tl import functions, types
from telethon.errors import BotMethodInvalidError, FloodWaitError, RPCError

# Ye 3 cheezein tere purane code me pehle se hen (clean_title, parse_link,
# get_cached_result, set_cached_result) - unko import/rehne dena.


# ---------------------------------------------------------------- helpers
def _res(url: str, status: str, reason: str, title=None, members: int = 0,
         is_channel: bool = False, is_group: bool = False,
         request_needed: bool = False, definitive: bool = True) -> Dict[str, Any]:
    return {
        'url': url,
        'status': status,            # 'working' ya 'expired' (expired = not working)
        'reason': reason,
        'title': title,
        'members': members,
        'is_channel': is_channel,
        'is_group': is_group,
        'request_needed': request_needed,
        'definitive': definitive,    # False = verify nahi ho paya
    }


def _sig(e: Exception) -> str:
    """
    Error ki pehchan: class name + message, upper-case aur '_' hata ke.
    Telethon ke specific errors (InviteHashExpiredError) ka str() human text hota he,
    'INVITE_HASH_EXPIRED' nahi - isliye class name bhi check karte hen.
    """
    raw = f"{type(e).__name__} {getattr(e, 'message', '')} {e}"
    return raw.upper().replace('_', '')


def _has(sig: str, codes: Tuple[str, ...]) -> bool:
    return any(c.replace('_', '') in sig for c in codes)


# In errors ka matlab: link pakka dead he
EXPIRED_CODES = (
    'INVITE_HASH_EXPIRED', 'INVITE_HASH_INVALID', 'INVITE_HASH_EMPTY',
    'INVITE_SLUG_EXPIRED', 'INVITE_SLUG_EMPTY', 'INVITE_SLUG_INVALID',
    'CHATLIST_INVALID', 'USERNAME_INVALID', 'USERNAME_NOT_OCCUPIED',
    'CHANNEL_PRIVATE', 'CHANNEL_INVALID', 'CHAT_INVALID',
)
# In errors ka matlab: link valid he
WORKING_CODES = ('INVITE_REQUEST_SENT', 'USER_ALREADY_PARTICIPANT', 'USER_BANNED_IN_CHANNEL')


# ------------------------------------------------------ per-type checkers
async def _check_addlist(client: TelegramClient, url: str, slug: str) -> Dict[str, Any]:
    result = await client(functions.chatlists.CheckChatlistInviteRequest(slug=slug))
    title = clean_title(getattr(result, 'title', None) or 'Chat Folder')
    chats = getattr(result, 'chats', None) or getattr(result, 'peers', None) or []
    count = len(chats)
    return _res(url, 'working', f'Active Chat Folder ({count} chats)', title, count)


async def _check_invite(client: TelegramClient, url: str, invite_hash: str) -> Dict[str, Any]:
    result = await client(functions.messages.CheckChatInviteRequest(hash=invite_hash))

    if isinstance(result, types.ChatInvite):
        is_chan = bool(getattr(result, 'channel', False) or getattr(result, 'broadcast', False))
        req = bool(getattr(result, 'request_needed', False))
        return _res(
            url, 'working',
            'Active invite link' + (' (Join Request Required)' if req else ''),
            clean_title(getattr(result, 'title', None) or 'Telegram Private Chat'),
            getattr(result, 'participants_count', 0) or 0,
            is_channel=is_chan, is_group=not is_chan, request_needed=req,
        )

    # ChatInviteAlready / ChatInvitePeek -> .chat me asli chat hoti he
    chat = getattr(result, 'chat', None)
    if isinstance(chat, types.ChannelForbidden):
        return _res(url, 'expired', 'Channel restricted / inaccessible',
                    clean_title(getattr(chat, 'title', None)), is_channel=True)

    is_chan = bool(getattr(chat, 'broadcast', False)) if chat else False
    return _res(
        url, 'working', 'Active invite link',
        clean_title(getattr(chat, 'title', None) if chat else None),
        (getattr(chat, 'participants_count', 0) or 0) if chat else 0,
        is_channel=is_chan, is_group=not is_chan,
    )


async def _check_username(client: TelegramClient, url: str, username: str) -> Dict[str, Any]:
    """
    Public channel/group + bot + user: sab yahin verify hote hen.
    NOTE: resolved.chats[0] pe bharosa nahi karna - usme linked discussion group bhi aa jata he.
    Isliye resolved.peer se sahi entity match karte hen.
    """
    resolved = await client(functions.contacts.ResolveUsernameRequest(username=username))
    peer = resolved.peer

    if isinstance(peer, types.PeerUser):
        user = next((u for u in resolved.users if u.id == peer.user_id), None)
        if user is None or getattr(user, 'deleted', False):
            return _res(url, 'expired', 'Account deleted / not found')
        name = f"{user.first_name or ''} {user.last_name or ''}".strip() or user.username or username
        if getattr(user, 'bot', False):
            return _res(url, 'working', 'Active Telegram Bot', clean_title(name), 1)
        return _res(url, 'working', 'Active user profile', clean_title(name), 1)

    if isinstance(peer, (types.PeerChannel, types.PeerChat)):
        pid = peer.channel_id if isinstance(peer, types.PeerChannel) else peer.chat_id
        chat = next((c for c in resolved.chats if c.id == pid), None)
        if chat is None:
            return _res(url, 'expired', 'Chat not found')
        if isinstance(chat, types.ChannelForbidden):
            return _res(url, 'expired', 'Channel restricted / inaccessible',
                        clean_title(getattr(chat, 'title', username)), is_channel=True)
        is_chan = bool(getattr(chat, 'broadcast', False))
        return _res(
            url, 'working', 'Active public chat',
            clean_title(getattr(chat, 'title', username)),
            getattr(chat, 'participants_count', 0) or 0,
            is_channel=is_chan, is_group=not is_chan,
        )

    return _res(url, 'expired', 'Username not found')


# ------------------------------------------------------------ main checker
async def check_single_link(client: TelegramClient, url: str, max_retries: int = 4) -> Dict[str, Any]:
    """
    Ek link ko REAL check karta he (bot link bhi - pehle bot links bina check kiye
    hamesha 'working' ho jate the).

    Rule:
      - Telegram confirm kare ki link zinda he       -> 'working'
      - Telegram confirm kare ki link dead he        -> 'expired'
      - Network / flood / unknown error (verify nahi) -> 'expired' + definitive=False
        (check_links_sequential inko end me ek baar aur retry karta he)
    """
    cached = get_cached_result(url)
    if cached:
        return cached

    link_type, ident = parse_link(url)
    if link_type == 'unknown':
        res = _res(url, 'expired', 'Invalid link format')
        set_cached_result(url, res)
        return res

    last_reason = 'Could not verify (network / rate limit)'

    for attempt in range(1, max_retries + 1):
        try:
            if link_type == 'addlist':
                res = await _check_addlist(client, url, ident)
            elif link_type == 'invite':
                res = await _check_invite(client, url, ident)
            else:  # 'public' aur 'bot'
                res = await _check_username(client, url, ident)

            set_cached_result(url, res)
            return res

        except FloodWaitError as e:
            # FloodWaitError, RPCError ka child he - isliye ise pehle pakadna zaroori he
            if e.seconds > 120:
                last_reason = f'Rate limited by Telegram ({e.seconds}s) - baad me dobara check karo'
                break
            print(f"⚠️ FloodWait {e.seconds}s on {url}, waiting...")
            await asyncio.sleep(e.seconds + 1)
            last_reason = 'Rate limited by Telegram'
            continue

        except BotMethodInvalidError:
            # Client bot-token se login he. Invite/folder check sirf USER account se hota he.
            last_reason = 'Client bot token se login he - user account (session) chahiye'
            break

        except RPCError as e:
            sig = _sig(e)
            if _has(sig, WORKING_CODES):
                res = _res(url, 'working', 'Active invite link',
                           request_needed='INVITEREQUESTSENT' in sig)
                set_cached_result(url, res)
                return res
            if _has(sig, EXPIRED_CODES):
                res = _res(url, 'expired', 'Link expired / invalid / revoked')
                set_cached_result(url, res)
                return res
            last_reason = f'Telegram error: {type(e).__name__}'
            await asyncio.sleep(1.5 * attempt)
            continue

        except Exception as e:  # network, timeout, etc.
            last_reason = f'Could not verify: {type(e).__name__}'
            await asyncio.sleep(1.5 * attempt)
            continue

    # Yaha tak aaye matlab verify nahi hua (dead confirm nahi hua)
    return _res(url, 'expired', last_reason, definitive=False)


# ------------------------------------------------ one-by-one + 2 sections
async def check_links_sequential(client: TelegramClient, links: List[str],
                                 delay: float = 1.2) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Har link ek ek karke check karta he (flood se bachne ke liye beech me delay).
    Return: (working_list, not_working_list)
    """
    results: List[Dict[str, Any]] = []
    for url in links:
        results.append(await check_single_link(client, url))
        await asyncio.sleep(delay)

    # Jo verify nahi hue the unhe end me ek baar aur try karo
    for i, r in enumerate(results):
        if not r['definitive']:
            await asyncio.sleep(3)
            results[i] = await check_single_link(client, r['url'], max_retries=3)

    working = [r for r in results if r['status'] == 'working']
    not_working = [r for r in results if r['status'] != 'working']
    return working, not_working


def build_report(working: List[Dict[str, Any]], not_working: List[Dict[str, Any]]) -> str:
    lines = [f"✅ WORKING LINKS ({len(working)})"]
    for i, r in enumerate(working, 1):
        extra = f" — {r['title']}" if r.get('title') else ''
        if r.get('members'):
            extra += f" ({r['members']})"
        lines.append(f"{i}. {r['url']}{extra}")
    if not working:
        lines.append("Koi nahi")

    lines.append("")
    lines.append(f"❌ NOT WORKING LINKS ({len(not_working)})")
    for i, r in enumerate(not_working, 1):
        lines.append(f"{i}. {r['url']} — {r['reason']}")
    if not not_working:
        lines.append("Koi nahi")

    return "\n".join(lines)


# Use kaise karna he (jaha bot links check karta he wahan):
#   links = extract_telegram_links(text)
#   working, not_working = await check_links_sequential(client, links)
#   await event.reply(build_report(working, not_working))
