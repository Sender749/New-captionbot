import asyncio
import logging
import os
import re
import time
import uuid
from collections import defaultdict

from pyrogram import Client, filters
from pyrogram.errors import FloodWait, MessageNotModified, MessageIdInvalid
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.enums import ParseMode

from body.database import (
    FORWARD_WORKERS,
    MAX_FORWARD_PER_PAIR,
    MAX_GLOBAL_FF_SESSIONS,
    FORWARD_DELAY,
    FF_PROGRESS_INTERVAL,
    enqueue_forward,
    forward_done,
    forward_retry,
    forward_queue,
)
from info import ADMIN, FF_CH

# ── structured logger (goes to stdout → Koyeb log stream) ────────────────────
log = logging.getLogger("FF")

# ── in-memory state ──────────────────────────────────────────────────────────
FORWARD_ACTIVE   = defaultdict(int)   # (src, dst) -> active worker count
FORWARD_COOLDOWN = {}                 # (src, dst) -> unblock timestamp

FF_SESSIONS        = {}               # uid -> session dict
CANCELLED_SESSIONS = set()            # session_ids that were cancelled

# ── Global slot limiter ───────────────────────────────────────────────────────
# Tracks UIDs of sessions that are ACTIVELY scanning/forwarding right now.
# When full, new sessions are placed in _FF_SLOT_QUEUE (FIFO) and auto-start
# when a slot opens up.  This prevents file-forwarding from eating the CPU/RAM
# budget that caption editing needs.
_FF_ACTIVE_UIDS:  set  = set()            # UIDs currently using a slot
_FF_SLOT_QUEUE:   list = []               # [(uid, client)] waiting for a slot
_FF_SLOT_LOCK:    asyncio.Lock | None = None  # created lazily in async context

# ── regex helpers ─────────────────────────────────────────────────────────────
USERNAME_RE = re.compile(r"@\w+",           flags=re.IGNORECASE)
URL_RE      = re.compile(r"(https?://\S+|t\.me/\S+)", flags=re.IGNORECASE)
HTML_TAG_RE = re.compile(r"<[^>]+>")
MD_LINK_RE  = re.compile(r"\[([^\]]+)\]\([^)]+\)")
MSG_LINK_RE = re.compile(
    r"(?:https?://)?t\.me/(?:c/(\d+)|([A-Za-z0-9_]+))/(\d+)",
    flags=re.IGNORECASE,
)

ANIM_FRAMES = [
    "🔄 Transferring files",
    "🔄 Transferring files.",
    "🔄 Transferring files..",
    "🔄 Transferring files...",
]

# Progress UI: update at most once every FF_PROGRESS_INTERVAL seconds per session.
# Keyed by session_id → last update timestamp.
_session_last_progress: dict[str, float] = {}

# Per-session done counter: session_id → forwarded count (in-memory, single-thread safe)
_session_done_count: dict[str, int] = defaultdict(int)

# Sessions whose completion message has already been sent.
_session_completed: set = set()


# ── per-session caption customization defaults ────────────────────────────────
def _default_ff_caption_settings() -> dict:
    """Fresh, empty caption-customization state for one forwarding session.
    `template` of None means "Same Caption" — files are forwarded untouched.
    """
    return {
        "template":      None,
        "block_words":   "",
        "replace_words": "",
        "prefix":        "",
        "suffix":        "",
        "url_buttons":   [],
        "link_remover":  False,
        "emoji_remover": False,
    }


async def _edit_with_retry(client: Client, chat_id, msg_id, text, reply_markup=None, max_retries: int = 5) -> bool:
    """
    Edit a message, retrying on FloodWait or transient errors.
    Used for the completion message so a rate-limit hit can never leave
    the UI stuck on "🔄 Transferring…" after forwarding actually finished.
    """
    for attempt in range(max_retries):
        try:
            await client.edit_message_text(
                chat_id, msg_id, text,
                reply_markup=reply_markup,
                disable_web_page_preview=True,
            )
            return True
        except FloodWait as e:
            wait = int(e.value) + 1
            log.warning("[EDIT_RETRY] FloodWait %ds (attempt %d)", wait, attempt + 1)
            await asyncio.sleep(wait)
        except (MessageNotModified, MessageIdInvalid):
            return True   # idempotent — message is already correct
        except Exception as e:
            log.warning("[EDIT_RETRY] attempt=%d err=%s", attempt + 1, e)
            if attempt == max_retries - 1:
                return False
            await asyncio.sleep(2)
    return False


# ── Global FF slot management ─────────────────────────────────────────────────
def _get_slot_lock() -> asyncio.Lock:
    global _FF_SLOT_LOCK
    if _FF_SLOT_LOCK is None:
        _FF_SLOT_LOCK = asyncio.Lock()
    return _FF_SLOT_LOCK


async def _acquire_ff_slot(uid: int, client: Client) -> bool:
    """
    Try to acquire a forwarding slot for `uid`.
    • If a slot is free  → grants it immediately, returns True.
    • If all slots full  → queues the user, sends a "waiting" message,
                           returns False (caller should NOT start the scan).
    The queued session will be auto-started by _release_ff_slot() when
    another session finishes.
    """
    async with _get_slot_lock():
        if uid in _FF_ACTIVE_UIDS:
            log.info("[FF_SLOT] uid=%d already has an active session — blocked", uid)
            return False
        if len(_FF_ACTIVE_UIDS) < MAX_GLOBAL_FF_SESSIONS:
            _FF_ACTIVE_UIDS.add(uid)
            log.info("[FF_SLOT] uid=%d acquired slot (%d/%d active)",
                     uid, len(_FF_ACTIVE_UIDS), MAX_GLOBAL_FF_SESSIONS)
            return True
        # All slots full — queue the user
        if any(u == uid for u, _ in _FF_SLOT_QUEUE):
            return False  # already queued
        _FF_SLOT_QUEUE.append((uid, client))
        position = len(_FF_SLOT_QUEUE)
        s = FF_SESSIONS.get(uid)
        if s:
            try:
                await client.edit_message_text(
                    s["chat_id"], s["msg_id"],
                    f"⏳ <b>Forwarding queue full</b>\n\n"
                    f"All {MAX_GLOBAL_FF_SESSIONS} forwarding slots are busy.\n"
                    f"Your session is in position <b>#{position}</b> in the queue.\n\n"
                    f"Bot will automatically start your forwarding when a slot opens up. "
                    f"Please don't cancel unless you want to stop.",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]]
                    ),
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
        log.info("[FF_SLOT] uid=%d queued at position %d (%d/%d slots full)",
                 uid, position, len(_FF_ACTIVE_UIDS), MAX_GLOBAL_FF_SESSIONS)
        return False


