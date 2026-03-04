import sys, time, os, re, asyncio
from typing import Tuple, List, Optional
from pyrogram import Client, filters, errors, enums
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ChatMemberUpdated, CallbackQuery
from pyrogram.errors import ChatAdminRequired, RPCError, FloodWait, MessageNotModified
from pyrogram.enums import ParseMode
from info import *
from Script import script
from body.database import *
from body.file_forward import FF_SESSIONS, enqueue_forward_jobs
from collections import deque, defaultdict

MESSAGE_LINK_RE = re.compile(r"(?:https?://)?t\.me/(?:c/\d+|[A-Za-z0-9_]+)/(\d+)")
DEFAULT_EDIT_DELAY = 0.8

bot_data = {
    "caption_set": {},
    "block_words_set": {},
    "suffix_set": {},
    "prefix_set": {},
    "replace_words_set": {},
    "url_set": {}
}

_BOT_ME_CACHE = None

async def get_bot_me(client):
    global _BOT_ME_CACHE
    if _BOT_ME_CACHE is None:
        _BOT_ME_CACHE = await client.get_me()
    return _BOT_ME_CACHE


def extract_msg_id_from_text(text: str) -> int | None:
    if not text:
        return None
    m = MESSAGE_LINK_RE.search(text)
    if m:
        return int(m.group(1))
    if text.strip().isdigit():
        return int(text.strip())
    return None


# ──────────────────────────────────────────────────────────────────────────────
#  SMART FILE NAME PARSER  (v2 — handles all provided example filenames)
# ──────────────────────────────────────────────────────────────────────────────

_LANG_MAP = {
    # full name → display
    "hindi":      "Hindi",  "hin":       "Hindi",
    "english":    "English","eng":       "English",
    "tamil":      "Tamil",  "tam":       "Tamil",
    "telugu":     "Telugu", "tel":       "Telugu",
    "malayalam":  "Malayalam",
    "kannada":    "Kannada",
    "marathi":    "Marathi",
    "gujarati":   "Gujarati",
    "bengali":    "Bengali","ben":       "Bengali",
    "punjabi":    "Punjabi",
    "urdu":       "Urdu",
    "japanese":   "Japanese","jpn":      "Japanese",
    "korean":     "Korean", "kor":       "Korean",
    "chinese":    "Chinese","chi":       "Chinese",
    "spanish":    "Spanish","spa":       "Spanish",
    "french":     "French", "fre":       "French",
    "german":     "German", "ger":       "German",
    "italian":    "Italian","ita":       "Italian",
    "russian":    "Russian","rus":       "Russian",
}

_QUALITY_RE = re.compile(
    r'\b(4K|2160p|1080p|720p|480p|360p|240p)\b', re.I
)
_SOURCE_RE = re.compile(
    r'\b(WEB-?DL|WEBRip|BluRay|Blu-Ray|HDRip|DVDRip|CAM|HQ|HDTV|AMZN|NF|DSNP|HULU|HBO|SonyLIV|ZEE5|JioCinema)\b', re.I
)
_VCODEC_RE = re.compile(
    r'\b(x265|x264|HEVC|AV1|VP9|H\.264|H\.265)\b', re.I
)
_ACODEC_RE = re.compile(
    r'\b(AAC|DD5\.1|DDP5\.1|DDP|DD|AC3|DTS|Atmos|MP3|FLAC|EAC3|TrueHD)\b', re.I
)
_EXT_RE = re.compile(r'\.(mkv|mp4|avi|webm|mov|flv|wmv)(?:\s|$)', re.I)
_YEAR_RE = re.compile(r'\b(19\d{2}|20\d{2})\b')
_SEASON_RE = re.compile(r'\bS(?:eason\s*)?(\d{1,2})\b', re.I)
_EPISODE_RE = re.compile(
    r'\bEp?\.?\s*(\d{1,3})\s*[-–to]+\s*(\d{1,3})\b'   # Ep.01-09 / E01-E05
    r'|\bE(?:p(?:isode)?\s*)?(\d{1,3})\b',              # E02 / EP07 / Episode 2
    re.I
)
_BITRATE_RE = re.compile(r'\b(\d+\s*Kbps)\b', re.I)

# sub/dub detection
_ESUB_RE  = re.compile(r'\bE\.?Subs?\b', re.I)
_HSUB_RE  = re.compile(r'\bH\.?Subs?\b', re.I)
_SUB_RE   = re.compile(r'\b(?:Sub(?:title)?s?|Subs?)\b', re.I)
_DUAL_RE  = re.compile(r'\bDual\s*Audio\b', re.I)
_MULTI_RE = re.compile(r'\bMulti\s*(?:Audio|Lang(?:uage)?)?\b', re.I)

# noise words to strip when extracting title
_NOISE_RE = re.compile(
    r'\b(UNCUT|Computed|Extended|Directors\s*Cut|Remastered|Restored'
    r'|PROPER|REPACK|WEB\s*Series|South\s*Movie|Hollywood\s*Movie'
    r'|Full\s*Movie|HD|HQ|ORG|ORIGINAL|DUBBED|RETAIL)\b', re.I
)

def _norm(s: str) -> str:
    return re.sub(r'\s+', ' ', s).strip()

def _parse_languages(text: str) -> List[str]:
    """Extract all language names/codes from text."""
    found_order = []
    seen = set()
    # Look for bracket groups first: [Hindi or English], (Hindi + Tamil)
    bracket_text = re.sub(r'[()[\]|+/]', ' ', text)
    words = re.split(r'[\s,]+', bracket_text)
    for w in words:
        key = w.lower().rstrip('.')
        if key in _LANG_MAP:
            lang = _LANG_MAP[key]
            if lang not in seen:
                seen.add(lang)
                found_order.append(lang)
    return found_order

