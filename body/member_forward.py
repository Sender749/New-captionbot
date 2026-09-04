"""
member_forward.py — /member_forward

Forwards files out of a channel where the BOT ITSELF is not a member/admin,
but a personal Telegram account (logged in via SESSION_STRING, see
body/user_session.py) is — into any channel this bot already administers.

This is impossible for a bot account alone: Telegram never lets a bot see
messages in a chat it hasn't been explicitly added to. The userbot client
reads (and where possible, directly copies) the messages instead.

Flow (admin-only, private chat)
────────────────────────────────────────────────────────────────────────────
  /member_forward
    → send the SOURCE channel: @username / t.me link / invite link / -100 ID
      (must be a chat the userbot account is already a member of)
    → pick the DESTINATION from this bot's existing admin channel list
    → enter a range: 0 (all) / a start id-or-link / "start - end"
    → background scan (via the userbot) queues one job per media message
    → a small worker pool forwards them into the destination

Send path per file
────────────────────────────────────────────────────────────────────────────
  1. Fast path: the userbot directly copies the message into the
     destination (works whenever that personal account also has send
     rights there — the common case, since the same person usually owns
     both the source and destination channels).
  2. Fallback: if the userbot can't write to the destination, it downloads
     the media instead and the BOT client re-uploads it there — slower,
     but works regardless of the userbot's rights in the destination.

Kept deliberately isolated from file_forward.py: separate in-memory state,
separate Mongo collection (member_forward_queue), separate worker pool.
A problem in this feature (bad SESSION_STRING, userbot FloodWaits, the
download/re-upload fallback) can never stall or corrupt a normal
/file_forward or /channels job.
"""

import asyncio
import os
import time
import uuid
from collections import defaultdict

from pyrogram import Client, enums, filters
from pyrogram.errors import FloodWait
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from body.database import (
    MEMBER_FORWARD_WORKERS,
    MAX_MEMBER_FORWARD_PER_PAIR,
    MEMBER_FORWARD_DELAY,
    enqueue_member_forward,
    member_forward_done,
    member_forward_retry,
    member_forward_queue,
)
from body.user_session import user_client, USER_SESSION_ENABLED
from body.file_forward import clean_text, parse_forward_input, _edit_with_retry
from body.dump_direction import _get_all_registered_channels
from info import ADMIN

# ── in-memory state ──────────────────────────────────────────────────────────
MF_ACTIVE   = defaultdict(int)   # (src, dst) -> active worker count
MF_COOLDOWN = {}                 # (src, dst) -> unblock timestamp

MF_SESSIONS           = {}       # uid -> session dict
MF_CANCELLED_SESSIONS = set()    # session_ids that were cancelled

_MF_PROGRESS_EVERY = 3
_mf_session_done_count: dict = defaultdict(int)
_mf_session_completed: set = set()

CHANNELS_PER_PAGE = 10   # same page size as /dump_change's picker

_ADMIN_FILTER = filters.user(ADMIN)


# ── startup hook ──────────────────────────────────────────────────────────────
def on_bot_start(client: Client):
    """Launch the member-forward worker pool. No-op if SESSION_STRING isn't
    set — the /member_forward command itself also checks this and tells the
    admin why, so this just avoids spinning up workers that can never do
    anything."""
    if not USER_SESSION_ENABLED:
        print("[MF] SESSION_STRING not set — member-forward workers not started")
        return

    async def _guarded(i):
        while True:
            try:
                await member_forward_worker(client)
            except Exception as e:
                print(f"[MF_WORKER_{i}] crashed unexpectedly, restarting in 3s: {e}")
            else:
                print(f"[MF_WORKER_{i}] exited unexpectedly, restarting in 3s")
            await asyncio.sleep(3)

    for i in range(MEMBER_FORWARD_WORKERS):
        asyncio.create_task(_guarded(i), name=f"mf_worker_{i}")
    print(f"[MF] {MEMBER_FORWARD_WORKERS} member-forward workers started")


