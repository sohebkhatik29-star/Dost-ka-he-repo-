import os
import sys
from dotenv import load_dotenv

# Load variables from .env file if present
load_dotenv()

# --- CREDENTIAL CONFIGURATION ---
API_ID_RAW = os.getenv("API_ID", "37110691").strip()
API_HASH = os.getenv("API_HASH", "7716785e7c8e83d29591ff749ce11cd6").strip()
BOT_TOKEN = os.getenv("BOT_TOKEN", "8889912983:AAEdEIc_SkwbD6GoHyy7nY9jGVG-Pzb0Lw0").strip()
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "8164563807,6814857981,5566977478").strip()

# User Session String (Required by Telegram MTProto to inspect private chat invites without BOT_METHOD_INVALID error)
SESSION_STRING = os.getenv("SESSION_STRING", "").strip()

# Validate API_ID
try:
    API_ID = int(API_ID_RAW) if API_ID_RAW else 0
except ValueError:
    print("❌ ERROR: API_ID must be a valid integer.")
    sys.exit(1)

# Validate ADMIN_ID (supports single ID or comma-separated list)
ADMIN_IDS = []
if ADMIN_ID_RAW:
    for aid in ADMIN_ID_RAW.split(","):
        aid = aid.strip()
        if aid.isdigit():
            ADMIN_IDS.append(int(aid))

# Check mandatory credentials
def validate_config():
    missing = []
    if not API_ID:
        missing.append("API_ID")
    if not API_HASH:
        missing.append("API_HASH")
    if not BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not ADMIN_IDS:
        missing.append("ADMIN_ID")
    
    if missing:
        print(f"❌ Missing required Environment Variables: {', '.join(missing)}")
        print("ℹ️ Please set them in your .env file or Render Environment Settings.")
        return False
    return True

# Safe delay between checking links (in seconds) to avoid Telegram FloodWait
CHECK_DELAY = float(os.getenv("CHECK_DELAY", "0.6"))

# Maximum links allowed per batch to keep operations safe and responsive
MAX_LINKS_PER_BATCH = int(os.getenv("MAX_LINKS_PER_BATCH", "1000"))
