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

USERNAME_RE = re.compile(r'@\w+',                     flags=re.IGNORECASE)
URL_RE      = re.compile(r'(https?://\S+|t\.me/\S+)', flags=re.IGNORECASE)
HTML_TAG_RE = re.compile(r'<[^>]+>')
MD_LINK_RE  = re.compile(r'\[([^\]]+)\]\([^)]+\)')

# All media types we consider "files" worth forwarding
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
    """True if the message contains any forwardable media (photo, video, doc, etc.)."""
    if msg is None or not getattr(msg, "id", 0):
        return False
    for t in MEDIA_TYPES:
        if getattr(msg, t, None) is not None:
            return True
    return bool(getattr(msg, "media", None))


def _build_progress_text(session: dict, done: int, errors: int,
                          total: int, elapsed: float, *, done_flag=False) -> str:
    src     = session.get("source_title", "Unknown")
    dst     = session.get("destination_title", "Unknown")
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
# Reliably get the LAST real message ID of a channel.
#
# Strategy (tries three methods in order):
#   1. get_chat_history(limit=1)  — works when bot has read access
#   2. Probe known very-high IDs via get_messages() — works for admin-only bots
#      because get_messages() uses a different MTProto method that admins can call
#   3. Fall back to a safe high ceiling (999_999) so scan still runs
# ─────────────────────────────────────────────────────────────────────────────
async def _get_last_msg_id(client: Client, channel_id: int) -> int:
    # Method 1: get_chat_history
    try:
        async for msg in client.get_chat_history(channel_id, limit=1):
            last = msg.id
            print(f"[FF] last_id via get_chat_history: {last} (channel {channel_id})")
            if last and last > 0:
                return last
    except Exception as e:
        print(f"[FF] get_chat_history failed for {channel_id}: {e}")

    # Method 2: binary-search using get_messages()
    # get_messages() works even for admin-only bots.
    # We probe exponentially upward to find an ID that returns empty,
    # then binary-search down to the last real message.
    print(f"[FF] falling back to probe search for last_id in channel {channel_id}")
    try:
        # Exponential probe: 1, 2, 4, 8, ... until we find an empty slot
        probe = 1
        last_known_real = 0
        while probe <= 2_000_000:
            msgs = await client.get_messages(channel_id, probe)
            if not isinstance(msgs, list):
                msgs = [msgs]
            real = [m for m in msgs if m and getattr(m, "id", 0)]
            if real:
                last_known_real = real[-1].id
                probe *= 2
            else:
                # probe is beyond channel end — binary search between last_known_real and probe
                break
            await asyncio.sleep(0.05)

        if last_known_real == 0:
            # Channel might be completely empty or unreachable
            print(f"[FF] probe found no messages in channel {channel_id}")
            return 1

        # Binary search between last_known_real and probe
        lo, hi = last_known_real, probe
        while hi - lo > 1:
            mid = (lo + hi) // 2
            msgs = await client.get_messages(channel_id, mid)
            if not isinstance(msgs, list):
                msgs = [msgs]
            real = [m for m in msgs if m and getattr(m, "id", 0)]
            if real:
                lo = real[-1].id
            else:
                hi = mid
            await asyncio.sleep(0.05)

        print(f"[FF] last_id via probe/binary-search: {lo} (channel {channel_id})")
        return lo

    except Exception as e:
        print(f"[FF] probe search failed for {channel_id}: {e}")

    # Method 3: safe ceiling fallback — scan will just encounter empty IDs beyond real end
    print(f"[FF] using fallback ceiling 999999 for channel {channel_id}")
    return 999_999


# ─────────────────────────────────────────────────────────────────────────────
# Helper: edit the session's UI message, swallowing errors
# ─────────────────────────────────────────────────────────────────────────────
async def _edit(client: Client, s: dict, text: str, cancel_btn: bool = True):
    markup = (
        InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]])
        if cancel_btn else None
    )
    try:
        await client.edit_message_text(s["chat_id"], s["msg_id"], text, reply_markup=markup)
    except (MessageNotModified, Exception) as e:
        print(f"[FF] _edit failed: {e}")


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
    print(f"[FF] uid={uid} source={src} ({s['source_title']})")

    kb = [[InlineKeyboardButton(x["channel_title"], callback_data=f"ff_dst_{x['channel_id']}")] for x in dst_channels]
    kb.append([InlineKeyboardButton("↩ Back (change source)", callback_data="ff_back_src")])
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")])
    await query.message.edit_text(
        f"📤 <b>Source:</b> {s['source_title']}\n\n📥 <b>Select DESTINATION channel</b>",
        reply_markup=InlineKeyboardMarkup(kb)
    )


