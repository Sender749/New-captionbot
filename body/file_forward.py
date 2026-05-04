import asyncio
import time
import uuid
import re
import os
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait
from body.database import *
from collections import defaultdict

# ─── Concurrency limits ─────────────────────────────────────────────────────
MAX_FORWARD_PER_PAIR = 1          # 1 active job per (src, dst) pair at a time
FORWARD_DELAY = 0.3               # seconds between forwarded files
SCAN_BATCH = 10                   # messages fetched per get_messages batch call
SCAN_YIELD_EVERY = 5              # yield to event loop every N messages scanned

# Progress update throttle: only edit the progress message at most once per N seconds.
# This prevents Telegram flood and keeps UI responsive without flooding the API.
PROGRESS_UPDATE_INTERVAL = 4      # seconds

# ─── Forward worker count is set in bot.py ──────────────────────────────────
# on_bot_start is intentionally a no-op so bot.py controls worker count.
def on_bot_start(client: Client):
    pass  # Workers started in bot.py

# ─── In-memory state ────────────────────────────────────────────────────────
FORWARD_ACTIVE = defaultdict(int)   # (src, dst) -> active count
FORWARD_COOLDOWN = {}               # (src, dst) -> unblock timestamp

# Per-session progress tracking (uid -> last_ui_edit_time)
_LAST_PROGRESS_UPDATE: dict[int, float] = {}

FF_SESSIONS = {}
CANCELLED_SESSIONS = set()

USERNAME_RE = re.compile(r'@\w+', flags=re.IGNORECASE)
URL_RE = re.compile(r'(https?://\S+|t\.me/\S+)', flags=re.IGNORECASE)
HTML_TAG_RE = re.compile(r'<[^>]+>')
MD_LINK_RE = re.compile(r'\[([^\]]+)\]\([^)]+\)')
MSG_LINK_RE = re.compile(
    r'(?:https?://)?t\.me/(?:c/(\d+)|([A-Za-z0-9_]+))/(\d+)',
    flags=re.IGNORECASE
)

ANIM_FRAMES = [
    "🔄 Transferring files",
    "🔄 Transferring files.",
    "🔄 Transferring files..",
    "🔄 Transferring files..."
]


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = MD_LINK_RE.sub(r'\1', text)
    text = HTML_TAG_RE.sub('', text)
    text = URL_RE.sub('', text)
    text = USERNAME_RE.sub('', text)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


def _parse_single(text: str):
    text = text.strip()
    m = MSG_LINK_RE.search(text)
    if m:
        numeric_cid = m.group(1)
        msg_id = int(m.group(3))
        if numeric_cid:
            return int(f"-100{numeric_cid}"), msg_id
        return None, msg_id
    if text.isdigit():
        return None, int(text)
    return None, None


def parse_forward_input(raw: str):
    parts = re.split(r'\s*-\s*(?=\S)', raw, maxsplit=1)
    if len(parts) == 2:
        src_hint1, start_id = _parse_single(parts[0])
        src_hint2, end_id = _parse_single(parts[1])
        if start_id is None or end_id is None:
            return {"error": "❌ Could not parse start or end message reference."}
        if start_id > end_id:
            return {"error": "❌ Start message ID must be less than end message ID."}
        src_hint = src_hint1 or src_hint2
        return {"skip_id": start_id - 1, "end_id": end_id, "src_hint": src_hint, "error": None}
    else:
        if raw.strip() == "0":
            return {"skip_id": 0, "end_id": None, "src_hint": None, "error": None}
        src_hint, msg_id = _parse_single(raw.strip())
        if msg_id is None:
            return {"error": "❌ Invalid message link or ID.\n\nSend a Telegram message link, a message ID, or 0 to forward all."}
        return {"skip_id": msg_id, "end_id": None, "src_hint": src_hint, "error": None}


async def validate_msg_in_channel(client: Client, channel_id: int, msg_id: int) -> bool:
    try:
        msg = await client.get_messages(channel_id, msg_id)
        return msg is not None and not getattr(msg, 'empty', True)
    except Exception:
        return False


# ─── SOURCE selection ────────────────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^ff_src_(-?\d+)$"))
async def ff_src(client, query):
    await query.answer()
    uid = query.from_user.id
    s = FF_SESSIONS.get(uid)
    if not s:
        return
    src = int(query.matches[0].group(1))
    s["source"] = src
    s["source_title"] = next(x["channel_title"] for x in s["channels"] if x["channel_id"] == src)
    s["channels"] = [x for x in s["channels"] if x["channel_id"] != src]
    s["step"] = "dst"
    kb = [
        [InlineKeyboardButton(x["channel_title"], callback_data=f"ff_dst_{x['channel_id']}")]
        for x in s["channels"]
    ]
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")])
    await query.message.edit_text(
        "📥 <b>Select DESTINATION channel</b>",
        reply_markup=InlineKeyboardMarkup(kb)
    )