async def _release_ff_slot(uid: int, client: Client):
    """Release the forwarding slot for `uid` and start the next queued session."""
    async with _get_slot_lock():
        _FF_ACTIVE_UIDS.discard(uid)
        log.info("[FF_SLOT] uid=%d released slot (%d/%d now active)",
                 uid, len(_FF_ACTIVE_UIDS), MAX_GLOBAL_FF_SESSIONS)

        # Promote the next queued user
        while _FF_SLOT_QUEUE and len(_FF_ACTIVE_UIDS) < MAX_GLOBAL_FF_SESSIONS:
            next_uid, next_client = _FF_SLOT_QUEUE.pop(0)
            if next_uid not in FF_SESSIONS:
                log.info("[FF_SLOT] queued uid=%d session expired — skipping", next_uid)
                continue
            if FF_SESSIONS[next_uid].get("session_id") in CANCELLED_SESSIONS:
                log.info("[FF_SLOT] queued uid=%d session cancelled — skipping", next_uid)
                continue
            _FF_ACTIVE_UIDS.add(next_uid)
            log.info("[FF_SLOT] uid=%d promoted from queue — starting scan", next_uid)
            asyncio.create_task(
                _scan_and_enqueue(next_client, next_uid),
                name=f"scan_{next_uid}_{int(time.time())}",
            )
            break


# ── startup hook ──────────────────────────────────────────────────────────────
def on_bot_start(client: Client):
    """Configure logging and launch the fixed pool of forward workers."""
    # Configure root logger so all [FF] messages appear in Koyeb log stream
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    for i in range(FORWARD_WORKERS):
        asyncio.create_task(forward_worker(client), name=f"ff_worker_{i}")
    log.info(
        "[BOOT] %d forward worker(s) started | slots=%d | delay=%.1fs | max_pair=%d",
        FORWARD_WORKERS, MAX_GLOBAL_FF_SESSIONS, FORWARD_DELAY, MAX_FORWARD_PER_PAIR,
    )


# ── text utilities ────────────────────────────────────────────────────────────
def clean_text(text: str) -> str:
    if not text:
        return ""
    text = MD_LINK_RE.sub(r"\1", text)
    text = HTML_TAG_RE.sub("", text)
    text = URL_RE.sub("", text)
    text = USERNAME_RE.sub("", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ── input parsing ─────────────────────────────────────────────────────────────
def _parse_single(text: str):
    text = text.strip()
    m = MSG_LINK_RE.search(text)
    if m:
        numeric_cid = m.group(1)
        msg_id = int(m.group(3))
        return (int(f"-100{numeric_cid}") if numeric_cid else None), msg_id
    if text.isdigit():
        return None, int(text)
    return None, None


def parse_forward_input(raw: str):
    parts = re.split(r"\s*-\s*(?=\S)", raw, maxsplit=1)
    if len(parts) == 2:
        src_hint1, start_id = _parse_single(parts[0])
        src_hint2, end_id   = _parse_single(parts[1])
        if start_id is None or end_id is None:
            return {"error": "❌ Could not parse start or end message reference."}
        if start_id > end_id:
            return {"error": "❌ Start message ID must be less than end message ID."}
        return {
            "skip_id":  start_id - 1,
            "end_id":   end_id,
            "src_hint": src_hint1 or src_hint2,
            "error":    None,
        }
    else:
        if raw.strip() == "0":
            return {"skip_id": 0, "end_id": None, "src_hint": None, "error": None}
        src_hint, msg_id = _parse_single(raw.strip())
        if msg_id is None:
            return {
                "error": (
                    "❌ Invalid message link or ID.\n\n"
                    "Send a Telegram message link, a message ID, or 0 to forward all."
                )
            }
        return {"skip_id": msg_id, "end_id": None, "src_hint": src_hint, "error": None}


async def validate_msg_in_channel(client: Client, channel_id: int, msg_id: int) -> bool:
    try:
        msg = await client.get_messages(channel_id, msg_id)
        return msg is not None and not getattr(msg, "empty", True)
    except Exception:
        return False


# ── callback: source selection ────────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^ff_src_(-?\d+)$"))
async def ff_src(client, query):
    uid = query.from_user.id

    # Prevent a user from starting a new session while one is already active
    if uid in _FF_ACTIVE_UIDS:
        await query.answer(
            "⚠️ You already have an active forwarding session! "
            "Cancel it first with ❌ Cancel before starting a new one.",
            show_alert=True,
        )
        log.warning("[FF_SRC] uid=%d tried to start duplicate session — blocked", uid)
        return

    s = FF_SESSIONS.get(uid)
    if not s:
        return
    src = int(query.matches[0].group(1))
    s["source"]       = src
    s["source_title"] = next(x["channel_title"] for x in s["channels"] if x["channel_id"] == src)
    s["channels"]     = [x for x in s["channels"] if x["channel_id"] != src]
    s["step"]         = "dst"
    log.info("[FF_SRC] uid=%d selected source=%d (%s)", uid, src, s["source_title"])
    kb = [
        [InlineKeyboardButton(x["channel_title"], callback_data=f"ff_dst_{x['channel_id']}")]
        for x in s["channels"]
    ]
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")])
    await query.message.edit_text(
        "📥 <b>Select DESTINATION channel</b>",
        reply_markup=InlineKeyboardMarkup(kb),
    )


# ── callback: destination selection ──────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^ff_dst_(-?\d+)$"))
async def ff_dst(client, query):
    uid = query.from_user.id
    s = FF_SESSIONS.get(uid)
    if not s:
        return
    dst = int(query.matches[0].group(1))
    s["destination"]       = dst
    s["destination_title"] = next(
        x["channel_title"] for x in s["channels"] if x["channel_id"] == dst
    )
    s["step"]    = "cap_menu"
    s["chat_id"] = query.message.chat.id
    s["msg_id"]  = query.message.id
    s["expires"] = time.time() + 900  # 15 minutes
    s["caption_settings"] = _default_ff_caption_settings()
    s.pop("pending_input", None)
    await _render_ff_cap_panel(client, s["chat_id"], s["msg_id"])


async def _show_ff_range_prompt(client: Client, chat_id, msg_id):
    await client.edit_message_text(
        chat_id,
        msg_id,
        "⏭ <b>Enter forwarding range</b>\n\n"
        "<b>Options:</b>\n"
        "• <code>0</code> — forward ALL files\n"
        "• <code>msg_link</code> or <code>id</code> — start AFTER this message\n"
        "• <code>start - end</code> — forward BETWEEN two messages (inclusive)\n\n"
        "<b>Examples:</b>\n"
        "<code>0</code>\n"
        "<code>https://t.me/c/1815162626/100</code>\n"
        "<code>100 - 500</code>\n"
        "<code>https://t.me/c/1234/100 - https://t.me/c/1234/500</code>\n\n"
        "• Session expires in <b>15 minutes</b>",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅ Back", callback_data="ffc_menu")],
             [InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]]
        ),
    )