# ── /member_forward ───────────────────────────────────────────────────────────
@Client.on_message(filters.private & _ADMIN_FILTER & filters.command("member_forward"))
async def mf_start(client: Client, message):
    if not USER_SESSION_ENABLED:
        return await message.reply_text(
            "❌ <b>Member-channel forwarding isn't set up.</b>\n\n"
            "Set the <code>SESSION_STRING</code> environment variable to a "
            "Pyrogram session string for a personal Telegram account that's "
            "a member of the channel you want to pull files from, then "
            "restart the bot.",
            parse_mode=ParseMode.HTML,
        )

    uid = message.from_user.id
    MF_SESSIONS[uid] = {
        "step": "await_source",
        "expires": time.time() + 900,
    }
    await message.reply_text(
        "📡 <b>Member-Channel Forward</b>\n\n"
        "Send the <b>source channel</b> — one your personal account (the "
        "userbot) is a member/admin of, but this bot is <u>not</u> added to.\n\n"
        "You can send:\n"
        "• a <code>@username</code>\n"
        "• a <code>t.me/...</code> link or invite link\n"
        "• a numeric channel ID (e.g. <code>-1001234567890</code>)\n\n"
        "• Session expires in <b>15 minutes</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="mf_cancel")]]),
    )


# ── text dispatch (called from Caption.py's catch-all handler) ───────────────
async def handle_member_forward_text(client: Client, message) -> bool:
    """
    Returns True if this message was consumed by an active member-forward
    session step (caller should stop further processing), False otherwise.
    """
    uid = message.from_user.id
    s = MF_SESSIONS.get(uid)
    if not s:
        return False

    if s.get("expires") and s["expires"] < time.time():
        MF_SESSIONS.pop(uid, None)
        await message.reply_text("⏰ Session expired.\nStart again using /member_forward")
        return True

    step = s.get("step")
    if step == "await_source":
        await _handle_source_input(client, message, uid, s)
        return True
    if step == "skip":
        await _handle_range_input(client, message, uid, s)
        return True
    return False


async def _handle_source_input(client: Client, message, uid: int, s: dict):
    raw = (message.text or "").strip()
    if not raw:
        return
    try:
        chat = await user_client.get_chat(raw)
    except FloodWait as e:
        await message.reply_text(
            f"⏳ Telegram is rate-limiting the userbot account right now.\n"
            f"Please wait <b>{int(e.value)}s</b> and send the channel again.",
            parse_mode=ParseMode.HTML,
        )
        return
    except Exception as e:
        await message.reply_text(
            "❌ Couldn't access that channel with the userbot account.\n\n"
            f"Error: <code>{clean_text(str(e))}</code>\n\n"
            "Make sure the personal account behind SESSION_STRING is a "
            "member of that channel, then try again — or /member_forward "
            "to restart.",
            parse_mode=ParseMode.HTML,
        )
        return

    if chat.type not in (enums.ChatType.CHANNEL, enums.ChatType.SUPERGROUP):
        await message.reply_text("❌ That's not a channel. Send a channel username, link, or ID.")
        return

    s["source"] = chat.id
    s["source_title"] = chat.title or str(chat.id)
    s["step"] = "dst"
    s["expires"] = time.time() + 900

    picker_msg = await message.reply_text("⏳ Loading channels…")
    s["chat_id"] = picker_msg.chat.id
    s["msg_id"] = picker_msg.id
    await _render_mf_dst_picker(client, s, page=0, force_refresh=True)


