import asyncio
import time
import uuid
import re
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait, MessageNotModified
from body.database import *
from collections import defaultdict

FORWARD_ACTIVE = defaultdict(int)
FORWARD_COOLDOWN = {}

MAX_FORWARD_PER_PAIR = 1
FORWARD_DELAY = 0.8
FORWARD_EXECUTORS = 6

FF_SESSIONS = {}
CANCELLED_SESSIONS = set()

USERNAME_RE = re.compile(r'@\w+', flags=re.IGNORECASE)
URL_RE = re.compile(r'(https?://\S+|t\.me/\S+)', flags=re.IGNORECASE)
HTML_TAG_RE = re.compile(r'<[^>]+>')
MD_LINK_RE = re.compile(r'\[([^\]]+)\]\([^)]+\)')


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


def _build_progress_text(session: dict, done: int, errors: int, total: int, elapsed: float, *, done_flag=False) -> str:
    src = session.get("source_title", "Unknown")
    dst = session.get("destination_title", "Unknown")
    bar_len = 10
    filled = int(bar_len * done / total) if total > 0 else 0
    bar = "█" * filled + "░" * (bar_len - filled)
    pct = int(100 * done / total) if total > 0 else 0

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


# ---------- SOURCE ----------
@Client.on_callback_query(filters.regex(r"^ff_src_(-?\d+)$"))
async def ff_src(client, query):
    uid = query.from_user.id
    s = FF_SESSIONS.get(uid)
    if not s:
        return await query.answer("Session expired. Use /file_forward again.", show_alert=True)
    await query.answer()
    src = int(query.matches[0].group(1))
    s["source"] = src
    s["source_title"] = next((x["channel_title"] for x in s["channels"] if x["channel_id"] == src), str(src))
    remaining = [x for x in s["channels"] if x["channel_id"] != src]
    if not remaining:
        return await query.message.edit_text("❌ You need at least 2 channels. Add bot to another channel first.")
    s["channels"] = remaining
    s["step"] = "dst"
    kb = [[InlineKeyboardButton(x["channel_title"], callback_data=f"ff_dst_{x['channel_id']}")] for x in remaining]
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")])
    await query.message.edit_text("📥 <b>Select DESTINATION channel</b>", reply_markup=InlineKeyboardMarkup(kb))


# ---------- DEST ----------
@Client.on_callback_query(filters.regex(r"^ff_dst_(-?\d+)$"))
async def ff_dst(client, query):
    uid = query.from_user.id
    s = FF_SESSIONS.get(uid)
    if not s:
        return await query.answer("Session expired. Use /file_forward again.", show_alert=True)
    await query.answer()
    dst = int(query.matches[0].group(1))
    s["destination"] = dst
    s["destination_title"] = next((x["channel_title"] for x in s["channels"] if x["channel_id"] == dst), str(dst))
    s["step"] = "skip"
    s["chat_id"] = query.message.chat.id
    s["msg_id"] = query.message.id
    s["expires"] = time.time() + 900
    await query.message.edit_text(
        "⏭ <b>Send MESSAGE LINK or MESSAGE ID to skip up to</b>\n\n"
        "Example:\n"
        "<code>https://t.me/c/1815162626/2458</code>\n\n"
        "• Send <b>0</b> to forward all files\n"
        "• Forwarding starts <b>AFTER</b> this message\n"
        "• Session expires in <b>15 minutes</b>",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]]),
        disable_web_page_preview=True
    )


# ---------- ENQUEUE + COUNT ----------
async def enqueue_forward_jobs(client: Client, uid: int):
    s = FF_SESSIONS[uid]
    if "session_id" not in s:
        s["session_id"] = str(uuid.uuid4())
    session_id = s["session_id"]
    src = s["source"]
    dst = s["destination"]
    skip_id = int(s.get("skip", 0))
    start_time = time.time()
    s["start_time"] = start_time

    # --- Phase 1: count & collect all media message IDs ---
    await client.edit_message_text(
        s["chat_id"], s["msg_id"],
        "🔍 <b>Scanning source channel…</b>\n\nCounting files to forward, please wait.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]])
    )

    media_ids = []
    msg_id = skip_id + 1
    consecutive_missing = 0
    MAX_CONSECUTIVE_MISSING = 500
    batch = []
    BATCH_SIZE = 200

    while True:
        if uid in CANCELLED_SESSIONS or s.get("session_id") in CANCELLED_SESSIONS:
            return
        try:
            msg = await client.get_messages(src, msg_id)
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
            continue
        except Exception:
            msg = None

        if not msg or not msg.id:
            consecutive_missing += 1
            if consecutive_missing >= MAX_CONSECUTIVE_MISSING:
                break
            msg_id += 1
            continue

        consecutive_missing = 0
        if msg.media:
            media_ids.append(msg.id)
        msg_id += 1

    total = len(media_ids)
    s["total"] = total
    s["done"] = 0
    s["errors"] = 0

    if total == 0:
        await client.edit_message_text(
            s["chat_id"], s["msg_id"],
            "❌ <b>No media files found</b> in the source channel after the given skip point."
        )
        FF_SESSIONS.pop(uid, None)
        return

    # --- Phase 2: bulk enqueue ---
    jobs = [
        {
            "user_id": uid,
            "src": src,
            "dst": dst,
            "msg_id": mid,
            "chat_id": s["chat_id"],
            "ui_msg": s["msg_id"],
            "source_title": s["source_title"],
            "destination_title": s["destination_title"],
            "session_id": session_id,
            "total": total,
            "start_time": start_time,
        }
        for mid in media_ids
    ]
    await enqueue_forward_bulk(jobs)

    elapsed = time.time() - start_time
    progress_text = _build_progress_text(s, 0, 0, total, elapsed)
    try:
        await client.edit_message_text(
            s["chat_id"], s["msg_id"],
            progress_text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]])
        )
    except MessageNotModified:
        pass