# ══════════════════════════════════════════════════════════════════════════
#  Per-session forwarding caption-customization panel
#  (mirrors the /settings autocaption panel, but state lives only on
#   FF_SESSIONS[uid]["caption_settings"] — never written to the DB)
# ══════════════════════════════════════════════════════════════════════════
async def _render_ff_cap_panel(client: Client, chat_id, msg_id):
    uid = None
    for u, sess in FF_SESSIONS.items():
        if sess.get("chat_id") == chat_id and sess.get("msg_id") == msg_id:
            uid = u
            break
    if uid is None:
        return
    s  = FF_SESSIONS[uid]
    cs = s.setdefault("caption_settings", _default_ff_caption_settings())

    template = cs.get("template")
    cap_preview = template if template else "♻️ <i>Same Caption — original caption kept as is</i>"
    link_text  = "Link & Username Remover (ON)"  if cs.get("link_remover")  else "Link & Username Remover (OFF)"
    emoji_text = "Emoji Remover (ON)"            if cs.get("emoji_remover") else "Emoji Remover (OFF)"

    text = (
        "🎨 <b>Customize Forwarding Caption</b>\n\n"
        f"📤 <b>{s['source_title']}</b>\n"
        f"         ⬇️⬇️⬇️\n"
        f"📥 <b>{s['destination_title']}</b>\n\n"
        f"📝 <b>Caption:</b>\n{cap_preview}\n\n"
        "<i>These settings apply only to this forwarding session — "
        "you'll need to set them again next time.</i>"
    )
    kb = [
        [InlineKeyboardButton("📝 Set Caption",            callback_data="ffc_setcap")],
        [InlineKeyboardButton("🧹 Set Words Remover",      callback_data="ffc_words")],
        [InlineKeyboardButton("🔤 Set Prefix & Suffix",    callback_data="ffc_suffixprefix")],
        [InlineKeyboardButton("🔄 Set Replace Words",      callback_data="ffc_replace")],
        [InlineKeyboardButton("🔘 Button URL",             callback_data="ffc_url")],
        [InlineKeyboardButton(f"🔗 {link_text}",          callback_data="ffc_togglelink")],
        [InlineKeyboardButton(f"😀 {emoji_text}",         callback_data="ffc_toggleemoji")],
        [InlineKeyboardButton("♻️ Same Caption",           callback_data="ffc_samecap")],
        [InlineKeyboardButton("➡️ Continue", callback_data="ffc_continue"),
         InlineKeyboardButton("❌ Cancel",   callback_data="ff_cancel")],
    ]
    try:
        await client.edit_message_text(
            chat_id, msg_id, text,
            reply_markup=InlineKeyboardMarkup(kb),
            disable_web_page_preview=True,
        )
    except Exception:
        pass


