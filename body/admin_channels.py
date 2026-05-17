"""
admin_channels.py  ── /channels admin command
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FIXES vs previous version:
  • Uses MANUAL_FF (not CP_CH) as the forwarding destination.
  • filters.user(ADMIN) now correctly handles ADMIN as a list — Pyrogram
    accepts both int and list[int] for filters.user().
  • Removed CP_CH import entirely.
  • Added /channels to the /admin command panel (done in Caption.py).
  • on_bot_start() no longer re-calls ensure_global_ff_indexes() (already
    called from bot.py at startup) — avoids a coroutine-in-non-async context.
  • get_global_ff_progress / save_global_ff_progress are imported from
    database.py (single source of truth) instead of being redefined here.
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
    get_global_ff_progress,
    save_global_ff_progress,
)
from info import ADMIN, MANUAL_FF

# ── In-memory state ───────────────────────────────────────────────────────────
ADMIN_FF_SESSIONS: dict[str, dict] = {}   # session_id → session dict
ADMIN_FF_CANCELLED: set             = set()

GLOBAL_FF_WORKERS       = 2     # dedicated workers – never starves caption workers
_GFF_PROGRESS_EVERY     = 5     # update UI every N files
_MAX_CONSECUTIVE_MISSING = 500   # stop scan after this many consecutive empty msg IDs
_GFF_FORWARD_DELAY      = 1.2   # seconds between forwards (flood protection)

_gff_job_queue: asyncio.Queue   = asyncio.Queue()
_gff_completed_sessions: set    = set()


# ── Admin filter helper ───────────────────────────────────────────────────────
# Pyrogram filters.user() accepts int or list[int], so passing ADMIN (list) is fine.
_ADMIN_FILTER = filters.user(ADMIN)


# ── Startup hook (called from bot.py) ────────────────────────────────────────

def on_bot_start(client: Client):
    """Launch the global-forward worker pool once at bot start."""
    for i in range(GLOBAL_FF_WORKERS):
        asyncio.create_task(
            _global_ff_worker(client),
            name=f"gff_worker_{i}",
        )
    print(f"[GFF] {GLOBAL_FF_WORKERS} global-forward workers started, dest={MANUAL_FF}")


# ── /channels command ─────────────────────────────────────────────────────────

@Client.on_message(filters.private & _ADMIN_FILTER & filters.command("channels"))
async def channels_cmd(client: Client, message):
    await _show_channel_list(client, message)


# ── Shared list renderer (works for Message AND CallbackQuery) ─────────────────

async def _get_user_channels_for_admin() -> list[dict]:
    """
    Returns all channels added by non-admin users.
    Deduplicates by channel_id. Excludes any channel added by an ADMIN id.
    """
    admin_ids    = set(ADMIN) if isinstance(ADMIN, (list, tuple, set)) else {ADMIN}
    result       = []
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
            })
    return result


async def _show_channel_list(client: Client, target):
    """
    Render the channel list.
    `target` is either a Message (from /channels) or a CallbackQuery (from Back button).
    """
    channels  = await _get_user_channels_for_admin()
    is_query  = hasattr(target, "data")   # True → CallbackQuery

    if not channels:
        text = (
            "📋 <b>No user-added channels found.</b>\n\n"
            "No non-admin user has added the bot to any channel yet."
        )
        if is_query:
            await target.message.edit_text(text)
        else:
            await target.reply_text(text)
        return

    kb = [
        [InlineKeyboardButton(
            f"📢 {ch['channel_title']}",
            callback_data=f"adm_ch_{ch['channel_id']}"
        )]
        for ch in channels
    ]
    kb.append([InlineKeyboardButton("❌ Close", callback_data="close_msg")])

    text = (
        f"📋 <b>All User Channels</b>  ({len(channels)} total)\n\n"
        "Select a channel to view details:"
    )

    if is_query:
        await target.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))


# ── Channel detail ────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^adm_ch_(-?\d+)$") & _ADMIN_FILTER)
async def adm_channel_detail(client: Client, query):
    channel_id = int(query.matches[0].group(1))
    await query.answer()
    await _show_channel_detail(client, query, channel_id)


async def _show_channel_detail(client: Client, query, channel_id: int):
    admin_ids = set(ADMIN) if isinstance(ADMIN, (list, tuple, set)) else {ADMIN}

    # ── Channel title ──────────────────────────────────────────────────────
    cap_doc = await get_channel_cached(channel_id)
    title   = cap_doc.get("_title")
    if not title:
        try:
            chat  = await client.get_chat(channel_id)
            title = getattr(chat, "title", str(channel_id))
            await set_channel_title_cache(channel_id, title)
        except Exception:
            title = str(channel_id)

    # ── Who added the bot ─────────────────────────────────────────────────
    added_by_id   = None
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

    # ── File count ─────────────────────────────────────────────────────────
    file_count_text = "Unknown"
    try:
        count = 0
        async for msg in client.get_chat_history(channel_id, limit=10000):
            if msg.media:
                count += 1
        file_count_text = f"{count}+" if count == 10000 else str(count)
    except Exception:
        file_count_text = "N/A"

    # ── Previous forwarding progress ──────────────────────────────────────
    progress  = await get_global_ff_progress(channel_id)
    last_fwd  = progress.get("last_msg_id", 0)
    total_fwd = progress.get("total_forwarded", 0)

    if last_fwd:
        progress_text = (
            f"📌 <b>Last forwarded msg ID:</b> <code>{last_fwd}</code>\n"
            f"📦 <b>Total forwarded so far:</b> <code>{total_fwd}</code>"
        )
        has_progress = True
    else:
        progress_text = "📌 <b>No previous forwarding history.</b>"
        has_progress  = False

    # ── Destination info ──────────────────────────────────────────────────
    dest_title = str(MANUAL_FF)
    try:
        dest_chat  = await client.get_chat(MANUAL_FF)
        dest_title = getattr(dest_chat, "title", dest_title)
    except Exception:
        pass

    text = (
        f"📢 <b>Channel:</b> {title}\n"
        f"🆔 <b>Channel ID:</b> <code>{channel_id}</code>\n\n"
        f"👤 <b>Added by:</b> {added_by_name}\n"
        f"🆔 <b>User ID:</b> <code>{added_by_id or 'Unknown'}</code>\n\n"
        f"🗂 <b>Files in channel:</b> <code>{file_count_text}</code>\n"
        f"📥 <b>Forward dest:</b> {dest_title}\n\n"
        f"{progress_text}"
    )

    buttons = [
        [InlineKeyboardButton("📤 File Forwarding (From Start)", callback_data=f"gff_start_{channel_id}")],
    ]
    if has_progress:
        buttons.append(
            [InlineKeyboardButton("⏩ Continue Previous Forwarding", callback_data=f"gff_continue_{channel_id}")]
        )
    buttons.append([InlineKeyboardButton("🔙 Back to Channels", callback_data="adm_ch_back")])

    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))


# ── Back button ───────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^adm_ch_back$") & _ADMIN_FILTER)
async def adm_ch_back(client: Client, query):
    await query.answer()
    await _show_channel_list(client, query)


# ── Start forwarding from scratch ─────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^gff_start_(-?\d+)$") & _ADMIN_FILTER)
async def gff_start(client: Client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    session_id = str(uuid.uuid4())

    cap_doc    = await get_channel_cached(channel_id)
    src_title  = cap_doc.get("_title", str(channel_id))

    dest_title = str(MANUAL_FF)
    try:
        dest_chat  = await client.get_chat(MANUAL_FF)
        dest_title = getattr(dest_chat, "title", dest_title)
    except Exception:
        pass

    session = {
        "session_id":    session_id,
        "channel_id":    channel_id,
        "channel_title": src_title,
        "dest_id":       MANUAL_FF,
        "dest_title":    dest_title,
        "start_from":    1,
        "chat_id":       query.message.chat.id,
        "ui_msg_id":     query.message.id,
        "total":         0,
        "forwarded":     0,
        "is_continue":   False,
        "scan_done":     False,
    }
    ADMIN_FF_SESSIONS[session_id] = session

    await query.message.edit_text(
        f"🔄 <b>Starting file forwarding…</b>\n\n"
        f"📢 <b>Source:</b> {src_title}\n"
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


# ── Continue from previous forwarding ────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^gff_continue_(-?\d+)$") & _ADMIN_FILTER)
async def gff_continue(client: Client, query):
    await query.answer()
    channel_id  = int(query.matches[0].group(1))
    progress    = await get_global_ff_progress(channel_id)
    last_msg_id = progress.get("last_msg_id", 0)
    prev_total  = progress.get("total_forwarded", 0)
    session_id  = str(uuid.uuid4())

    cap_doc   = await get_channel_cached(channel_id)
    src_title = cap_doc.get("_title", str(channel_id))

    dest_title = str(MANUAL_FF)
    try:
        dest_chat  = await client.get_chat(MANUAL_FF)
        dest_title = getattr(dest_chat, "title", dest_title)
    except Exception:
        pass

    session = {
        "session_id":    session_id,
        "channel_id":    channel_id,
        "channel_title": src_title,
        "dest_id":       MANUAL_FF,
        "dest_title":    dest_title,
        "start_from":    last_msg_id + 1,
        "chat_id":       query.message.chat.id,
        "ui_msg_id":     query.message.id,
        "total":         0,
        "forwarded":     prev_total,
        "is_continue":   True,
        "prev_total":    prev_total,
        "scan_done":     False,
    }
    ADMIN_FF_SESSIONS[session_id] = session

    await query.message.edit_text(
        f"⏩ <b>Resuming file forwarding…</b>\n\n"
        f"📢 <b>Source:</b> {src_title}\n"
        f"📥 <b>Destination:</b> {dest_title}\n\n"
        f"📌 <b>Continuing from msg ID:</b> <code>{last_msg_id + 1}</code>\n"
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

@Client.on_callback_query(filters.regex(r"^gff_stop_([a-zA-Z0-9\-]+)$") & _ADMIN_FILTER)
async def gff_stop(client: Client, query):
    await query.answer()
    session_id = query.matches[0].group(1)
    ADMIN_FF_CANCELLED.add(session_id)
    session    = ADMIN_FF_SESSIONS.pop(session_id, None)

    forwarded  = session.get("forwarded", 0) if session else 0
    total      = session.get("total", 0)     if session else 0
    title      = session.get("channel_title", "Channel") if session else "Channel"

    await query.message.edit_text(
        f"🛑 <b>Forwarding stopped.</b>\n\n"
        f"📢 <b>Channel:</b> {title}\n"
        f"📦 <b>Files forwarded so far:</b> <code>{forwarded}</code>\n"
        f"🗂 <b>Files detected:</b> <code>{total}</code>\n\n"
        "You can resume later with <b>Continue Forwarding</b>.",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🔙 Back to Channels", callback_data="adm_ch_back")]
        ]),
    )


# ── Background scan ───────────────────────────────────────────────────────────

async def _gff_scan_and_enqueue(client: Client, session_id: str):
    """
    Scans the source channel for media messages and enqueues them.
    Yields control on every iteration — never blocks caption/other workers.
    """
    session = ADMIN_FF_SESSIONS.get(session_id)
    if not session:
        return

    channel_id          = session["channel_id"]
    start_from          = session["start_from"]
    consecutive_missing = 0
    msg_id              = start_from
    total_found         = 0

    while True:
        await asyncio.sleep(0)   # yield every iteration

        if session_id in ADMIN_FF_CANCELLED:
            return

        try:
            msg = await client.get_messages(channel_id, msg_id)
        except FloodWait as e:
            wait = int(e.value) + 2
            print(f"[GFF_SCAN] FloodWait {wait}s on ch={channel_id}")
            await asyncio.sleep(wait)
            continue
        except Exception as e:
            print(f"[GFF_SCAN] error msg_id={msg_id}: {e}")
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
        total_found       += 1
        session["total"]   = total_found
        msg_id            += 1

    # Scan finished
    if session_id not in ADMIN_FF_CANCELLED:
        session["scan_done"] = True
        if total_found == 0:
            try:
                await client.edit_message_text(
                    session["chat_id"],
                    session["ui_msg_id"],
                    f"✅ <b>No new media files found.</b>\n\n"
                    f"📢 <b>Channel:</b> {session['channel_title']}\n"
                    f"📌 All files from msg ID <code>{start_from}</code> onwards already forwarded.",
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
    Long-running worker — pulls jobs from _gff_job_queue, copies each media
    message to MANUAL_FF, saves progress, updates UI.
    Completely isolated from caption and user file_forward workers.
    """
    while True:
        try:
            job = await asyncio.wait_for(_gff_job_queue.get(), timeout=1.0)
        except asyncio.TimeoutError:
            continue

        session_id = job["session_id"]

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

            await client.copy_message(
                chat_id=dest_id,
                from_chat_id=channel_id,
                message_id=msg_id,
            )

            session["forwarded"] = session.get("forwarded", 0) + 1
            forwarded = session["forwarded"]

            # Persist progress
            await save_global_ff_progress(channel_id, msg_id, forwarded)

            # Update UI every N files or when queue is empty
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
            print(f"[GFF_WORKER] FloodWait {wait}s ch={channel_id}")
            await asyncio.sleep(wait)
            await _gff_job_queue.put(job)   # re-queue
            _gff_job_queue.task_done()
            continue

        except Exception as e:
            print(f"[GFF_WORKER_ERR] msg_id={msg_id} ch={channel_id}: {e}")

        _gff_job_queue.task_done()

        await _gff_maybe_complete(client, job, session)