# ─── DESTINATION selection ───────────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^ff_dst_(-?\d+)$"))
async def ff_dst(client, query):
    await query.answer()
    uid = query.from_user.id
    s = FF_SESSIONS.get(uid)
    if not s:
        return
    dst = int(query.matches[0].group(1))
    s["destination"] = dst
    s["destination_title"] = next(
        x["channel_title"] for x in s["channels"] if x["channel_id"] == dst
    )
    s["step"] = "skip"
    s["chat_id"] = query.message.chat.id
    s["msg_id"] = query.message.id
    s["expires"] = time.time() + 900   # 15 min

    await query.message.edit_text(
        "⏭ <b>Enter forwarding range</b>\n\n"
        "<b>Options:</b>\n"
        "• <code>0</code> — forward <b>ALL</b> files\n"
        "• <code>msg_link</code> or <code>id</code> — start <b>AFTER</b> this message\n"
        "• <code>start - end</code> — forward <b>BETWEEN</b> two messages (inclusive)\n\n"
        "<b>Examples:</b>\n"
        "<code>0</code>\n"
        "<code>https://t.me/c/1815162626/100</code>\n"
        "<code>100 - 500</code>\n"
        "<code>https://t.me/c/1234/100 - https://t.me/c/1234/500</code>\n\n"
        "• Session expires in <b>15 minutes</b>",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]]
        ),
        disable_web_page_preview=True
    )


# ─── SCAN + ENQUEUE (background task, non-blocking) ─────────────────────────
async def _scan_and_enqueue(client: Client, uid: int):
    """
    Scans the source channel and inserts jobs into DB.
    Runs as its own asyncio task so it never blocks UI handlers or other users.

    Key improvements vs original:
    - Uses get_messages() in batches of SCAN_BATCH instead of one-by-one
    - Yields to the event loop every SCAN_YIELD_EVERY messages to stay responsive
    - Each user's scan is fully isolated in its own task
    """
    s = FF_SESSIONS.get(uid)
    if not s:
        return

    session_id = s["session_id"]
    src = s["source"]
    dst = s["destination"]
    start_id = int(s["skip"]) + 1
    end_id = s.get("end_id")

    s["total"] = 0
    msg_id = start_id
    consecutive_missing = 0
    MAX_CONSECUTIVE_MISSING = 500
    scanned_since_yield = 0

    while True:
        if end_id is not None and msg_id > end_id:
            break

        # Yield to event loop periodically so UI handlers stay fast
        scanned_since_yield += 1
        if scanned_since_yield >= SCAN_YIELD_EVERY:
            await asyncio.sleep(0)
            scanned_since_yield = 0

        # Fetch in batches for speed
        batch_end = msg_id + SCAN_BATCH - 1
        if end_id is not None:
            batch_end = min(batch_end, end_id)
        ids_to_fetch = list(range(msg_id, batch_end + 1))

        try:
            messages = await client.get_messages(src, ids_to_fetch)
        except FloodWait as e:
            await asyncio.sleep(int(e.value) + 1)
            continue
        except Exception:
            msg_id += SCAN_BATCH
            consecutive_missing += SCAN_BATCH
            if consecutive_missing >= MAX_CONSECUTIVE_MISSING:
                break
            continue

        for msg in messages:
            if not msg or getattr(msg, 'empty', True):
                consecutive_missing += 1
                if consecutive_missing >= MAX_CONSECUTIVE_MISSING:
                    break
                continue

            consecutive_missing = 0

            if not msg.media:
                continue

            await enqueue_forward({
                "user_id": uid,
                "src": src,
                "dst": dst,
                "msg_id": msg.id,
                "chat_id": s["chat_id"],
                "ui_msg": s["msg_id"],
                "source_title": s["source_title"],
                "destination_title": s["destination_title"],
                "session_id": session_id,
                "total": 0
            })
            s["total"] += 1
        else:
            msg_id += SCAN_BATCH
            continue
        break  # inner break (consecutive_missing exceeded) propagates out

    # Stamp real total on all pending jobs for this session
    await forward_queue.update_many(
        {"session_id": session_id, "total": 0},
        {"$set": {"total": s["total"]}}
    )

    if session_id not in CANCELLED_SESSIONS:
        try:
            await client.edit_message_text(
                s["chat_id"],
                s["msg_id"],
                (
                    f"📤 <b>{s['source_title']}</b>\n"
                    f"         ⬇️⬇️⬇️\n"
                    f"📥 <b>{s['destination_title']}</b>\n\n"
                    f"✅ Scan complete — <b>{s['total']}</b> files queued\n"
                    "🔄 Transfer starting…"
                ),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]]
                )
            )
        except Exception:
            pass


