"""
admin_channels.py  ── /channels admin command
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
• /channels  (admin-only): lists all user-added channels as buttons
  - Excludes channels owned/added by any ADMIN id
  - Shows channel name, added-by user info, file count, forwarding options
• "File Forwarding" button: forward ALL media from that channel from msg 1
• "Continue Forwarding" button: resume from last forwarded message_id (saved per channel in DB)
• "Back" button: return to channel list
• Forwarding runs in a separate background task pool (GLOBAL_FF_WORKERS),
  completely isolated from the existing caption and file_forward workers.
• Throttle: if event loop is busy (queue growing), forwarding yields/slows down.
• Progress message updated every N files.
"""

import asyncio
import time
import uuid
from collections import defaultdict

from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from body.database import (
    db,
    users,
    get_channel_cached,
    set_channel_title_cache,
    forward_queue,
)
from info import ADMIN

# ── Collection for global-forward progress state ──────────────────────────────
# Stores: { channel_id, last_msg_id, total_forwarded, updated_at }
global_ff_progress = db.global_ff_progress

# ── In-memory state for admin channel forwarding sessions ────────────────────
# Maps: session_id -> session dict
ADMIN_FF_SESSIONS: dict[str, dict] = {}

# Cancelled session ids
ADMIN_FF_CANCELLED: set = set()

# Active worker count for global forwarding (separate from existing FF workers)
GLOBAL_FF_ACTIVE = defaultdict(int)  # channel_id -> active worker count

# Number of dedicated workers for admin/global forwarding
GLOBAL_FF_WORKERS = 2  # Low value so it never starves other bot functions

# Progress update frequency (every N files)
_GFF_PROGRESS_EVERY = 5

# Max consecutive missing messages before stopping scan
_MAX_CONSECUTIVE_MISSING = 500

# Delay between each forwarded file (keeps Telegram happy, avoids FloodWait)
_GFF_FORWARD_DELAY = 1.2

# In-memory job queue for global forwarding (list of job dicts)
_gff_job_queue: asyncio.Queue = asyncio.Queue()

# Track sessions whose completion was already announced
_gff_completed_sessions: set = set()


# ── DB helpers ────────────────────────────────────────────────────────────────

async def get_global_ff_progress(channel_id: int) -> dict:
    """Return saved forwarding progress for a channel, or empty dict."""
    doc = await global_ff_progress.find_one({"channel_id": channel_id})
    return doc or {}


async def save_global_ff_progress(channel_id: int, last_msg_id: int, total_forwarded: int):
    """Upsert the last forwarded message id and count for a channel."""
    await global_ff_progress.update_one(
        {"channel_id": channel_id},
        {
            "$set": {
                "last_msg_id": last_msg_id,
                "total_forwarded": total_forwarded,
                "updated_at": time.time(),
            }
        },
        upsert=True,
    )


async def ensure_global_ff_indexes():
    await global_ff_progress.create_index([("channel_id", 1)], unique=True)


# ── Fetch all user-added channels (excluding ADMIN channels) ─────────────────

async def get_all_user_channels_for_admin() -> list[dict]:
    """
    Return a flat list of {channel_id, channel_title, user_id, user_name}
    for every channel added by a non-admin user.
    Admin ids from info.ADMIN are excluded.
    """
    admin_ids = set(ADMIN) if isinstance(ADMIN, (list, tuple, set)) else {ADMIN}
    result = []
    seen_channels: set = set()

    async for user_doc in users.find({}):
        uid = user_doc["_id"]
        if uid in admin_ids:
            continue
        for ch in user_doc.get("channels", []):
            cid = ch.get("channel_id")
            if not cid or cid in seen_channels:
                continue
            seen_channels.add(cid)
            result.append({
                "channel_id":    cid,
                "channel_title": ch.get("channel_title", str(cid)),
                "user_id":       uid,
                "user_name":     user_doc.get("first_name", f"User {uid}"),
            })
    return result


# ── Startup hook ──────────────────────────────────────────────────────────────

def on_bot_start(client: Client):
    """Launch global-forward worker pool at bot start."""
    asyncio.create_task(ensure_global_ff_indexes(), name="gff_idx")
    for i in range(GLOBAL_FF_WORKERS):
        asyncio.create_task(
            _global_ff_worker(client),
            name=f"gff_worker_{i}",
        )
    print(f"[GFF] {GLOBAL_FF_WORKERS} global-forward workers started")


