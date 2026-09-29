import re
import time
import asyncio
from typing import List, Dict, Any, Tuple, Optional
from telethon import TelegramClient
from telethon.tl import functions, types
from telethon.errors import BotMethodInvalidError, FloodWaitError, RPCError

# Persistent link verification cache (only for definitively verified links)
LINK_CACHE: Dict[str, Dict[str, Any]] = {}
CACHE_TTL = 300  # 5 minutes TTL for verified links

# Non-chat Telegram system paths to ignore
IGNORED_SYSTEM_PATHS = {
    'c', 's', 'share', 'iv', 'proxy', 'socks', 'addstickers',
    'addemoji', 'addtheme', 'invoice', 'boost', 'setlanguage',
    'login', 'passport', 'terms', 'privacy', 'addlist', 'joinchat'
}


def clean_title(val: Any) -> str:
    """
    Safely converts any Telegram title (including TextWithEntities objects) to plain clean text.
    """
    if not val:
        return 'Telegram Chat'
    if hasattr(val, 'text'):
        return str(val.text).strip() or 'Telegram Chat'
    val_str = str(val).strip()
    if val_str.startswith('TextWithEntities('):
        m = re.search(r"text=['\"](.*?)['\"]", val_str)
        if m:
            return m.group(1).strip()
    return val_str or 'Telegram Chat'


