"""
FileForward.py — File Forwarding System for CaptionBot
Handles: source/dest channel selection, skip/range input, queue-based forwarding,
FloodWait handling, auto-resume on restart, dump-channel integration.
"""

import re
import time
import asyncio
import logging
from typing import Optional

from pyrogram import Client, filters, errors
from pyrogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    CallbackQuery,
    Message,
)
from pyrogram.errors import FloodWait, RPCError

from info import ADMIN
from body.database import (
    db,
    users,
    get_user_channels,
    get_cached_chat_title,
    set_cached_chat_title,
)
from body.Caption import (
    _is_admin_member,
    build_smart_filename,
    strip_links_only,
    remove_emojis,
    clean_text,
    get_bot_me,
)

logger = logging.getLogger(__name__)

# ─── Constants ────────────────────────────────────────────────────────────────
MSG_LINK_RE = re.compile(r"(?:https?://)?t\.me/(?:c/(\d+)|([A-Za-z0-9_]+))/(\d+)")
PROGRESS_UPDATE_EVERY = 15   # update progress every N messages
FF_QUEUE_COL = None          # set in on_bot_start

# ─── In-memory active tasks {user_id: asyncio.Task} ──────────────────────────
_active_tasks: dict[int, asyncio.Task] = {}
_cancel_flags: dict[int, bool] = {}
# pending state while waiting for user text input
_ff_sessions: dict[int, dict] = {}   # user_id → session dict


# ─── DB helpers ───────────────────────────────────────────────────────────────

async def _get_ff_col():
    global FF_QUEUE_COL
    if FF_QUEUE_COL is None:
        from body.database import db as _db
        FF_QUEUE_COL = _db["ff_queue"]
        await FF_QUEUE_COL.create_index([("status", 1), ("ts", 1)])
        await FF_QUEUE_COL.create_index([("user_id", 1)])
    return FF_QUEUE_COL


async def _enqueue_ff_task(doc: dict):
    col = await _get_ff_col()
    await col.insert_one({**doc, "status": "pending", "ts": time.time()})


async def _get_pending_tasks():
    col = await _get_ff_col()
    return [d async for d in col.find({"status": "pending"})]


async def _mark_ff_running(task_id):
    col = await _get_ff_col()
    await col.update_one({"_id": task_id}, {"$set": {"status": "running", "started": time.time()}})


async def _mark_ff_done(task_id):
    col = await _get_ff_col()
    await col.delete_one({"_id": task_id})


async def _mark_ff_cancelled(task_id):
    col = await _get_ff_col()
    await col.delete_one({"_id": task_id})


async def _update_ff_progress(task_id, forwarded: int, total: int):
    col = await _get_ff_col()
    await col.update_one({"_id": task_id}, {"$set": {"forwarded": forwarded, "total": total}})


# ─── Utility ──────────────────────────────────────────────────────────────────

def _parse_msg_link(text: str) -> Optional[int]:
    """Extract message ID from a t.me link or plain integer."""
    text = text.strip()
    m = MSG_LINK_RE.search(text)
    if m:
        return int(m.group(3))
    if text.isdigit():
        return int(text)
    return None


async def _get_admin_channels(client: Client, user_id: int) -> list[dict]:
    """Return list of channels where the bot is admin (from user's channel list)."""
    raw_channels = await get_user_channels(user_id)
    result = []
    for ch in raw_channels:
        ch_id = ch.get("channel_id")
        ch_title = ch.get("channel_title", str(ch_id))
        cached = get_cached_chat_title(ch_id)
        if cached:
            ch_title = cached
        try:
            member = await client.get_chat_member(ch_id, "me")
            if _is_admin_member(member):
                try:
                    chat = await client.get_chat(ch_id)
                    ch_title = getattr(chat, "title", ch_title)
                    set_cached_chat_title(ch_id, ch_title)
                except Exception:
                    pass
                result.append({"id": ch_id, "title": ch_title})
        except Exception:
            pass
    return result


def _channel_buttons(channels: list[dict], cb_prefix: str, cancel_cb: str) -> InlineKeyboardMarkup:
    buttons = []
    for i in range(0, len(channels), 2):
        row = []
        for ch in channels[i:i+2]:
            row.append(InlineKeyboardButton(
                ch["title"][:30],
                callback_data=f"{cb_prefix}{ch['id']}"
            ))
        buttons.append(row)
    buttons.append([InlineKeyboardButton("❌ Cancel", callback_data=cancel_cb)])
    return InlineKeyboardMarkup(buttons)


