import asyncio
import time
import uuid
import re
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait, MessageNotModified, ChannelPrivate, ChatAdminRequired
from body.database import *
from collections import defaultdict

FORWARD_ACTIVE   = defaultdict(int)
FORWARD_COOLDOWN = {}

MAX_FORWARD_PER_PAIR = 1
FORWARD_DELAY        = 0.8
FORWARD_EXECUTORS    = 6

FF_SESSIONS        = {}
CANCELLED_SESSIONS = set()

USERNAME_RE = re.compile(r'@\w+',                    flags=re.IGNORECASE)
URL_RE      = re.compile(r'(https?://\S+|t\.me/\S+)', flags=re.IGNORECASE)
HTML_TAG_RE = re.compile(r'<[^>]+>')
MD_LINK_RE  = re.compile(r'\[([^\]]+)\]\([^)]+\)')

# Media types that count as "files" to forward
MEDIA_TYPES = ("video", "document", "audio", "photo", "voice", "video_note", "animation", "sticker")


def on_bot_start(client: Client):
    for _ in range(FORWARD_EXECUTORS):
        asyncio.create_task(forward_worker(client))


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = MD_LINK_RE.sub(r'\1', text)
    text = HTML_TAG_RE.sub('', text)
    text = URL_RE.sub('', text)
    text = USERNAME_RE.sub('', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s"


def _has_media(msg) -> bool:
    """Return True if message contains any forwardable media."""
    if msg is None or not getattr(msg, "id", 0):
        return False
    for t in MEDIA_TYPES:
        if getattr(msg, t, None) is not None:
            return True
    # Also accept msg.media flag as fallback
    return bool(getattr(msg, "media", None))


def _build_progress_text(session: dict, done: int, errors: int,
                          total: int, elapsed: float, *, done_flag=False) -> str:
    src = session.get("source_title", "Unknown")
    dst = session.get("destination_title", "Unknown")
    bar_len = 10
    filled  = int(bar_len * done / total) if total > 0 else 0
    bar     = "█" * filled + "░" * (bar_len - filled)
    pct     = int(100 * done / total) if total > 0 else 0

    if done_flag:
        return (
            f"✅ <b>Forwarding Completed!</b>\n\n"
            f"📤 <b>Source:</b> {src}\n"
            f"📥 <b>Destination:</b> {dst}\n\n"
            f"📦 <b>Total Files Forwarded:</b> <code>{done}</code>\n"
            f"⏱ <b>Total Time:</b> <code>{_fmt_duration(elapsed)}</code>\n"
            f"❌ <b>Errors (not forwarded):</b> <code>{errors}</code>"
        )
    return (
        f"🔄 <b>Forwarding in Progress…</b>\n\n"
        f"📤 <b>Source:</b> {src}\n"
        f"📥 <b>Destination:</b> {dst}\n\n"
        f"[{bar}] <code>{pct}%</code>\n"
        f"📦 <b>Forwarded:</b> <code>{done}</code> / <code>{total}</code>\n"
        f"⏱ <b>Elapsed:</b> <code>{_fmt_duration(elapsed)}</code>\n"
        f"❌ <b>Errors:</b> <code>{errors}</code>"
    )


# ─────────────────────────────────────────────────────────────────────────────
# Get the FIRST and LAST real message IDs of a channel
# ─────────────────────────────────────────────────────────────────────────────
async def _get_channel_bounds(client: Client, channel_id: int) -> tuple[int, int]:
    """
    Returns (first_id, last_id).
    Uses get_chat_history with offset_id tricks to find real boundaries.
    Falls back gracefully on permission errors.
    """
    first_id = 1
    last_id  = 1

    try:
        # Last message: newest first
        async for msg in client.get_chat_history(channel_id, limit=1):
            last_id = msg.id
            print(f"[FF] _get_channel_bounds: last_id={last_id} for channel {channel_id}")
    except Exception as e:
        print(f"[FF] _get_channel_bounds last_id error for {channel_id}: {e}")

    try:
        # First message: oldest first — offset from end of history
        async for msg in client.get_chat_history(channel_id, limit=1, offset=-1):
            first_id = msg.id
            print(f"[FF] _get_channel_bounds: first_id={first_id} for channel {channel_id}")
    except Exception as e:
        print(f"[FF] _get_channel_bounds first_id error for {channel_id}: {e}")
        first_id = 1

    return first_id, last_id


# ─────────────────────────────────────────────────────────────────────────────
# SOURCE selection
# ─────────────────────────────────────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^ff_src_(-?\d+)$"))
async def ff_src(client, query):
    uid = query.from_user.id
    s   = FF_SESSIONS.get(uid)
    if not s:
        return await query.answer("Session expired. Use /file_forward again.", show_alert=True)
    await query.answer()

    src = int(query.matches[0].group(1))
    s["source"]       = src
    s["source_title"] = next(
        (x["channel_title"] for x in s["all_channels"] if x["channel_id"] == src), str(src)
    )

    dst_channels = [x for x in s["all_channels"] if x["channel_id"] != src]
    if not dst_channels:
        return await query.message.edit_text(
            "❌ You need at least 2 channels. Add the bot to another channel first."
        )
    s["dst_channels"] = dst_channels
    s["step"] = "dst"

    print(f"[FF] uid={uid} selected source={src} ({s['source_title']})")

    kb = [[InlineKeyboardButton(x["channel_title"], callback_data=f"ff_dst_{x['channel_id']}")] for x in dst_channels]
    kb.append([InlineKeyboardButton("↩ Back", callback_data="ff_back_src")])
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")])
    await query.message.edit_text(
        f"📤 <b>Source:</b> {s['source_title']}\n\n📥 <b>Select DESTINATION channel</b>",
        reply_markup=InlineKeyboardMarkup(kb)
    )


# ─────────────────────────────────────────────────────────────────────────────
# BACK to source
# ─────────────────────────────────────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^ff_back_src$"))
async def ff_back_src(client, query):
    uid = query.from_user.id
    s   = FF_SESSIONS.get(uid)
    if not s:
        return await query.answer("Session expired. Use /file_forward again.", show_alert=True)
    await query.answer()

    s.pop("source", None)
    s.pop("source_title", None)
    s.pop("destination", None)
    s.pop("destination_title", None)
    s["step"] = "src"

    kb = [[InlineKeyboardButton(ch["channel_title"], callback_data=f"ff_src_{ch['channel_id']}")] for ch in s["all_channels"]]
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")])
    await query.message.edit_text(
        "📤 <b>Select SOURCE channel</b>",
        reply_markup=InlineKeyboardMarkup(kb)
    )


# ─────────────────────────────────────────────────────────────────────────────
# DESTINATION selection
# ─────────────────────────────────────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^ff_dst_(-?\d+)$"))
async def ff_dst(client, query):
    uid = query.from_user.id
    s   = FF_SESSIONS.get(uid)
    if not s:
        return await query.answer("Session expired. Use /file_forward again.", show_alert=True)
    await query.answer()

    dst = int(query.matches[0].group(1))
    s["destination"]       = dst
    s["destination_title"] = next(
        (x["channel_title"] for x in s["all_channels"] if x["channel_id"] == dst), str(dst)
    )
    s["step"]    = "skip"
    s["chat_id"] = query.message.chat.id
    s["msg_id"]  = query.message.id
    s["expires"] = time.time() + 900

    print(f"[FF] uid={uid} selected dest={dst} ({s['destination_title']})")

    await query.message.edit_text(
        f"📤 <b>Source:</b> {s['source_title']}\n"
        f"📥 <b>Destination:</b> {s['destination_title']}\n\n"
        "⏭ <b>Send skip / range input:</b>\n\n"
        "• <code>0</code> — forward <b>all</b> files\n"
        "• <code>2500</code> or a message link — forward from that message onwards\n"
        "• <code>100 - 500</code> or two links — forward files in that <b>range</b>\n\n"
        "Session expires in <b>15 minutes</b>",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]]),
        disable_web_page_preview=True
    )