# ── /channels command ─────────────────────────────────────────────────────────

@Client.on_message(filters.private & filters.user(ADMIN) & filters.command("channels"))
async def channels_cmd(client: Client, message):
    await _show_channel_list(client, message)


async def _show_channel_list(client: Client, message_or_query):
    """Render the channel list.  Works for both Message and CallbackQuery."""
    channels = await get_all_user_channels_for_admin()

    is_query = hasattr(message_or_query, "data")  # True if CallbackQuery

    if not channels:
        text = "📋 <b>No user-added channels found.</b>\n\nNo non-admin user has added the bot to any channel yet."
        if is_query:
            await message_or_query.message.edit_text(text)
        else:
            await message_or_query.reply_text(text)
        return

    kb = [
        [InlineKeyboardButton(
            f"📢 {ch['channel_title']}",
            callback_data=f"adm_ch_{ch['channel_id']}"
        )]
        for ch in channels
    ]
    kb.append([InlineKeyboardButton("❌ Close", callback_data="close_msg")])

    text = f"📋 <b>All User Channels</b>  ({len(channels)} total)\n\nSelect a channel to view details:"

    if is_query:
        await message_or_query.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(kb),
        )
    else:
        await message_or_query.reply_text(
            text,
            reply_markup=InlineKeyboardMarkup(kb),
        )


# ── Channel detail view ───────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^adm_ch_(-?\d+)$") & filters.user(ADMIN))
async def adm_channel_detail(client: Client, query):
    channel_id = int(query.matches[0].group(1))
    await _show_channel_detail(client, query, channel_id)


async def _show_channel_detail(client: Client, query, channel_id: int):
    """Show details for a single channel with forwarding action buttons."""
    # --- Gather channel info ---
    cap_doc = await get_channel_cached(channel_id)
    title = cap_doc.get("_title")
    if not title:
        try:
            chat = await client.get_chat(channel_id)
            title = getattr(chat, "title", str(channel_id))
            await set_channel_title_cache(channel_id, title)
        except Exception:
            title = str(channel_id)

    # --- Find who added the bot ---
    admin_ids = set(ADMIN) if isinstance(ADMIN, (list, tuple, set)) else {ADMIN}
    added_by_id = None
    added_by_name = "Unknown"

    async for user_doc in users.find({}):
        uid = user_doc["_id"]
        if uid in admin_ids:
            continue
        for ch in user_doc.get("channels", []):
            if ch.get("channel_id") == channel_id:
                added_by_id = uid
                try:
                    u = await client.get_users(uid)
                    added_by_name = (
                        f"{u.first_name or ''} {u.last_name or ''}".strip()
                        or f"User {uid}"
                    )
                    if u.username:
                        added_by_name += f" (@{u.username})"
                except Exception:
                    added_by_name = f"User {uid}"
                break
        if added_by_id:
            break

    # --- Count media messages in channel ---
    file_count_text = "Unknown"
    try:
        # Count via history (max 10k for speed; shows "10000+" if large)
        count = 0
        async for msg in client.get_chat_history(channel_id, limit=10000):
            if msg.media:
                count += 1
        file_count_text = str(count)
        if count == 10000:
            file_count_text = "10000+"
    except Exception:
        file_count_text = "N/A"

    # --- Progress info ---
    progress = await get_global_ff_progress(channel_id)
    last_fwd = progress.get("last_msg_id", 0)
    total_fwd = progress.get("total_forwarded", 0)

    if last_fwd:
        progress_text = (
            f"📌 <b>Last forwarded msg ID:</b> <code>{last_fwd}</code>\n"
            f"📦 <b>Total forwarded so far:</b> <code>{total_fwd}</code>"
        )
        has_progress = True
    else:
        progress_text = "📌 <b>No previous forwarding history.</b>"
        has_progress = False

    text = (
        f"📢 <b>Channel:</b> {title}\n"
        f"🆔 <b>Channel ID:</b> <code>{channel_id}</code>\n\n"
        f"👤 <b>Added by:</b> {added_by_name}\n"
        f"🆔 <b>User ID:</b> <code>{added_by_id or 'Unknown'}</code>\n\n"
        f"🗂 <b>Files in channel:</b> <code>{file_count_text}</code>\n\n"
        f"{progress_text}"
    )

    buttons = [
        [InlineKeyboardButton("📤 File Forwarding (From Start)", callback_data=f"gff_start_{channel_id}")],
    ]
    if has_progress:
        buttons.append(
            [InlineKeyboardButton("⏩ Continue Previous Forwarding", callback_data=f"gff_continue_{channel_id}")]
        )
    buttons.append(
        [InlineKeyboardButton("🔙 Back to Channels", callback_data="adm_ch_back")]
    )

    await query.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(buttons),
    )