def _extract_title(raw: str) -> str:
    """Strip everything after the year / season marker to get a clean title."""
    text = re.sub(r'[._\-]+', ' ', raw)
    text = re.sub(r'\s+', ' ', text).strip()

    # Cut at year
    m = _YEAR_RE.search(text)
    if m:
        text = text[:m.start()].strip()

    # Cut at season marker
    m = _SEASON_RE.search(text)
    if m:
        text = text[:m.start()].strip()

    # Cut at quality
    m = _QUALITY_RE.search(text)
    if m:
        text = text[:m.start()].strip()

    # Remove noise
    text = _NOISE_RE.sub('', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text.title()

def _extract_season_episode(text: str) -> Tuple[str, str]:
    season = ""
    episode = ""

    sm = _SEASON_RE.search(text)
    if sm:
        season = f"S{int(sm.group(1)):02d}"

    # ep range like Ep.01-09
    range_m = re.search(
        r'\bEp?\.?\s*(\d{1,3})\s*[-–to]+\s*(\d{1,3})\b', text, re.I
    )
    if range_m:
        e1, e2 = int(range_m.group(1)), int(range_m.group(2))
        episode = f"E{e1:02d}-E{e2:02d}"
        return season, episode

    single_m = re.search(r'\bE(?:p(?:isode)?\s*)?(\d{1,3})\b', text, re.I)
    if single_m:
        episode = f"E{int(single_m.group(1)):02d}"

    return season, episode

def _extract_quality(text: str) -> str:
    m = _QUALITY_RE.search(text)
    return m.group(0).upper().replace("2160P", "4K") if m else ""

def _extract_source(text: str) -> str:
    m = _SOURCE_RE.search(text)
    if not m:
        return ""
    s = m.group(0)
    # Normalize
    sl = s.lower().replace("-", "").replace(" ", "")
    if sl in ("webdl", "webdl"): return "WEB-DL"
    if sl == "webrip": return "WEBRip"
    if sl in ("bluray", "blu-ray"): return "BluRay"
    if sl == "hdrip": return "HDRip"
    if sl == "dvdrip": return "DVDRip"
    return s

def _extract_vcodec(text: str) -> str:
    m = _VCODEC_RE.search(text)
    return m.group(0).upper() if m else ""

def _extract_acodec(text: str) -> str:
    m = _ACODEC_RE.search(text)
    return m.group(0) if m else ""

def _extract_ext(text: str) -> str:
    m = _EXT_RE.search(text)
    return m.group(1).upper() if m else ""

def _extract_sub_tag(text: str) -> str:
    if _ESUB_RE.search(text): return "ESub"
    if _HSUB_RE.search(text): return "HSub"
    if _SUB_RE.search(text):  return "Sub"
    return ""

def _extract_audio_tag(langs: List[str]) -> str:
    if _MULTI_RE.search(" ".join(langs) if not isinstance(langs, str) else langs):
        return "Multi Audio"
    if _DUAL_RE.search(" ".join(langs) if not isinstance(langs, str) else langs):
        return "Dual Audio"
    return ""


def build_smart_filename(file_name: str, caption: str) -> str:
    """
    Build a clean, standardised filename from raw file_name + caption.
    Handles movies, series, anime, dual/multi audio, ESub etc.
    Examples (see requirements for full list).
    """
    # Combine both sources; caption often has extra metadata
    combined = f"{file_name} {caption}"

    title     = _extract_title(file_name)          # title from filename only (cleaner)
    year_m    = _YEAR_RE.search(combined)
    year      = year_m.group(0) if year_m else ""

    season, episode = _extract_season_episode(combined)
    quality   = _extract_quality(combined)
    source    = _extract_source(combined)
    vcodec    = _extract_vcodec(combined)
    acodec    = _extract_acodec(combined)
    ext       = _extract_ext(combined) or _extract_ext(file_name)
    langs     = _parse_languages(combined)
    sub_tag   = _extract_sub_tag(combined)
    is_dual   = bool(_DUAL_RE.search(combined))
    is_multi  = bool(_MULTI_RE.search(combined))

    parts: List[str] = [title]

    if season or episode:
        parts.append(f"{season}{episode}".strip())

    if year:
        parts.append(f"({year})")

    # Language/audio tag
    if langs:
        if is_dual or is_multi:
            audio_tag = "Multi Audio" if is_multi else "Dual Audio"
            lang_str = " + ".join(langs[:3])
            parts.append(f"{lang_str} {audio_tag}")
        else:
            parts.append(" + ".join(langs[:2]))

    # Source & quality
    if source:
        parts.append(source)
    if quality:
        parts.append(quality)

    # Codecs
    if vcodec:
        parts.append(vcodec)
    if acodec:
        parts.append(acodec)

    if sub_tag:
        parts.append(sub_tag)

    if ext:
        result = " ".join(p for p in parts if p)
        return f"{result}.{ext}"

    return " ".join(p for p in parts if p)


def extract_audio_languages(text: str) -> List[str]:
    return _parse_languages(text)

def extract_year(text: str) -> Optional[str]:
    m = _YEAR_RE.search(text or "")
    return m.group(1) if m else None

# ──────────────────────────────────────────────────────────────────────────────
#  CHANNEL EVENTS
# ──────────────────────────────────────────────────────────────────────────────

@Client.on_chat_member_updated()
async def when_added_as_admin(client, chat_member_update: ChatMemberUpdated):
    try:
        new = chat_member_update.new_chat_member
        chat = chat_member_update.chat
        if not new or not getattr(new, "user", None) or not new.user.is_self:
            return
        owner = getattr(chat_member_update, "from_user", None)
        if not owner:
            return
        owner_id = owner.id
        owner_name = owner.first_name or "Unknown"
        await add_user_channel(owner_id, chat.id, chat.title or "Unnamed Channel")
        existing = await get_channel_caption(chat.id)
        if not existing:
            await set_block_words(chat.id, "")
            await set_prefix(chat.id, "")
            await set_suffix(chat.id, "")
            await set_replace_words(chat.id, "")
            await set_link_remover_status(chat.id, False)
            await set_emoji_remover_status(chat.id, False)
        try:
            msg = await client.send_message(
                owner_id,
                f"✅ Bot added to <b>{chat.title}</b>.\nManage it anytime via /settings.",
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ Open Settings", callback_data="settings_cb")]])
            )
            if chat.username:
                ch_link = f"<a href='https://t.me/{chat.username}'>{chat.title}</a>"
            else:
                ch_link = f"{chat.title} (Private)"
            log_text = script.NEW_CHANNEL_TXT.format(
                owner_name=owner_name, owner_id=owner_id,
                channel_name=ch_link, channel_id=chat.id
            )
            await client.send_message(LOG_CH, log_text, disable_web_page_preview=True)
            asyncio.create_task(auto_delete_message(msg, 60))
        except Exception as e:
            print(f"[WARN] notify owner: {e}")
    except Exception as e:
        print(f"[ERROR] when_added_as_admin: {e}")


async def auto_delete_message(msg, delay: int):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except:
        pass

# ──────────────────────────────────────────────────────────────────────────────
#  CALLBACK HANDLERS
# ──────────────────────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^settings_cb$"))
async def settings_button_handler(client: Client, query: CallbackQuery):
    await query.answer()
    await user_settings(client, user=query.from_user, send_func=query.message.edit_text)


@Client.on_callback_query(filters.regex("^help$"))
async def help_callback(client, query: CallbackQuery):
    await query.answer()
    bot_me = await get_bot_me(client)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕️ Add me to your channel", url=f"https://t.me/{bot_me.username}?startchannel=true")],
        [InlineKeyboardButton("⬅️ Back", callback_data="start")]
    ])
    await query.message.edit_text(script.HELP_TEXT, reply_markup=keyboard, disable_web_page_preview=True)