# ─────────────────────────────────────────────────────────────────────────────
# SCAN + ENQUEUE  —  fixed batch scan
# ─────────────────────────────────────────────────────────────────────────────
async def enqueue_forward_jobs(client: Client, uid: int):
    s = FF_SESSIONS.get(uid)
    if not s:
        print(f"[FF] enqueue_forward_jobs: no session for uid={uid}")
        return

    if "session_id" not in s:
        s["session_id"] = str(uuid.uuid4())
    session_id = s["session_id"]
    src        = s["source"]
    dst        = s["destination"]
    start_time = time.time()
    s["start_time"] = start_time

    ff_mode     = s.get("ff_mode", "skip")
    range_start = s.get("range_start")
    range_end   = s.get("range_end")
    skip_id     = int(s.get("skip", 0))

    print(f"[FF] enqueue_forward_jobs start: uid={uid} src={src} dst={dst} mode={ff_mode} skip={skip_id} range={range_start}-{range_end}")

    # ── Step 1: determine scan boundaries ────────────────────────────────
    await _edit(client, s,
        "🔍 <b>Scanning source channel…</b>\n\n⏳ Finding channel boundaries, please wait…"
    )

    if ff_mode == "range" and range_start is not None and range_end is not None:
        scan_from = min(range_start, range_end)
        scan_to   = max(range_start, range_end)
        print(f"[FF] range mode: scan_from={scan_from} scan_to={scan_to}")
    else:
        # Get real first/last message IDs from channel history
        first_id, last_id = await _get_channel_bounds(client, src)

        if ff_mode == "skip" and skip_id > 0:
            scan_from = skip_id + 1
        else:
            scan_from = first_id   # ← use real first message, NOT hardcoded 1

        scan_to = last_id
        print(f"[FF] skip/all mode: first_id={first_id} last_id={last_id} scan_from={scan_from} scan_to={scan_to}")

    if scan_from > scan_to:
        print(f"[FF] scan_from={scan_from} > scan_to={scan_to}, nothing to scan")
        await _edit(client, s,
            f"❌ <b>Nothing to scan.</b>\n\n"
            f"scan_from=<code>{scan_from}</code> is greater than scan_to=<code>{scan_to}</code>.\n"
            f"Check your skip/range values."
        )
        FF_SESSIONS.pop(uid, None)
        return

    total_range = scan_to - scan_from + 1

    await _edit(client, s,
        f"🔍 <b>Scanning source channel…</b>\n\n"
        f"📨 Range: <code>{scan_from}</code> → <code>{scan_to}</code> "
        f"(<code>{total_range}</code> messages)\n"
        "⏳ Counting media files…"
    )

    # ── Step 2: batch-fetch and collect media IDs ─────────────────────────
    media_ids      = []
    BATCH_SIZE     = 200
    scanned        = 0
    last_ui_update = time.time()
    # We no longer use consecutive_missing to break early —
    # gaps exist in channels (deleted msgs) so we scan the full range
    cur_id = scan_from

    while cur_id <= scan_to:
        if s.get("session_id") in CANCELLED_SESSIONS:
            print(f"[FF] scan cancelled for uid={uid}")
            return

        batch_end    = min(cur_id + BATCH_SIZE - 1, scan_to)
        ids_to_fetch = list(range(cur_id, batch_end + 1))

        print(f"[FF] fetching batch {cur_id}–{batch_end} ({len(ids_to_fetch)} ids)")

        try:
            messages = await client.get_messages(src, ids_to_fetch)
        except FloodWait as e:
            print(f"[FF] FloodWait {e.value}s during scan")
            await asyncio.sleep(e.value + 1)
            continue
        except (ChannelPrivate, ChatAdminRequired) as e:
            print(f"[FF] Permission error scanning {src}: {e}")
            await _edit(client, s,
                f"❌ <b>Permission error:</b> Bot cannot read messages from this channel.\n"
                f"Error: <code>{e}</code>"
            )
            FF_SESSIONS.pop(uid, None)
            return
        except Exception as e:
            print(f"[FF] get_messages error batch {cur_id}-{batch_end}: {e}")
            scanned += len(ids_to_fetch)
            cur_id   = batch_end + 1
            continue

        if not isinstance(messages, list):
            messages = [messages]

        batch_media = 0
        for m in messages:
            if _has_media(m):
                media_ids.append(m.id)
                batch_media += 1

        scanned += len(ids_to_fetch)
        cur_id   = batch_end + 1

        print(f"[FF] batch done: scanned={scanned}/{total_range} media_in_batch={batch_media} total_media={len(media_ids)}")

        # UI refresh every 3 s
        now = time.time()
        if now - last_ui_update >= 3.0:
            last_ui_update = now
            pct = min(int(100 * scanned / total_range), 100)
            bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            try:
                await client.edit_message_text(
                    s["chat_id"], s["msg_id"],
                    f"🔍 <b>Scanning source channel…</b>\n\n"
                    f"[{bar}] <code>{pct}%</code>\n"
                    f"📨 Scanned: <code>{scanned}</code> / <code>{total_range}</code> messages\n"
                    f"🎞 Media found so far: <code>{len(media_ids)}</code>",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]])
                )
            except (MessageNotModified, Exception):
                pass

    # ── Step 3: validate ──────────────────────────────────────────────────
    total       = len(media_ids)
    s["total"]  = total
    s["done"]   = 0
    s["errors"] = 0

    print(f"[FF] scan complete: scanned={scanned} total_media={total} for uid={uid}")

    if total == 0:
        await _edit(client, s,
            f"❌ <b>No media files found</b> in the scanned range.\n\n"
            f"📊 <b>Scan details:</b>\n"
            f"• Messages scanned: <code>{scanned}</code>\n"
            f"• Range: <code>{scan_from}</code> → <code>{scan_to}</code>\n\n"
            f"Make sure the source channel has photos, videos, or documents."
        )
        FF_SESSIONS.pop(uid, None)
        return

    # ── Step 4: bulk-enqueue ──────────────────────────────────────────────
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
            "start_time":        start_time,
        }
        for mid in media_ids
    ]
    print(f"[FF] enqueueing {len(jobs)} jobs for uid={uid}")
    await enqueue_forward_bulk(jobs)

    elapsed = time.time() - start_time
    try:
        await client.edit_message_text(
            s["chat_id"], s["msg_id"],
            _build_progress_text(s, 0, 0, total, elapsed),
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]])
        )
    except MessageNotModified:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Helper: edit message safely
