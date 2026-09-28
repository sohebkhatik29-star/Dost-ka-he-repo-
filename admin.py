import os
import json
import time
import asyncio
from typing import Dict, Any, Set, Tuple, Optional
from telethon import TelegramClient, events, Button
from telethon.sessions import StringSession
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PasswordHashInvalidError,
    PhoneNumberInvalidError,
    RPCError,
)
from config import ADMIN_IDS, API_ID, API_HASH, OWNER_USERNAME, DEVELOPER_USERNAME
from fsub import load_fsub_config, save_fsub_config

USERS_FILE = "data/users.json"
BANNED_FILE = "data/banned.json"
SAVED_SESSION_FILE = "data/session.txt"
os.makedirs("data", exist_ok=True)


def load_json_set(file_path: str) -> Set[int]:
    if os.path.exists(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(int(x) for x in data)
        except Exception:
            return set()
    return set()


def save_json_set(file_path: str, data_set: Set[int]):
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(list(data_set), f, indent=2)
    except Exception as e:
        print(f"Error saving {file_path}: {e}")


known_users: Set[int] = load_json_set(USERS_FILE)
banned_users: Set[int] = load_json_set(BANNED_FILE)

# In-memory storage for active login processes
login_states: Dict[int, Dict[str, Any]] = {}


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def is_banned(user_id: int) -> bool:
    return user_id in banned_users


def register_user(user_id: int) -> bool:
    """Registers user if new. Returns True if was newly added."""
    if user_id not in known_users:
        known_users.add(user_id)
        save_json_set(USERS_FILE, known_users)
        return True
    return False


def ban_user(user_id: int):
    banned_users.add(user_id)
    save_json_set(BANNED_FILE, banned_users)


def unban_user(user_id: int):
    banned_users.discard(user_id)
    save_json_set(BANNED_FILE, banned_users)


def get_stats() -> Dict[str, int]:
    return {
        "total_users": len(known_users),
        "banned_users": len(banned_users),
        "admin_count": len(ADMIN_IDS)
    }


def get_admin_dashboard_buttons() -> list:
    fsub_cfg = load_fsub_config()
    fsub_status_text = "🟢 ON" if fsub_cfg.get("is_active") else "🔴 OFF"

    return [
        [
            Button.inline("📊 Statistics", data=b"admin_stats"),
            Button.inline("📢 Broadcast", data=b"admin_broadcast")
        ],
        [
            Button.inline(f"🔒 Force Sub ({fsub_status_text})", data=b"admin_fsub_menu"),
            Button.inline("🔑 MTProto Engine", data=b"admin_mtproto_status")
        ],
        [
            Button.inline("🚫 Ban/Unban User", data=b"admin_ban_menu"),
            Button.inline("🔄 Refresh Panel", data=b"admin_main")
        ],
        [Button.inline("❌ Close Panel", data=b"close_admin_panel")]
    ]


# --- MTPROTO INTERACTIVE LOGIN FLOW ---

async def start_login_request(user_id: int, phone_number: str) -> Tuple[bool, str]:
    """Sends OTP code to phone number and prepares login state."""
    clean_phone = phone_number.strip().replace(" ", "").replace("-", "")
    if not clean_phone.startswith("+"):
        clean_phone = "+" + clean_phone

    temp_client = TelegramClient(StringSession(), API_ID, API_HASH)
    await temp_client.connect()

    try:
        sent_code = await temp_client.send_code_request(clean_phone)
        login_states[user_id] = {
            "client": temp_client,
            "phone": clean_phone,
            "phone_code_hash": sent_code.phone_code_hash,
            "step": "AWAITING_CODE",
            "time": time.time()
        }
        return True, "Code sent successfully"
    except PhoneNumberInvalidError:
        await temp_client.disconnect()
        return False, "❌ Invalid Phone Number format. Example: `+919876543210`"
    except FloodWaitError as e:
        await temp_client.disconnect()
        return False, f"⚠️ Telegram Rate Limit: Please wait `{e.seconds}` seconds before requesting again."
    except Exception as e:
        await temp_client.disconnect()
        return False, f"❌ Failed to send code: {str(e)}"


async def verify_login_code(user_id: int, code: str) -> Tuple[bool, str, Optional[TelegramClient], Optional[str]]:
    """Submits the OTP code. Handles 2FA if enabled."""
    state = login_states.get(user_id)
    if not state or state.get("step") != "AWAITING_CODE":
        return False, "No active login session found. Please send `/phone +91...` again.", None, None

    temp_client: TelegramClient = state["client"]
    phone = state["phone"]
    phone_code_hash = state["phone_code_hash"]
    clean_code = code.strip().replace(" ", "").replace("-", "")

    try:
        await temp_client.sign_in(phone=phone, code=clean_code, phone_code_hash=phone_code_hash)
        session_str = temp_client.session.save()
        login_states.pop(user_id, None)

        # Save session to file
        try:
            with open(SAVED_SESSION_FILE, "w") as f:
                f.write(session_str)
        except Exception:
            pass

        return True, "SUCCESS", temp_client, session_str

    except SessionPasswordNeededError:
        state["step"] = "AWAITING_2FA"
        return False, "2FA_REQUIRED", None, None
    except PhoneCodeInvalidError:
        return False, "❌ Invalid OTP Code. Please double check and send `/code XXXXX` again.", None, None
    except PhoneCodeExpiredError:
        login_states.pop(user_id, None)
        await temp_client.disconnect()
        return False, "⚠️ OTP Code has expired. Please restart with `/phone +91...`", None, None
    except Exception as e:
        login_states.pop(user_id, None)
        await temp_client.disconnect()
        return False, f"❌ Sign-in failed: {str(e)}", None, None


async def verify_login_2fa(user_id: int, password: str) -> Tuple[bool, str, Optional[TelegramClient], Optional[str]]:
    """Submits 2FA password."""
    state = login_states.get(user_id)
    if not state or state.get("step") != "AWAITING_2FA":
        return False, "No active 2FA login session found.", None, None

    temp_client: TelegramClient = state["client"]

    try:
        await temp_client.sign_in(password=password.strip())
        session_str = temp_client.session.save()
        login_states.pop(user_id, None)

        try:
            with open(SAVED_SESSION_FILE, "w") as f:
                f.write(session_str)
        except Exception:
            pass

        return True, "SUCCESS", temp_client, session_str
    except PasswordHashInvalidError:
        return False, "❌ Incorrect 2FA Password. Please send `/password YourPassword` again.", None, None
    except Exception as e:
        login_states.pop(user_id, None)
        await temp_client.disconnect()
        return False, f"❌ 2FA verification failed: {str(e)}", None, None
