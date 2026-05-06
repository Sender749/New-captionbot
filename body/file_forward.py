"""
file_forward.py  ── improved worker & executor system
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Key changes vs original:
  • FORWARD_WORKERS pulled from database constants (4 workers)
  • Per-pair concurrency cap enforced with atomic DB claim
  • Exponential back-off on FloodWait (capped at 5 min)
  • Scan loop yields every iteration so caption workers aren't starved
  • _scan_and_enqueue uses asyncio.sleep(0) to cooperate with event loop
  • Progress updates are rate-limited (once every 3 completions)
    so we don't spam edit_message_text and cause MORE FloodWaits
  • Session expiry (15 min) enforced in FF_SESSIONS
  • CANCELLED_SESSIONS auto-cleaned after session ends
"""
import asyncio
import os
import re
import time
import uuid
from collections import defaultdict

from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from body.database import (
    FORWARD_WORKERS,
    MAX_FORWARD_PER_PAIR,
    FORWARD_DELAY,
    enqueue_forward,
    forward_done,
    forward_retry,
    forward_queue,
)
from info import ADMIN, FF_CH

# ── in-memory state ──────────────────────────────────────────────────────────
FORWARD_ACTIVE   = defaultdict(int)   # (src, dst) -> active worker count
FORWARD_COOLDOWN = {}                 # (src, dst) -> unblock timestamp

FF_SESSIONS       = {}                # uid -> session dict
CANCELLED_SESSIONS = set()            # session_ids that were cancelled

# ── regex helpers ─────────────────────────────────────────────────────────────
USERNAME_RE = re.compile(r"@\w+",           flags=re.IGNORECASE)
URL_RE      = re.compile(r"(https?://\S+|t\.me/\S+)", flags=re.IGNORECASE)
HTML_TAG_RE = re.compile(r"<[^>]+>")
MD_LINK_RE  = re.compile(r"\[([^\]]+)\]\([^)]+\)")
MSG_LINK_RE = re.compile(
    r"(?:https?://)?t\.me/(?:c/(\d+)|([A-Za-z0-9_]+))/(\d+)",
    flags=re.IGNORECASE,
)

ANIM_FRAMES = [
    "🔄 Transferring files",
    "🔄 Transferring files.",
    "🔄 Transferring files..",
    "🔄 Transferring files...",
]

# Rate-limit progress edits: update UI every N completions
_PROGRESS_EVERY = 3
_session_done_count: dict[str, int] = defaultdict(int)
# Tracks sessions whose completion message has already been sent.
# Prevents multiple workers all racing to send "✅ Forwarding completed".
_session_completed: set = set()


# ── startup hook ──────────────────────────────────────────────────────────────
def on_bot_start(client: Client):
    """Launch the fixed pool of forward workers once at bot start."""
    for i in range(FORWARD_WORKERS):
        asyncio.create_task(forward_worker(client), name=f"ff_worker_{i}")
    print(f"[FF] {FORWARD_WORKERS} forward workers started")