@Client.on_callback_query(filters.regex("^start$"))
async def back_to_start(client: Client, query: CallbackQuery):
    await query.answer()
    await show_start_ui(client, chat_id=query.message.chat.id,
                        mention=query.from_user.mention, edit_message=query.message)


async def show_start_ui(client: Client, *, chat_id: int, mention: str, edit_message=None):
    bot_me = await get_bot_me(client)
    bot_username = bot_me.username or BOT_USERNAME
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕️ Add me to your channel ➕️", url=f"https://t.me/{bot_username}?startchannel=true")],
        [InlineKeyboardButton("📂 Help", callback_data="help"), InlineKeyboardButton("⚙ Settings", callback_data="settings_cb")],
        [InlineKeyboardButton("ℹ️ About", callback_data="about_cb")],
    ])
    text = script.START_TXT.format(mention=mention)
    if edit_message:
        await edit_message.edit_text(text=text, reply_markup=keyboard, disable_web_page_preview=True)
    else:
        await client.send_message(chat_id=chat_id, text=text, reply_markup=keyboard, disable_web_page_preview=True)


@Client.on_callback_query(filters.regex("^about_cb$"))
async def about_callback(client: Client, query: CallbackQuery):
    await query.answer()
    bot = await get_bot_me(client)
    text = script.ABOUT_TXT.format(bot_name=bot.first_name, bot_username=bot.username)
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Owner", url="https://t.me/Navex_69"),
         InlineKeyboardButton("⬅️ Back", callback_data="start")]
    ])
    await query.message.edit_text(text=text, reply_markup=keyboard,
                                   parse_mode=ParseMode.HTML, disable_web_page_preview=True)


# ──────────────────────────────────────────────────────────────────────────────
#  COMMANDS
# ──────────────────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("start") & filters.private)
async def start_cmd(client, message):
    try:
        user = message.from_user
        is_new = await insert_user_check_new(int(user.id))
        await show_start_ui(client, chat_id=message.chat.id, mention=user.mention)
        if is_new:
            try:
                uname = f"<a href='https://t.me/{user.username}'>{user.first_name}</a>" if user.username else user.first_name
                await client.send_message(LOG_CH, script.NEW_USER_TXT.format(user=uname, user_id=user.id), disable_web_page_preview=True)
            except Exception as e:
                print(f"[ERROR] log new user: {e}")
    except Exception as e:
        print(f"[ERROR] start_cmd: {e}")


@Client.on_message(filters.command("settings") & filters.private)
async def settings_cmd(client, message):
    loading = await message.reply_text("⚙️ Fetching your channels…")
    await user_settings(client, user=message.from_user, send_func=loading.edit_text)


@Client.on_message(filters.private & filters.command("file_forward"))
async def ff_start(client, message):
    uid = message.from_user.id
    channels = await get_user_channels(uid)
    if not channels:
        return await message.reply_text("❌ No admin channels found. Add me to a channel first.")
    if len(channels) < 2:
        return await message.reply_text("❌ You need at least 2 channels to use file forward.")
    FF_SESSIONS[uid] = {"step": "src", "channels": channels, "expires": None}
    kb = [[InlineKeyboardButton(ch["channel_title"], callback_data=f"ff_src_{ch['channel_id']}")] for ch in channels]
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")])
    await message.reply_text("📤 <b>Select SOURCE channel</b>", reply_markup=InlineKeyboardMarkup(kb))