# ── Back to channel list ──────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^adm_ch_back$") & filters.user(ADMIN))
async def adm_ch_back(client: Client, query):
    await _show_channel_list(client, query)


# ── Start forwarding from beginning ──────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^gff_start_(-?\d+)$") & filters.user(ADMIN))
async def gff_start(client: Client, query):
    channel_id = int(query.matches[0].group(1))
    session_id = str(uuid.uuid4())

    cap_doc = await get_channel_cached(channel_id)
    title = cap_doc.get("_title", str(channel_id))

    # Destination: get CP_CH from info
    from info import CP_CH as DEST_CH
    dest_title = "Destination Channel"
    try:
        dest_chat = await client.get_chat(DEST_CH)
        dest_title = getattr(dest_chat, "title", str(DEST_CH))
    except Exception:
        pass

    session = {
        "session_id":   session_id,
        "channel_id":   channel_id,
        "channel_title": title,
        "dest_id":      DEST_CH,
        "dest_title":   dest_title,
        "start_from":   1,           # from message id 1
        "chat_id":      query.message.chat.id,
        "ui_msg_id":    query.message.id,
        "total":        0,
        "forwarded":    0,
        "is_continue":  False,
    }
    ADMIN_FF_SESSIONS[session_id] = session

    await query.message.edit_text(
        f"🔄 <b>Starting file forwarding…</b>\n\n"
        f"📢 <b>Source:</b> {title}\n"
        f"📥 <b>Destination:</b> {dest_title}\n\n"
        f"⏳ Scanning channel for media files…",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛑 Stop", callback_data=f"gff_stop_{session_id}")]
        ]),
    )

    asyncio.create_task(
        _gff_scan_and_enqueue(client, session_id),
        name=f"gff_scan_{session_id[:8]}",
    )


# ── Continue from previous progress ──────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^gff_continue_(-?\d+)$") & filters.user(ADMIN))
async def gff_continue(client: Client, query):
    channel_id = int(query.matches[0].group(1))
    progress = await get_global_ff_progress(channel_id)
    last_msg_id = progress.get("last_msg_id", 0)
    prev_total = progress.get("total_forwarded", 0)

    session_id = str(uuid.uuid4())
    cap_doc = await get_channel_cached(channel_id)
    title = cap_doc.get("_title", str(channel_id))

    from info import CP_CH as DEST_CH
    dest_title = "Destination Channel"
    try:
        dest_chat = await client.get_chat(DEST_CH)
        dest_title = getattr(dest_chat, "title", str(DEST_CH))
    except Exception:
        pass

    session = {
        "session_id":    session_id,
        "channel_id":    channel_id,
        "channel_title": title,
        "dest_id":       DEST_CH,
        "dest_title":    dest_title,
        "start_from":    last_msg_id + 1,   # resume after last forwarded
        "chat_id":       query.message.chat.id,
        "ui_msg_id":     query.message.id,
        "total":         0,
        "forwarded":     prev_total,         # carry over previous count
        "is_continue":   True,
        "prev_total":    prev_total,
    }
    ADMIN_FF_SESSIONS[session_id] = session

    await query.message.edit_text(
        f"⏩ <b>Resuming file forwarding…</b>\n\n"
        f"📢 <b>Source:</b> {title}\n"
        f"📥 <b>Destination:</b> {dest_title}\n\n"
        f"📌 <b>Continuing from message ID:</b> <code>{last_msg_id + 1}</code>\n"
        f"📦 <b>Previously forwarded:</b> <code>{prev_total}</code>\n\n"
        f"⏳ Scanning for remaining files…",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛑 Stop", callback_data=f"gff_stop_{session_id}")]
        ]),
    )

    asyncio.create_task(
        _gff_scan_and_enqueue(client, session_id),
        name=f"gff_scan_{session_id[:8]}",
    )