def get_cached_result(url: str) -> Optional[Dict[str, Any]]:
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
    Extracts and normalizes all unique Telegram invite, public, bot, and folder links from raw text.
    Preserves exact order and removes duplicates.
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

    tg_domains = re.findall(r'tg://resolve\?domain=([a-zA-Z0-9_]{4,32})(?:&start=([^\s\n\(\)\[\]\{\}<>\"\',;]+))?', text, re.IGNORECASE)
    for d, st in tg_domains:
        if d.lower() not in IGNORED_SYSTEM_PATHS and not d.isdigit():
            canonical = f"https://t.me/{d}" + (f"?start={st}" if st else "")
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
        r'(?:https?://)?(?:www\.)?(?:t(?:elegram)?\.(?:me|dog)|telegram\.org)/([^\s\n\(\)\[\]\{\}<>\"\',;]+)',
        re.IGNORECASE
    )

    for match in url_pattern.finditer(text):
        raw_full = match.group(1).strip().rstrip('.,;:!?\"\')]}')
        if not raw_full:
            continue

        raw_path = raw_full.split('?')[0].split('#')[0].strip('/')
        query_part = ('?' + raw_full.split('?')[1]) if '?' in raw_full else ''

        if not raw_path:
            continue

        path_first_part = raw_path.split('/')[0].lower()

        # Handle Addlist folder
        if path_first_part == 'addlist':
            parts = raw_path.split('/')
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
        if raw_path.startswith('+'):
            invite_hash = raw_path[1:]
            if re.match(r'^[a-zA-Z0-9_\-]+$', invite_hash) and len(invite_hash) >= 4:
                canonical_url = f"https://t.me/+{invite_hash}"
                if canonical_url not in seen:
                    seen.add(canonical_url)
                    found_links.append(canonical_url)
        elif raw_path.startswith('joinchat/'):
            invite_hash = raw_path[len('joinchat/'):]
            if re.match(r'^[a-zA-Z0-9_\-]+$', invite_hash) and len(invite_hash) >= 4:
                canonical_url = f"https://t.me/+{invite_hash}"
                if canonical_url not in seen:
                    seen.add(canonical_url)
                    found_links.append(canonical_url)
        else:
            # Public username or bot
            username = raw_path.split('/')[0]
            if re.match(r'^[a-zA-Z0-9_]{4,32}$', username) and not username.isdigit() and username.lower() not in IGNORED_SYSTEM_PATHS:
                canonical_url = f"https://t.me/{username}{query_part}"
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
    Extracts all Telegram links from a Telethon Message object.
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
    Identifies link type ('invite', 'addlist', 'bot', 'public') and returns the clean identifier.
    """
    # 1. Chat Folder / Addlist
    addlist_match = re.search(r't\.me/addlist/([a-zA-Z0-9_\-]+)', url)
    if addlist_match:
        return ('addlist', addlist_match.group(1))

    # Bare addlist without slug -> Invalid
    if re.search(r't\.me/addlist/?$', url):
        return ('unknown', url)

    # 2. Private Invite: https://t.me/+hash or https://t.me/joinchat/hash
    invite_match = re.search(r't\.me/(?:\+|joinchat/)([a-zA-Z0-9_\-]+)', url)
    if invite_match:
        return ('invite', invite_match.group(1))

    # 3. Bot link: contains ?start= or username ends with 'bot'
    bot_match = re.search(r't\.me/([a-zA-Z0-9_]{3,32}(?:bot|_bot))\b', url, re.IGNORECASE)
    if bot_match or '?start=' in url or '&start=' in url:
        uname_match = re.search(r't\.me/([a-zA-Z0-9_]{3,32})', url)
        uname = uname_match.group(1) if uname_match else url
        return ('bot', uname)

    # 4. Public Chat / Channel: https://t.me/username
    public_match = re.search(r't\.me/([a-zA-Z0-9_]{4,32})', url)
    if public_match:
        uname = public_match.group(1)
        if uname.lower() not in IGNORED_SYSTEM_PATHS:
            return ('public', uname)

    return ('unknown', url)


# =====================================================================
#                       FIXED LINK CHECKER
# =====================================================================
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
    """Error ki pehchan: class name + message, upper-case aur '_' hata ke."""
    raw = f"{type(e).__name__} {getattr(e, 'message', '')} {e}"
    return raw.upper().replace('_', '')


def _has(sig: str, codes: Tuple[str, ...]) -> bool:
    return any(c.replace('_', '') in sig for c in codes)


# Only definitive "dead" errors from Telegram. Do NOT put account-restriction
# errors (CHANNEL_PRIVATE etc.) here — those often mean the link is still
# valid for other users and were causing false "expired" results.
EXPIRED_CODES = (
    'INVITE_HASH_EXPIRED', 'INVITE_HASH_INVALID', 'INVITE_HASH_EMPTY',
    'INVITE_SLUG_EXPIRED', 'INVITE_SLUG_EMPTY', 'INVITE_SLUG_INVALID',
    'CHATLIST_INVALID', 'USERNAME_INVALID', 'USERNAME_NOT_OCCUPIED',
    'CHANNEL_INVALID', 'CHAT_INVALID',
)
# These mean the link is still usable (or the account is restricted, but link lives)
WORKING_CODES = (
    'INVITE_REQUEST_SENT', 'USER_ALREADY_PARTICIPANT', 'USER_BANNED_IN_CHANNEL',
    'CHANNEL_PRIVATE',  # account cannot see content, but invite/username can still be valid
)


async def _check_addlist(client: TelegramClient, url: str, slug: str) -> Dict[str, Any]:
    result = await client(functions.chatlists.CheckChatlistInviteRequest(slug=slug))
    title = clean_title(getattr(result, 'title', None) or 'Chat Folder')
    chats = getattr(result, 'chats', None) or getattr(result, 'peers', None) or []
    count = len(chats)
    return _res(url, 'working', f'Active Chat Folder ({count} chats)', title, count)


async def _check_invite(client: TelegramClient, url: str, invite_hash: str) -> Dict[str, Any]:
    result = await client(functions.messages.CheckChatInviteRequest(hash=invite_hash))

    # ChatInvite = not joined yet, but link is valid/active
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

    # ChatInviteAlready / ChatInvitePeek → already member or peek available → still WORKING
    # IMPORTANT FIX: Even if chat is ChannelForbidden (this account is banned/restricted),
    # the INVITE LINK itself is still valid for other users. Do NOT mark it expired.
    # False "expired" on active links was happening because of this.
    chat = getattr(result, 'chat', None)
    is_chan = True
    title = 'Telegram Private Chat'
    members = 0
    if chat is not None:
        title = clean_title(getattr(chat, 'title', None) or 'Telegram Private Chat')
        if isinstance(chat, types.ChannelForbidden):
            is_chan = True
            # Link is valid; only this checker account cannot access the content
        else:
            is_chan = bool(getattr(chat, 'broadcast', False))
            members = getattr(chat, 'participants_count', 0) or 0

    reason = (
        'Active invite link (account restricted, but link valid)'
        if isinstance(chat, types.ChannelForbidden)
        else 'Active invite link (already joined / accessible)'
    )
    return _res(
        url, 'working', reason,
        title, members,
        is_channel=is_chan, is_group=not is_chan,
    )


async def _check_username(client: TelegramClient, url: str, username: str) -> Dict[str, Any]:
    """Public channel/group + bot + user: resolved.peer se sahi entity match hoti he."""
    resolved = await client(functions.contacts.ResolveUsernameRequest(username=username))
    peer = resolved.peer

    if isinstance(peer, types.PeerUser):
        user = next((u for u in resolved.users if u.id == peer.user_id), None)
        if user is None or getattr(user, 'deleted', False):
            return _res(url, 'expired', 'Account deleted / not found')
        name = f"{user.first_name or ''} {user.last_name or ''}".strip() or getattr(user, 'username', None) or username
        if getattr(user, 'bot', False):
            return _res(url, 'working', 'Active Telegram Bot', clean_title(name), 1)
        return _res(url, 'working', 'Active user profile', clean_title(name), 1)

    if isinstance(peer, (types.PeerChannel, types.PeerChat)):
        pid = peer.channel_id if isinstance(peer, types.PeerChannel) else peer.chat_id
        chat = next((c for c in resolved.chats if c.id == pid), None)
        if chat is None:
            return _res(url, 'expired', 'Chat not found')
        # FIX: ChannelForbidden means THIS account cannot access content,
        # but the public username / link still exists and is valid for others.
        # Do not mark active public links as expired.
        is_chan = True
        title = clean_title(getattr(chat, 'title', None) or username)
        members = 0
        if isinstance(chat, types.ChannelForbidden):
            reason = 'Active public chat (account restricted, but link valid)'
        else:
            is_chan = bool(getattr(chat, 'broadcast', False))
            members = getattr(chat, 'participants_count', 0) or 0
            reason = 'Active public chat'
        return _res(
            url, 'working', reason,
            title, members,
            is_channel=is_chan, is_group=not is_chan,
        )

    return _res(url, 'expired', 'Username not found')


async def check_single_link(client: TelegramClient, url: str, max_retries: int = 5) -> Dict[str, Any]:
    """
    Telegram confirm kare link zinda he  -> 'working'
    Telegram confirm kare link dead he   -> 'expired'
    Verify nahi ho paya (network/flood)  -> 'expired' + definitive=False

    Private invite links require a USER session (not bot token).
    Public usernames / bots work with both user and bot clients.
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
            wait_s = int(getattr(e, 'seconds', 30) or 30)
            if wait_s > 180:
                last_reason = f'Rate limited by Telegram ({wait_s}s) - try again later'
                break
            print(f"⚠️ FloodWait {wait_s}s on {url}, waiting...")
            await asyncio.sleep(wait_s + 2)
            last_reason = 'Rate limited by Telegram'
            continue

        except BotMethodInvalidError:
            # Bot token cannot call CheckChatInviteRequest (private invites).
            # For public/bot links this error is rare; treat as needs user session.
            if link_type == 'invite' or link_type == 'addlist':
                last_reason = 'Private link – user session (MTProto engine) required'
            else:
                last_reason = 'BotMethodInvalid – try with user session'
            break

        except RPCError as e:
            sig = _sig(e)
            if _has(sig, WORKING_CODES):
                res = _res(url, 'working', 'Active invite link',
                           request_needed=('INVITEREQUESTSENT' in sig))
                set_cached_result(url, res)
                return res
            if _has(sig, EXPIRED_CODES):
                # These are definitive dead/expired/invalid from Telegram itself
                res = _res(url, 'expired', 'Link expired / invalid / revoked')
                set_cached_result(url, res)
                return res
            # Unknown RPC – retry a few times
            last_reason = f'Telegram error: {type(e).__name__}'
            await asyncio.sleep(min(2.0 * attempt, 8))
            continue

        except Exception as e:  # network, timeout, connection reset etc.
            last_reason = f'Could not verify: {type(e).__name__}'
            await asyncio.sleep(min(1.8 * attempt, 7))
            continue

    # After all retries still not verified → mark expired but non-definitive
    return _res(url, 'expired', last_reason, definitive=False)


async def check_links_sequential(client: TelegramClient, links: List[str],
                                 delay: float = 1.5) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Har link ek-ek karke sequentially check karta hai.
    Returns: (working_list, not_working_list)

    - Temporary failures (network / short FloodWait) pe end me ek baar re-try hota hai.
    - Sirf Telegram se confirmed dead links + final failures 'expired' mein jaate hain.
    """
    results: List[Dict[str, Any]] = []
    for url in links:
        results.append(await check_single_link(client, url))
        await asyncio.sleep(max(0.8, delay))

    # Non-definitive results ko end me ek baar aur try karo (better accuracy)
    for i, r in enumerate(results):
        if not r.get('definitive', True):
            await asyncio.sleep(3.5)
            results[i] = await check_single_link(client, r['url'], max_retries=3)

    working = [r for r in results if r.get('status') == 'working']
    not_working = [r for r in results if r.get('status') != 'working']
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