@Client.on_message(filters.private & filters.user(ADMIN) & filters.command("admin"))
async def admin_help(client, message):
    from bot import EXECUTORS
    text = script.ADMIN_HELP_TEXT.format(workers=EXECUTORS, delay=DEFAULT_EDIT_DELAY)
    await message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


@Client.on_message(filters.private & filters.user(ADMIN) & filters.command("stats"))
async def bot_stats(client, message):
    pending    = await queue_col.count_documents({"status": "pending"})
    processing = await queue_col.count_documents({"status": "processing"})
    users_count = await total_user()
    text = (
        "📊 <b>BOT STATS</b>\n\n"
        f"• Users: <code>{users_count}</code>\n"
        f"• Pending Jobs: <code>{pending}</code>\n"
        f"• Processing Jobs: <code>{processing}</code>\n"
    )
    await message.reply_text(text, parse_mode=ParseMode.HTML)


@Client.on_message(filters.private & filters.user(ADMIN) & filters.command("dump_skip"))
async def dump_skip_cmd(client, message):
    if len(message.command) != 2:
        return await message.reply_text("❌ Usage:\n`/dump_skip -100xxxxxxxxxx`", parse_mode=ParseMode.MARKDOWN)
    try:
        channel_id = int(message.command[1])
    except ValueError:
        return await message.reply_text("❌ Invalid channel ID")
    await set_dump_skip(channel_id, True)
    text = "✅ <b>Dump skip enabled</b>\n\n" + await format_dump_skip_list(client)
    await message.reply_text(text, parse_mode=ParseMode.HTML)


@Client.on_message(filters.private & filters.user(ADMIN) & filters.command("remove_dump"))
async def remove_dump_cmd(client, message):
    if len(message.command) != 2:
        return await message.reply_text("❌ Usage:\n`/remove_dump -100xxxxxxxxxx`", parse_mode=ParseMode.MARKDOWN)
    try:
        channel_id = int(message.command[1])
    except ValueError:
        return await message.reply_text("❌ Invalid channel ID")
    await remove_dump_skip(channel_id)
    text = "🗑 <b>Dump skip removed</b>\n\n" + await format_dump_skip_list(client)
    await message.reply_text(text, parse_mode=ParseMode.HTML)


@Client.on_message(filters.private & filters.user(ADMIN) & filters.command(["broadcast"]))
async def broadcast(client, message):
    if not message.reply_to_message:
        return await message.reply_text("❌ Reply to a message to broadcast.")
    silicon = await message.reply_text("Getting all IDs from database...")
    all_users = await getid()
    tot = await total_user()
    success = failed = deactivated = blocked = 0
    await silicon.edit("Broadcasting…")
    for user in all_users:
        try:
            await asyncio.sleep(0.05)
            await message.reply_to_message.copy(user["_id"])
            success += 1
        except errors.InputUserDeactivated:
            deactivated += 1
            await delete_user(user["_id"])
        except errors.UserIsBlocked:
            blocked += 1
            await delete_user(user["_id"])
        except Exception:
            failed += 1
        try:
            await silicon.edit(
                f"<u>Broadcast Processing</u>\n\n"
                f"• Total: {tot}\n• Success: {success}\n• Blocked: {blocked}"
                f"\n• Deactivated: {deactivated}\n• Failed: {failed}"
            )
        except errors.FloodWait as e:
            await asyncio.sleep(e.value)
    await silicon.edit(
        f"<u>Broadcast Completed</u>\n\n"
        f"• Total: {tot}\n• Success: {success}\n• Blocked: {blocked}"
        f"\n• Deactivated: {deactivated}\n• Failed: {failed}"
    )


@Client.on_message(filters.private & filters.user(ADMIN) & filters.command("restart"))
async def restart_bot(client, message):
    m = await message.reply_text("🔄 Restarting bot…")
    await asyncio.sleep(2)
    await m.edit("✅ Bot restarted. You can now use me.")
    os.execl(sys.executable, sys.executable, *sys.argv)


@Client.on_message(filters.command("reset") & filters.user(ADMIN))
async def reset_db(client, message):
    await message.reply_text("⚠️ Deleting all data…")
    await users.delete_many({})
    await chnl_ids.delete_many({})
    await user_channels.delete_many({})
    _CHANNEL_CACHE.clear()
    await message.reply_text("✅ All database records deleted.")