async def _render_ff_capsub(client: Client, chat_id, msg_id, cs: dict):
    template = cs.get("template")
    disp = f"📝 <b>Current Caption:</b>\n{template}" if template else "📝 <b>Current Caption:</b> None set (Same Caption active)."
    kb = [
        [InlineKeyboardButton("🆕 Set Caption",   callback_data="ffc_setcapmsg"),
         InlineKeyboardButton("❌ Delete Caption", callback_data="ffc_delcap")],
        [InlineKeyboardButton("↩ Back", callback_data="ffc_menu")],
    ]
    await client.edit_message_text(
        chat_id, msg_id,
        f"⚙️ <b>Forwarding Caption</b>\n{disp}\n\nChoose what you want to do 👇",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def _render_ff_words_menu(client: Client, chat_id, msg_id, cs: dict):
    blocked = cs.get("block_words", "")
    words_text = (
        "\n".join(f"• {w.strip()}" for w in re.split(r"[,\n]+", blocked) if w.strip())
        if blocked else "None set yet."
    )
    kb = [
        [InlineKeyboardButton("📝 Set Block Words",    callback_data="ffc_addwords"),
         InlineKeyboardButton("🗑️ Delete Block Words", callback_data="ffc_delwords")],
        [InlineKeyboardButton("↩ Back", callback_data="ffc_menu")],
    ]
    await client.edit_message_text(
        chat_id, msg_id,
        f"🚫 <b>Blocked Words:</b>\n{words_text}\n\nChoose what you want to do 👇",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def _render_ff_replace_menu(client: Client, chat_id, msg_id, cs: dict):
    replace_raw = cs.get("replace_words", "")
    replace_text = (
        "\n".join(line.strip() for line in replace_raw.splitlines() if line.strip())
        if replace_raw else "None set yet."
    )
    kb = [
        [InlineKeyboardButton("📝 Set Replace Words",    callback_data="ffc_addreplace"),
         InlineKeyboardButton("🗑️ Delete Replace Words", callback_data="ffc_delreplace")],
        [InlineKeyboardButton("↩ Back", callback_data="ffc_menu")],
    ]
    await client.edit_message_text(
        chat_id, msg_id,
        f"🔤 <b>Replace Words:</b>\n{replace_text}\n\nChoose what you want to do 👇",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def _render_ff_suffixprefix_menu(client: Client, chat_id, msg_id, cs: dict):
    kb = [
        [InlineKeyboardButton("Set Suffix", callback_data="ffc_setsuf"),
         InlineKeyboardButton("Del Suffix", callback_data="ffc_delsuf")],
        [InlineKeyboardButton("Set Prefix", callback_data="ffc_setpre"),
         InlineKeyboardButton("Del Prefix", callback_data="ffc_delpre")],
        [InlineKeyboardButton("↩ Back", callback_data="ffc_menu")],
    ]
    await client.edit_message_text(
        chat_id, msg_id,
        f"📌 Current Suffix: {cs.get('suffix') or 'None'}\n"
        f"📌 Current Prefix: {cs.get('prefix') or 'None'}",
        reply_markup=InlineKeyboardMarkup(kb),
    )


async def _render_ff_url_menu(client: Client, chat_id, msg_id, cs: dict):
    has_urls = bool(cs.get("url_buttons"))
    kb = [
        [InlineKeyboardButton("➕ Set URL",   callback_data="ffc_seturlmsg"),
         InlineKeyboardButton("🗑 Delete URL", callback_data="ffc_delurl")],
        [InlineKeyboardButton("↩ Back", callback_data="ffc_menu")],
    ]
    await client.edit_message_text(
        chat_id, msg_id,
        f"🔘 <b>Button URLs:</b> {'Configured' if has_urls else 'None set yet.'}",
        reply_markup=InlineKeyboardMarkup(kb),
    )


def _ff_session_for(uid: int):
    s = FF_SESSIONS.get(uid)
    if not s:
        return None
    return s, s.setdefault("caption_settings", _default_ff_caption_settings())


@Client.on_callback_query(filters.regex(r"^ffc_menu$"))
async def ffc_menu(client, query):
    await query.answer()
    uid = query.from_user.id
    if uid not in FF_SESSIONS:
        return
    FF_SESSIONS[uid].pop("pending_input", None)
    FF_SESSIONS[uid]["step"] = "cap_menu"
    await _render_ff_cap_panel(client, query.message.chat.id, query.message.id)


@Client.on_callback_query(filters.regex(r"^ffc_setcap$"))
async def ffc_setcap(client, query):
    await query.answer()
    pair = _ff_session_for(query.from_user.id)
    if not pair:
        return
    _, cs = pair
    await _render_ff_capsub(client, query.message.chat.id, query.message.id, cs)


@Client.on_callback_query(filters.regex(r"^ffc_setcapmsg$"))
async def ffc_setcapmsg(client, query):
    await query.answer()
    uid = query.from_user.id
    pair = _ff_session_for(uid)
    if not pair:
        return
    s, _ = pair
    s["pending_input"] = "caption"
    await client.edit_message_text(
        query.message.chat.id, query.message.id,
        "📌 <b>Send the caption for this forwarding session</b>\n\n"
        "<blockquote expandable>"
        "📦 <b>Placeholders</b>\n\n"
        "File name ⇛ <code>{file_name}</code>\n"
        "File size ⇛ <code>{file_size}</code>\n"
        "Original caption ⇛ <code>{default_caption}</code>\n"
        "Smart file name ⇛ <code>{smart_file_name}</code>\n"
        "Title ⇛ <code>{title}</code>  Year ⇛ <code>{year}</code>\n"
        "Season ⇛ <code>{season}</code>  Episode ⇛ <code>{episode}</code>\n"
        "Audio ⇛ <code>{audio}</code>  Subtitle ⇛ <code>{subtitle}</code>\n"
        "Quality ⇛ <code>{quality}</code>  Source ⇛ <code>{source}</code>\n"
        "Video codec ⇛ <code>{vcodec}</code>  Audio codec ⇛ <code>{acodec}</code>"
        "</blockquote>\n\n"
        "✍️ <b>Example:</b>\n"
        "<code>&lt;b&gt;{title}&lt;/b&gt; {season}{episode} ({year})\n"
        "{audio} | {quality} | {subtitle}\n"
        "💾 {file_size}</code>",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back", callback_data="ffc_setcap")]]),
    )


@Client.on_callback_query(filters.regex(r"^ffc_delcap$"))
async def ffc_delcap(client, query):
    await query.answer("Caption cleared — Same Caption is now active.")
    pair = _ff_session_for(query.from_user.id)
    if not pair:
        return
    _, cs = pair
    cs["template"] = None
    await _render_ff_capsub(client, query.message.chat.id, query.message.id, cs)


@Client.on_callback_query(filters.regex(r"^ffc_words$"))
async def ffc_words(client, query):
    await query.answer()
    pair = _ff_session_for(query.from_user.id)
    if not pair:
        return
    _, cs = pair
    await _render_ff_words_menu(client, query.message.chat.id, query.message.id, cs)


@Client.on_callback_query(filters.regex(r"^ffc_addwords$"))
async def ffc_addwords(client, query):
    await query.answer()
    pair = _ff_session_for(query.from_user.id)
    if not pair:
        return
    s, _ = pair
    s["pending_input"] = "block_words"
    await client.edit_message_text(
        query.message.chat.id, query.message.id,
        "📝 <b>Send words to block</b> (comma or newline separated).\n"
        "These words will be stripped from the built caption.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back", callback_data="ffc_words")]]),
    )


@Client.on_callback_query(filters.regex(r"^ffc_delwords$"))
async def ffc_delwords(client, query):
    await query.answer("Blocked words cleared.")
    pair = _ff_session_for(query.from_user.id)
    if not pair:
        return
    _, cs = pair
    cs["block_words"] = ""
    await _render_ff_words_menu(client, query.message.chat.id, query.message.id, cs)


@Client.on_callback_query(filters.regex(r"^ffc_replace$"))
async def ffc_replace(client, query):
    await query.answer()
    pair = _ff_session_for(query.from_user.id)
    if not pair:
        return
    _, cs = pair
    await _render_ff_replace_menu(client, query.message.chat.id, query.message.id, cs)


@Client.on_callback_query(filters.regex(r"^ffc_addreplace$"))
async def ffc_addreplace(client, query):
    await query.answer()
    pair = _ff_session_for(query.from_user.id)
    if not pair:
        return
    s, _ = pair
    s["pending_input"] = "replace_words"
    await client.edit_message_text(
        query.message.chat.id, query.message.id,
        "🔄 <b>Send replace pairs</b>, one per line, e.g.\n"
        "<code>old text = new text</code>",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back", callback_data="ffc_replace")]]),
    )


@Client.on_callback_query(filters.regex(r"^ffc_delreplace$"))
async def ffc_delreplace(client, query):
    await query.answer("Replace words cleared.")
    pair = _ff_session_for(query.from_user.id)
    if not pair:
        return
    _, cs = pair
    cs["replace_words"] = ""
    await _render_ff_replace_menu(client, query.message.chat.id, query.message.id, cs)


@Client.on_callback_query(filters.regex(r"^ffc_suffixprefix$"))
async def ffc_suffixprefix(client, query):
    await query.answer()
    pair = _ff_session_for(query.from_user.id)
    if not pair:
        return
    _, cs = pair
    await _render_ff_suffixprefix_menu(client, query.message.chat.id, query.message.id, cs)


@Client.on_callback_query(filters.regex(r"^ffc_setpre$"))
async def ffc_setpre(client, query):
    await query.answer()
    pair = _ff_session_for(query.from_user.id)
    if not pair:
        return
    s, _ = pair
    s["pending_input"] = "prefix"
    await client.edit_message_text(
        query.message.chat.id, query.message.id, "🔤 <b>Send the prefix text.</b>",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back", callback_data="ffc_suffixprefix")]]),
    )


@Client.on_callback_query(filters.regex(r"^ffc_setsuf$"))
async def ffc_setsuf(client, query):
    await query.answer()
    pair = _ff_session_for(query.from_user.id)
    if not pair:
        return
    s, _ = pair
    s["pending_input"] = "suffix"
    await client.edit_message_text(
        query.message.chat.id, query.message.id, "🔤 <b>Send the suffix text.</b>",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back", callback_data="ffc_suffixprefix")]]),
    )


