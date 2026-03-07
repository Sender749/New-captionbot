"""
FileForward.py — File Forwarding System for CaptionBot
Works for ALL users. Skip/range input is routed via _ff_sessions.
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
from pyrogram.errors import FloodWait, ChatAdminRequired, RPCError

from body.database import (
    db,
    get_user_channels,
    get_cached_chat_title,
    set_cached_chat_title,
    users,
)
from body.Caption import (
    _is_admin_member,
    build_smart_filename,
    strip_links_only,
    remove_emojis,
    clean_text,
)

# ── Logging — visible in Koyeb/stdout ────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("FileForward")

MSG_LINK_RE = re.compile(r"(?:https?://)?t\.me/(?:c/(\d+)|([A-Za-z0-9_]+))/(\d+)")
PROGRESS_UPDATE_EVERY = 15

_active_tasks: dict = {}
_cancel_flags: dict = {}
_ff_sessions: dict = {}   # shared — Caption.py's capture_user_input checks this

_FF_COL = None


# ── DB helpers ────────────────────────────────────────────────────────────────

async def _get_ff_col():
    global _FF_COL
    if _FF_COL is None:
        _FF_COL = db["ff_queue"]
        await _FF_COL.create_index([("status", 1), ("ts", 1)])
        await _FF_COL.create_index([("user_id", 1)])
    return _FF_COL


async def _mark_ff_running(task_id):
    col = await _get_ff_col()
    await col.update_one({"_id": task_id},
                         {"$set": {"status": "running", "started": time.time()}})


async def _mark_ff_done(task_id):
    col = await _get_ff_col()
    await col.delete_one({"_id": task_id})


async def _update_ff_progress(task_id, forwarded: int, current_id: int):
    col = await _get_ff_col()
    await col.update_one({"_id": task_id},
                         {"$set": {"forwarded": forwarded, "current_id": current_id}})


# ── Utilities ─────────────────────────────────────────────────────────────────

def _parse_msg_ref(text: str) -> Optional[int]:
    text = text.strip()
    m = MSG_LINK_RE.search(text)
    if m:
        return int(m.group(3))
    if text.isdigit():
        return int(text)
    return None


async def _get_bot_admin_channels(client: Client, user_id: int) -> list:
    """
    Return list of {id, title} dicts for channels where:
      - the user has registered the channel (via /settings)
      - the bot is currently an admin

    Uses asyncio.gather for parallel checks (fast even with many channels).
    Mirrors the logic of Caption.py's user_settings() which is known to work.
    """
    logger.info(f"[FF] _get_bot_admin_channels called for user_id={user_id}")
    raw = await get_user_channels(user_id)
    logger.info(f"[FF] get_user_channels returned {len(raw)} raw channel(s): {raw}")

    if not raw:
        logger.info(f"[FF] No channels in DB for user {user_id}")
        return []

    async def _check(ch):
        ch_id    = ch.get("channel_id")
        ch_title = ch.get("channel_title", str(ch_id))

        cached = get_cached_chat_title(ch_id)
        if cached:
            ch_title = cached

        try:
            member = await client.get_chat_member(ch_id, "me")
            logger.info(f"[FF] ch={ch_id} member_status={getattr(member, 'status', '?')}")
        except (ChatAdminRequired, RPCError) as e:
            logger.warning(f"[FF] ch={ch_id} get_chat_member RPC error: {e} — removing from user")
            await users.update_one({"_id": user_id}, {"$pull": {"channels": {"channel_id": ch_id}}})
            return None
        except Exception as e:
            # Don't remove — might be a transient error; still include the channel
            logger.warning(f"[FF] ch={ch_id} get_chat_member unexpected error: {e} — keeping channel")
            return {"id": ch_id, "title": ch_title}

        if not _is_admin_member(member):
            logger.info(f"[FF] ch={ch_id} bot is NOT admin — skipping")
            await users.update_one({"_id": user_id}, {"$pull": {"channels": {"channel_id": ch_id}}})
            return None

        # Bot is admin — refresh title if not cached
        if not cached:
            try:
                chat = await client.get_chat(ch_id)
                ch_title = getattr(chat, "title", ch_title) or ch_title
                set_cached_chat_title(ch_id, ch_title)
            except Exception as e:
                logger.warning(f"[FF] ch={ch_id} get_chat error: {e}")

        logger.info(f"[FF] ch={ch_id} '{ch_title}' — bot is admin ✓")
        return {"id": ch_id, "title": ch_title}

    results = await asyncio.gather(*[_check(ch) for ch in raw])
    valid = [r for r in results if r is not None]
    logger.info(f"[FF] _get_bot_admin_channels returning {len(valid)} valid channel(s)")
    return valid


def _channel_keyboard(channels: list, cb_prefix: str, cancel_cb: str) -> InlineKeyboardMarkup:
    rows = []
    for i in range(0, len(channels), 2):
        row = []
        for ch in channels[i:i + 2]:
            row.append(InlineKeyboardButton(
                ch["title"][:28],
                callback_data=f"{cb_prefix}{ch['id']}"
            ))
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=cancel_cb)])
    return InlineKeyboardMarkup(rows)


def _progress_bar(done: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return "▱" * width
    filled = min(width, int(width * done / total))
    return "▰" * filled + "▱" * (width - filled)


def _progress_text(forwarded: int, total: int, skipped: int = 0, eta_s: float = 0) -> str:
    pct = int(100 * forwarded / total) if total else 0
    bar = _progress_bar(forwarded, total)
    remaining = max(0, total - forwarded - skipped)
    eta_str = f"{int(eta_s // 60)}m {int(eta_s % 60)}s" if eta_s > 0 else "–"
    return (
        f"<b>📤 Forwarding in Progress</b>\n\n"
        f"{bar}  <b>{pct}%</b>\n\n"
        f"✅ Forwarded : <code>{forwarded}</code>\n"
        f"⏭ Skipped   : <code>{skipped}</code>\n"
        f"📨 Total     : <code>{total}</code>\n"
        f"⏳ Remaining : <code>{remaining}</code>\n"
        f"🕐 ETA       : <code>{eta_str}</code>"
    )


def _get_ff_ch():
    try:
        from info import FF_CH
        val = int(FF_CH) if FF_CH else None
        logger.debug(f"[FF] FF_CH={val}")
        return val
    except Exception as e:
        logger.warning(f"[FF] FF_CH read error: {e}")
        return None


# ── /file_forward command ─────────────────────────────────────────────────────

@Client.on_message(filters.command("file_forward") & filters.private, group=-1)
async def cmd_file_forward(client: Client, message: Message):
    user_id = message.from_user.id
    logger.info(f"[FF] /file_forward from user_id={user_id}")

    task = _active_tasks.get(user_id)
    if task and not task.done():
        logger.info(f"[FF] user {user_id} already has a running task")
        return await message.reply_text(
            "⚠️ <b>You already have a forwarding task running.</b>\n"
            "Press <b>🛑 Cancel</b> on the progress message to stop it first.",
            parse_mode="html"
        )

    loading = await message.reply_text("⏳ Loading your channels…")

    try:
        channels = await _get_bot_admin_channels(client, user_id)
    except Exception as e:
        logger.error(f"[FF] _get_bot_admin_channels crashed: {e}", exc_info=True)
        return await loading.edit_text(
            f"❌ <b>Error loading channels:</b> <code>{e}</code>",
            parse_mode="html"
        )

    logger.info(f"[FF] Got {len(channels)} channel(s) for user {user_id}: {channels}")

    if not channels:
        return await loading.edit_text(
            "❌ <b>No channels found.</b>\n\n"
            "Make sure the bot is added as admin to your channels "
            "and they appear in /settings.",
            parse_mode="html"
        )

    markup = _channel_keyboard(channels, "ff_src_", "ff_cancel_main")
    await loading.edit_text(
        "<b>📥 Step 1 — Select Source Channel</b>\n\n"
        "Choose the channel to forward files <b>from</b>:",
        reply_markup=markup,
        parse_mode="html"
    )
    logger.info(f"[FF] Source channel list shown to user {user_id}")


# ── Source selected ───────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^ff_src_-?\d+$"), group=-1)
async def cb_ff_source(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    src_id  = int(query.data[7:])   # strip "ff_src_"
    logger.info(f"[FF] Source selected: ch={src_id} by user={user_id}")

    try:
        member = await client.get_chat_member(src_id, "me")
        if not _is_admin_member(member):
            logger.warning(f"[FF] Bot not admin in src ch={src_id}")
            return await query.answer("Bot is no longer admin there!", show_alert=True)
    except Exception as e:
        logger.error(f"[FF] get_chat_member src={src_id}: {e}")
        return await query.answer(f"Cannot verify channel: {e}", show_alert=True)

    try:
        src_chat  = await client.get_chat(src_id)
        src_title = src_chat.title or str(src_id)
    except Exception as e:
        logger.warning(f"[FF] get_chat src={src_id}: {e}")
        src_title = str(src_id)

    _ff_sessions[user_id] = {"src_id": src_id, "src_title": src_title, "step": "dst"}

    try:
        channels = await _get_bot_admin_channels(client, user_id)
    except Exception as e:
        logger.error(f"[FF] _get_bot_admin_channels (dst list) crashed: {e}", exc_info=True)
        return await query.message.edit_text(f"❌ Error loading channels: <code>{e}</code>", parse_mode="html")

    channels = [c for c in channels if c["id"] != src_id]

    if not channels:
        _ff_sessions.pop(user_id, None)
        logger.info(f"[FF] No destination channels available for user {user_id}")
        return await query.message.edit_text(
            "❌ No other admin channels available as destination.\n"
            "Add the bot as admin to at least one more channel.",
            parse_mode="html"
        )

    markup = _channel_keyboard(channels, "ff_dst_", "ff_cancel_main")
    await query.message.edit_text(
        f"<b>📤 Step 2 — Select Destination Channel</b>\n\n"
        f"Source: <b>{src_title}</b>\n\n"
        f"Choose the channel to forward files <b>to</b>:",
        reply_markup=markup,
        parse_mode="html"
    )
    await query.answer()
    logger.info(f"[FF] Destination channel list shown to user {user_id}")


# ── Destination selected ──────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^ff_dst_-?\d+$"), group=-1)
async def cb_ff_dest(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    dst_id  = int(query.data[7:])   # strip "ff_dst_"
    logger.info(f"[FF] Destination selected: ch={dst_id} by user={user_id}")

    session = _ff_sessions.get(user_id)
    if not session or session.get("step") != "dst":
        logger.warning(f"[FF] No active dst session for user {user_id}")
        return await query.message.edit_text("❌ Session expired. Please run /file_forward again.")

    try:
        dst_chat  = await client.get_chat(dst_id)
        dst_title = dst_chat.title or str(dst_id)
    except Exception as e:
        logger.warning(f"[FF] get_chat dst={dst_id}: {e}")
        dst_title = str(dst_id)

    session["dst_id"]    = dst_id
    session["dst_title"] = dst_title
    session["step"]      = "skip"
    logger.info(f"[FF] Session for user {user_id}: src={session['src_id']} dst={dst_id}, awaiting skip input")

    await query.message.edit_text(
        f"<b>⏭ Step 3 — Enter Range / Skip Number</b>\n\n"
        f"Source : <b>{session['src_title']}</b>\n"
        f"Dest   : <b>{dst_title}</b>\n\n"
        f"<b>Now type one of these in this chat:</b>\n\n"
        f"• <code>0</code> — Forward <b>all</b> files\n"
        f"• <code>12345</code> or a message link — Start from that message to end\n"
        f"• <code>100 - 500</code> or two links — Forward that exact range\n\n"
        f"<i>Paste a t.me link or a plain message ID.</i>",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel_main")]
        ]),
        parse_mode="html"
    )
    await query.answer()


# ── Cancel ────────────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^ff_cancel_main$"), group=-1)
async def cb_ff_cancel_main(client: Client, query: CallbackQuery):
    user_id = query.from_user.id
    _ff_sessions.pop(user_id, None)
    logger.info(f"[FF] Session cancelled by user {user_id}")
    await query.message.edit_text("❌ File forwarding cancelled.")
    await query.answer("Cancelled")


@Client.on_callback_query(filters.regex(r"^ff_cancel_task_\d+$"), group=-1)
async def cb_ff_cancel_task(client: Client, query: CallbackQuery):
    uid = int(query.data[len("ff_cancel_task_"):])
    if uid != query.from_user.id:
        return await query.answer("This is not your task!", show_alert=True)
    _cancel_flags[uid] = True
    logger.info(f"[FF] Cancel flag set for user {uid}")
    await query.answer("⏹ Cancellation sent…")


# ── Skip input — called from Caption.py's capture_user_input ─────────────────

async def handle_ff_skip_input(client: Client, message: Message) -> bool:
    """
    Returns True if this message was consumed as a forwarding skip/range input.
    Caption.py's capture_user_input calls this FIRST.
    """
    user_id = message.from_user.id
    session = _ff_sessions.get(user_id)
    if not session or session.get("step") != "skip":
        return False

    text = (message.text or "").strip()
    if not text:
        return False

    logger.info(f"[FF] Skip input from user {user_id}: '{text}'")
    _ff_sessions.pop(user_id, None)   # consume immediately

    src_id    = session["src_id"]
    src_title = session["src_title"]
    dst_id    = session["dst_id"]
    dst_title = session["dst_title"]

    start_id: Optional[int] = None
    end_id:   Optional[int] = None

    # Range: "100 - 500"  or  "link1 - link2"
    sep = re.search(r"\s[-–]\s", text)
    if sep:
        parts = re.split(r"\s[-–]\s", text, maxsplit=1)
        s = _parse_msg_ref(parts[0])
        e = _parse_msg_ref(parts[1])
        if s and e:
            start_id, end_id = min(s, e), max(s, e)
            logger.info(f"[FF] Range parsed: {start_id}–{end_id}")
        else:
            logger.warning(f"[FF] Could not parse range from: {text}")
            try:
                await message.delete()
            except Exception:
                pass
            await message.reply_text(
                "❌ Could not parse the range.\n"
                "Use format: <code>100 - 500</code> or two t.me links separated by <code> - </code>",
                parse_mode="html"
            )
            return True
    elif text == "0":
        start_id = 1
        logger.info("[FF] Forward all: start_id=1")
    else:
        mid = _parse_msg_ref(text)
        if mid:
            start_id = mid
            logger.info(f"[FF] Start from msg_id={mid}")
        else:
            logger.warning(f"[FF] Invalid skip input: '{text}'")
            try:
                await message.delete()
            except Exception:
                pass
            await message.reply_text(
                "❌ Invalid input.\nSend <code>0</code>, a message ID, a t.me link, "
                "or <code>start - end</code>.",
                parse_mode="html"
            )
            return True

    # Resolve end_id to last message in source channel
    if end_id is None:
        try:
            async for last in client.get_chat_history(src_id, limit=1):
                end_id = last.id
            logger.info(f"[FF] Resolved end_id={end_id} from channel history")
        except Exception as e:
            logger.error(f"[FF] get_chat_history src={src_id}: {e}")
            end_id = start_id

    if start_id > end_id:
        try:
            await message.delete()
        except Exception:
            pass
        await message.reply_text(
            f"❌ Start ID (<code>{start_id}</code>) > End ID (<code>{end_id}</code>).",
            parse_mode="html"
        )
        return True

    try:
        await message.delete()
    except Exception:
        pass

    total = end_id - start_id + 1
    logger.info(f"[FF] Task: src={src_id} dst={dst_id} range={start_id}-{end_id} total={total}")

    col = await _get_ff_col()
    task_doc = {
        "user_id":    user_id,
        "src_id":     src_id,
        "src_title":  src_title,
        "dst_id":     dst_id,
        "dst_title":  dst_title,
        "start_id":   start_id,
        "end_id":     end_id,
        "total":      total,
        "forwarded":  0,
        "current_id": start_id,
        "status":     "pending",
        "ts":         time.time(),
    }
    result  = await col.insert_one(task_doc)
    task_doc["_id"] = result.inserted_id
    logger.info(f"[FF] Task inserted to DB: _id={result.inserted_id}")

    cancel_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 Cancel", callback_data=f"ff_cancel_task_{user_id}")]
    ])
    progress_msg = await message.reply_text(
        _progress_text(0, total),
        reply_markup=cancel_kb,
        parse_mode="html"
    )

    _cancel_flags[user_id] = False
    t = asyncio.create_task(_run_forwarding(client, task_doc["_id"], task_doc, progress_msg))
    _active_tasks[user_id] = t
    logger.info(f"[FF] Forwarding task started for user {user_id}")
    return True


# ── Core forwarding worker ────────────────────────────────────────────────────

async def _run_forwarding(client: Client, task_id, task: dict, progress_msg: Message):
    user_id   = task["user_id"]
    src_id    = task["src_id"]
    dst_id    = task["dst_id"]
    start_id  = task["start_id"]
    end_id    = task["end_id"]
    total     = task["total"]

    logger.info(f"[FF] _run_forwarding started: user={user_id} src={src_id} dst={dst_id} {start_id}-{end_id}")
    await _mark_ff_running(task_id)

    forwarded  = task.get("forwarded", 0)
    skipped    = 0
    failed     = 0
    current_id = task.get("current_id", start_id)
    t_start    = time.time()
    ff_ch      = _get_ff_ch()

    cancel_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🛑 Cancel", callback_data=f"ff_cancel_task_{user_id}")]
    ])
    BATCH = 100

    try:
        while current_id <= end_id:
            if _cancel_flags.get(user_id):
                logger.info(f"[FF] Cancelled by user {user_id} at msg_id={current_id}")
                await _mark_ff_done(task_id)
                await _safe_edit(
                    progress_msg,
                    f"🛑 <b>Forwarding Cancelled</b>\n\n"
                    f"✅ Forwarded : <code>{forwarded}</code>\n"
                    f"⏭ Skipped   : <code>{skipped}</code>\n"
                    f"❌ Failed    : <code>{failed}</code>",
                    parse_mode="html"
                )
                return

            batch_end = min(current_id + BATCH - 1, end_id)
            msg_ids   = list(range(current_id, batch_end + 1))

            try:
                msgs = await client.get_messages(src_id, msg_ids)
            except FloodWait as e:
                logger.warning(f"[FF] FloodWait {e.value}s on get_messages")
                await asyncio.sleep(e.value + 2)
                continue
            except Exception as e:
                logger.error(f"[FF] get_messages src={src_id} ids={msg_ids[0]}-{msg_ids[-1]}: {e}")
                await asyncio.sleep(5)
                current_id = batch_end + 1
                continue

            for msg in msgs:
                if _cancel_flags.get(user_id):
                    break

                if not msg or msg.empty or msg.service:
                    skipped += 1
                    current_id += 1
                    continue

                has_media = any(
                    getattr(msg, ft, None)
                    for ft in ("document", "video", "audio", "photo",
                               "voice", "video_note", "sticker", "animation")
                )
                if not has_media:
                    skipped += 1
                    current_id += 1
                    continue

                ok = await _copy_one(client, msg, dst_id)
                if ok:
                    forwarded += 1
                    if ff_ch:
                        asyncio.create_task(_send_to_ff_dump(client, msg, ff_ch))
                else:
                    failed += 1
                    logger.warning(f"[FF] Failed to copy msg_id={msg.id}")

                current_id += 1

                done_count = forwarded + skipped + failed
                if done_count % PROGRESS_UPDATE_EVERY == 0 or current_id > end_id:
                    elapsed = time.time() - t_start
                    rate    = forwarded / elapsed if elapsed > 0 else 0
                    eta     = ((total - done_count) / rate) if rate > 0 else 0
                    await _update_ff_progress(task_id, forwarded, current_id)
                    await _safe_edit(
                        progress_msg,
                        _progress_text(forwarded, total, skipped, eta),
                        reply_markup=cancel_kb,
                        parse_mode="html"
                    )

            current_id = batch_end + 1

        await _mark_ff_done(task_id)
        elapsed = int(time.time() - t_start)
        m, s = divmod(elapsed, 60)
        logger.info(f"[FF] Completed: user={user_id} forwarded={forwarded} skipped={skipped} failed={failed} time={m}m{s}s")
        await _safe_edit(
            progress_msg,
            f"🎉 <b>Forwarding Completed!</b>\n\n"
            f"✅ Forwarded : <code>{forwarded}</code>\n"
            f"⏭ Skipped   : <code>{skipped}</code>\n"
            f"❌ Failed    : <code>{failed}</code>\n"
            f"⏱ Time Taken: <code>{m}m {s}s</code>",
            parse_mode="html"
        )

    except Exception as e:
        logger.error(f"[FF] _run_forwarding crashed: {e}", exc_info=True)
        try:
            await _mark_ff_done(task_id)
        except Exception:
            pass
        await _safe_edit(
            progress_msg,
            f"❌ <b>Forwarding Error</b>\n\n<code>{e}</code>",
            parse_mode="html"
        )
    finally:
        _active_tasks.pop(user_id, None)
        _cancel_flags.pop(user_id, None)


async def _copy_one(client: Client, msg: Message, dst_id: int, attempt: int = 0) -> bool:
    try:
        await client.copy_message(
            chat_id=dst_id,
            from_chat_id=msg.chat.id,
            message_id=msg.id,
        )
        return True
    except FloodWait as e:
        logger.warning(f"[FF] FloodWait {e.value}s on copy_message msg={msg.id}")
        await asyncio.sleep(e.value + 2)
        return await _copy_one(client, msg, dst_id, attempt)
    except (errors.MessageIdInvalid, errors.ChannelInvalid):
        return False
    except Exception as e:
        if attempt < 3:
            await asyncio.sleep(3)
            return await _copy_one(client, msg, dst_id, attempt + 1)
        logger.warning(f"[FF] copy_message failed after retries: {e}")
        return False


async def _send_to_ff_dump(client: Client, msg: Message, ff_ch: int):
    try:
        file_name = None
        for ft in ("document", "video", "audio", "voice", "animation"):
            obj = getattr(msg, ft, None)
            if obj:
                file_name = getattr(obj, "file_name", None)
                break
        if not file_name:
            file_name = "File"

        raw_cap    = msg.caption or ""
        clean_cap  = strip_links_only(raw_cap)
        clean_cap  = remove_emojis(clean_cap)
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
        logger.debug(f"[FF] dump send skipped msg={msg.id}: {e}")


async def _safe_edit(msg: Message, text: str, **kwargs):
    try:
        await msg.edit_text(text, **kwargs)
    except errors.MessageNotModified:
        pass
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        await _safe_edit(msg, text, **kwargs)
    except Exception as e:
        logger.debug(f"[FF] edit failed: {e}")


# ── Auto-resume on restart ────────────────────────────────────────────────────

async def resume_pending_ff_tasks(client: Client):
    try:
        col = await _get_ff_col()
        await col.update_many({"status": "running"}, {"$set": {"status": "pending"}})
        pending = [d async for d in col.find({"status": "pending"})]
        if not pending:
            logger.info("[FF] No pending forwarding tasks to resume")
            return
        logger.info(f"[FF] Resuming {len(pending)} interrupted forwarding task(s)")
        for task_doc in pending:
            task_id = task_doc["_id"]
            user_id = task_doc["user_id"]
            try:
                resume_msg = await client.send_message(
                    user_id,
                    f"♻️ <b>Resuming forwarding task</b>\n\n"
                    f"Source : <b>{task_doc.get('src_title', task_doc['src_id'])}</b>\n"
                    f"Dest   : <b>{task_doc.get('dst_title', task_doc['dst_id'])}</b>\n"
                    f"Progress: <code>{task_doc.get('forwarded', 0)}</code> / <code>{task_doc.get('total', '?')}</code>",
                    parse_mode="html",
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🛑 Cancel",
                                              callback_data=f"ff_cancel_task_{user_id}")]
                    ])
                )
                logger.info(f"[FF] Resume notification sent to user {user_id}")
            except Exception as e:
                logger.error(f"[FF] Could not message user {user_id} for resume: {e} — deleting task")
                await _mark_ff_done(task_id)
                continue

            _cancel_flags[user_id] = False
            t = asyncio.create_task(_run_forwarding(client, task_id, task_doc, resume_msg))
            _active_tasks[user_id] = t

    except Exception as e:
        logger.error(f"[FF] resume_pending_ff_tasks: {e}", exc_info=True)


# ── /ff_status ────────────────────────────────────────────────────────────────

@Client.on_message(filters.command("ff_status") & filters.private, group=-1)
async def cmd_ff_status(client: Client, message: Message):
    user_id = message.from_user.id
    col     = await _get_ff_col()
    running = [d async for d in col.find({"status": "running", "user_id": user_id})]
    pending = [d async for d in col.find({"status": "pending", "user_id": user_id})]
    task    = _active_tasks.get(user_id)

    if not running and not pending and (not task or task.done()):
        return await message.reply_text("✅ No active or pending forwarding tasks.")

    lines = ["<b>📊 Your Forwarding Tasks</b>\n"]
    for doc in running:
        total = doc.get("total", 0)
        fwd   = doc.get("forwarded", 0)
        pct   = int(100 * fwd / total) if total else 0
        lines.append(
            f"▶️ <b>Running</b>\n"
            f"  {doc.get('src_title', doc['src_id'])} → {doc.get('dst_title', doc['dst_id'])}\n"
            f"  {fwd}/{total} ({pct}%)"
        )
    for doc in pending:
        lines.append(
            f"⏳ <b>Pending</b>\n"
            f"  {doc.get('src_title', doc['src_id'])} → {doc.get('dst_title', doc['dst_id'])}\n"
            f"  Range: {doc.get('start_id')}–{doc.get('end_id')}"
        )

    await message.reply_text("\n\n".join(lines), parse_mode="html")


# ── Startup hook ──────────────────────────────────────────────────────────────

def on_bot_start(client: Client):
    logger.info("[FF] FileForward module loaded, scheduling resume task")
    asyncio.get_event_loop().create_task(resume_pending_ff_tasks(client))