# ── destination picker (paginated, same style/source as /dump_change) ────────
async def _render_mf_dst_picker(client: Client, s: dict, page: int = 0, force_refresh: bool = False):
    try:
        channels = await _get_all_registered_channels(force=force_refresh)
    except Exception as e:
        await client.edit_message_text(
            s["chat_id"], s["msg_id"],
            f"❌ Failed to load channel list:\n<code>{clean_text(str(e))}</code>",
        )
        return

    # A channel can't forward into itself.
    candidates = [c for c in channels if c["channel_id"] != s["source"]]

    if not candidates:
        await client.edit_message_text(
            s["chat_id"], s["msg_id"],
            "❌ No destination channels available (no other admin-owned channels found).",
        )
        return

    total_pages = max(1, (len(candidates) + CHANNELS_PER_PAGE - 1) // CHANNELS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * CHANNELS_PER_PAGE
    page_channels = candidates[start:start + CHANNELS_PER_PAGE]

    kb = [
        [InlineKeyboardButton(f"📢 {c['channel_title']}", callback_data=f"mf_dst_sel_{c['channel_id']}")]
        for c in page_channels
    ]
    if len(candidates) > CHANNELS_PER_PAGE:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"mf_dst_pg_{page - 1}"))
        nav_row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("➡️", callback_data=f"mf_dst_pg_{page + 1}"))
        kb.append(nav_row)
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="mf_cancel")])

    text = (
        f"✅ Source: <b>{s['source_title']}</b>\n\n"
        f"📥 <b>Select DESTINATION channel</b>  (page {page + 1}/{total_pages})"
    )
    await client.edit_message_text(s["chat_id"], s["msg_id"], text, reply_markup=InlineKeyboardMarkup(kb))


@Client.on_callback_query(filters.regex(r"^mf_dst_pg_(\d+)$"))
async def mf_dst_page(client: Client, query):
    uid = query.from_user.id
    s = MF_SESSIONS.get(uid)
    if not s:
        return await query.answer()
    await query.answer()
    await _render_mf_dst_picker(client, s, page=int(query.matches[0].group(1)))


@Client.on_callback_query(filters.regex(r"^mf_dst_sel_(-?\d+)$"))
async def mf_dst_sel(client: Client, query):
    uid = query.from_user.id
    s = MF_SESSIONS.get(uid)
    if not s:
        return await query.answer()
    dst = int(query.matches[0].group(1))
    channels = await _get_all_registered_channels()
    title = next((c["channel_title"] for c in channels if c["channel_id"] == dst), str(dst))

    s["destination"] = dst
    s["destination_title"] = title
    s["step"] = "skip"
    s["expires"] = time.time() + 900
    await query.answer()
    await _show_mf_range_prompt(client, s["chat_id"], s["msg_id"])


async def _show_mf_range_prompt(client: Client, chat_id, msg_id):
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
        "<code>100</code>\n"
        "<code>100 - 500</code>\n\n"
        "• Session expires in <b>15 minutes</b>",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="mf_cancel")]]),
    )


async def _handle_range_input(client: Client, message, uid: int, s: dict):
    raw = (message.text or "").strip()
    parsed = parse_forward_input(raw)
    if parsed.get("error"):
        await message.reply_text(parsed["error"])
        return

    skip_id = parsed["skip_id"]
    end_id = parsed["end_id"]
    src_hint = parsed["src_hint"]
    src_channel = s["source"]

    # A pasted message link only carries a *numeric* channel id (t.me/c/...),
    # so ignore the hint entirely when the source was resolved from a
    # @username / invite link — there's nothing meaningful to cross-check.
    if src_hint is not None and src_hint != src_channel:
        await message.reply_text(
            "❌ <b>Wrong channel!</b>\n\n"
            "The message link you sent does not belong to the selected source channel."
        )
        return

    if skip_id > 0:
        if not await _validate_msg_in_member_channel(src_channel, skip_id):
            await message.reply_text(
                "❌ <b>Message not found!</b>\n\nThe start message ID/link does not exist in the source channel."
            )
            return
    if end_id is not None:
        if not await _validate_msg_in_member_channel(src_channel, end_id):
            await message.reply_text(
                "❌ <b>End message not found!</b>\n\nThe end message ID/link does not exist in the source channel."
            )
            return

    s["skip"] = skip_id
    s["end_id"] = end_id
    s["step"] = "queue"
    try:
        await message.delete()
    except Exception:
        pass
    try:
        await client.delete_messages(s["chat_id"], s["msg_id"])
    except Exception:
        pass
    progress_msg = await client.send_message(s["chat_id"], "🚚 Preparing member-channel forwarding…")
    s["msg_id"] = progress_msg.id
    await enqueue_member_forward_jobs(client, uid)