@Client.on_callback_query(filters.regex(r"^ffc_delpre$"))
async def ffc_delpre(client, query):
    await query.answer("Prefix cleared.")
    pair = _ff_session_for(query.from_user.id)
    if not pair:
        return
    _, cs = pair
    cs["prefix"] = ""
    await _render_ff_suffixprefix_menu(client, query.message.chat.id, query.message.id, cs)


@Client.on_callback_query(filters.regex(r"^ffc_delsuf$"))
async def ffc_delsuf(client, query):
    await query.answer("Suffix cleared.")
    pair = _ff_session_for(query.from_user.id)
    if not pair:
        return
    _, cs = pair
    cs["suffix"] = ""
    await _render_ff_suffixprefix_menu(client, query.message.chat.id, query.message.id, cs)


@Client.on_callback_query(filters.regex(r"^ffc_url$"))
async def ffc_url(client, query):
    await query.answer()
    pair = _ff_session_for(query.from_user.id)
    if not pair:
        return
    _, cs = pair
    await _render_ff_url_menu(client, query.message.chat.id, query.message.id, cs)


@Client.on_callback_query(filters.regex(r"^ffc_seturlmsg$"))
async def ffc_seturlmsg(client, query):
    await query.answer()
    pair = _ff_session_for(query.from_user.id)
    if not pair:
        return
    s, _ = pair
    s["pending_input"] = "url_buttons"
    await client.edit_message_text(
        query.message.chat.id, query.message.id,
        "🔘 <b>Send button rows</b>, one row per line, e.g.\n"
        '<code>"Button 1" "https://example.com"</code>',
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back", callback_data="ffc_url")]]),
    )


@Client.on_callback_query(filters.regex(r"^ffc_delurl$"))
async def ffc_delurl(client, query):
    await query.answer("Button URLs cleared.")
    pair = _ff_session_for(query.from_user.id)
    if not pair:
        return
    _, cs = pair
    cs["url_buttons"] = []
    await _render_ff_url_menu(client, query.message.chat.id, query.message.id, cs)


@Client.on_callback_query(filters.regex(r"^ffc_togglelink$"))
async def ffc_togglelink(client, query):
    pair = _ff_session_for(query.from_user.id)
    if not pair:
        return
    s, cs = pair
    cs["link_remover"] = not cs.get("link_remover", False)
    await query.answer("Link & Username Remover " + ("enabled" if cs["link_remover"] else "disabled"))
    await _render_ff_cap_panel(client, query.message.chat.id, query.message.id)


@Client.on_callback_query(filters.regex(r"^ffc_toggleemoji$"))
async def ffc_toggleemoji(client, query):
    pair = _ff_session_for(query.from_user.id)
    if not pair:
        return
    s, cs = pair
    cs["emoji_remover"] = not cs.get("emoji_remover", False)
    await query.answer("Emoji Remover " + ("enabled" if cs["emoji_remover"] else "disabled"))
    await _render_ff_cap_panel(client, query.message.chat.id, query.message.id)


@Client.on_callback_query(filters.regex(r"^ffc_samecap$"))
async def ffc_samecap(client, query):
    """'Same Caption' — the only button that doesn't open a sub-menu.
    Clearing the template means the file caption is left untouched on
    forward, exactly like the bot behaved before this feature existed.
    """
    pair = _ff_session_for(query.from_user.id)
    if not pair:
        return
    s, cs = pair
    cs["template"] = None
    await query.answer("✅ Same Caption selected — captions will not be changed.", show_alert=True)
    await _render_ff_cap_panel(client, query.message.chat.id, query.message.id)


@Client.on_callback_query(filters.regex(r"^ffc_continue$"))
async def ffc_continue(client, query):
    await query.answer()
    uid = query.from_user.id
    s = FF_SESSIONS.get(uid)
    if not s:
        return
    s.pop("pending_input", None)
    s["step"]    = "skip"
    s["expires"] = time.time() + 900
    await _show_ff_range_prompt(client, query.message.chat.id, query.message.id)


# ── scan & enqueue (background task, one per user session) ───────────────────
async def _scan_and_enqueue(client: Client, uid: int):
    """
    Scans the source channel and writes one DB job per media message.
    Runs in the background – never blocks caption workers.
    Releases the global FF slot when done (success, cancel, or error).
    """
    s = FF_SESSIONS.get(uid)
    if not s:
        await _release_ff_slot(uid, client)
        return
    session_id = s["session_id"]
    src        = s["source"]
    dst        = s["destination"]
    start_id   = int(s["skip"]) + 1
    end_id     = s.get("end_id")
    caption_settings_snapshot = dict(s.get("caption_settings") or _default_ff_caption_settings())

    # Reset counters for this scan
    s["total"]     = 0
    s["forwarded"] = 0
    s["scan_done"] = False
    msg_id              = start_id
    consecutive_missing = 0
    MAX_CONSECUTIVE_MISSING = 500

    log.info(
        "[SCAN] uid=%d session=%s src=%d dst=%d start=%d end=%s",
        uid, session_id[:8], src, dst, start_id, end_id,
    )
    t_scan_start = time.time()

    try:
        while True:
            if end_id is not None and msg_id > end_id:
                break
            await asyncio.sleep(0)  # yield so caption workers aren't starved

            if session_id in CANCELLED_SESSIONS:
                log.info("[SCAN] uid=%d session=%s cancelled during scan", uid, session_id[:8])
                return

            try:
                msg = await client.get_messages(src, msg_id)
            except FloodWait as e:
                wait = int(e.value) + 2
                log.warning("[SCAN] FloodWait %ds uid=%d msg=%d", wait, uid, msg_id)
                await asyncio.sleep(wait)
                continue
            except Exception as e:
                log.warning("[SCAN] get_messages error uid=%d msg=%d: %s", uid, msg_id, e)
                msg = None

            if not msg or getattr(msg, "empty", True):
                consecutive_missing += 1
                if consecutive_missing >= MAX_CONSECUTIVE_MISSING:
                    log.info("[SCAN] uid=%d hit %d consecutive missing — stopping scan", uid, MAX_CONSECUTIVE_MISSING)
                    break
                msg_id += 1
                continue

            consecutive_missing = 0
            if not msg.media:
                msg_id += 1
                continue

            await enqueue_forward({
                "user_id":           uid,
                "src":               src,
                "dst":               dst,
                "msg_id":            msg.id,
                "chat_id":           s["chat_id"],
                "ui_msg":            s["msg_id"],
                "source_title":      s["source_title"],
                "destination_title": s["destination_title"],
                "session_id":        session_id,
                "total":             0,
                "caption_settings":  caption_settings_snapshot,
            })
            s["total"] += 1
            msg_id += 1

        # ── Stamp total on all pending jobs ───────────────────────────────────
        if s["total"] > 0:
            await forward_queue.update_many(
                {"session_id": session_id, "total": 0},
                {"$set": {"total": s["total"]}},
            )

        scan_elapsed = time.time() - t_scan_start
        log.info(
            "[SCAN] uid=%d session=%s done: %d file(s) queued in %.1fs",
            uid, session_id[:8], s["total"], scan_elapsed,
        )

        if s["total"] == 0:
            log.info("[SCAN] uid=%d no media found — releasing slot", uid)
            try:
                await client.edit_message_text(
                    s["chat_id"], s["msg_id"],
                    "⚠️ <b>No media files found</b> in the specified range.\n\n"
                    "Please check the source channel and range, then try again.",
                )
            except Exception:
                pass
            FF_SESSIONS.pop(uid, None)
            return  # slot released in finally

        if session_id not in CANCELLED_SESSIONS:
            try:
                await client.edit_message_text(
                    s["chat_id"], s["msg_id"],
                    f"📤 <b>{s['source_title']}</b>\n"
                    f"         ⬇️⬇️⬇️\n"
                    f"📥 <b>{s['destination_title']}</b>\n\n"
                    f"📦 Found <b>{s['total']}</b> file(s) — forwarding started…",
                    reply_markup=InlineKeyboardMarkup(
                        [[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]]
                    ),
                    disable_web_page_preview=True,
                )
            except Exception:
                pass

        # Mark scan complete so workers know the total is final
        s["scan_done"] = True

    except Exception as e:
        log.error("[SCAN] uid=%d unexpected error: %s", uid, e, exc_info=True)
        s["scan_done"] = True
    finally:
        # NOTE: we do NOT release the slot here — the slot is held until the
        # last forward job completes (see _maybe_update_progress completion block).
        # We only release early if no files were found (handled above) or on error
        # with nothing queued.
        if s.get("total", 0) == 0:
            await _release_ff_slot(uid, client)