@Client.on_message(filters.private & filters.user(ADMIN) & filters.command("queue"))
async def queue_status(client, message):
    cap_pending    = await queue_col.count_documents({"status": "pending"})
    cap_processing = await queue_col.count_documents({"status": "processing"})
    f_pending      = await forward_queue.count_documents({"status": "pending"})
    f_processing   = await forward_queue.count_documents({"status": "processing"})

    cap_pipeline = [
        {"$match": {"status": "pending"}},
        {"$group": {"_id": "$chat_id", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 10}
    ]
    cap_lines = []
    async for row in queue_col.aggregate(cap_pipeline):
        ch_id, count = row["_id"], row["count"]
        try:
            name = (await client.get_chat(ch_id)).title
        except:
            name = str(ch_id)
        eta = int((count / DEFAULT_MAX_WORKERS) * DEFAULT_EDIT_DELAY)
        cap_lines.append(f"• <b>{name}</b> — {count} jobs (~{eta//60}m{eta%60}s)")

    text = (
        "📊 <b>QUEUE STATUS</b>\n\n"
        f"📝 Caption — Pending: <code>{cap_pending}</code> | Processing: <code>{cap_processing}</code>\n"
        f"📦 Forward — Pending: <code>{f_pending}</code> | Processing: <code>{f_processing}</code>\n"
    )
    if cap_lines:
        text += "\n🔥 <b>Busy Caption Channels</b>\n" + "\n".join(cap_lines)
    await message.reply_text(text, parse_mode=enums.ParseMode.HTML)


# ──────────────────────────────────────────────────────────────────────────────
#  SETTINGS UI
# ──────────────────────────────────────────────────────────────────────────────

async def user_settings(client: Client, *, user, send_func):
    user_id = user.id
    channels = await get_user_channels(user_id)
    if not channels:
        bot_me = await get_bot_me(client)
        return await send_func(
            "You haven't added me to any channels yet!\n\n➕ Add me as admin via the button below. 👇",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton(
                "➕ Add me to your channel",
                url=f"https://t.me/{bot_me.username}?startchannel=true"
            )]]),
            disable_web_page_preview=True
        )

    valid_channels, removed_titles = [], []

    async def check_channel(ch):
        ch_id    = ch.get("channel_id")
        ch_title = ch.get("channel_title", str(ch_id))
        cached   = get_cached_chat_title(ch_id)
        if cached:
            ch_title = cached
        try:
            member = await client.get_chat_member(ch_id, "me")
            if _is_admin_member(member):
                if not cached:
                    try:
                        chat = await client.get_chat(ch_id)
                        ch_title = getattr(chat, "title", ch_title)
                        set_cached_chat_title(ch_id, ch_title)
                    except:
                        pass
                return {"valid": True, "channel_id": ch_id, "channel_title": ch_title}
            else:
                await users.update_one({"_id": user_id}, {"$pull": {"channels": {"channel_id": ch_id}}})
                return {"valid": False, "title": ch_title}
        except (ChatAdminRequired, RPCError):
            await users.update_one({"_id": user_id}, {"$pull": {"channels": {"channel_id": ch_id}}})
            return {"valid": False, "title": ch_title}
        except Exception:
            return {"valid": True, "channel_id": ch_id, "channel_title": ch_title}

    results = await asyncio.gather(*[check_channel(ch) for ch in channels])
    for res in results:
        if res["valid"]:
            valid_channels.append(res)
        else:
            removed_titles.append(res["title"])

    if removed_titles:
        await send_func(f"⚠️ Removed (no admin access):\n• " + "\n• ".join(removed_titles))

    if not valid_channels:
        return await send_func("No active channels where I am admin.")

    buttons = [[InlineKeyboardButton(ch["channel_title"], callback_data=f"chinfo_{ch['channel_id']}")] for ch in valid_channels]
    buttons.append([InlineKeyboardButton("❌ Close", callback_data="close_msg")])
    await send_func("📋 Your added channels:", reply_markup=InlineKeyboardMarkup(buttons))


@Client.on_callback_query(filters.regex("^close_msg$"))
async def close_message(client, query):
    await query.answer()
    try:
        await query.message.delete()
    except:
        pass


# ──────────────────────────────────────────────────────────────────────────────
#  AUTO CAPTION CORE
# ──────────────────────────────────────────────────────────────────────────────

def sanitize_caption_html(text: str) -> str:
    if not text:
        return ""
    allowed = {"b", "i", "u", "s", "code", "pre", "a", "spoiler", "blockquote"}
    def repl(m):
        tag = m.group(1).casefold()
        return m.group(0) if tag in allowed else ""
    return re.sub(r"</?\\s*([a-zA-Z0-9]+)(?:\\s[^>]*)?>", repl, text)


async def caption_worker(client: Client):
    while True:
        job = await fetch_channel_job()
        if not job:
            await asyncio.sleep(0.2)
            continue
        ch = job["chat_id"]
        try:
            markup = None
            if job.get("url_buttons"):
                markup = InlineKeyboardMarkup([
                    [InlineKeyboardButton(btn["text"], url=btn["url"]) for btn in row]
                    for row in job["url_buttons"]
                ])
            await client.edit_message_caption(
                chat_id=ch,
                message_id=job["message_id"],
                caption=job["caption"],
                parse_mode=ParseMode.HTML,
                reply_markup=markup
            )
            if not await is_dump_skip(ch):
                try:
                    orig = await client.get_messages(ch, job["message_id"])
                    fname = None
                    for t in ("document", "video", "audio", "voice"):
                        obj = getattr(orig, t, None)
                        if obj:
                            fname = getattr(obj, "file_name", None)
                            break
                    fname = clean_text(fname or "File")
                    fname = remove_emojis(fname)
                    await client.copy_message(chat_id=CP_CH, from_chat_id=ch,
                                              message_id=job["message_id"], caption=fname)
                except:
                    pass
            await mark_done(job["_id"])
            await asyncio.sleep(DEFAULT_EDIT_DELAY)
        except FloodWait as e:
            wait = e.value + 2
            CHANNEL_COOLDOWN[ch] = time.time() + wait
            await reschedule(job["_id"], delay=wait)
        except MessageNotModified:
            await mark_done(job["_id"])
        except Exception:
            if job.get("retries", 0) >= 5:
                await mark_done(job["_id"])
            else:
                await reschedule(job["_id"], delay=10)
        finally:
            CHANNEL_ACTIVE[ch] = max(0, CHANNEL_ACTIVE[ch] - 1)