async def _validate_msg_in_member_channel(channel_id: int, msg_id: int) -> bool:
    if not USER_SESSION_ENABLED:
        return False
    try:
        msg = await user_client.get_messages(channel_id, msg_id)
        return msg is not None and not getattr(msg, "empty", True)
    except FloodWait as e:
        # Let Pyrogram's own sleep_threshold absorb small waits; for a
        # bigger one here it's safer to just wait it out once inline than
        # to fail the whole range-entry step over a transient limit.
        await asyncio.sleep(int(e.value) + 1)
        try:
            msg = await user_client.get_messages(channel_id, msg_id)
            return msg is not None and not getattr(msg, "empty", True)
        except Exception:
            return False
    except Exception:
        return False


# ── scan & enqueue (background task, one per user session) ───────────────────
async def _scan_and_enqueue_member(client: Client, uid: int):
    s = MF_SESSIONS.get(uid)
    if not s:
        return
    session_id = s["session_id"]
    src = s["source"]
    dst = s["destination"]
    start_id = int(s["skip"]) + 1
    end_id = s.get("end_id")

    s["total"] = 0
    s["forwarded"] = 0
    msg_id = start_id
    consecutive_missing = 0
    MAX_CONSECUTIVE_MISSING = 500

    while True:
        if end_id is not None and msg_id > end_id:
            break
        await asyncio.sleep(0)

        if session_id in MF_CANCELLED_SESSIONS:
            return

        try:
            msg = await user_client.get_messages(src, msg_id)
        except FloodWait as e:
            wait = int(e.value) + 2
            print(f"[MF_SCAN] FloodWait {wait}s on {src}")
            await asyncio.sleep(wait)
            continue
        except Exception as e:
            print(f"[MF_SCAN] get_messages error: {e}")
            msg = None

        if not msg or getattr(msg, "empty", True):
            consecutive_missing += 1
            if consecutive_missing >= MAX_CONSECUTIVE_MISSING:
                break
            msg_id += 1
            continue

        consecutive_missing = 0
        if not msg.media:
            msg_id += 1
            continue

        await enqueue_member_forward({
            "user_id": uid,
            "src": src,
            "dst": dst,
            "msg_id": msg.id,
            "chat_id": s["chat_id"],
            "ui_msg": s["msg_id"],
            "source_title": s["source_title"],
            "destination_title": s["destination_title"],
            "session_id": session_id,
            "total": 0,
        })
        s["total"] += 1
        msg_id += 1

    if s["total"] > 0:
        await member_forward_queue.update_many(
            {"session_id": session_id, "total": 0},
            {"$set": {"total": s["total"]}},
        )

    if session_id not in MF_CANCELLED_SESSIONS:
        try:
            await client.edit_message_text(
                s["chat_id"],
                s["msg_id"],
                (
                    f"📤 <b>{s['source_title']}</b>\n"
                    f"         ⬇️⬇️⬇️\n"
                    f"📥 <b>{s['destination_title']}</b>\n\n"
                    f"🔄 Preparing {s['total']} file(s) for transfer…"
                ),
                reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="mf_cancel")]]),
            )
        except Exception:
            pass

    s["scan_done"] = True


async def enqueue_member_forward_jobs(client: Client, uid: int):
    s = MF_SESSIONS.get(uid)
    if not s:
        return
    if "session_id" not in s:
        s["session_id"] = str(uuid.uuid4())

    _mf_session_done_count.pop(s["session_id"], None)
    _mf_session_completed.discard(s["session_id"])

    try:
        await client.edit_message_text(
            s["chat_id"],
            s["msg_id"],
            (
                f"📤 <b>{s['source_title']}</b>\n"
                f"         ⬇️⬇️⬇️\n"
                f"📥 <b>{s['destination_title']}</b>\n\n"
                "🔄 Scanning files…"
            ),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="mf_cancel")]]),
        )
    except Exception:
        pass

    asyncio.create_task(
        _scan_and_enqueue_member(client, uid),
        name=f"mf_scan_{uid}_{s['session_id'][:8]}",
    )