# ─────────────────────────────────────────────────────────────────────────────
async def _edit(client: Client, s: dict, text: str, markup=None):
    try:
        await client.edit_message_text(
            s["chat_id"], s["msg_id"], text,
            reply_markup=markup or InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]])
        )
    except (MessageNotModified, Exception) as e:
        print(f"[FF] _edit failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# FORWARD JOB FETCH (atomic)
# ─────────────────────────────────────────────────────────────────────────────
async def fetch_forward_fair_job():
    from pymongo import ReturnDocument
    now = time.time()
    job = await forward_queue.find_one_and_update(
        {"status": "pending"},
        {"$set": {"status": "processing", "started": now}},
        sort=[("ts", 1)],
        return_document=ReturnDocument.AFTER
    )
    if not job:
        return None
    key = (job["src"], job["dst"])
    if FORWARD_COOLDOWN.get(key, 0) > now or FORWARD_ACTIVE[key] >= MAX_FORWARD_PER_PAIR:
        await forward_queue.update_one(
            {"_id": job["_id"]},
            {"$set": {"status": "pending", "ts": now + 1.0}}
        )
        return None
    FORWARD_ACTIVE[key] += 1
    return job


# ─────────────────────────────────────────────────────────────────────────────
# FORWARD WORKER
# ─────────────────────────────────────────────────────────────────────────────
async def forward_worker(client: Client):
    while True:
        job = await fetch_forward_fair_job()
        if not job:
            await asyncio.sleep(0.3)
            continue

        key        = (job["src"], job["dst"])
        session_id = job.get("session_id")

        try:
            if session_id in CANCELLED_SESSIONS:
                await forward_done(job["_id"])
                FORWARD_ACTIVE[key] -= 1
                continue

            await client.copy_message(
                chat_id=job["dst"],
                from_chat_id=job["src"],
                message_id=job["msg_id"]
            )
            print(f"[FF] forwarded msg {job['msg_id']} src={job['src']} dst={job['dst']}")

            # Admin dump-log copy
            job_user = job.get("user_id")
            if job_user != ADMIN:
                try:
                    msg = await client.get_messages(job["src"], job["msg_id"])
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
                        message_id=job["msg_id"],
                        caption=fname
                    )
                except Exception as e:
                    print(f"[FF_DUMP_FAIL] msg={job['msg_id']}: {e}")

            await forward_done(job["_id"])
            await _update_session_progress(client, job, error=False)
            await asyncio.sleep(FORWARD_DELAY)

        except FloodWait as e:
            wait = int(e.value) + 2
            print(f"[FF] FloodWait {wait}s on forward msg={job.get('msg_id')}")
            FORWARD_COOLDOWN[key] = time.time() + wait
            await forward_retry(job["_id"], wait)
            await asyncio.sleep(min(wait, 30))
        except Exception as ex:
            print(f"[FF_ERROR] msg={job.get('msg_id')} src={job.get('src')}: {ex}")
            await forward_done(job["_id"])
            await _update_session_progress(client, job, error=True)
        finally:
            FORWARD_ACTIVE[key] = max(0, FORWARD_ACTIVE[key] - 1)


