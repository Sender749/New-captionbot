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


def ff_user_is_active(uid: int) -> bool:
    """
    Public helper — returns True if `uid` currently holds a forwarding slot
    (scanning or forwarding in progress).  Importable by Caption.py via
    `from body.file_forward import *` unlike the underscore-prefixed
    `_FF_ACTIVE_UIDS` set which `import *` silently skips.
    """
    return uid in _FF_ACTIVE_UIDS


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


@Client.on_callback_query(filters.regex(r"^ff_dismiss_notice$"))
async def ff_dismiss_notice(client, query):
    """Deletes the 'already active session' notice without touching anything else."""
    await query.answer()
    try:
        await query.message.delete()
    except Exception:
        pass


# ── callback: source selection ────────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^ff_src_(-?\d+)$"))
async def ff_src(client, query):
    uid = query.from_user.id

    # Double-guard: if the user managed to hit ff_src while already active
    # (e.g. tapped a stale message), block them without touching the running session.
    if uid in _FF_ACTIVE_UIDS:
        await query.answer(
            "⚠️ You already have an active forwarding session running! "
            "It will complete on its own. Cancel it first if you want to start a new one.",
            show_alert=True,
        )
        log.warning("[FF_SRC] uid=%d blocked — already active", uid)
        return

    s = FF_SESSIONS.get(uid)
    if not s or s.get("step") != "src":
        # Stale callback from a previous session's message
        await query.answer("⚠️ This session has expired. Use /file_forward to start a new one.", show_alert=True)
        return

    src = int(query.matches[0].group(1))
    s["source"]       = src
    s["source_title"] = next(x["channel_title"] for x in s["channels"] if x["channel_id"] == src)
    s["channels"]     = [x for x in s["channels"] if x["channel_id"] != src]
    s["step"]         = "dst"
    log.info("[FF_SRC] uid=%d selected source=%d (%s)", uid, src, s["source_title"])

    if not s["channels"]:
        await query.answer("⚠️ No other channels available as destination.", show_alert=True)
        FF_SESSIONS.pop(uid, None)
        return

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
    """Skip the intermediate 'current caption' sub-screen and go straight to input."""
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
        "Video codec ⇛ <code>{vcodec}</code>  Audio codec ⇛ <code>{acodec}</code>\n"
        "Remove caption ⇛ <code>{empty}</code>"
        "</blockquote>\n\n"
        "💡 <b>Tip:</b> Send <code>{empty}</code> alone to forward files without any caption.\n\n"
        "✍️ <b>Example:</b>\n"
        "<code>&lt;b&gt;{title}&lt;/b&gt; {season}{episode} ({year})\n"
        "{audio} | {quality} | {subtitle}\n"
        "💾 {file_size}</code>",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back", callback_data="ffc_menu")]]),
        disable_web_page_preview=True,
    )


# ffc_setcapmsg is now unused (ffc_setcap goes directly to input), but kept for
# safety in case of old callback_data still in flight from an open session.
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
        "Send <code>{empty}</code> to remove the caption entirely.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back", callback_data="ffc_menu")]]),
    )