@Client.on_message(filters.channel & filters.media)
async def reCap(client, msg):
    if msg.edit_date or not msg.media:
        return
    chnl_id = msg.chat.id
    default_caption = msg.caption or ""
    file_name = None
    file_size = None
    for ft in ("video", "audio", "document", "voice"):
        obj = getattr(msg, ft, None)
        if obj:
            file_name = getattr(obj, "file_name", None)
            if not file_name:
                file_name = "Voice Message" if ft == "voice" else "File"
            file_name = file_name.replace("_", " ").replace(".", " ")
            file_size = get_size(getattr(obj, "file_size", 0))
            # resolution & duration (video only)
            break
    if not file_name:
        return

    cap_doc = await get_channel_cached(chnl_id)
    cap_template = cap_doc.get("caption")
    if not cap_template:
        return

    link_remover_on  = bool(cap_doc.get("link_remover", False))
    emoji_remover_on = bool(cap_doc.get("emoji_remover", False))
    blocked_raw      = cap_doc.get("block_words", "")
    suffix           = cap_doc.get("suffix", "") or ""
    prefix           = cap_doc.get("prefix", "") or ""
    replace_raw      = cap_doc.get("replace_words", None)
    url_buttons      = cap_doc.get("url_buttons", [])

    audio_langs = extract_audio_languages(default_caption)
    language    = " ".join(audio_langs)
    year        = extract_year(default_caption) or ""

    # New placeholders
    resolution = ""
    duration   = ""
    video_obj  = getattr(msg, "video", None)
    if video_obj:
        w = getattr(video_obj, "width", 0)
        h = getattr(video_obj, "height", 0)
        if h:
            resolution = f"{w}x{h}"
        dur = getattr(video_obj, "duration", 0)
        if dur:
            m_, s_ = divmod(int(dur), 60)
            h_, m_ = divmod(m_, 60)
            duration = f"{h_:02d}:{m_:02d}:{s_:02d}" if h_ else f"{m_:02d}:{s_:02d}"

    try:
        raw_file_name    = normalize_series_name(file_name)
        smart_file_name  = ""
        if "{smart_file_name}" in cap_template:
            smart_file_name = build_smart_filename(raw_file_name, default_caption)
        empty = ""
        new_caption = cap_template.format(
            file_name=raw_file_name,
            smart_file_name=smart_file_name,
            file_size=file_size,
            default_caption=default_caption,
            language=language,
            year=year,
            resolution=resolution,
            duration=duration,
            empty=empty,
        )
    except Exception:
        new_caption = cap_template

    if blocked_raw:
        new_caption = apply_block_words(new_caption, blocked_raw)
    if replace_raw:
        pairs = parse_replace_pairs(replace_raw)
        if pairs:
            new_caption = apply_replacements(new_caption, pairs)
    if link_remover_on:
        new_caption = strip_links_only(new_caption)
    if prefix:
        new_caption = f"{prefix}\n{new_caption}".strip()
    if suffix:
        new_caption = f"{new_caption}\n{suffix}".strip()
    if emoji_remover_on:
        new_caption = remove_emojis(new_caption)
    new_caption = new_caption.strip()
    if "<" in new_caption and ">" in new_caption:
        new_caption = sanitize_caption_html(new_caption)

    await enqueue_caption({
        "chat_id": msg.chat.id,
        "message_id": msg.id,
        "caption": new_caption,
        "url_buttons": url_buttons,
        "user_id": msg.from_user.id if msg.from_user else None
    })


# ──────────────────────────────────────────────────────────────────────────────
#  HELPER FUNCTIONS
# ──────────────────────────────────────────────────────────────────────────────

def normalize_series_name(name: str) -> str:
    if not name:
        return ""
    name = re.sub(r'\.(mkv|mp4|avi|webm)$', '', name, flags=re.I)
    name = re.sub(r'[._\-]+', ' ', name)
    name = re.sub(r'\s+', ' ', name)
    return name.strip().title()

def _status_name(m):
    status = getattr(m, "status", "")
    try:
        return str(status.value).lower() if hasattr(status, "value") else str(status).lower()
    except:
        return ""

def _is_admin_member(m) -> bool:
    if not m: return False
    status = getattr(m, "status", "")
    try:
        status = str(status.value) if hasattr(status, "value") else str(status)
    except:
        status = str(status)
    return status.lower() in ("administrator", "creator", "owner")

def get_size(size: int) -> str:
    units = ["Bytes", "KB", "MB", "GB", "TB"]
    i = 0
    while size >= 1024.0 and i < len(units) - 1:
        size /= 1024.0
        i += 1
    return "%.2f %s" % (size, units[i])

URL_RE    = re.compile(r"(https?://[^\s]+|www\.[^\s]+|t\.me/[^\s/]+(?:/[^\s]+)?)", re.I)
MENTION_RE= re.compile(r'@\w+', re.I)
MD_LINK_RE= re.compile(r'\[([^\]]+)\]\((?:https?:\/\/[^\)]+|tg:\/\/[^\)]+)\)', re.I)
HTML_A_RE = re.compile(r'<a\s+[^>]*href=["\'](?:https?:\/\/|tg:\/)[^"\']+["\'][^>]*>(.*?)</a>', re.I)
TG_USER_LINK_RE = re.compile(r'\[([^\]]+)\]\(tg:\/\/user\?id=\d+\)', re.I)