async def enqueue_forward_jobs(client: Client, uid: int):
    """
    Called from the message handler after the user enters the range.
    Tries to acquire a global FF slot:
    • If a slot is free  → shows 'Scanning…' and starts the scan task immediately.
    • If all slots full  → places user in queue; _acquire_ff_slot() already
                           updates the message with a "waiting" notice.
    """
    s = FF_SESSIONS.get(uid)
    if not s:
        return
    if "session_id" not in s:
        s["session_id"] = str(uuid.uuid4())

    # Reset per-session counters
    _session_done_count.pop(s["session_id"], None)
    _session_last_progress.pop(s["session_id"], None)
    _session_completed.discard(s["session_id"])

    log.info(
        "[FF_ENQUEUE] uid=%d session=%s src=%d dst=%d skip=%s end=%s",
        uid, s["session_id"][:8],
        s.get("source", 0), s.get("destination", 0),
        s.get("skip"), s.get("end_id"),
    )

    # Try to get a slot — if full, user is queued and informed automatically
    got_slot = await _acquire_ff_slot(uid, client)
    if not got_slot:
        return  # queued or already active

    # Got a slot — show scanning status and start
    try:
        await client.edit_message_text(
            s["chat_id"], s["msg_id"],
            f"📤 <b>{s['source_title']}</b>\n"
            f"         ⬇️⬇️⬇️\n"
            f"📥 <b>{s['destination_title']}</b>\n\n"
            "🔍 Scanning files…",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]]
            ),
            disable_web_page_preview=True,
        )
    except Exception:
        pass

    asyncio.create_task(
        _scan_and_enqueue(client, uid),
        name=f"scan_{uid}_{s['session_id'][:8]}",
    )


# ── fair-pick from forward queue (atomic claim) ───────────────────────────────
async def _fetch_forward_job():
    now    = time.time()
    cursor = forward_queue.find({"status": "pending"}).sort("ts", 1)
    async for job in cursor:
        key = (job["src"], job["dst"])
        if FORWARD_COOLDOWN.get(key, 0) > now:
            continue
        if FORWARD_ACTIVE[key] >= MAX_FORWARD_PER_PAIR:
            continue
        FORWARD_ACTIVE[key] += 1
        updated = await forward_queue.find_one_and_update(
            {"_id": job["_id"], "status": "pending"},
            {"$set": {"status": "processing", "started": now}},
        )
        if updated is None:
            # Race condition – another worker grabbed it
            FORWARD_ACTIVE[key] = max(0, FORWARD_ACTIVE[key] - 1)
            continue
        return job
    return None


# ── caption builder for custom forwarding captions ────────────────────────────
async def build_ff_caption(msg, cs: dict):
    """
    Build a (caption, reply_markup) pair for a forwarded message using the
    per-session caption_settings snapshot `cs`.

    Returns (None, None) when `cs["template"]` is empty/None — this is the
    "Same Caption" case, meaning the caller should leave the original
    caption completely untouched.

    Reuses the same building blocks as the autocaption pipeline in
    body/Caption.py. Imported lazily (inside the function) to avoid a
    circular import, since Caption.py does `from body.file_forward import *`
    at module load time.
    """
    template = (cs or {}).get("template")
    if not template:
        return None, None

    from body.Caption import (
        parse_file_info, build_smart_filename, apply_block_words,
        apply_replacements, parse_replace_pairs, strip_links_only,
        remove_emojis, sanitize_caption_html, extract_audio_languages,
        extract_year, normalize_series_name, get_size,
    )

    default_caption = msg.caption or ""
    original_file_name = ""
    file_size = get_size(0)
    file_name = "File"
    for t in ("video", "audio", "document", "voice"):
        obj = getattr(msg, t, None)
        if obj:
            original_file_name = getattr(obj, "file_name", None) or ""
            raw_name = original_file_name or ("Voice Message" if t == "voice" else "File")
            file_name = raw_name.replace("_", " ").replace(".", " ")
            file_size = get_size(getattr(obj, "file_size", 0))
            break

    combined_raw = f"{original_file_name} {default_caption}"
    audio_lang_list = extract_audio_languages(combined_raw)
    language = " + ".join(audio_lang_list) if audio_lang_list else ""
    year = extract_year(default_caption) or extract_year(original_file_name) or ""

    try:
        raw_file_name = normalize_series_name(file_name)
        file_info = parse_file_info(original_file_name or raw_file_name, default_caption)
        smart_file_name = ""
        if "{smart_file_name}" in template:
            smart_file_name = build_smart_filename(original_file_name or raw_file_name, default_caption)
        new_caption = template.format(
            file_name=raw_file_name,
            smart_file_name=smart_file_name,
            file_size=file_size,
            default_caption=default_caption,
            language=language or file_info.get("audio", ""),
            year=year or file_info.get("year", ""),
            title=file_info.get("title", ""),
            season=file_info.get("season", ""),
            episode=file_info.get("episode", ""),
            audio=file_info.get("audio", ""),
            subtitle=file_info.get("subtitle", ""),
            quality=file_info.get("quality", ""),
            resolution=file_info.get("resolution", ""),
            source=file_info.get("source", ""),
            vcodec=file_info.get("vcodec", ""),
            acodec=file_info.get("acodec", ""),
            extension=file_info.get("extension", ""),
            duration="",
            empty="",
        )
    except Exception:
        new_caption = template

    blocked = cs.get("block_words") or ""
    if blocked:
        new_caption = apply_block_words(new_caption, blocked)

    replace_raw = cs.get("replace_words") or ""
    if replace_raw:
        pairs = parse_replace_pairs(replace_raw)
        if pairs:
            new_caption = apply_replacements(new_caption, pairs)

    if cs.get("link_remover"):
        new_caption = strip_links_only(new_caption)

    prefix = cs.get("prefix") or ""
    if prefix:
        new_caption = f"{prefix}\n{new_caption}".strip()

    suffix = cs.get("suffix") or ""
    if suffix:
        new_caption = f"{new_caption}\n{suffix}".strip()

    if cs.get("emoji_remover"):
        new_caption = remove_emojis(new_caption)

    new_caption = new_caption.strip()
    if "<" in new_caption and ">" in new_caption:
        new_caption = sanitize_caption_html(new_caption)

    reply_markup = None
    url_buttons = cs.get("url_buttons") or []
    if url_buttons:
        reply_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(b["text"], url=b["url"]) for b in row]
            for row in url_buttons
        ])
    return new_caption, reply_markup