@Client.on_callback_query(filters.regex(r"^ffc_delcap$"))
async def ffc_delcap(client, query):
    await query.answer("Caption cleared — Same Caption is now active.")
    pair = _ff_session_for(query.from_user.id)
    if not pair:
        return
    _, cs = pair
    cs["template"] = None
    await _render_ff_cap_panel(client, query.message.chat.id, query.message.id)


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
# ── media send (always copy_message — reliable for all media types) ───────────
async def _send_media(
    client: Client, src: int, dst: int, msg,
    custom_caption: str | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """
    Forward one media message to dst using copy_message.
    Handles every media type (video, photo, document, audio, voice, animation,
    sticker…) without downloading thumbnails — which was the main failure path
    in the old _forward_with_thumb approach.

    custom_caption=None → original caption preserved (same caption mode).
    custom_caption=""   → file sent without any caption ({empty} placeholder).
    custom_caption=str  → parsed as HTML and applied to the copy.
    """
    if custom_caption is not None:
        await client.copy_message(
            chat_id=dst,
            from_chat_id=src,
            message_id=msg.id,
            caption=custom_caption,
            parse_mode=ParseMode.HTML,
            reply_markup=reply_markup,
        )
    else:
        await client.copy_message(
            chat_id=dst,
            from_chat_id=src,
            message_id=msg.id,
            reply_markup=reply_markup,
        )


# ── scan & bulk-insert (background task, one per user session) ────────────────
async def _scan_and_enqueue(client: Client, uid: int):
    """
    Three-phase design that eliminates the race condition where workers
    processed all jobs before scan_done was set:

    PHASE 1 — SCAN:   iter_messages collects all media IDs into a local list.
                       Nothing written to MongoDB yet → workers cannot start.

    PHASE 2 — INSERT: insert_many writes ALL jobs at once with total already
                       stamped correctly on every document.

    PHASE 3 — READY:  scan_done=True is set synchronously (no await between
                       insert_many and this line). asyncio is single-threaded;
                       workers cannot run between those two statements.
                       When workers pick up their first job, scan_done is
                       guaranteed to already be True.
    """
    s = FF_SESSIONS.get(uid)
    if not s:
        await _release_ff_slot(uid, client)
        return

    session_id = s["session_id"]
    src        = s["source"]
    dst        = s["destination"]
    skip_id    = int(s.get("skip", 0))
    end_id     = s.get("end_id")
    caption_settings_snapshot = dict(
        s.get("caption_settings") or _default_ff_caption_settings()
    )

    s["total"]     = 0
    s["scan_done"] = False
    _session_done_count[session_id] = 0

    log.info("[SCAN] uid=%d session=%s src=%d dst=%d skip=%d end=%s",
             uid, session_id[:8], src, dst, skip_id, end_id)
    t_start = time.time()

    media_ids: list = []

    try:
        # ══════════════════════════════════════════════════════════════
        #  PHASE 1 — SCAN with iter_messages (oldest → newest)
        #  Each internal API call fetches 200 messages — ~200× faster
        #  than the old one-by-one get_messages approach.
        # ══════════════════════════════════════════════════════════════
        scan_count = 0
        last_ui_ts = time.time()

        async for msg in client.iter_messages(src, offset_id=skip_id, reverse=True):
            if session_id in CANCELLED_SESSIONS:
                log.info("[SCAN] uid=%d session=%s cancelled", uid, session_id[:8])
                return

            if end_id is not None and msg.id > end_id:
                break

            scan_count += 1
            if msg.media:
                media_ids.append(msg.id)

            # Yield + update UI at most every 5 s during long scans
            now = time.time()
            if now - last_ui_ts >= 5.0:
                last_ui_ts = now
                await asyncio.sleep(0)
                if session_id not in CANCELLED_SESSIONS:
                    try:
                        await client.edit_message_text(
                            s["chat_id"], s["msg_id"],
                            f"📤 <b>{s['source_title']}</b>\n"
                            f"         ⬇️⬇️⬇️\n"
                            f"📥 <b>{s['destination_title']}</b>\n\n"
                            f"🔍 Scanning… <b>{len(media_ids)}</b> files found\n"
                            f"<i>({scan_count} messages checked)</i>",
                            reply_markup=InlineKeyboardMarkup([[
                                InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")
                            ]]),
                            disable_web_page_preview=True,
                        )
                    except Exception:
                        pass

        if session_id in CANCELLED_SESSIONS:
            return

        total = len(media_ids)
        log.info("[SCAN] uid=%d session=%s done: %d files / %d msgs in %.1fs",
                 uid, session_id[:8], total, scan_count, time.time() - t_start)

        if total == 0:
            try:
                await client.edit_message_text(
                    s["chat_id"], s["msg_id"],
                    "⚠️ <b>No media files found</b> in the specified range.\n\n"
                    "Please check the source channel and try again.",
                )
            except Exception:
                pass
            FF_SESSIONS.pop(uid, None)
            return   # slot released in finally

        # ══════════════════════════════════════════════════════════════
        #  PHASE 2 — BULK INSERT
        #  All jobs land in MongoDB at once, total correct from day one.
        # ══════════════════════════════════════════════════════════════
        s["total"] = total
        now = time.time()
        jobs = [
            {
                "user_id":           uid,
                "src":               src,
                "dst":               dst,
                "msg_id":            mid,
                "chat_id":           s["chat_id"],
                "ui_msg":            s["msg_id"],
                "source_title":      s["source_title"],
                "destination_title": s["destination_title"],
                "session_id":        session_id,
                "total":             total,
                "caption_settings":  caption_settings_snapshot,
                "status":            "pending",
                "retries":           0,
                "ts":                now + i * 0.001,
            }
            for i, mid in enumerate(media_ids)
        ]
        await forward_queue.insert_many(jobs, ordered=False)

        # ══════════════════════════════════════════════════════════════
        #  PHASE 3 — MARK READY  (no await between here and above)
        #  Workers see scan_done=True from their very first job.
        # ══════════════════════════════════════════════════════════════
        s["scan_done"] = True   # atomic from asyncio's perspective

        log.info("[SCAN] uid=%d session=%s %d jobs inserted — forwarding starts now",
                 uid, session_id[:8], total)

        # Show initial forwarding progress (first await after scan_done=True)
        try:
            await client.edit_message_text(
                s["chat_id"], s["msg_id"],
                f"📤 <b>{s['source_title']}</b>\n"
                f"         ⬇️⬇️⬇️\n"
                f"📥 <b>{s['destination_title']}</b>\n\n"
                f"🔄 Transferring files\n"
                f"[░░░░░░░░░░] <code>0%</code>\n"
                f"📦 <b>Forwarded:</b> <code>0</code> / <code>{total}</code>",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")
                ]]),
                disable_web_page_preview=True,
            )
        except Exception:
            pass

    except Exception as e:
        log.error("[SCAN] uid=%d session=%s error: %s", uid, session_id[:8], e, exc_info=True)
        # Graceful degradation: insert whatever we found before the error
        total = len(media_ids)
        if total > 0 and not s.get("scan_done"):
            s["total"]     = total
            s["scan_done"] = True
            try:
                now  = time.time()
                partial_jobs = [
                    {
                        "user_id": uid, "src": src, "dst": dst, "msg_id": mid,
                        "chat_id": s["chat_id"], "ui_msg": s["msg_id"],
                        "source_title": s["source_title"],
                        "destination_title": s["destination_title"],
                        "session_id": session_id, "total": total,
                        "caption_settings": caption_settings_snapshot,
                        "status": "pending", "retries": 0, "ts": now + i * 0.001,
                    }
                    for i, mid in enumerate(media_ids)
                ]
                await forward_queue.insert_many(partial_jobs, ordered=False)
                log.info("[SCAN] uid=%d partial insert: %d jobs after error", uid, total)
            except Exception as ie:
                log.error("[SCAN] uid=%d partial insert failed: %s", uid, ie)
        else:
            s["scan_done"] = True

    finally:
        if s.get("total", 0) == 0:
            await _release_ff_slot(uid, client)