def _progress_bar(done: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "▱" * width
    filled = int(width * done / total)
    return "▰" * filled + "▱" * (width - filled)


def _build_progress_text(forwarded: int, total: int, skipped: int = 0, eta_s: float = 0) -> str:
    pct = int(100 * forwarded / total) if total else 0
    bar = _progress_bar(forwarded, total)
    remaining = max(0, total - forwarded)
    eta_str = f"{int(eta_s // 60)}m {int(eta_s % 60)}s" if eta_s else "–"
    return (
        f"<b>📤 Forwarding in Progress</b>\n\n"
        f"{bar}  <b>{pct}%</b>\n\n"
        f"✅ Forwarded: <code>{forwarded}</code>\n"
        f"⏭ Skipped: <code>{skipped}</code>\n"
        f"📨 Total: <code>{total}</code>\n"
        f"⏳ Remaining: <code>{remaining}</code>\n"
        f"🕐 ETA: <code>{eta_str}</code>"
    )


# ─── Import FF_CH from info ───────────────────────────────────────────────────
def _get_ff_ch():
    try:
        from info import FF_CH
        return int(FF_CH) if FF_CH else None
    except Exception:
        return None


# ─── /file_forward command ────────────────────────────────────────────────────

@Client.on_message(filters.command("file_forward") & filters.private & filters.user(ADMIN))
async def cmd_file_forward(client: Client, message: Message):
    user_id = message.from_user.id

    # Block if already running
    if user_id in _active_tasks and not _active_tasks[user_id].done():
        return await message.reply_text(
            "⚠️ A forwarding task is already running.\n"
            "Use the <b>Cancel</b> button in the progress message to stop it first.",
            parse_mode="html"
        )

    loading = await message.reply_text("⏳ Loading your channels…")
    channels = await _get_admin_channels(client, user_id)

    if not channels:
        return await loading.edit_text(
            "❌ No channels found where the bot is admin.\n"
            "Add the bot as admin to at least one channel first."
        )

    markup = _channel_buttons(channels, "ff_src_", "ff_cancel_main")
    await loading.edit_text(
        "<b>📥 Select Source Channel</b>\n\n"
        "Choose the channel to forward files <b>from</b>:",
        reply_markup=markup,
        parse_mode="html"
    )


# ─── Source channel selected ──────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^ff_src_(-?\d+)$") & filters.user(ADMIN))
async def cb_ff_source(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    src_id = int(query.data.split("_")[2])

    # Verify bot is still admin
    try:
        member = await client.get_chat_member(src_id, "me")
        if not _is_admin_member(member):
            return await query.answer("Bot is no longer admin in that channel!", show_alert=True)
    except Exception:
        return await query.answer("Could not verify bot membership!", show_alert=True)

    try:
        src_chat = await client.get_chat(src_id)
        src_title = src_chat.title or str(src_id)
    except Exception:
        src_title = str(src_id)

    # Save source to session
    _ff_sessions[user_id] = {"src_id": src_id, "src_title": src_title}

    channels = await _get_admin_channels(client, user_id)
    # Exclude source from destination list
    channels = [c for c in channels if c["id"] != src_id]

    if not channels:
        return await query.message.edit_text(
            "❌ No other admin channels available as destination.\n"
            "Add the bot as admin to at least one other channel."
        )

    markup = _channel_buttons(channels, "ff_dst_", "ff_cancel_main")
    await query.message.edit_text(
        f"<b>📤 Select Destination Channel</b>\n\n"
        f"Source: <b>{src_title}</b>\n\n"
        f"Choose the channel to forward files <b>to</b>:",
        reply_markup=markup,
        parse_mode="html"
    )
    await query.answer()


# ─── Destination channel selected ─────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^ff_dst_(-?\d+)$") & filters.user(ADMIN))
async def cb_ff_dest(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    dst_id = int(query.data.split("_")[2])

    session = _ff_sessions.get(user_id)
    if not session:
        return await query.message.edit_text("❌ Session expired. Run /file_forward again.")

    try:
        dst_chat = await client.get_chat(dst_id)
        dst_title = dst_chat.title or str(dst_id)
    except Exception:
        dst_title = str(dst_id)

    session["dst_id"] = dst_id
    session["dst_title"] = dst_title
    session["awaiting"] = "skip"

    cancel_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel_main")]
    ])

    await query.message.edit_text(
        f"<b>⏭ Enter Skip / Range</b>\n\n"
        f"Source: <b>{session['src_title']}</b>\n"
        f"Destination: <b>{dst_title}</b>\n\n"
        f"Send one of the following:\n\n"
        f"• <code>0</code> — Forward ALL files\n"
        f"• <code>msg_id</code> or <code>msg_link</code> — Forward from that message to end\n"
        f"• <code>start_id - end_id</code> — Forward specific range\n\n"
        f"<i>You can paste t.me links or plain message IDs.</i>",
        reply_markup=cancel_markup,
        parse_mode="html"
    )
    await query.answer()


# ─── Text input: skip / range ─────────────────────────────────────────────────

@Client.on_message(filters.private & filters.user(ADMIN) & filters.text)
async def ff_text_input(client: Client, message: Message):
    user_id = message.from_user.id
    session = _ff_sessions.get(user_id)

    if not session or session.get("awaiting") != "skip":
        return  # not our input; let other handlers deal with it

    text = message.text.strip()
    _ff_sessions.pop(user_id, None)

    src_id    = session["src_id"]
    src_title = session["src_title"]
    dst_id    = session["dst_id"]
    dst_title = session["dst_title"]

    start_id: Optional[int] = None
    end_id:   Optional[int] = None

    # Parse input
    # Case: "start - end"
    range_m = re.match(
        r"^(.+?)\s*[-–]\s*(.+)$", text
    )
    if range_m:
        s = _parse_msg_link(range_m.group(1))
        e = _parse_msg_link(range_m.group(2))
        if s and e:
            start_id = min(s, e)
            end_id   = max(s, e)
        else:
            await message.delete()
            return await message.reply_text("❌ Could not parse range. Try again with /file_forward.")
    elif text == "0":
        start_id = None   # will fetch from 1 to latest
        end_id   = None
    else:
        mid = _parse_msg_link(text)
        if mid:
            start_id = mid
            end_id   = None   # forward from mid to latest
        else:
            await message.delete()
            return await message.reply_text("❌ Invalid input. Send 0, a message ID, or start-end range.")

    await message.delete()

    # Get the latest message ID in source channel to know total
    try:
        last_msgs = await client.get_messages(src_id, [999_999_999])
    except Exception:
        last_msgs = []

    try:
        history = client.get_chat_history(src_id, limit=1)
        last_msg = None
        async for m in history:
            last_msg = m
        last_id = last_msg.id if last_msg else 1
    except Exception:
        last_id = 1

    if start_id is None:
        start_id = 1
    if end_id is None:
        end_id = last_id

    if start_id > end_id:
        return await message.reply_text("❌ Start ID is greater than end ID. Please check and try again.")

    total = end_id - start_id + 1

    # Enqueue to DB for persistence
    task_doc = {
        "user_id": user_id,
        "src_id":  src_id,
        "dst_id":  dst_id,
        "start_id": start_id,
        "end_id":   end_id,
        "total":    total,
        "forwarded": 0,
    }
    col = await _get_ff_col()
    result = await col.insert_one({**task_doc, "status": "pending", "ts": time.time()})
    task_id = result.inserted_id

    cancel_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 Cancel", callback_data=f"ff_cancel_{user_id}")]
    ])
    progress_msg = await message.reply_text(
        _build_progress_text(0, total),
        reply_markup=cancel_markup,
        parse_mode="html"
    )

    # Launch forwarding task
    _cancel_flags[user_id] = False
    task = asyncio.create_task(
        _run_forwarding(client, task_id, task_doc, progress_msg)
    )
    _active_tasks[user_id] = task