# ── media send helpers ────────────────────────────────────────────────────────
async def _forward_with_thumb(
    client: Client, src: int, dst: int, msg,
    custom_caption: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """
    Re-sends the media preserving its thumbnail.
    Falls back to copy_message when no special handling is needed.

    custom_caption=None means "Same Caption" — the original caption/entities
    are forwarded untouched (the pre-existing behaviour). When a custom
    caption string is supplied (built via build_ff_caption), it overrides
    the original caption and is parsed as HTML.
    """
    thumb_path = None
    try:
        media_type = None
        media_obj  = None
        for t in ("video", "document", "animation"):
            obj = getattr(msg, t, None)
            if obj:
                media_type = t
                media_obj  = obj
                break

        use_custom = custom_caption is not None
        caption    = custom_caption if use_custom else (msg.caption or "")
        parse_mode = ParseMode.HTML if use_custom else None
        has_thumb  = bool(media_obj and getattr(media_obj, "thumbs", None))

        if media_type == "video" and has_thumb:
            thumb_path = await client.download_media(
                media_obj.thumbs[0].file_id,
                file_name=f"/tmp/thumb_ff_{msg.id}.jpg",
            )
            await client.send_video(
                chat_id=dst,
                video=media_obj.file_id,
                caption=caption,
                thumb=thumb_path,
                duration=getattr(media_obj, "duration", 0),
                width=getattr(media_obj, "width", 0),
                height=getattr(media_obj, "height", 0),
                supports_streaming=True,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        elif media_type == "animation" and has_thumb:
            thumb_path = await client.download_media(
                media_obj.thumbs[0].file_id,
                file_name=f"/tmp/thumb_ff_{msg.id}.jpg",
            )
            await client.send_animation(
                chat_id=dst,
                animation=media_obj.file_id,
                caption=caption,
                thumb=thumb_path,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        elif media_type == "document" and has_thumb:
            thumb_path = await client.download_media(
                media_obj.thumbs[0].file_id,
                file_name=f"/tmp/thumb_ff_{msg.id}.jpg",
            )
            await client.send_document(
                chat_id=dst,
                document=media_obj.file_id,
                caption=caption,
                thumb=thumb_path,
                parse_mode=parse_mode,
                reply_markup=reply_markup,
            )
        else:
            if use_custom or reply_markup:
                # Need to override caption and/or attach buttons
                await client.copy_message(
                    chat_id=dst,
                    from_chat_id=src,
                    message_id=msg.id,
                    caption=caption,
                    parse_mode=parse_mode,
                    reply_markup=reply_markup,
                )
            else:
                # Fast path – Same Caption, no thumb needed, no buttons
                await client.copy_message(
                    chat_id=dst,
                    from_chat_id=src,
                    message_id=msg.id,
                )
    finally:
        if thumb_path:
            try:
                os.remove(thumb_path)
            except Exception:
                pass


# ── forward worker ────────────────────────────────────────────────────────────
async def forward_worker(client: Client):
    """
    Single long-running worker. Pulls one DB job at a time, sends the file,
    handles FloodWait with exponential back-off, then loops.
    Only FORWARD_WORKERS of these run concurrently.
    """
    worker_name = asyncio.current_task().get_name() if asyncio.current_task() else "ff_worker"
    log.info("[WORKER] %s started", worker_name)

    while True:
        job = await _fetch_forward_job()
        if not job:
            await asyncio.sleep(1)
            continue

        key        = (job["src"], job["dst"])
        session_id = job.get("session_id")
        msg_id     = job.get("msg_id")
        uid        = job.get("user_id")

        try:
            if session_id in CANCELLED_SESSIONS:
                log.info("[WORKER] %s skipping cancelled job session=%s msg=%d",
                         worker_name, str(session_id)[:8], msg_id)
                await forward_done(job["_id"])
                continue

            msg = await client.get_messages(job["src"], msg_id)

            custom_caption, ff_reply_markup = None, None
            cs = job.get("caption_settings")
            if cs and cs.get("template"):
                try:
                    custom_caption, ff_reply_markup = await build_ff_caption(msg, cs)
                except Exception as e:
                    log.warning("[WORKER] caption build fail session=%s msg=%d: %s",
                                str(session_id)[:8], msg_id, e)
                    custom_caption, ff_reply_markup = None, None

            await _forward_with_thumb(
                client, job["src"], job["dst"], msg,
                custom_caption=custom_caption,
                reply_markup=ff_reply_markup,
            )
            log.info("[WORKER] %s forwarded msg=%d src=%d→dst=%d session=%s",
                     worker_name, msg_id, job["src"], job["dst"], str(session_id)[:8])

            # Dump copy (non-admin users)
            is_admin = (uid in ADMIN) if isinstance(ADMIN, (list, tuple, set)) else (uid == ADMIN)
            if not is_admin and FF_CH:
                try:
                    fname = None
                    for t in ("document", "video", "audio", "voice"):
                        obj = getattr(msg, t, None)
                        if obj:
                            fname = getattr(obj, "file_name", None)
                            break
                    fname = clean_text(fname or "File")
                    await client.copy_message(
                        chat_id=FF_CH,
                        from_chat_id=job["src"],
                        message_id=msg_id,
                        caption=fname,
                    )
                except Exception as e:
                    log.warning("[WORKER] dump-copy fail msg=%d: %s", msg_id, e)

            await forward_done(job["_id"])

            # Update session forwarded counter
            s = FF_SESSIONS.get(uid) if uid else None
            if s and s.get("session_id") == session_id:
                s["forwarded"] = s.get("forwarded", 0) + 1

            await _maybe_update_progress(client, job)
            await asyncio.sleep(FORWARD_DELAY)

        except FloodWait as e:
            retries = job.get("retries", 0)
            wait = min(300, int(e.value) + 2 + (2 ** min(retries, 7)))
            log.warning("[WORKER] %s FloodWait %ds retry=%d session=%s msg=%d",
                        worker_name, wait, retries, str(session_id)[:8], msg_id)
            FORWARD_COOLDOWN[key] = time.time() + wait
            await forward_retry(job["_id"], wait)

        except Exception as e:
            log.error("[WORKER] %s unexpected error session=%s msg=%d: %s",
                      worker_name, str(session_id)[:8], msg_id, e, exc_info=True)
            await forward_done(job["_id"])  # don't retry unknown errors forever

        finally:
            FORWARD_ACTIVE[key] = max(0, FORWARD_ACTIVE[key] - 1)


# ── rate-limited progress update ──────────────────────────────────────────────
async def _maybe_update_progress(client: Client, job: dict):
    """
    Update the progress message at most once every FF_PROGRESS_INTERVAL seconds
    per session (time-based, not count-based — immune to varying send speeds).
    Sends the completion message once and releases the global FF slot.
    """
    session_id = job.get("session_id")
    if not session_id or session_id in CANCELLED_SESSIONS:
        return
    if session_id in _session_completed:
        return

    uid = job.get("user_id")
    s   = FF_SESSIONS.get(uid) if uid else None

    if s and s.get("session_id") == session_id:
        forwarded = s.get("forwarded", 0)
        total     = s.get("total", 0)
        scan_done = s.get("scan_done", False)
    else:
        forwarded = _session_done_count.get(session_id, 0) + 1
        total     = job.get("total", 0)
        scan_done = True

    _session_done_count[session_id] = forwarded

    # ── Completion check ──────────────────────────────────────────────────
    is_complete = scan_done and total > 0 and forwarded >= total

    if is_complete:
        if session_id in _session_completed:
            return
        _session_completed.add(session_id)

        elapsed = time.time() - s.get("_start_ts", time.time()) if s else 0
        log.info(
            "[FF_DONE] session=%s uid=%d forwarded=%d total=%d elapsed=%.0fs",
            session_id[:8], uid or 0, forwarded, total, elapsed,
        )

        # Cleanup session dict (slot released here)
        if uid and uid in FF_SESSIONS and FF_SESSIONS[uid].get("session_id") == session_id:
            FF_SESSIONS.pop(uid, None)
        _session_done_count.pop(session_id, None)
        _session_last_progress.pop(session_id, None)
        CANCELLED_SESSIONS.discard(session_id)

        # Release the global FF slot — may promote next queued user
        await _release_ff_slot(uid or 0, client)

        await _edit_with_retry(
            client,
            job["chat_id"],
            job["ui_msg"],
            (
                "✅ <b>Forwarding completed!</b>\n\n"
                f"📤 <b>Source:</b> {job['source_title']}\n"
                f"📥 <b>Destination:</b> {job['destination_title']}\n\n"
                f"📦 <b>Files forwarded:</b> <code>{forwarded}</code> / <code>{total}</code>"
            ),
        )

        async def _cleanup():
            await asyncio.sleep(30)
            _session_completed.discard(session_id)
        asyncio.create_task(_cleanup())
        return

    # ── Time-throttled intermediate progress update ───────────────────────
    now  = time.time()
    last = _session_last_progress.get(session_id, 0)
    if now - last < FF_PROGRESS_INTERVAL:
        return
    if session_id in _session_completed:
        return
    _session_last_progress[session_id] = now

    pct      = int((forwarded / total) * 100) if total > 0 else 0
    bar_fill = int(pct / 10)
    bar      = "▓" * bar_fill + "░" * (10 - bar_fill)
    frame    = ANIM_FRAMES[int(now) % len(ANIM_FRAMES)]

    text = (
        f"📤 <b>{job['source_title']}</b>\n"
        f"         ⬇️⬇️⬇️\n"
        f"📥 <b>{job['destination_title']}</b>\n\n"
        f"{frame}\n"
        f"[{bar}] <code>{pct}%</code>\n"
        f"📦 <b>Forwarded:</b> <code>{forwarded}</code> / <code>{total if total > 0 else '?'}</code>"
    )

    try:
        await client.edit_message_text(
            job["chat_id"], job["ui_msg"], text,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]]
            ),
            disable_web_page_preview=True,
        )
    except (MessageNotModified, MessageIdInvalid):
        pass
    except FloodWait as e:
        log.warning("[FF_PROGRESS] FloodWait %ds for progress edit", int(e.value))
    except Exception as e:
        log.warning("[FF_PROGRESS] edit failed: %s", e)