# ---------- FORWARD JOB FETCH (atomic) ----------
async def fetch_forward_fair_job():
    from pymongo import ReturnDocument
    now = time.time()

    query = {"status": "pending"}
    blocked_pairs = [(k, ts) for k, ts in FORWARD_COOLDOWN.items() if ts > now]
    # We can't filter tuple-keyed cooldowns in MongoDB easily,
    # so fetch atomically and reject post-fetch if blocked
    job = await forward_queue.find_one_and_update(
        query,
        {"$set": {"status": "processing", "started": now}},
        sort=[("ts", 1)],
        return_document=ReturnDocument.AFTER
    )
    if not job:
        return None

    key = (job["src"], job["dst"])
    if FORWARD_COOLDOWN.get(key, 0) > now or FORWARD_ACTIVE[key] >= MAX_FORWARD_PER_PAIR:
        # Put it back — cooldown or at capacity
        await forward_queue.update_one(
            {"_id": job["_id"]},
            {"$set": {"status": "pending", "ts": now + 1.0}}
        )
        return None

    FORWARD_ACTIVE[key] += 1
    return job


# ---------- FORWARD WORKER ----------
async def forward_worker(client: Client):
    while True:
        job = await fetch_forward_fair_job()
        if not job:
            await asyncio.sleep(0.3)
            continue

        key = (job["src"], job["dst"])
        session_id = job.get("session_id")

        try:
            if session_id in CANCELLED_SESSIONS:
                await forward_done(job["_id"])
                FORWARD_ACTIVE[key] -= 1
                continue

            # Copy message as-is (caption/media unchanged)
            await client.copy_message(
                chat_id=job["dst"],
                from_chat_id=job["src"],
                message_id=job["msg_id"]
            )

            # Dump copy (admin log channel)
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


# ---------- PROGRESS UPDATER ----------
_LAST_PROGRESS_EDIT: dict = {}  # session_id -> last edit timestamp

async def _update_session_progress(client: Client, job: dict, *, error: bool):
    session_id = job.get("session_id")
    if not session_id or session_id in CANCELLED_SESSIONS:
        return

    uid = job.get("user_id")
    s = FF_SESSIONS.get(uid, {})

    # Update in-memory counters
    if error:
        s["errors"] = s.get("errors", 0) + 1
    else:
        s["done"] = s.get("done", 0) + 1

    done = s.get("done", 0)
    errors = s.get("errors", 0)
    total = job.get("total", 1)
    start_time = job.get("start_time", time.time())
    elapsed = time.time() - start_time

    # Rate-limit UI edits to once per 2 seconds to avoid flood
    now = time.time()
    last = _LAST_PROGRESS_EDIT.get(session_id, 0)
    remaining = await forward_queue.count_documents({"session_id": session_id, "status": {"$in": ["pending", "processing"]}})
    is_complete = remaining == 0

    if not is_complete and now - last < 2.0:
        return

    _LAST_PROGRESS_EDIT[session_id] = now

    if is_complete:
        text = _build_progress_text(s, done, errors, total, elapsed, done_flag=True)
        markup = None
        # Cleanup
        _LAST_PROGRESS_EDIT.pop(session_id, None)
        FF_SESSIONS.pop(uid, None)
        CANCELLED_SESSIONS.discard(session_id)
    else:
        text = _build_progress_text(s, done, errors, total, elapsed)
        markup = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]])

    try:
        await client.edit_message_text(
            job["chat_id"], job["ui_msg"],
            text,
            reply_markup=markup
        )
    except (MessageNotModified, Exception):
        pass


# ---------- CANCEL ----------
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
        remaining = await forward_queue.count_documents({"session_id": session_id})
        total = s.get("total", 0)
        done = s.get("done", 0)
        errors = s.get("errors", 0)
        await forward_queue.delete_many({"session_id": session_id})
        elapsed = time.time() - s.get("start_time", time.time())
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