# ─── Cancel callbacks ─────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^ff_cancel_main$") & filters.user(ADMIN))
async def cb_ff_cancel_main(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    _ff_sessions.pop(user_id, None)
    await query.message.edit_text("❌ File forwarding cancelled.")
    await query.answer("Cancelled")


@Client.on_callback_query(filters.regex(r"^ff_cancel_(\d+)$") & filters.user(ADMIN))
async def cb_ff_cancel_task(client: Client, query: CallbackQuery):
    user_id = int(query.data.split("_")[2])
    if user_id != query.from_user.id:
        return await query.answer("Not your task!", show_alert=True)

    _cancel_flags[user_id] = True
    await query.answer("Cancellation requested…", show_alert=False)


# ─── Core forwarding worker ───────────────────────────────────────────────────

async def _run_forwarding(
    client: Client,
    task_id,
    task: dict,
    progress_msg: Message,
):
    user_id  = task["user_id"]
    src_id   = task["src_id"]
    dst_id   = task["dst_id"]
    start_id = task["start_id"]
    end_id   = task["end_id"]
    total    = task["end_id"] - task["start_id"] + 1

    await _mark_ff_running(task_id)

    forwarded = task.get("forwarded", 0)
    skipped   = 0
    failed    = 0
    t_start   = time.time()

    BATCH = 200  # fetch messages in chunks to avoid limits

    ff_ch = _get_ff_ch()

    cancel_markup = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 Cancel", callback_data=f"ff_cancel_{user_id}")]
    ])

    current_id = start_id + forwarded  # resume support

    try:
        while current_id <= end_id:
            if _cancel_flags.get(user_id):
                await _mark_ff_cancelled(task_id)
                await _safe_edit(
                    progress_msg,
                    f"🛑 <b>Forwarding Cancelled</b>\n\n"
                    f"✅ Forwarded: <code>{forwarded}</code>\n"
                    f"⏭ Skipped: <code>{skipped}</code>",
                    parse_mode="html"
                )
                return

            # Fetch a batch
            batch_end  = min(current_id + BATCH - 1, end_id)
            msg_ids    = list(range(current_id, batch_end + 1))

            try:
                messages = await client.get_messages(src_id, msg_ids)
            except FloodWait as e:
                await asyncio.sleep(e.value + 2)
                continue
            except Exception as e:
                logger.error(f"[FF] get_messages error: {e}")
                await asyncio.sleep(5)
                current_id = batch_end + 1
                continue

            for msg in messages:
                if _cancel_flags.get(user_id):
                    break

                if not msg or msg.empty or msg.service:
                    skipped += 1
                    current_id += 1
                    continue

                # Skip non-media messages (text-only)
                has_media = any(
                    getattr(msg, ft, None)
                    for ft in ("document", "video", "audio", "photo", "voice", "video_note", "sticker", "animation")
                )
                if not has_media:
                    skipped += 1
                    current_id += 1
                    continue

                # Forward to destination
                success = await _forward_one(client, msg, dst_id)
                if success:
                    forwarded += 1
                    # Send to FF dump channel (not during admin forwarding)
                    if ff_ch:
                        await _send_to_ff_dump(client, msg, ff_ch)
                else:
                    failed += 1

                current_id += 1

                # Update progress every N messages
                if (forwarded + skipped) % PROGRESS_UPDATE_EVERY == 0:
                    elapsed = time.time() - t_start
                    rate = forwarded / elapsed if elapsed > 0 else 0
                    remaining = max(0, total - forwarded - skipped - failed)
                    eta = (remaining / rate) if rate > 0 else 0
                    await _update_ff_progress(task_id, forwarded, total)
                    await _safe_edit(
                        progress_msg,
                        _build_progress_text(forwarded, total, skipped, eta),
                        reply_markup=cancel_markup,
                        parse_mode="html"
                    )

            current_id = batch_end + 1

        await _mark_ff_done(task_id)
        elapsed = int(time.time() - t_start)
        m, s = divmod(elapsed, 60)
        await _safe_edit(
            progress_msg,
            f"🎉 <b>Forwarding Completed!</b>\n\n"
            f"✅ Forwarded: <code>{forwarded}</code>\n"
            f"⏭ Skipped: <code>{skipped}</code>\n"
            f"❌ Failed: <code>{failed}</code>\n"
            f"⏱ Time Taken: <code>{m}m {s}s</code>",
            parse_mode="html"
        )

    except Exception as e:
        logger.error(f"[FF] Unexpected error: {e}", exc_info=True)
        await _mark_ff_done(task_id)
        await _safe_edit(
            progress_msg,
            f"❌ <b>Forwarding Error</b>\n\n<code>{e}</code>",
            parse_mode="html"
        )
    finally:
        _active_tasks.pop(user_id, None)
        _cancel_flags.pop(user_id, None)