# ── Stop forwarding ───────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^gff_stop_([a-z0-9\-]+)$") & filters.user(ADMIN))
async def gff_stop(client: Client, query):
    session_id = query.matches[0].group(1)
    session = ADMIN_FF_SESSIONS.get(session_id)

    ADMIN_FF_CANCELLED.add(session_id)

    if session:
        forwarded = session.get("forwarded", 0)
        title = session.get("channel_title", "Channel")
        total = session.get("total", 0)
        await query.message.edit_text(
            f"🛑 <b>Forwarding stopped.</b>\n\n"
            f"📢 <b>Channel:</b> {title}\n"
            f"📦 <b>Files forwarded so far:</b> <code>{forwarded}</code>\n"
            f"🗂 <b>Files detected:</b> <code>{total}</code>\n\n"
            f"You can resume later with <b>Continue Forwarding</b>.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back to Channels", callback_data="adm_ch_back")]
            ]),
        )
        ADMIN_FF_SESSIONS.pop(session_id, None)
    else:
        await query.message.edit_text(
            "🛑 <b>Forwarding stopped.</b>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back to Channels", callback_data="adm_ch_back")]
            ]),
        )


# ── Background scan + enqueue ─────────────────────────────────────────────────

async def _gff_scan_and_enqueue(client: Client, session_id: str):
    """
    Scan source channel for media messages starting from start_from.
    Enqueues each found message into _gff_job_queue for workers.
    Yields control every iteration so caption/other workers are not starved.
    """
    session = ADMIN_FF_SESSIONS.get(session_id)
    if not session:
        return

    channel_id = session["channel_id"]
    start_from = session["start_from"]
    consecutive_missing = 0
    msg_id = start_from
    total_found = 0

    while True:
        # Yield every iteration — never block event loop
        await asyncio.sleep(0)

        if session_id in ADMIN_FF_CANCELLED:
            return

        try:
            msg = await client.get_messages(channel_id, msg_id)
        except FloodWait as e:
            wait = int(e.value) + 2
            print(f"[GFF_SCAN] FloodWait {wait}s on {channel_id}")
            await asyncio.sleep(wait)
            continue
        except Exception as e:
            print(f"[GFF_SCAN] get_messages error msg_id={msg_id}: {e}")
            msg = None

        if not msg or getattr(msg, "empty", True):
            consecutive_missing += 1
            if consecutive_missing >= _MAX_CONSECUTIVE_MISSING:
                break
            msg_id += 1
            continue

        consecutive_missing = 0

        if not msg.media:
            msg_id += 1
            continue

        # Enqueue for worker
        await _gff_job_queue.put({
            "session_id":    session_id,
            "channel_id":    channel_id,
            "dest_id":       session["dest_id"],
            "msg_id":        msg_id,
            "chat_id":       session["chat_id"],
            "ui_msg_id":     session["ui_msg_id"],
            "channel_title": session["channel_title"],
            "dest_title":    session["dest_title"],
        })
        total_found += 1
        session["total"] = total_found
        msg_id += 1

    # Scan done — mark session as scan-complete
    if session_id not in ADMIN_FF_CANCELLED:
        session["scan_done"] = True
        if total_found == 0:
            try:
                await client.edit_message_text(
                    session["chat_id"],
                    session["ui_msg_id"],
                    f"✅ <b>No new media files found.</b>\n\n"
                    f"📢 <b>Channel:</b> {session['channel_title']}\n"
                    f"📌 All files starting from message ID <code>{start_from}</code> have been forwarded already.",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🔙 Back to Channels", callback_data="adm_ch_back")]
                    ]),
                )
            except Exception:
                pass
            ADMIN_FF_SESSIONS.pop(session_id, None)


# ── Global forward workers ────────────────────────────────────────────────────