# ── fair-pick from member-forward queue (atomic claim) ────────────────────────
async def _fetch_member_forward_job():
    now = time.time()
    cursor = member_forward_queue.find({"status": "pending"}).sort("ts", 1)
    async for job in cursor:
        key = (job["src"], job["dst"])
        if MF_COOLDOWN.get(key, 0) > now:
            continue
        if MF_ACTIVE[key] >= MAX_MEMBER_FORWARD_PER_PAIR:
            continue
        MF_ACTIVE[key] += 1
        updated = await member_forward_queue.find_one_and_update(
            {"_id": job["_id"], "status": "pending"},
            {"$set": {"status": "processing", "started": now}},
        )
        if updated is None:
            MF_ACTIVE[key] = max(0, MF_ACTIVE[key] - 1)
            continue
        return job
    return None


# ── send: direct userbot copy, fall back to download + bot re-upload ─────────
async def _member_copy_with_fallback(bot_client: Client, src: int, dst: int, msg) -> None:
    """
    Primary path: the userbot copies the message directly into the
    destination — no download/re-upload, works whenever that account also
    has send rights there (the common case).

    Fallback: if the userbot can't write to the destination, download the
    media via the userbot and re-upload it via the BOT client, which always
    has send rights in its own admin channels.
    """
    try:
        await user_client.copy_message(chat_id=dst, from_chat_id=src, message_id=msg.id)
        return
    except FloodWait:
        raise
    except Exception as e:
        print(f"[MF_DIRECT_COPY_FAIL] falling back to download+reupload: {e}")

    tmp_path = None
    try:
        tmp_path = await user_client.download_media(msg, file_name=f"/tmp/mf_{uuid.uuid4().hex}_")
        if not tmp_path:
            raise RuntimeError("download_media returned no file path")

        caption = msg.caption.html if msg.caption else ""
        if msg.video:
            v = msg.video
            await bot_client.send_video(
                dst, tmp_path, caption=caption, parse_mode=ParseMode.HTML,
                duration=getattr(v, "duration", 0), width=getattr(v, "width", 0),
                height=getattr(v, "height", 0), supports_streaming=True,
            )
        elif msg.animation:
            await bot_client.send_animation(dst, tmp_path, caption=caption, parse_mode=ParseMode.HTML)
        elif msg.audio:
            await bot_client.send_audio(dst, tmp_path, caption=caption, parse_mode=ParseMode.HTML)
        elif msg.voice:
            await bot_client.send_voice(dst, tmp_path, caption=caption, parse_mode=ParseMode.HTML)
        elif msg.photo:
            await bot_client.send_photo(dst, tmp_path, caption=caption, parse_mode=ParseMode.HTML)
        else:
            await bot_client.send_document(dst, tmp_path, caption=caption, parse_mode=ParseMode.HTML)
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


# ── forward worker ────────────────────────────────────────────────────────────
async def member_forward_worker(client: Client):
    """Single long-running worker. `client` here is the BOT client — only
    used for the download/re-upload fallback path; the userbot (user_client)
    does all the reading and, usually, the sending too."""
    while True:
        try:
            job = await _fetch_member_forward_job()
        except Exception as e:
            print(f"[MF_WORKER] _fetch_member_forward_job error: {e}")
            await asyncio.sleep(2)
            continue
        if not job:
            await asyncio.sleep(1)
            continue

        key = (job["src"], job["dst"])
        session_id = job.get("session_id")
        msg_id = job.get("msg_id")

        try:
            if session_id in MF_CANCELLED_SESSIONS:
                await member_forward_done(job["_id"])
                continue

            msg = await user_client.get_messages(job["src"], msg_id)
            if not msg or getattr(msg, "empty", True):
                await member_forward_done(job["_id"])
                continue

            await _member_copy_with_fallback(client, job["src"], job["dst"], msg)
            await member_forward_done(job["_id"])

            uid = job.get("user_id")
            s = MF_SESSIONS.get(uid) if uid else None
            if s and s.get("session_id") == session_id:
                s["forwarded"] = s.get("forwarded", 0) + 1

            await _maybe_update_mf_progress(client, job)
            await asyncio.sleep(MEMBER_FORWARD_DELAY)

        except FloodWait as e:
            retries = job.get("retries", 0)
            wait = min(300, int(e.value) + 2 + (2 ** min(retries, 7)))
            print(f"[MF_WORKER] FloodWait {wait}s on ({key})")
            MF_COOLDOWN[key] = time.time() + wait
            await member_forward_retry(job["_id"], wait)

        except Exception as e:
            print(f"[MF_WORKER_ERR] {e}")
            await member_forward_done(job["_id"])

        finally:
            MF_ACTIVE[key] = max(0, MF_ACTIVE[key] - 1)