# ── forward worker ────────────────────────────────────────────────────────────
async def forward_worker(client: Client):
    """
    Single long-running worker. Pulls one DB job at a time, sends the file,
    handles FloodWait with exponential back-off, then loops.

    Counter rule: _session_done_count[session_id] is the ONLY forwarded counter.
    Incremented on EVERY exit path (success, error, cancelled skip) so that
    forwarded always reaches total and completion always fires.
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
        _job_done  = False

        try:
            # ── Cancelled session ──────────────────────────────────────
            if session_id in CANCELLED_SESSIONS:
                log.info("[WORKER] %s skip-cancelled session=%s msg=%d",
                         worker_name, str(session_id)[:8], msg_id)
                await forward_done(job["_id"])
                _job_done = True
                _session_done_count[session_id] = _session_done_count.get(session_id, 0) + 1
                continue

            # ── Fetch source message ───────────────────────────────────
            msg = await client.get_messages(job["src"], msg_id)

            if not msg or getattr(msg, "empty", False) or not msg.media:
                log.info("[WORKER] %s empty/no-media msg=%d session=%s — skipping",
                         worker_name, msg_id, str(session_id)[:8])
                await forward_done(job["_id"])
                _job_done = True
                _session_done_count[session_id] = _session_done_count.get(session_id, 0) + 1
                await _maybe_update_progress(client, job)
                continue

            # ── Build custom caption if configured ─────────────────────
            custom_caption, ff_reply_markup = None, None
            cs = job.get("caption_settings")
            if cs and cs.get("template"):
                try:
                    custom_caption, ff_reply_markup = await build_ff_caption(msg, cs)
                except Exception as e:
                    log.warning("[WORKER] caption build fail session=%s msg=%d: %s",
                                str(session_id)[:8], msg_id, e)

            # ── Send to destination ────────────────────────────────────
            await _send_media(
                client, job["src"], job["dst"], msg,
                custom_caption=custom_caption,
                reply_markup=ff_reply_markup,
            )
            log.info("[WORKER] %s ✓ msg=%d src=%d→dst=%d session=%s",
                     worker_name, msg_id, job["src"], job["dst"], str(session_id)[:8])

            # ── Dump copy for non-admin users ──────────────────────────
            is_admin = (uid in ADMIN) if isinstance(ADMIN, (list, tuple, set)) else (uid == ADMIN)
            if not is_admin and FF_CH:
                try:
                    fname = None
                    for t in ("document", "video", "audio", "voice"):
                        obj = getattr(msg, t, None)
                        if obj:
                            fname = getattr(obj, "file_name", None)
                            break
                    await client.copy_message(
                        chat_id=FF_CH,
                        from_chat_id=job["src"],
                        message_id=msg_id,
                        caption=clean_text(fname or "File"),
                    )
                except Exception as e:
                    log.warning("[WORKER] dump-copy fail msg=%d: %s", msg_id, e)

            # ── Mark done and update counter ───────────────────────────
            await forward_done(job["_id"])
            _job_done = True
            _session_done_count[session_id] = _session_done_count.get(session_id, 0) + 1
            await _maybe_update_progress(client, job)
            await asyncio.sleep(FORWARD_DELAY)

        except FloodWait as e:
            retries = job.get("retries", 0)
            wait    = min(300, int(e.value) + 2 + (2 ** min(retries, 7)))
            log.warning("[WORKER] %s FloodWait %ds retry=%d session=%s msg=%d",
                        worker_name, wait, retries, str(session_id)[:8], msg_id)
            FORWARD_COOLDOWN[key] = time.time() + wait
            await forward_retry(job["_id"], wait)
            # Do NOT increment — job will be retried

        except Exception as e:
            log.error("[WORKER] %s error session=%s msg=%d: %s",
                      worker_name, str(session_id)[:8], msg_id, e, exc_info=True)
            if not _job_done:
                await forward_done(job["_id"])
                _job_done = True
            # Count as done even on error — ensures completion always fires
            _session_done_count[session_id] = _session_done_count.get(session_id, 0) + 1
            await _maybe_update_progress(client, job)

        finally:
            FORWARD_ACTIVE[key] = max(0, FORWARD_ACTIVE[key] - 1)


# ── rate-limited progress update ──────────────────────────────────────────────
async def _maybe_update_progress(client: Client, job: dict):
    """
    Called after every job is processed (success OR error).
    _session_done_count[session_id] is the SINGLE forwarded counter.
    scan_done is always True by the time the first worker runs (guaranteed
    by the phase-2/3 atomic design in _scan_and_enqueue).
    """
    session_id = job.get("session_id")
    if not session_id or session_id in CANCELLED_SESSIONS:
        return
    if session_id in _session_completed:
        return

    uid = job.get("user_id")
    s   = FF_SESSIONS.get(uid) if uid else None
    session_alive = s is not None and s.get("session_id") == session_id

    forwarded = _session_done_count.get(session_id, 0)

    if session_alive:
        total     = s.get("total", 0)
        scan_done = s.get("scan_done", True)   # should always be True now
    else:
        total     = job.get("total", 0)
        scan_done = True

    # ── Completion check ──────────────────────────────────────────────────────
    is_complete = scan_done and total > 0 and forwarded >= total

    if is_complete:
        if session_id in _session_completed:
            return
        _session_completed.add(session_id)

        log.info("[FF_DONE] session=%s uid=%d forwarded=%d total=%d",
                 session_id[:8], uid or 0, forwarded, total)

        if uid and uid in FF_SESSIONS and FF_SESSIONS[uid].get("session_id") == session_id:
            FF_SESSIONS.pop(uid, None)
        _session_done_count.pop(session_id, None)
        _session_last_progress.pop(session_id, None)
        CANCELLED_SESSIONS.discard(session_id)

        await _release_ff_slot(uid or 0, client)

        await _edit_with_retry(
            client,
            job["chat_id"],
            job["ui_msg"],
            "✅ <b>Forwarding completed!</b>\n\n"
            f"📤 <b>Source:</b> {job['source_title']}\n"
            f"📥 <b>Destination:</b> {job['destination_title']}\n\n"
            f"📦 <b>Files forwarded:</b> <code>{forwarded}</code> / <code>{total}</code>",
        )

        async def _cleanup():
            await asyncio.sleep(60)
            _session_completed.discard(session_id)
        asyncio.create_task(_cleanup())
        return

    # ── Time-throttled intermediate progress ──────────────────────────────────
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
        f"📦 <b>Forwarded:</b> <code>{forwarded}</code> / <code>{total}</code>"
    )

    try:
        await client.edit_message_text(
            job["chat_id"], job["ui_msg"], text,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")
            ]]),
            disable_web_page_preview=True,
        )
    except (MessageNotModified, MessageIdInvalid):
        pass
    except FloodWait as e:
        wait = int(e.value)
        log.warning("[FF_PROGRESS] FloodWait %ds — backing off", wait)
        _session_last_progress[session_id] = now + wait
    except Exception as e:
        log.warning("[FF_PROGRESS] edit failed: %s", e)