async def _gff_maybe_complete(client: Client, job: dict, session: dict):
    """Send the completion message when the session is fully done."""
    session_id = job["session_id"]

    if session_id in ADMIN_FF_CANCELLED:
        return
    if session_id in _gff_completed_sessions:
        return
    if not session.get("scan_done"):
        return
    if not _gff_job_queue.empty():
        return

    total     = session.get("total", 0)
    forwarded = session.get("forwarded", 0)
    if total > 0 and forwarded < total:
        return

    # Guard — only one worker fires the completion
    if session_id in _gff_completed_sessions:
        return
    _gff_completed_sessions.add(session_id)
    ADMIN_FF_SESSIONS.pop(session_id, None)

    try:
        await client.edit_message_text(
            job["chat_id"],
            job["ui_msg_id"],
            f"✅ <b>Forwarding completed!</b>\n\n"
            f"📢 <b>Source:</b> {job['channel_title']}\n"
            f"📥 <b>Destination:</b> {job['dest_title']}\n\n"
            f"📦 <b>Files forwarded:</b> <code>{forwarded}</code>\n"
            f"🗂 <b>Total detected:</b> <code>{total}</code>",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔙 Back to Channels", callback_data="adm_ch_back")]
            ]),
        )
    except Exception:
        pass

    async def _cleanup():
        await asyncio.sleep(60)
        _gff_completed_sessions.discard(session_id)
        ADMIN_FF_CANCELLED.discard(session_id)

    asyncio.create_task(_cleanup())


def _gff_progress_text(session: dict, pct: int) -> str:
    forwarded  = session.get("forwarded", 0)
    total      = session.get("total", 0)
    title      = session.get("channel_title", "Channel")
    dest       = session.get("dest_title", "Destination")
    bar_filled = int(pct / 10)
    bar        = "▓" * bar_filled + "░" * (10 - bar_filled)
    return (
        f"📤 <b>{title}</b>\n"
        f"         ⬇️⬇️⬇️\n"
        f"📥 <b>{dest}</b>\n\n"
        f"🔄 <b>Forwarding in progress…</b>\n\n"
        f"[{bar}] <code>{pct}%</code>\n"
        f"📦 <b>Forwarded:</b> <code>{forwarded}</code> / <code>{total}</code>"
    )