EMOJI_LIST = [
    "😀","😃","😄","😁","😆","😅","😂","🤣","😊","😇","🙂","🙃","😉","😌","😍","🥰","😘","😗","😙","😚",
    "😋","😜","😝","😛","🔐","🔒","🔓","🗝","🪪","🧾","📜","📝","📊","📈","📉","🗒","🗓","📅","⏰","⏳",
    "🤪","🤨","🧐","🤓","😎","🥸","🤩","🥳","😏","😒","😞","😔","😟","😕","🙁","☹️","😣","😖","😫","😩",
    "🥺","😢","😭","💡","🔦","🕯","🧯","🛠","⚙️","🔧","🔩","🪛","🧲","📡","🛰","🖥","💻","📱",
    "😤","😠","😡","🤬","🤯","😳","🥵","🥶","😱","😨","😰","😥","😓","🤗","🤔","🫣","🤭","🫢","🤫","🤥",
    "😶","😐","🌈","☀️","🌤","⛅","🌥","☁️","🌦","🌧","⛈","🌩","🌨","❄️","☃️","⛄","🌬","💨",
    "😑","😬","🙄","😯","😦","😧","😮","😲","🥱","😴","🤤","😪","😵","🤐","🥴","🤢","🤮","🤧","😷","🤒","🤕",
    "🌍","🌎","🌏","🗺","🏔","⛰","🌋","🏕","🏖","🏜","🏝","🏞","🌅","🌄","🌠","🌌",
    "👍","👎","👌","✌️","🤞","🤟","🤘","🤙","🫶","👏","🙌","👐","🤲","🙏","✋","🖐","🖖","👋","🤚",
    "💯","✔️","✅","❌","❎","⚠️","🚫","⭕","❗","❓","🔥","💥","✨","🌟","⚡","💫","🎉","🎊","🎬",
    "🎞","📽","🎥","📺","📼","🎧","🎵","🎶","🎼","🛑","🏁","🚦","🚥","🛣","🛤","🚧",
    "🍿","📀","💿","📌","📍","📎","📂","📁","📄","🗂","🗃","🔔","🔕","📢","📣","📯",
    "👑","🎯","🏆","🥇","🥈","🥉","🎖","🏅","🎁","🎈","🎀","🪄","🎨","🧩","♟",
    "🚀","🛸","🚨","🧨","⬆️","⬇️","➡️","⬅️","🔁","🔄","⏩","⏪","⏭","⏮","👀","👁️","🧠",
]

def remove_emojis(text: str) -> str:
    if not text: return text
    for emo in EMOJI_LIST:
        text = text.replace(emo, "")
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

async def format_dump_skip_list(client: Client) -> str:
    items = await get_all_dump_skip_channels()
    if not items:
        return "📭 <b>No dump-skip channels</b>"
    lines = ["📌 <b>Dump-skip channels:</b>\n"]
    for doc in items:
        cid = doc["chnl_id"]
        try:
            title = (await client.get_chat(cid)).title
        except:
            title = "Unknown"
        lines.append(f"• <b>{title}</b>\n  <code>{cid}</code>")
    return "\n".join(lines)

def strip_links_only(text: str) -> str:
    if not text: return text
    text = MD_LINK_RE.sub(r'\1', text)
    text = TG_USER_LINK_RE.sub(r'\1', text)
    text = HTML_A_RE.sub(r'\1', text)
    text = URL_RE.sub("", text)
    text = MENTION_RE.sub("", text)
    text = re.sub(r'\(\s*\)', '', text)
    text = re.sub(r'\[\s*\]', '', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def apply_block_words(caption: str, raw_blocked: str) -> str:
    if not caption or not raw_blocked: return caption
    for item in re.split(r"[,\n]+", raw_blocked):
        item = item.strip()
        if item:
            caption = caption.replace(item, "")
    caption = "\n".join(l.rstrip() for l in caption.splitlines())
    caption = "\n".join(l for l in caption.splitlines() if l.strip())
    return re.sub(r"[ \t]{2,}", " ", caption).strip()

def parse_replace_pairs(raw):
    if not raw: return []
    if isinstance(raw, list): raw = ','.join(map(str, raw))
    elif not isinstance(raw, str): raw = str(raw)
    raw = raw.replace('\n', ',')
    pairs = []
    for item in [p.strip() for p in raw.split(',') if p.strip()]:
        parts = item.split(None, 1)
        if len(parts) == 2:
            pairs.append((parts[0], parts[1]))
    return pairs

def apply_replacements(text: str, pairs: List[Tuple[str, str]]) -> str:
    if not pairs or not text: return text
    for old, new in pairs:
        if not old: continue
        try:
            text = re.sub(re.escape(old), new, text, flags=re.IGNORECASE)
        except re.error:
            text = text.replace(old, new)
    return re.sub(r'[ \t]+', ' ', text).strip()

def clean_text(text: str) -> str:
    if not text: return ""
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'(https?://\S+|t\.me/\S+)', '', text)
    text = re.sub(r'@\w+', '', text)
    return re.sub(r'\s+', ' ', text).strip()


# ──────────────────────────────────────────────────────────────────────────────
#  USER INPUT HANDLER (catches text replies for all settings flows)
# ──────────────────────────────────────────────────────────────────────────────

