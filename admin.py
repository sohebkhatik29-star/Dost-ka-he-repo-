import os
import json
import time
import asyncio
from typing import Dict, Any, Set
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