# ── text utilities ────────────────────────────────────────────────────────────
def clean_text(text: str) -> str:
    if not text:
        return ""
    text = MD_LINK_RE.sub(r"\1", text)
    text = HTML_TAG_RE.sub("", text)
    text = URL_RE.sub("", text)
    text = USERNAME_RE.sub("", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ── input parsing ─────────────────────────────────────────────────────────────
def _parse_single(text: str):
    text = text.strip()
    m = MSG_LINK_RE.search(text)
    if m:
        numeric_cid = m.group(1)
        msg_id = int(m.group(3))
        return (int(f"-100{numeric_cid}") if numeric_cid else None), msg_id
    if text.isdigit():
        return None, int(text)
    return None, None


def parse_forward_input(raw: str):
    parts = re.split(r"\s*-\s*(?=\S)", raw, maxsplit=1)
    if len(parts) == 2:
        src_hint1, start_id = _parse_single(parts[0])
        src_hint2, end_id   = _parse_single(parts[1])
        if start_id is None or end_id is None:
            return {"error": "❌ Could not parse start or end message reference."}
        if start_id > end_id:
            return {"error": "❌ Start message ID must be less than end message ID."}
        return {
            "skip_id":  start_id - 1,
            "end_id":   end_id,
            "src_hint": src_hint1 or src_hint2,
            "error":    None,
        }
    else:
        if raw.strip() == "0":
            return {"skip_id": 0, "end_id": None, "src_hint": None, "error": None}
        src_hint, msg_id = _parse_single(raw.strip())
        if msg_id is None:
            return {
                "error": (
                    "❌ Invalid message link or ID.\n\n"
                    "Send a Telegram message link, a message ID, or 0 to forward all."
                )
            }
        return {"skip_id": msg_id, "end_id": None, "src_hint": src_hint, "error": None}


async def validate_msg_in_channel(client: Client, channel_id: int, msg_id: int) -> bool:
    try:
        msg = await client.get_messages(channel_id, msg_id)
        return msg is not None and not getattr(msg, "empty", True)
    except Exception:
        return False


# ── callback: source selection ────────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^ff_src_(-?\d+)$"))
async def ff_src(client, query):
    uid = query.from_user.id
    s = FF_SESSIONS.get(uid)
    if not s:
        return
    src = int(query.matches[0].group(1))
    s["source"]       = src
    s["source_title"] = next(x["channel_title"] for x in s["channels"] if x["channel_id"] == src)
    s["channels"]     = [x for x in s["channels"] if x["channel_id"] != src]
    s["step"]         = "dst"
    kb = [
        [InlineKeyboardButton(x["channel_title"], callback_data=f"ff_dst_{x['channel_id']}")]
        for x in s["channels"]
    ]
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")])
    await query.message.edit_text(
        "📥 **Select DESTINATION channel**",
        reply_markup=InlineKeyboardMarkup(kb),
    )


# ── callback: destination selection ──────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^ff_dst_(-?\d+)$"))
async def ff_dst(client, query):
    uid = query.from_user.id
    s = FF_SESSIONS.get(uid)
    if not s:
        return
    dst = int(query.matches[0].group(1))
    s["destination"]       = dst
    s["destination_title"] = next(
        x["channel_title"] for x in s["channels"] if x["channel_id"] == dst
    )
    s["step"]    = "skip"
    s["chat_id"] = query.message.chat.id
    s["msg_id"]  = query.message.id
    s["expires"] = time.time() + 900  # 15 minutes
    await query.message.edit_text(
        "⏭ <b>Enter forwarding range</b>\n\n"
        "<b>Options:</b>\n"
        "• <code>0</code> — forward ALL files\n"
        "• <code>msg_link</code> or <code>id</code> — start AFTER this message\n"
        "• <code>start - end</code> — forward BETWEEN two messages (inclusive)\n\n"
        "<b>Examples:</b>\n"
        "<code>0</code>\n"
        "<code>https://t.me/c/1815162626/100</code>\n"
        "<code>100 - 500</code>\n"
        "<code>https://t.me/c/1234/100 - https://t.me/c/1234/500</code>\n\n"
        "• Session expires in <b>15 minutes</b>",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]]
        ),
        disable_web_page_preview=True,
    )


# ── scan & enqueue (background task, one per user session) ───────────────────
async def _scan_and_enqueue(client: Client, uid: int):
    """
    Scans the source channel and writes one DB job per media message.
    Runs entirely in the background – never blocks caption workers.
    Uses asyncio.sleep(0) on every iteration so the event loop stays free.
    """
    s = FF_SESSIONS.get(uid)
    if not s:
        return
    session_id = s["session_id"]
    src        = s["source"]
    dst        = s["destination"]
    start_id   = int(s["skip"]) + 1
    end_id     = s.get("end_id")

    s["total"]            = 0
    msg_id                = start_id
    consecutive_missing   = 0
    MAX_CONSECUTIVE_MISSING = 500

    while True:
        if end_id is not None and msg_id > end_id:
            break
        # Yield every iteration so other handlers run freely
        await asyncio.sleep(0)

        if session_id in CANCELLED_SESSIONS:
            return

        try:
            msg = await client.get_messages(src, msg_id)
        except FloodWait as e:
            wait = int(e.value) + 2
            print(f"[SCAN] FloodWait {wait}s on {src}")
            await asyncio.sleep(wait)
            continue
        except Exception as e:
            print(f"[SCAN] get_messages error: {e}")
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

        await enqueue_forward({
            "user_id":           uid,
            "src":               src,
            "dst":               dst,
            "msg_id":            msg.id,
            "chat_id":           s["chat_id"],
            "ui_msg":            s["msg_id"],
            "source_title":      s["source_title"],
            "destination_title": s["destination_title"],
            "session_id":        session_id,
            "total":             0,
        })
        s["total"] += 1
        msg_id += 1

    # Stamp actual total on all pending jobs for this session
    if s["total"] > 0:
        await forward_queue.update_many(
            {"session_id": session_id, "total": 0},
            {"$set": {"total": s["total"]}},
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
                    f"🔄 Preparing {s['total']} file(s) for transfer…"
                ),
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]]
                ),
            )
        except Exception:
            pass