@Client.on_message(filters.private)
async def capture_user_input(client, message):
    user_id = message.from_user.id
    active_users = set()
    for key in ("caption_set", "block_words_set", "replace_words_set", "prefix_set", "suffix_set", "url_set"):
        active_users.update(bot_data.get(key, {}).keys())
    active_users.update(FF_SESSIONS.keys())

    if user_id not in active_users:
        return

    text = (
        message.text.html if message.text else
        message.caption.html if message.caption else
        ""
    )
    if not text.strip():
        return

    # ---------- CAPTION ----------
    if user_id in bot_data["caption_set"]:
        session = bot_data["caption_set"].pop(user_id)
        channel_id = session["channel_id"]
        instr_msg_id = session["instr_msg_id"]
        await updateCap(channel_id, text)
        invalidate_channel_cache(channel_id)
        await client.delete_messages(user_id, message.id)
        await client.edit_message_text(
            user_id, instr_msg_id, "✅ Caption updated!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back", callback_data=f"back_to_captionmenu_{channel_id}")]]))
        return

    # ---------- BLOCK WORDS ----------
    if user_id in bot_data["block_words_set"]:
        session = bot_data["block_words_set"].pop(user_id)
        channel_id = session["channel_id"]
        old = await get_block_words(channel_id)
        combined = f"{old.rstrip()}\n{text.strip()}" if old else text.strip()
        await set_block_words(channel_id, combined)
        await client.delete_messages(user_id, message.id)
        await client.edit_message_text(
            user_id, session["instr_msg_id"], "✅ Blocked words updated!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back", callback_data=f"back_to_blockwords_{channel_id}")]]))
        return

    # ---------- REPLACE WORDS ----------
    if user_id in bot_data["replace_words_set"]:
        session = bot_data["replace_words_set"].pop(user_id)
        channel_id = session["channel_id"]
        old = await get_replace_words(channel_id) or ""
        combined = f"{old.rstrip()}\n{text.strip()}" if old else text.strip()
        await set_replace_words(channel_id, combined)
        await client.delete_messages(user_id, message.id)
        await client.edit_message_text(
            user_id, session["instr_msg_id"], "✅ Replace words updated!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back", callback_data=f"back_to_replace_{channel_id}")]]))
        return

    # ---------- PREFIX ----------
    if user_id in bot_data["prefix_set"]:
        session = bot_data["prefix_set"].pop(user_id)
        channel_id = session["channel_id"]
        _, old_prefix = await get_suffix_prefix(channel_id)
        final = f"{old_prefix.rstrip()}\n{text.strip()}" if old_prefix else text.strip()
        await set_prefix(channel_id, final)
        await client.delete_messages(user_id, message.id)
        await client.edit_message_text(
            user_id, session["instr_msg_id"], "✅ Prefix updated!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back", callback_data=f"back_to_suffixprefix_{channel_id}")]]))
        return

    # ---------- SUFFIX ----------
    if user_id in bot_data["suffix_set"]:
        session = bot_data["suffix_set"].pop(user_id)
        channel_id = session["channel_id"]
        old_suffix, _ = await get_suffix_prefix(channel_id)
        final = f"{old_suffix.rstrip()}\n{text.strip()}" if old_suffix else text.strip()
        await set_suffix(channel_id, final)
        await client.delete_messages(user_id, message.id)
        await client.edit_message_text(
            user_id, session["instr_msg_id"], "✅ Suffix updated!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back", callback_data=f"back_to_suffixprefix_{channel_id}")]]))
        return

    # ---------- URL BUTTONS ----------
    if user_id in bot_data.get("url_set", {}):
        session = bot_data["url_set"].pop(user_id)
        channel_id = session["channel_id"]
        rows = []
        for line in text.strip().splitlines():
            row = []
            for part in [p.strip() for p in line.split("|") if p.strip()]:
                match = re.findall(r'"([^"]+)"', part)
                if len(match) == 2:
                    row.append({"text": match[0], "url": match[1]})
            if row:
                rows.append(row)
        if not rows:
            return await message.reply_text("❌ Invalid format. Try again.")
        await set_url_buttons(channel_id, rows)
        await client.delete_messages(user_id, message.id)
        await client.edit_message_text(
            user_id, session["instr_msg_id"], "✅ URL buttons updated!",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back", callback_data=f"seturl_{channel_id}")]]))
        return

    # ---------- FILE FORWARD SKIP ----------
    # ---------- FILE FORWARD SKIP / RANGE ----------
    if user_id in FF_SESSIONS:
        session = FF_SESSIONS[user_id]
        if session.get("expires") and session["expires"] < time.time():
            FF_SESSIONS.pop(user_id, None)
            return await message.reply_text("⏰ Session expired. Start again with /file_forward")

        # ── SKIP mode ──
        if session.get("step") == "skip":
            raw = (message.text or "").strip()
            msg_id = extract_msg_id_from_text(raw)
            if msg_id is None:
                return await message.reply_text("❌ Invalid. Send a Telegram message link or a message ID number.")
            session["skip"] = int(msg_id)
            session["step"] = "queue"
            try:
                await message.delete()
            except:
                pass
            # Always edit the existing bot message — never send a new one
            try:
                await client.edit_message_text(
                    session["chat_id"], session["msg_id"],
                    "🔍 <b>Scanning source channel…</b>\n\nStarting from message <code>{}</code>, please wait.".format(msg_id),
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]])
                )
            except:
                pass
            asyncio.create_task(enqueue_forward_jobs(client, user_id))
            return

        # ── RANGE mode ──
        if session.get("step") == "range_input":
            raw = (message.text or "").strip()
            # Accept: "100 500", "100\n500", or two links separated by space/newline
            parts = re.split(r"[\s\n]+", raw.strip())
            # Filter out empty strings
            parts = [p for p in parts if p]
            id1 = extract_msg_id_from_text(parts[0]) if len(parts) >= 1 else None
            id2 = extract_msg_id_from_text(parts[1]) if len(parts) >= 2 else None
            if id1 is None or id2 is None:
                return await message.reply_text(
                    "❌ Invalid format. Send two message IDs or links:\n"
                    "<code>100 500</code>\nor\n"
                    "<code>https://t.me/c/.../100 https://t.me/c/.../500</code>"
                )
            session["range_start"] = int(id1)
            session["range_end"]   = int(id2)
            session["step"] = "queue"
            try:
                await message.delete()
            except:
                pass
            # Always edit the existing bot message
            try:
                await client.edit_message_text(
                    session["chat_id"], session["msg_id"],
                    f"🔍 <b>Scanning source channel…</b>\n\n"
                    f"📌 Range: <code>{min(id1,id2)}</code> → <code>{max(id1,id2)}</code>\n"
                    f"⏳ Please wait…",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]])
                )
            except:
                pass
            asyncio.create_task(enqueue_forward_jobs(client, user_id))
            return