async def _forward_one(client: Client, msg: Message, dst_id: int, retries: int = 0) -> bool:
    """Copy a single message to dst_id. Returns True on success."""
    try:
        await client.copy_message(
            chat_id=dst_id,
            from_chat_id=msg.chat.id,
            message_id=msg.id,
        )
        return True
    except FloodWait as e:
        wait = e.value + 2
        await asyncio.sleep(wait)
        return await _forward_one(client, msg, dst_id, retries)
    except errors.MessageIdInvalid:
        return False
    except Exception as e:
        logger.warning(f"[FF] copy_message failed: {e}")
        if retries < 3:
            await asyncio.sleep(3)
            return await _forward_one(client, msg, dst_id, retries + 1)
        return False


async def _send_to_ff_dump(client: Client, msg: Message, ff_ch: int):
    """Send media to FF dump channel with smart filename as caption (no links/emojis)."""
    try:
        # Get file metadata
        file_name = None
        for ft in ("document", "video", "audio", "voice"):
            obj = getattr(msg, ft, None)
            if obj:
                file_name = getattr(obj, "file_name", None)
                break

        if not file_name:
            file_name = "File"

        raw_caption = msg.caption or ""
        # Strip links and emojis from the original caption
        clean_cap = strip_links_only(raw_caption)
        clean_cap = remove_emojis(clean_cap)

        # Build smart filename as the dump caption
        smart_name = build_smart_filename(file_name, clean_cap)
        if not smart_name:
            smart_name = clean_text(file_name)

        await client.copy_message(
            chat_id=ff_ch,
            from_chat_id=msg.chat.id,
            message_id=msg.id,
            caption=smart_name,
        )
    except FloodWait as e:
        await asyncio.sleep(e.value + 2)
        await _send_to_ff_dump(client, msg, ff_ch)
    except Exception as e:
        logger.warning(f"[FF] dump send failed: {e}")