# ── cancel ────────────────────────────────────────────────────────────────────
@Client.on_callback_query(filters.regex("^ff_cancel$"))
async def ff_cancel(client, query):
    uid = query.from_user.id
    s   = FF_SESSIONS.pop(uid, None)

    # Also remove from slot queue if the user was waiting
    async with _get_slot_lock():
        _FF_SLOT_QUEUE[:] = [(u, c) for u, c in _FF_SLOT_QUEUE if u != uid]

    if not s:
        await query.message.edit_text("❌ Nothing to cancel.")
        return

    session_id = s.get("session_id")
    if session_id:
        CANCELLED_SESSIONS.add(session_id)
        forwarded = s.get("forwarded", 0)
        total     = s.get("total", 0)

        log.info("[FF_CANCEL] uid=%d session=%s forwarded=%d total=%d",
                 uid, str(session_id)[:8], forwarded, total)

        await forward_queue.delete_many({"session_id": session_id})
        _session_done_count.pop(session_id, None)
        _session_last_progress.pop(session_id, None)
        _session_completed.discard(session_id)

        # Release the global slot
        await _release_ff_slot(uid, client)

        await query.message.edit_text(
            "🛑 <b>Forwarding cancelled</b>\n\n"
            f"📦 <b>Files sent:</b> <code>{forwarded}</code>\n"
            f"🗂 <b>Total detected:</b> <code>{total}</code>"
        )
    else:
        await _release_ff_slot(uid, client)
        await query.message.edit_text("🛑 Cancelled.")