async def enqueue_forward_jobs(client: Client, uid: int):
    """
    Entry point: shows instant feedback, fires background scan task, returns immediately.
    The calling handler is freed immediately — no blocking.
    """
    s = FF_SESSIONS.get(uid)
    if not s:
        return
    if "session_id" not in s:
        s["session_id"] = str(uuid.uuid4())

    try:
        await client.edit_message_text(
            s["chat_id"],
            s["msg_id"],
            (
                f"📤 <b>{s['source_title']}</b>\n"
                f"         ⬇️⬇️⬇️\n"
                f"📥 <b>{s['destination_title']}</b>\n\n"
                "🔍 Scanning files…"
            ),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]]
            )
        )
    except Exception:
        pass

    # Each user's scan runs in its own independent task
    asyncio.create_task(_scan_and_enqueue(client, uid))


# ─── FAIR JOB FETCH ──────────────────────────────────────────────────────────
async def fetch_forward_fair_job():
    """
    Atomically claim one pending forward job, respecting per-pair limits and cooldowns.
    Uses find_one_and_update to prevent two workers grabbing the same job.
    """
    now = time.time()

    # Build blocked pairs in-memory (no extra DB call)
    blocked_pairs_active = {k for k, v in FORWARD_ACTIVE.items() if v >= MAX_FORWARD_PER_PAIR}
    blocked_pairs_cooldown = {k for k, v in FORWARD_COOLDOWN.items() if v > now}
    blocked_pairs = blocked_pairs_active | blocked_pairs_cooldown

    # Build query: exclude blocked (src, dst) pairs
    query = {"status": "pending"}
    if blocked_pairs:
        blocked_list = [{"src": p[0], "dst": p[1]} for p in blocked_pairs]
        query["$nor"] = blocked_list

    job = await forward_queue.find_one_and_update(
        query,
        {"$set": {"status": "processing", "started": now}},
        sort=[("ts", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if job:
        key = (job["src"], job["dst"])
        FORWARD_ACTIVE[key] += 1
    return job


# ─── THUMBNAIL-PRESERVING FORWARD ────────────────────────────────────────────
async def _forward_with_thumb(client: Client, src: int, dst: int, msg) -> None:
    thumb_path = None
    try:
        media_type = None
        media_obj = None
        for t in ("video", "document", "animation"):
            obj = getattr(msg, t, None)
            if obj:
                media_type = t
                media_obj = obj
                break

        caption = msg.caption or ""
        has_thumb = False
        if media_obj:
            thumbs = getattr(media_obj, "thumbs", None)
            if thumbs and len(thumbs) > 0:
                has_thumb = True

        if media_type == "video" and has_thumb:
            thumb_path = await client.download_media(
                media_obj.thumbs[0].file_id,
                file_name=f"/tmp/thumb_ff_{msg.id}.jpg"
            )
            await client.send_video(
                chat_id=dst,
                video=media_obj.file_id,
                caption=caption,
                thumb=thumb_path,
                duration=getattr(media_obj, "duration", 0),
                width=getattr(media_obj, "width", 0),
                height=getattr(media_obj, "height", 0),
                supports_streaming=True,
                parse_mode=None
            )
        elif media_type in ("document", "animation") and has_thumb:
            thumb_path = await client.download_media(
                media_obj.thumbs[0].file_id,
                file_name=f"/tmp/thumb_ff_{msg.id}.jpg"
            )
            if media_type == "animation":
                await client.send_animation(
                    chat_id=dst,
                    animation=media_obj.file_id,
                    caption=caption,
                    thumb=thumb_path,
                    parse_mode=None
                )
            else:
                await client.send_document(
                    chat_id=dst,
                    document=media_obj.file_id,
                    caption=caption,
                    thumb=thumb_path,
                    parse_mode=None
                )
        else:
            await client.copy_message(
                chat_id=dst,
                from_chat_id=src,
                message_id=msg.id
            )
    finally:
        if thumb_path:
            try:
                os.remove(thumb_path)
            except Exception:
                pass


# ─── FORWARD WORKER ──────────────────────────────────────────────────────────
async def forward_worker(client: Client):
    """
    One worker handles one job at a time.
    Many workers run concurrently → many users/pairs progress simultaneously.
    Workers never block each other — each awaits its own I/O independently.
    """
    while True:
        job = await fetch_forward_fair_job()
        if not job:
            await asyncio.sleep(0.5)
            continue

        key = (job["src"], job["dst"])
        session_id = job.get("session_id")
        msg_id = job.get("msg_id")

        try:
            if session_id in CANCELLED_SESSIONS:
                await forward_done(job["_id"])
                continue

            msg = await client.get_messages(job["src"], msg_id)
            await _forward_with_thumb(client, job["src"], job["dst"], msg)

            job_user = job.get("user_id")
            is_admin = (job_user in ADMIN) if isinstance(ADMIN, (list, tuple, set)) else (job_user == ADMIN)
            if not is_admin:
                try:
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
                        message_id=msg_id,
                        caption=fname
                    )
                except Exception as e:
                    print(f"[FF_DUMP_FAIL] {e}")

            await forward_done(job["_id"])
            await _update_forward_progress_throttled(client, job)
            await asyncio.sleep(FORWARD_DELAY)

        except FloodWait as e:
            wait = int(e.value) + 2
            retries = job.get("retries", 0)
            wait += min(60, retries * 2)
            FORWARD_COOLDOWN[key] = time.time() + wait
            await forward_retry(job["_id"], wait)
        except Exception as e:
            print(f"[FF_WORKER_ERR] {e}")
            await forward_done(job["_id"])
        finally:
            FORWARD_ACTIVE[key] = max(0, FORWARD_ACTIVE[key] - 1)


# ─── THROTTLED PROGRESS UPDATE ───────────────────────────────────────────────
async def _update_forward_progress_throttled(client: Client, job: dict):
    """
    Update the UI progress message, but only once per PROGRESS_UPDATE_INTERVAL
    seconds per session. This avoids flooding Telegram with edit_message calls
    when many files complete in rapid succession.
    """
    session = job.get("session_id")
    if session in CANCELLED_SESSIONS:
        return

    now = time.time()
    last = _LAST_PROGRESS_UPDATE.get(session, 0)

    # Always check if done; only throttle the "in-progress" updates
    remaining = await forward_queue.count_documents({"session_id": session})

    if remaining == 0:
        # Session finished — always show completion message
        _LAST_PROGRESS_UPDATE.pop(session, None)
        try:
            await client.edit_message_text(
                job["chat_id"],
                job["ui_msg"],
                (
                    "✅ <b>Forwarding completed!</b>\n\n"
                    f"📤 <b>Source:</b> {job['source_title']}\n"
                    f"📥 <b>Destination:</b> {job['destination_title']}\n"
                    f"📦 <b>Files sent:</b> <code>{job.get('total', 0)}</code>"
                )
            )
        except Exception:
            pass
        return

    # Throttle in-progress updates
    if now - last < PROGRESS_UPDATE_INTERVAL:
        return

    _LAST_PROGRESS_UPDATE[session] = now
    total = job.get("total", 0)
    done = max(0, total - remaining)
    pct = int((done / total * 100) if total > 0 else 0)
    bar_filled = pct // 10
    bar = "█" * bar_filled + "░" * (10 - bar_filled)
    frame = ANIM_FRAMES[int(now) % len(ANIM_FRAMES)]

    text = (
        f"📤 <b>{job['source_title']}</b>\n"
        f"         ⬇️⬇️⬇️\n"
        f"📥 <b>{job['destination_title']}</b>\n\n"
        f"{frame}\n\n"
        f"<code>[{bar}]</code> {pct}%\n"
        f"📦 <b>{done}</b> / <b>{total}</b> files"
    )
    try:
        await client.edit_message_text(
            job["chat_id"],
            job["ui_msg"],
            text,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]]
            )
        )
    except Exception:
        pass


# ─── CANCEL ──────────────────────────────────────────────────────────────────
@Client.on_callback_query(filters.regex("^ff_cancel$"))
async def ff_cancel(client, query):
    await query.answer()
    uid = query.from_user.id
    s = FF_SESSIONS.pop(uid, None)
    if not s:
        await query.message.edit_text("❌ Nothing to cancel.")
        return

    session_id = s.get("session_id")
    if session_id:
        CANCELLED_SESSIONS.add(session_id)
        _LAST_PROGRESS_UPDATE.pop(session_id, None)

        remaining = await forward_queue.count_documents({"session_id": session_id})
        total = s.get("total", 0)
        sent = max(total - remaining, 0)

        await forward_queue.delete_many({"session_id": session_id})
        await query.message.edit_text(
            "🛑 <b>Forwarding cancelled</b>\n\n"
            f"📦 <b>Files sent:</b> <code>{sent}</code>\n"
            f"🗂 <b>Initially detected:</b> <code>{total}</code>"
        )
    else:
        await query.message.edit_text("🛑 Cancelled.")