async def _safe_edit(msg: Message, text: str, **kwargs):
    try:
        await msg.edit_text(text, **kwargs)
    except errors.MessageNotModified:
        pass
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        await _safe_edit(msg, text, **kwargs)
    except Exception as e:
        logger.warning(f"[FF] edit failed: {e}")


# ─── Auto-resume pending tasks on bot start ───────────────────────────────────

async def resume_pending_ff_tasks(client: Client):
    """Called at bot startup to resume any interrupted forwarding tasks."""
    try:
        col = await _get_ff_col()
        # Reset stuck 'running' tasks back to pending
        await col.update_many(
            {"status": "running"},
            {"$set": {"status": "pending"}}
        )
        pending = [d async for d in col.find({"status": "pending"})]
        if not pending:
            return
        logger.info(f"[FF] Resuming {len(pending)} pending forwarding task(s)…")
        for task_doc in pending:
            task_id = task_doc["_id"]
            user_id = task_doc["user_id"]
            # Send a resume notification
            try:
                resume_msg = await client.send_message(
                    user_id,
                    f"♻️ <b>Resuming interrupted forwarding task</b>\n\n"
                    f"Source ID: <code>{task_doc['src_id']}</code>\n"
                    f"Destination ID: <code>{task_doc['dst_id']}</code>\n"
                    f"Progress: <code>{task_doc.get('forwarded', 0)}</code> / <code>{task_doc.get('total', '?')}</code>",
                    parse_mode="html",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🛑 Cancel", callback_data=f"ff_cancel_{user_id}")]
                    ])
                )
            except Exception:
                # Admin can't be messaged; skip resume
                await _mark_ff_done(task_id)
                continue

            _cancel_flags[user_id] = False
            t = asyncio.create_task(
                _run_forwarding(client, task_id, task_doc, resume_msg)
            )
            _active_tasks[user_id] = t

    except Exception as e:
        logger.error(f"[FF] resume_pending_ff_tasks error: {e}", exc_info=True)


# ─── /ff_status command ───────────────────────────────────────────────────────

@Client.on_message(filters.command("ff_status") & filters.private & filters.user(ADMIN))
async def cmd_ff_status(client: Client, message: Message):
    col = await _get_ff_col()
    running   = [d async for d in col.find({"status": "running"})]
    pending   = [d async for d in col.find({"status": "pending"})]
    active_in_memory = [uid for uid, t in _active_tasks.items() if not t.done()]

    if not running and not pending and not active_in_memory:
        return await message.reply_text("✅ No active or pending file forwarding tasks.")

    lines = ["<b>📊 File Forwarding Status</b>\n"]

    for doc in running:
        pct = int(100 * doc.get("forwarded", 0) / doc.get("total", 1)) if doc.get("total") else 0
        lines.append(
            f"▶️ <b>Running</b>\n"
            f"  Src: <code>{doc['src_id']}</code> → Dst: <code>{doc['dst_id']}</code>\n"
            f"  Progress: <code>{doc.get('forwarded', 0)}/{doc.get('total', '?')}</code> ({pct}%)"
        )

    for doc in pending:
        lines.append(
            f"⏳ <b>Pending</b>\n"
            f"  Src: <code>{doc['src_id']}</code> → Dst: <code>{doc['dst_id']}</code>\n"
            f"  Range: <code>{doc.get('start_id')}–{doc.get('end_id')}</code>"
        )

    await message.reply_text("\n\n".join(lines), parse_mode="html")


# ─── Startup hook (called by bot.py plugin loader) ────────────────────────────

def on_bot_start(client: Client):
    asyncio.create_task(resume_pending_ff_tasks(client))