# ─────────────────────────────────────────────────────────────────────────────
# BACK to source list
# ─────────────────────────────────────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^ff_back_src$"))
async def ff_back_src(client, query):
    uid = query.from_user.id
    s   = FF_SESSIONS.get(uid)
    if not s:
        return await query.answer("Session expired. Use /file_forward again.", show_alert=True)
    await query.answer()
    s.pop("source", None);  s.pop("source_title", None)
    s.pop("destination", None); s.pop("destination_title", None)
    s["step"] = "src"
    kb = [[InlineKeyboardButton(ch["channel_title"], callback_data=f"ff_src_{ch['channel_id']}")] for ch in s["all_channels"]]
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")])
    await query.message.edit_text("📤 <b>Select SOURCE channel</b>", reply_markup=InlineKeyboardMarkup(kb))


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
    print(f"[FF] uid={uid} dest={dst} ({s['destination_title']})")

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
# SCAN + ENQUEUE
#
# Core design:
#   - scan_from = 1 (or skip_id+1) — ALWAYS start from beginning, never from
#     a "first_id" guess.  Deleted IDs are just empty in the batch and get
#     skipped instantly.  16–17 batch calls cover 3310 deleted messages — fast.
#   - scan_to   = real last message, found reliably via _get_last_msg_id()
#   - No consecutive_missing early exit — channels have gaps everywhere
# ─────────────────────────────────────────────────────────────────────────────
async def enqueue_forward_jobs(client: Client, uid: int):
    s = FF_SESSIONS.get(uid)
    if not s:
        print(f"[FF] enqueue: no session uid={uid}")
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

    print(f"[FF] enqueue start uid={uid} src={src} dst={dst} mode={ff_mode} skip={skip_id} range={range_start}-{range_end}")

    await _edit(client, s, "🔍 <b>Scanning source channel…</b>\n\n⏳ Finding last message ID…")

    # ── Determine scan_from / scan_to ─────────────────────────────────────
    if ff_mode == "range" and range_start is not None and range_end is not None:
        scan_from = min(range_start, range_end)
        scan_to   = max(range_start, range_end)
        print(f"[FF] range mode: {scan_from} → {scan_to}")
    else:
        # Get last real message ID (robust, three fallback methods)
        scan_to = await _get_last_msg_id(client, src)
        # scan_from: always start at 1 (or skip+1).
        # Deleted IDs 1..N are handled by batch fetch — no early exit.
        scan_from = max(1, skip_id + 1)
        print(f"[FF] skip/all mode: scan_from={scan_from} scan_to={scan_to}")

    # Sanity check
    if scan_to < 1:
        print(f"[FF] scan_to={scan_to} invalid, aborting")
        await _edit(client, s,
            "❌ <b>Could not determine channel message range.</b>\n\n"
            "Make sure the bot is an admin of the source channel with full read permissions.",
            cancel_btn=False
        )
        FF_SESSIONS.pop(uid, None)
        return

    # If user skipped past the last message
    if scan_from > scan_to:
        print(f"[FF] scan_from={scan_from} > scan_to={scan_to}")
        await _edit(client, s,
            f"❌ <b>Skip ID is beyond the last message.</b>\n\n"
            f"Last message in channel: <code>{scan_to}</code>\n"
            f"Your skip value: <code>{skip_id}</code>\n\n"
            "Send a smaller skip value.",
            cancel_btn=False
        )
        FF_SESSIONS.pop(uid, None)
        return

    total_range = scan_to - scan_from + 1
    print(f"[FF] scanning {total_range} IDs: {scan_from} → {scan_to}")

    await _edit(client, s,
        f"🔍 <b>Scanning source channel…</b>\n\n"
        f"📨 Range: <code>{scan_from}</code> → <code>{scan_to}</code> "
        f"(<code>{total_range}</code> IDs)\n"
        "⏳ Counting media files…"
    )

    # ── Batch-fetch and collect media IDs ────────────────────────────────
    # Batches of 200 IDs each.
    # Deleted/missing messages come back as Message(id=0) — _has_media() returns False.
    # We do NOT break early on gaps — channels can have large gaps of deleted messages.
    media_ids      = []
    BATCH_SIZE     = 200
    scanned        = 0
    last_ui_update = time.time()
    cur_id         = scan_from

    while cur_id <= scan_to:
        if s.get("session_id") in CANCELLED_SESSIONS:
            print(f"[FF] scan cancelled uid={uid}")
            return

        batch_end    = min(cur_id + BATCH_SIZE - 1, scan_to)
        ids_to_fetch = list(range(cur_id, batch_end + 1))

        try:
            messages = await client.get_messages(src, ids_to_fetch)
        except FloodWait as e:
            print(f"[FF] FloodWait {e.value}s during scan at id={cur_id}")
            await asyncio.sleep(e.value + 1)
            continue
        except (ChannelPrivate, ChatAdminRequired) as e:
            print(f"[FF] Permission error on {src}: {e}")
            await _edit(client, s,
                f"❌ <b>Permission error:</b> Bot cannot read messages from source channel.\n"
                f"<code>{e}</code>",
                cancel_btn=False
            )
            FF_SESSIONS.pop(uid, None)
            return
        except Exception as e:
            print(f"[FF] get_messages error batch {cur_id}-{batch_end}: {e}")
            # Skip this batch and continue — don't abort the whole scan
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

        print(f"[FF] batch {cur_id-len(ids_to_fetch)}-{batch_end}: "
              f"scanned={scanned}/{total_range} media_this_batch={batch_media} total={len(media_ids)}")

        # UI update every 3 s
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
                    f"📨 Scanned: <code>{scanned}</code> / <code>{total_range}</code> IDs\n"
                    f"🎞 Media found: <code>{len(media_ids)}</code>",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]])
                )
            except (MessageNotModified, Exception):
                pass

    # ── Results ───────────────────────────────────────────────────────────
    total       = len(media_ids)
    s["total"]  = total
    s["done"]   = 0
    s["errors"] = 0

    print(f"[FF] scan complete: scanned={scanned} IDs, found={total} media, uid={uid}")

    if total == 0:
        await _edit(client, s,
            f"❌ <b>No media files found</b> in the source channel.\n\n"
            f"📊 <b>Scan summary:</b>\n"
            f"• IDs scanned: <code>{scanned}</code>\n"
            f"• Range: <code>{scan_from}</code> → <code>{scan_to}</code>\n\n"
            "Make sure the source channel has photos, videos, documents, or audio files.",
            cancel_btn=False
        )
        FF_SESSIONS.pop(uid, None)
        return

    # ── Bulk-enqueue ──────────────────────────────────────────────────────
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
    print(f"[FF] enqueueing {len(jobs)} jobs uid={uid}")
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
            print(f"[FF] ✓ forwarded msg={job['msg_id']} src={job['src']} → dst={job['dst']}")

            # Admin dump-log copy
            if job.get("user_id") != ADMIN:
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
                    print(f"[FF] dump_fail msg={job['msg_id']}: {e}")

            await forward_done(job["_id"])
            await _update_session_progress(client, job, error=False)
            await asyncio.sleep(FORWARD_DELAY)

        except FloodWait as e:
            wait = int(e.value) + 2
            print(f"[FF] FloodWait {wait}s msg={job.get('msg_id')}")
            FORWARD_COOLDOWN[key] = time.time() + wait
            await forward_retry(job["_id"], wait)
            await asyncio.sleep(min(wait, 30))
        except Exception as ex:
            print(f"[FF] error msg={job.get('msg_id')}: {ex}")
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
    elapsed    = time.time() - job.get("start_time", time.time())

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
        print(f"[FF] complete: session={session_id} done={done} errors={errors} total={total}")
        text   = _build_progress_text(s, done, errors, total, elapsed, done_flag=True)
        markup = None
        _LAST_PROGRESS_EDIT.pop(session_id, None)
        FF_SESSIONS.pop(uid, None)
        CANCELLED_SESSIONS.discard(session_id)
    else:
        text   = _build_progress_text(s, done, errors, total, elapsed)
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]])

    try:
        await client.edit_message_text(job["chat_id"], job["ui_msg"], text, reply_markup=markup)
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
        print(f"[FF] cancel: session={session_id} done={s.get('done',0)}/{s.get('total',0)}")
        try:
            await query.message.edit_text(
                f"🛑 <b>Forwarding Cancelled</b>\n\n"
                f"📤 <b>Source:</b> {s.get('source_title', 'N/A')}\n"
                f"📥 <b>Destination:</b> {s.get('destination_title', 'N/A')}\n\n"
                f"📦 <b>Files Forwarded:</b> <code>{s.get('done',0)}</code>\n"
                f"🗂 <b>Total Detected:</b> <code>{s.get('total',0)}</code>\n"
                f"❌ <b>Errors:</b> <code>{s.get('errors',0)}</code>\n"
                f"⏱ <b>Time Elapsed:</b> <code>{_fmt_duration(elapsed)}</code>"
            )
        except:
            pass
    else:
        try:
            await query.message.edit_text("🛑 Cancelled.")
        except:
            pass