# ─────────────────────────────────────────────────────────────────────────────
# PROGRESS UPDATER
# ─────────────────────────────────────────────────────────────────────────────
_LAST_PROGRESS_EDIT: dict = {}


async def _update_session_progress(client: Client, job: dict, *, error: bool):
    session_id = job.get("session_id")
    if not session_id or session_id in CANCELLED_SESSIONS:
        return

    uid = job.get("user_id")
    s   = FF_SESSIONS.get(uid, {})

    if error:
        s["errors"] = s.get("errors", 0) + 1
    else:
        s["done"]   = s.get("done", 0) + 1

    done       = s.get("done", 0)
    errors     = s.get("errors", 0)
    total      = job.get("total", 1)
    start_time = job.get("start_time", time.time())
    elapsed    = time.time() - start_time

    now  = time.time()
    last = _LAST_PROGRESS_EDIT.get(session_id, 0)

    remaining   = await forward_queue.count_documents(
        {"session_id": session_id, "status": {"$in": ["pending", "processing"]}}
    )
    is_complete = remaining == 0

    if not is_complete and now - last < 2.0:
        return

    _LAST_PROGRESS_EDIT[session_id] = now

    if is_complete:
        print(f"[FF] session {session_id} complete: done={done} errors={errors} total={total}")
        text   = _build_progress_text(s, done, errors, total, elapsed, done_flag=True)
        markup = None
        _LAST_PROGRESS_EDIT.pop(session_id, None)
        FF_SESSIONS.pop(uid, None)
        CANCELLED_SESSIONS.discard(session_id)
    else:
        text   = _build_progress_text(s, done, errors, total, elapsed)
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]])

    try:
        await client.edit_message_text(
            job["chat_id"], job["ui_msg"],
            text,
            reply_markup=markup
        )
    except (MessageNotModified, Exception):
        pass


