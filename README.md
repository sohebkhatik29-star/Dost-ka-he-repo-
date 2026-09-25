# 💎 Telegram Bulk Invite Link Checker Bot (MVP)

A powerful, fast, and safe Telegram Bot to bulk-check Telegram channel/group invite links (`t.me/+...`, `t.me/joinchat/...`, `t.me/username`), remove duplicates, filter out expired/invalid links using deep MTProto checks, and export only working links as clean Text or `.TXT` file.

---

## 🚀 Features

- ✅ **Admin Only Access:** Only users listed in `ADMIN_ID` can check links.
- ⚡ **Messy Text Bulk Link Extraction:** Automatically extracts all invite links from emojis, chat logs, forwarded messages, or text files.
- 🧹 **Automatic Duplicate Removal:** Removes duplicates before checking.
- 🛡️ **Deep MTProto Verification:** Uses official Telegram MTProto `CheckChatInviteRequest` (does NOT join channels; safely queries metadata).
- ⏱️ **FloodWait & Rate-Limit Safe:** Automatically handles Telegram temporary rate limits without falsely marking working links as expired.
- 📊 **Real-time Live Progress:** Updates progress bar (`Checked 15/50 (30%)`) directly in Telegram.
- 📥 **Export Options:** Receive verified links directly as Telegram message chunks or download as `.txt` report file.
- ☁️ **Render 24/7 Ready:** Ready to deploy on Render.com with 1-click `render.yaml`.

---

## 📁 File Structure

```text
├── bot.py             # Main bot event handlers, buttons, commands, session manager
├── checker.py         # Regex link extraction & MTProto validity inspection engine
├── config.py          # Environment variables validator
├── requirements.txt   # Telethon, aiohttp, python-dotenv
├── render.yaml        # Render.com automatic deployment configuration
├── .env.example       # Sample environment configuration
└── README.md          # Guide & documentation
```

---

## 📱 Mobile se GitHub Repo Banane ka Tareeqa (Step-by-Step)

1. **GitHub par login karo:**
   - [github.com](https://github.com) open karo mobile browser me.
   - **New Repository** (`+` icon) par click karo.
   - Repo name do: `tg-invite-checker-bot`
   - Visibility: **Public** ya **Private** rakho.
   - **Create repository** par tap karo.

2. **Files add karo:**
   - Repo ke andar `Add file` -> `Create new file` par tap karo.
   - Ek-ek karke ye files create karo aur code paste karo:
     1. `requirements.txt`
     2. `config.py`
     3. `checker.py`
     4. `bot.py`
     5. `render.yaml`
   - Har file ke end me **Commit changes...** button dabao.

---

## 🔑 Telegram Credentials Kaise Milegi?

1. **`BOT_TOKEN`**:
   - Telegram me `@BotFather` search karo.
   - `/newbot` bhejo, naam aur username choose karo.
   - BotFather aapko `BOT_TOKEN` dega (e.g. `7123456789:AAH...`).

2. **`API_ID` & `API_HASH`**:
   - [my.telegram.org](https://my.telegram.org) par mobile browser me login karo (apna Telegram phone number dalo).
   - **API development tools** par click karo.
   - App title aur short name kuch bhi dalo (e.g., `CheckerBot`).
   - `api_id` (numeric) aur `api_hash` (string) copy karo.

3. **`ADMIN_ID`**:
   - Telegram me `@userinfobot` ya `@MissRose_bot` ko `/id` bhejo.
   - Apna numerical Telegram ID copy karo (e.g. `123456789`).

---

## ☁️ Render par 24/7 Deploy Karne ka Tareeqa

1. [render.com](https://render.com) par free account banakar login karo.
2. Dashboard me **New +** -> **Background Worker** (ya Web Service) select karo.
3. Apna GitHub repo connect karo (`tg-invite-checker-bot`).
4. Settings me:
   - **Name:** `tg-invite-checker-bot`
   - **Runtime:** `Python 3`
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python bot.py`
5. **Environment Variables** section me 4 keys add karo:
   - `BOT_TOKEN` = *(Aapka Bot Father Token)*
   - `API_ID` = *(Aapka my.telegram.org API ID)*
   - `API_HASH` = *(Aapka my.telegram.org API Hash)*
   - `ADMIN_ID` = *(Aapka Telegram User ID)*
6. **Create Background Worker** par tap karo!
7. Render automatic build karega aur 1 minute me bot LIVE ho jayega!

---

## 🤖 Bot ko Use Kaise Karein?

1. Apne Telegram bot me jao aur `/start` press karo.
2. Koi bhi message paste ya forward karo jisme invite links ho.
3. Bot automatically unique links extract karke count batayega.
4. **🚀 Start Checking** button dabao.
5. Bot live progress dikhayega aur check complete hone par **[📋 Get as Text]** ya **[📁 Get as File (.txt)]** se active links provide karega!