# ── rate-limited progress update ──────────────────────────────────────────────
async def _maybe_update_mf_progress(client: Client, job: dict):
    session_id = job.get("session_id")
    if not session_id or session_id in MF_CANCELLED_SESSIONS:
        return
    if session_id in _mf_session_completed:
        return

    uid = job.get("user_id")
    s = MF_SESSIONS.get(uid) if uid else None

    if s and s.get("session_id") == session_id:
        forwarded = s.get("forwarded", 0)
        total = s.get("total", 0)
        scan_done = s.get("scan_done", False)
    else:
        forwarded = _mf_session_done_count.get(session_id, 0) + 1
        total = job.get("total", 0)
        scan_done = True

    _mf_session_done_count[session_id] = forwarded

    is_complete = scan_done and total > 0 and forwarded >= total

    if is_complete:
        if session_id in _mf_session_completed:
            return
        _mf_session_completed.add(session_id)

        if uid and uid in MF_SESSIONS and MF_SESSIONS[uid].get("session_id") == session_id:
            MF_SESSIONS.pop(uid, None)
        _mf_session_done_count.pop(session_id, None)
        MF_CANCELLED_SESSIONS.discard(session_id)

        try:
            await _edit_with_retry(
                client,
                job["chat_id"],
                job["ui_msg"],
                (
                    "✅ <b>Member-channel forwarding completed</b>\n\n"
                    f"📤 <b>Source:</b> {job['source_title']}\n"
                    f"📥 <b>Destination:</b> {job['destination_title']}\n\n"
                    f"📦 <b>Files forwarded:</b> <code>{forwarded}</code>\n"
                    f"🗂 <b>Total detected:</b> <code>{total}</code>"
                ),
            )
        except Exception:
            pass

        async def _cleanup_session():
            await asyncio.sleep(30)
            _mf_session_completed.discard(session_id)

        asyncio.create_task(_cleanup_session())
        return

    if forwarded % _MF_PROGRESS_EVERY != 0:
        return
    if session_id in _mf_session_completed:
        return

    pct = int((forwarded / total) * 100) if total > 0 else 0
    bar_fill = int(pct / 10)
    bar = "▓" * bar_fill + "░" * (10 - bar_fill)

    text = (
        f"📤 <b>{job['source_title']}</b>\n"
        f"         ⬇️⬇️⬇️\n"
        f"📥 <b>{job['destination_title']}</b>\n\n"
        f"🔄 Transferring files…\n"
        f"[{bar}] <code>{pct}%</code>\n"
        f"📦 <b>Forwarded:</b> <code>{forwarded}</code> / <code>{total if total > 0 else '?'}</code>"
    )

    try:
        await client.edit_message_text(
            job["chat_id"],
            job["ui_msg"],
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="mf_cancel")]]),
        )
    except Exception:
        pass


# ── cancel ────────────────────────────────────────────────────────────────────
@Client.on_callback_query(filters.regex("^mf_cancel$"))
async def mf_cancel(client: Client, query):
    uid = query.from_user.id
    s = MF_SESSIONS.pop(uid, None)
    if not s:
        await query.message.edit_text("❌ Nothing to cancel.")
        return

    session_id = s.get("session_id")
    if session_id:
        MF_CANCELLED_SESSIONS.add(session_id)
        forwarded = s.get("forwarded", 0)
        total = s.get("total", 0)

        await member_forward_queue.delete_many({"session_id": session_id})
        _mf_session_done_count.pop(session_id, None)
        _mf_session_completed.discard(session_id)

        await query.message.edit_text(
            "🛑 <b>Member-channel forwarding cancelled</b>\n\n"
            f"📦 <b>Files sent:</b> <code>{forwarded}</code>\n"
            f"🗂 <b>Total detected:</b> <code>{total}</code>"
        )
    else:
        await query.message.edit_text("🛑 Cancelled.")