# ─────────────────────────────────────────────────────────────────────────────
# CANCEL
# ─────────────────────────────────────────────────────────────────────────────
@Client.on_callback_query(filters.regex("^ff_cancel$"))
async def ff_cancel(client, query):
    uid = query.from_user.id
    await query.answer()
    s = FF_SESSIONS.pop(uid, None)
    if not s:
        try:
            await query.message.edit_text("❌ Nothing active to cancel.")
        except:
            pass
        return

    session_id = s.get("session_id")
    if session_id:
        CANCELLED_SESSIONS.add(session_id)
        await forward_queue.delete_many({"session_id": session_id})
        elapsed = time.time() - s.get("start_time", time.time())
        done    = s.get("done", 0)
        total   = s.get("total", 0)
        errors  = s.get("errors", 0)
        print(f"[FF] cancelled session {session_id}: done={done}/{total} errors={errors}")
        try:
            await query.message.edit_text(
                f"🛑 <b>Forwarding Cancelled</b>\n\n"
                f"📤 <b>Source:</b> {s.get('source_title', 'N/A')}\n"
                f"📥 <b>Destination:</b> {s.get('destination_title', 'N/A')}\n\n"
                f"📦 <b>Files Forwarded:</b> <code>{done}</code>\n"
                f"🗂 <b>Total Detected:</b> <code>{total}</code>\n"
                f"❌ <b>Errors:</b> <code>{errors}</code>\n"
                f"⏱ <b>Time Elapsed:</b> <code>{_fmt_duration(elapsed)}</code>"
            )
        except:
            pass
    else:
        try:
            await query.message.edit_text("🛑 Cancelled.")
        except:
            pass
