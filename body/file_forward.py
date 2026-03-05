import asyncio
import time
import uuid
import re
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait, MessageNotModified
from body.database import *
from collections import defaultdict

FORWARD_ACTIVE  = defaultdict(int)
FORWARD_COOLDOWN = {}

MAX_FORWARD_PER_PAIR = 1
FORWARD_DELAY        = 0.8
FORWARD_EXECUTORS    = 6

FF_SESSIONS       = {}
CANCELLED_SESSIONS = set()

USERNAME_RE = re.compile(r'@\w+',            flags=re.IGNORECASE)
URL_RE      = re.compile(r'(https?://\S+|t\.me/\S+)', flags=re.IGNORECASE)
HTML_TAG_RE = re.compile(r'<[^>]+>')
MD_LINK_RE  = re.compile(r'\[([^\]]+)\]\([^)]+\)')


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


# ─────────────────── get last msg id ──────────────────────────────────────
async def _get_last_msg_id(client: Client, channel_id: int) -> int:
    try:
        async for msg in client.get_chat_history(channel_id, limit=1):
            return msg.id
    except Exception:
        pass
    return 1


# ─────────────────── SOURCE selection ────────────────────────────────────
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

    # Destination list = all channels except source
    dst_channels = [x for x in s["all_channels"] if x["channel_id"] != src]
    if not dst_channels:
        return await query.message.edit_text(
            "❌ You need at least 2 channels. Add the bot to another channel first."
        )
    s["dst_channels"] = dst_channels
    s["step"] = "dst"

    kb = [[InlineKeyboardButton(x["channel_title"], callback_data=f"ff_dst_{x['channel_id']}")] for x in dst_channels]
    kb.append([InlineKeyboardButton("↩ Back (change source)", callback_data="ff_back_src")])
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")])
    await query.message.edit_text(
        f"📤 <b>Source:</b> {s['source_title']}\n\n📥 <b>Select DESTINATION channel</b>",
        reply_markup=InlineKeyboardMarkup(kb)
    )


# ─────────────────── BACK to source ──────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^ff_back_src$"))
async def ff_back_src(client, query):
    uid = query.from_user.id
    s   = FF_SESSIONS.get(uid)
    if not s:
        return await query.answer("Session expired. Use /file_forward again.", show_alert=True)
    await query.answer()

    # Reset source/dest selections
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


# ─────────────────── DESTINATION selection ───────────────────────────────
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


# ─────────────────── SCAN + ENQUEUE (batch, bottom-up) ───────────────────
async def enqueue_forward_jobs(client: Client, uid: int):
    s = FF_SESSIONS.get(uid)
    if not s:
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

    # ── Determine scan window ─────────────────────────────────────────────
    if ff_mode == "range" and range_start is not None and range_end is not None:
        scan_from = min(range_start, range_end)
        scan_to   = max(range_start, range_end)
    else:
        # skip/all: scan from (skip_id+1) to last message — fetched from bottom
        last_id   = await _get_last_msg_id(client, src)
        scan_from = skip_id + 1
        scan_to   = last_id

    total_range = max(scan_to - scan_from + 1, 1)

    await client.edit_message_text(
        s["chat_id"], s["msg_id"],
        f"🔍 <b>Scanning source channel…</b>\n\n"
        f"📨 Range: <code>{scan_from}</code> → <code>{scan_to}</code>\n"
        "⏳ Counting media files…",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]])
    )

    # ── Batch-fetch and collect media IDs ────────────────────────────────
    media_ids          = []
    BATCH_SIZE         = 200
    scanned            = 0
    last_ui_update     = time.time()
    consecutive_missing = 0
    MAX_CONSEC_MISSING  = 300
    cur_id             = scan_from

    while cur_id <= scan_to:
        if s.get("session_id") in CANCELLED_SESSIONS:
            return

        batch_end    = min(cur_id + BATCH_SIZE - 1, scan_to)
        ids_to_fetch = list(range(cur_id, batch_end + 1))

        try:
            messages = await client.get_messages(src, ids_to_fetch)
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
            continue
        except Exception:
            scanned += len(ids_to_fetch)
            cur_id   = batch_end + 1
            continue

        if not isinstance(messages, list):
            messages = [messages]

        for m in messages:
            if not m or not m.id:
                consecutive_missing += 1
            else:
                consecutive_missing = 0
                if m.media:
                    media_ids.append(m.id)

        scanned += len(ids_to_fetch)
        cur_id   = batch_end + 1

        # UI refresh every 2 s
        now = time.time()
        if now - last_ui_update >= 2.0:
            last_ui_update = now
            pct = min(int(100 * scanned / total_range), 100)
            bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
            try:
                await client.edit_message_text(
                    s["chat_id"], s["msg_id"],
                    f"🔍 <b>Scanning source channel…</b>\n\n"
                    f"[{bar}] <code>{pct}%</code>\n"
                    f"📨 Scanned: <code>{scanned}</code> / <code>{total_range}</code>\n"
                    f"🎞 Media found: <code>{len(media_ids)}</code>",
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]])
                )
            except (MessageNotModified, Exception):
                pass

        if consecutive_missing >= MAX_CONSEC_MISSING:
            break

    # ── Validate ──────────────────────────────────────────────────────────
    total      = len(media_ids)
    s["total"] = total
    s["done"]  = 0
    s["errors"] = 0

    if total == 0:
        await client.edit_message_text(
            s["chat_id"], s["msg_id"],
            "❌ <b>No media files found</b> in the selected range."
        )
        FF_SESSIONS.pop(uid, None)
        return

    # ── Bulk-enqueue jobs ─────────────────────────────────────────────────
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


# ─────────────────── FORWARD JOB FETCH ───────────────────────────────────
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


# ─────────────────── FORWARD WORKER ──────────────────────────────────────
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
                    print(f"[FF_DUMP_FAIL] {e}")

            await forward_done(job["_id"])
            await _update_session_progress(client, job, error=False)
            await asyncio.sleep(FORWARD_DELAY)

        except FloodWait as e:
            wait = int(e.value) + 2
            FORWARD_COOLDOWN[key] = time.time() + wait
            await forward_retry(job["_id"], wait)
            await asyncio.sleep(min(wait, 30))
        except Exception as ex:
            print(f"[FF_ERROR] msg {job.get('msg_id')}: {ex}")
            await forward_done(job["_id"])
            await _update_session_progress(client, job, error=True)
        finally:
            FORWARD_ACTIVE[key] = max(0, FORWARD_ACTIVE[key] - 1)


# ─────────────────── PROGRESS UPDATER ────────────────────────────────────
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


# ─────────────────── CANCEL ──────────────────────────────────────────────
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