async def enqueue_forward_jobs(client: Client, uid: int):
    """
    Called from the message handler.
    Shows instant 'Scanning…' feedback then fires the background scan task.
    Returns immediately so the handler is free for all other requests.
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
                "🔄 Scanning files…"
            ),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]]
            ),
        )
    except Exception:
        pass

    # Each user's scan runs in its own independent task
    asyncio.create_task(
        _scan_and_enqueue(client, uid),
        name=f"scan_{uid}_{s['session_id'][:8]}",
    )


# ── fair-pick from forward queue (atomic claim) ───────────────────────────────
async def _fetch_forward_job():
    now    = time.time()
    cursor = forward_queue.find({"status": "pending"}).sort("ts", 1)
    async for job in cursor:
        key = (job["src"], job["dst"])
        if FORWARD_COOLDOWN.get(key, 0) > now:
            continue
        if FORWARD_ACTIVE[key] >= MAX_FORWARD_PER_PAIR:
            continue
        FORWARD_ACTIVE[key] += 1
        updated = await forward_queue.find_one_and_update(
            {"_id": job["_id"], "status": "pending"},
            {"$set": {"status": "processing", "started": now}},
        )
        if updated is None:
            # Race condition – another worker grabbed it
            FORWARD_ACTIVE[key] = max(0, FORWARD_ACTIVE[key] - 1)
            continue
        return job
    return None


# ── media send helpers ────────────────────────────────────────────────────────
async def _forward_with_thumb(client: Client, src: int, dst: int, msg) -> None:
    """
    Re-sends the media preserving its thumbnail.
    Falls back to copy_message when no special handling is needed.
    """
    thumb_path = None
    try:
        media_type = None
        media_obj  = None
        for t in ("video", "document", "animation"):
            obj = getattr(msg, t, None)
            if obj:
                media_type = t
                media_obj  = obj
                break

        caption  = msg.caption or ""
        has_thumb = bool(
            media_obj and getattr(media_obj, "thumbs", None)
        )

        if media_type == "video" and has_thumb:
            thumb_path = await client.download_media(
                media_obj.thumbs[0].file_id,
                file_name=f"/tmp/thumb_ff_{msg.id}.jpg",
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
                parse_mode=None,
            )
        elif media_type == "animation" and has_thumb:
            thumb_path = await client.download_media(
                media_obj.thumbs[0].file_id,
                file_name=f"/tmp/thumb_ff_{msg.id}.jpg",
            )
            await client.send_animation(
                chat_id=dst,
                animation=media_obj.file_id,
                caption=caption,
                thumb=thumb_path,
                parse_mode=None,
            )
        elif media_type == "document" and has_thumb:
            thumb_path = await client.download_media(
                media_obj.thumbs[0].file_id,
                file_name=f"/tmp/thumb_ff_{msg.id}.jpg",
            )
            await client.send_document(
                chat_id=dst,
                document=media_obj.file_id,
                caption=caption,
                thumb=thumb_path,
                parse_mode=None,
            )
        else:
            # Fast path – no thumb needed
            await client.copy_message(
                chat_id=dst,
                from_chat_id=src,
                message_id=msg.id,
            )
    finally:
        if thumb_path:
            try:
                os.remove(thumb_path)
            except Exception:
                pass


# ── forward worker ────────────────────────────────────────────────────────────
async def forward_worker(client: Client):
    """
    Single long-running worker.  Pulls one job at a time, sends it,
    handles FloodWait with exponential back-off, then loops.
    Only FORWARD_WORKERS of these run concurrently (set in database.py).
    """
    while True:
        job = await _fetch_forward_job()
        if not job:
            await asyncio.sleep(1)
            continue

        key        = (job["src"], job["dst"])
        session_id = job.get("session_id")
        msg_id     = job.get("msg_id")

        try:
            if session_id in CANCELLED_SESSIONS:
                await forward_done(job["_id"])
                continue

            msg = await client.get_messages(job["src"], msg_id)
            await _forward_with_thumb(client, job["src"], job["dst"], msg)

            # Dump copy (non-admin users)
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
                        caption=fname,
                    )
                except Exception as e:
                    print(f"[FF_DUMP_FAIL] {e}")

            await forward_done(job["_id"])
            await _maybe_update_progress(client, job)
            await asyncio.sleep(FORWARD_DELAY)

        except FloodWait as e:
            retries = job.get("retries", 0)
            # Exponential back-off: base wait + 2^retries seconds, max 300 s
            wait = min(300, int(e.value) + 2 + (2 ** min(retries, 7)))
            print(f"[FF_WORKER] FloodWait {wait}s on ({key})")
            FORWARD_COOLDOWN[key] = time.time() + wait
            await forward_retry(job["_id"], wait)

        except Exception as e:
            print(f"[FF_WORKER_ERR] {e}")
            await forward_done(job["_id"])  # don't retry unknown errors forever

        finally:
            FORWARD_ACTIVE[key] = max(0, FORWARD_ACTIVE[key] - 1)


# ── rate-limited progress update ──────────────────────────────────────────────
async def _maybe_update_progress(client: Client, job: dict):
    """
    Update the progress message after each forwarded file.

    Race-condition fix:
    Before this fix, when multiple workers finished the last few jobs
    simultaneously, they all saw remaining==0 and all tried to send the
    completion message, causing:
      (a) duplicate "✅ Forwarding completed" messages, or
      (b) a worker that finished just BEFORE the last one sending the
          completion message early, then the final worker overwriting it
          with a stale "in-progress" edit.

    Fix: _session_completed is a set that is checked+updated atomically
    (Python's GIL makes single-dict/set operations thread-safe in asyncio).
    Only the first worker to see remaining==0 sends the completion message.
    All subsequent workers for the same session bail out immediately.
    """
    session = job.get("session_id")
    if not session or session in CANCELLED_SESSIONS:
        return

    # Already completed by another worker for this session
    if session in _session_completed:
        return

    _session_done_count[session] += 1
    done = _session_done_count[session]

    remaining = await forward_queue.count_documents({"session_id": session})

    if remaining == 0:
        # Guard: only the first worker to reach this point sends the final msg.
        # Check-then-set is safe here because asyncio is single-threaded and
        # there is no await between the check and the add.
        if session in _session_completed:
            return
        _session_completed.add(session)

        final_done = _session_done_count.pop(session, done)
        CANCELLED_SESSIONS.discard(session)

        try:
            await client.edit_message_text(
                job["chat_id"],
                job["ui_msg"],
                (
                    "✅ <b>Forwarding completed</b>\n\n"
                    f"📤 <b>Source:</b> {job['source_title']}\n"
                    f"📥 <b>Destination:</b> {job['destination_title']}\n\n"
                    f"📦 <b>Files forwarded:</b> <code>{final_done}</code>"
                ),
            )
        except Exception:
            pass
        # Delayed cleanup so any late-arriving workers still see it and bail
        async def _cleanup_session():
            await asyncio.sleep(30)
            _session_completed.discard(session)
        asyncio.create_task(_cleanup_session())
        return

    # Rate-limit intermediate updates to avoid FloodWait on edit_message_text
    if done % _PROGRESS_EVERY != 0:
        return

    # Don't overwrite a completion message that was just sent
    if session in _session_completed:
        return

    frame = ANIM_FRAMES[int(time.time()) % len(ANIM_FRAMES)]
    total = job.get("total", 0)
    pct   = int(((total - remaining) / total) * 100) if total > 0 else 0
    text  = (
        f"📤 <b>{job['source_title']}</b>\n"
        f"         ⬇️⬇️⬇️\n"
        f"📥 <b>{job['destination_title']}</b>\n\n"
        f"{frame}\n"
        f"<code>{pct}%</code> — {total - remaining}/{total} done"
    )
    try:
        await client.edit_message_text(
            job["chat_id"],
            job["ui_msg"],
            text,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]]
            ),
        )
    except Exception:
        pass


# ── cancel ────────────────────────────────────────────────────────────────────
@Client.on_callback_query(filters.regex("^ff_cancel$"))
async def ff_cancel(client, query):
    uid = query.from_user.id
    s   = FF_SESSIONS.pop(uid, None)
    if not s:
        await query.message.edit_text("❌ Nothing to cancel.")
        return

    session_id = s.get("session_id")
    if session_id:
        CANCELLED_SESSIONS.add(session_id)
        remaining = await forward_queue.count_documents({"session_id": session_id})
        total     = s.get("total", 0)
        sent      = max(total - remaining, 0)
        await forward_queue.delete_many({"session_id": session_id})
        _session_done_count.pop(session_id, None)
        _session_completed.discard(session_id)
        await query.message.edit_text(
            "🛑 <b>Forwarding cancelled</b>\n\n"
            f"📦 <b>Files sent:</b> <code>{sent}</code>\n"
            f"🗂 <b>Initially detected:</b> <code>{total}</code>"
        )
    else:
        await query.message.edit_text("🛑 Cancelled.")