async def _global_ff_worker(client: Client):
    """
    Long-running worker pulling jobs from _gff_job_queue.
    - Forwards media to destination channel.
    - Saves progress after each file.
    - Handles FloodWait with exponential back-off.
    - Completely separate from the existing caption/file_forward workers.
    """
    while True:
        # Non-blocking get with 1s timeout so we don't spin on empty queue
        try:
            job = await asyncio.wait_for(_gff_job_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue

        session_id = job["session_id"]

        # Skip if cancelled
        if session_id in ADMIN_FF_CANCELLED:
            _gff_job_queue.task_done()
            continue

        session = ADMIN_FF_SESSIONS.get(session_id)
        if not session:
            _gff_job_queue.task_done()
            continue

        channel_id = job["channel_id"]
        dest_id    = job["dest_id"]
        msg_id     = job["msg_id"]

        try:
            msg = await client.get_messages(channel_id, msg_id)
            if not msg or getattr(msg, "empty", True) or not msg.media:
                _gff_job_queue.task_done()
                continue

            # Forward the file
            await client.copy_message(
                chat_id=dest_id,
                from_chat_id=channel_id,
                message_id=msg_id,
            )

            # Update session counters
            session["forwarded"] = session.get("forwarded", 0) + 1
            forwarded = session["forwarded"]

            # Save progress to DB
            await save_global_ff_progress(channel_id, msg_id, forwarded)

            # Update progress message every N files
            if forwarded % _GFF_PROGRESS_EVERY == 0 or _gff_job_queue.empty():
                if session_id not in ADMIN_FF_CANCELLED and session_id not in _gff_completed_sessions:
                    total = session.get("total", 0)
                    pct   = int((forwarded / total) * 100) if total > 0 else 0
                    try:
                        await client.edit_message_text(
                            job["chat_id"],
                            job["ui_msg_id"],
                            _gff_progress_text(session, pct),
                            reply_markup=InlineKeyboardMarkup([
                                [InlineKeyboardButton("🛑 Stop", callback_data=f"gff_stop_{session_id}")]
                            ]),
                        )
                    except Exception:
                        pass

            await asyncio.sleep(_GFF_FORWARD_DELAY)

        except FloodWait as e:
            wait = min(300, int(e.value) + 5)
            print(f"[GFF_WORKER] FloodWait {wait}s for channel {channel_id}")
            # Put job back at front (re-queue) — wait, then continue
            await asyncio.sleep(wait)
            await _gff_job_queue.put(job)
            _gff_job_queue.task_done()
            continue

        except Exception as e:
            print(f"[GFF_WORKER_ERR] msg_id={msg_id} ch={channel_id}: {e}")
            # Don't retry unknown errors; just move on

        _gff_job_queue.task_done()

        # Check if this was the last job for this session
        await _gff_maybe_complete(client, job, session)


async def _gff_maybe_complete(client: Client, job: dict, session: dict):
    """Send the completion message when queue is drained and scan is done."""
    session_id = job["session_id"]

    if session_id in ADMIN_FF_CANCELLED:
        return
    if session_id in _gff_completed_sessions:
        return

    # Only mark complete if scan is finished AND queue has no more jobs for this session
    if not session.get("scan_done"):
        return

    # Check if there are remaining jobs for this session still in queue
    # (rough check — queue is async, so we just check if queue is empty overall)
    if not _gff_job_queue.empty():
        return

    # Double-check by looking at session state
    # (scan_done is set after scan finishes; if forwarded >= total, we're done)
    total     = session.get("total", 0)
    forwarded = session.get("forwarded", 0)
    if total > 0 and forwarded < total:
        return

    # Guard: only one worker sends the completion
    if session_id in _gff_completed_sessions:
        return
    _gff_completed_sessions.add(session_id)
    ADMIN_FF_SESSIONS.pop(session_id, None)

    try:
        await client.edit_message_text(
            job["chat_id"],
            job["ui_msg_id"],
            f"✅ <b>Forwarding completed!</b>\n\n"
            f"📢 <b>Channel:</b> {job['channel_title']}\n"
            f"📥 <b>Destination:</b> {job['dest_title']}\n\n"
            f"📦 <b>Files forwarded:</b> <code>{forwarded}</code>\n"
            f"🗂 <b>Total detected:</b> <code>{total}</code>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back to Channels", callback_data="adm_ch_back")]
            ]),
        )
    except Exception:
        pass

    # Cleanup after delay so late-arriving workers still see it
    async def _cleanup():
        await asyncio.sleep(60)
        _gff_completed_sessions.discard(session_id)
        ADMIN_FF_CANCELLED.discard(session_id)

    asyncio.create_task(_cleanup())


def _gff_progress_text(session: dict, pct: int) -> str:
    forwarded = session.get("forwarded", 0)
    total     = session.get("total", 0)
    title     = session.get("channel_title", "Channel")
    dest      = session.get("dest_title", "Destination")
    bar_filled = int(pct / 10)
    bar = "▓" * bar_filled + "░" * (10 - bar_filled)
    return (
        f"📤 <b>{title}</b>\n"
        f"         ⬇️⬇️⬇️\n"
        f"📥 <b>{dest}</b>\n\n"
        f"🔄 <b>Forwarding in progress…</b>\n\n"
        f"[{bar}] <code>{pct}%</code>\n"
        f"📦 <b>Forwarded:</b> <code>{forwarded}</code> / <code>{total}</code>"
    )
