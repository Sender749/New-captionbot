import asyncio
import os
import re
import time
import uuid
from collections import defaultdict

from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.enums import ParseMode

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

FF_SESSIONS        = {}               # uid -> session dict
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

# Rate-limit progress edits: update UI every N completions (per session)
_PROGRESS_EVERY = 3

# Per-session counters: session_id -> number of files forwarded so far
_session_done_count: dict[str, int] = defaultdict(int)

# Sessions whose completion message has already been sent.
# Prevents multiple workers racing to send "✅ Forwarding completed".
_session_completed: set = set()


async def _edit_with_retry(client: Client, chat_id, msg_id, text, reply_markup=None, max_retries: int = 4) -> bool:
    """
    Like client.edit_message_text but retries on FloodWait / transient errors
    instead of silently giving up. Used for the completion message so that a
    rate-limit hit can never leave the UI stuck on "🔄 Transferring files…"
    after forwarding has actually finished.
    """
    for attempt in range(max_retries):
        try:
            await client.edit_message_text(chat_id, msg_id, text, reply_markup=reply_markup)
            return True
        except FloodWait as e:
            await asyncio.sleep(int(e.value) + 1)
        except Exception as e:
            # MessageNotModified etc. — nothing more we can do, and nothing to retry
            print(f"[FF_EDIT_RETRY_FAIL] attempt={attempt} err={e}")
            if attempt == max_retries - 1:
                return False
            await asyncio.sleep(1)
    return False


# ── startup hook ──────────────────────────────────────────────────────────────
def on_bot_start(client: Client):
    """Launch the fixed pool of forward workers once at bot start.

    Each worker is wrapped so that if it ever exits unexpectedly (it
    shouldn't, now that _fetch_forward_job() errors are caught above, but
    this is a safety net matching the caption-queue supervisor), it's
    logged and restarted instead of permanently shrinking the pool.
    """
    async def _guarded(i):
        while True:
            try:
                await forward_worker(client)
            except Exception as e:
                print(f"[FF_WORKER_{i}] crashed unexpectedly, restarting in 3s: {e}")
            else:
                print(f"[FF_WORKER_{i}] exited unexpectedly, restarting in 3s")
            await asyncio.sleep(3)

    for i in range(FORWARD_WORKERS):
        asyncio.create_task(_guarded(i), name=f"ff_worker_{i}")
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
    s.pop("pending_input", None)
    await _show_ff_range_prompt(client, s["chat_id"], s["msg_id"])


async def _show_ff_range_prompt(client: Client, chat_id, msg_id):
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
        "<code>https://t.me/c/1815162626/100</code>\n"
        "<code>100 - 500</code>\n"
        "<code>https://t.me/c/1234/100 - https://t.me/c/1234/500</code>\n\n"
        "• Session expires in <b>15 minutes</b>",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]]
        ),
    )


# ── scan & enqueue (background task, one per user session) ───────────────────
async def _scan_and_enqueue(client: Client, uid: int):
    """
    Scans the source channel and writes one DB job per media message.
    Runs entirely in the background – never blocks caption workers.
    Uses asyncio.sleep(0) on every iteration so the event loop stays free.

    FIX: total count is now tracked on the session dict (s["total"]) and
    also stored on each job document once scanning finishes.  Workers read
    total from the session, not from the job, so they always get the correct
    value even while scanning is still running.
    """
    s = FF_SESSIONS.get(uid)
    if not s:
        return
    session_id = s["session_id"]
    src        = s["source"]
    dst        = s["destination"]
    start_id   = int(s["skip"]) + 1
    end_id     = s.get("end_id")

    # Reset counters for this scan
    s["total"]     = 0
    s["forwarded"] = 0   # ← NEW: track forwarded count on session
    msg_id              = start_id
    consecutive_missing = 0
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
            # NOTE: total=0 here; we stamp the real value below after scan
            "total":             0,
        })
        s["total"] += 1
        msg_id += 1

    # ── Stamp actual total on all pending jobs for this session ───────────────
    # Workers that started before this runs will update their total from the
    # session dict via _maybe_update_progress — the DB value is a fallback.
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

    # Mark scan complete on session so workers know the total is final
    s["scan_done"] = True


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

    # Reset per-session counters
    _session_done_count.pop(s["session_id"], None)
    _session_completed.discard(s["session_id"])

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
    Re-sends the media preserving its thumbnail, with the original caption
    and entities left completely untouched (file forwarding is now a plain
    copy — no caption customization).
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

        has_thumb = bool(media_obj and getattr(media_obj, "thumbs", None))

        if media_type == "video" and has_thumb:
            thumb_path = await client.download_media(
                media_obj.thumbs[0].file_id,
                file_name=f"/tmp/thumb_ff_{msg.id}.jpg",
            )
            await client.send_video(
                chat_id=dst,
                video=media_obj.file_id,
                caption=msg.caption or "",
                thumb=thumb_path,
                duration=getattr(media_obj, "duration", 0),
                width=getattr(media_obj, "width", 0),
                height=getattr(media_obj, "height", 0),
                supports_streaming=True,
            )
        elif media_type == "animation" and has_thumb:
            thumb_path = await client.download_media(
                media_obj.thumbs[0].file_id,
                file_name=f"/tmp/thumb_ff_{msg.id}.jpg",
            )
            await client.send_animation(
                chat_id=dst,
                animation=media_obj.file_id,
                caption=msg.caption or "",
                thumb=thumb_path,
            )
        elif media_type == "document" and has_thumb:
            thumb_path = await client.download_media(
                media_obj.thumbs[0].file_id,
                file_name=f"/tmp/thumb_ff_{msg.id}.jpg",
            )
            await client.send_document(
                chat_id=dst,
                document=media_obj.file_id,
                caption=msg.caption or "",
                thumb=thumb_path,
            )
        else:
            # Fast path – no special thumb handling needed, plain copy
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
        try:
            job = await _fetch_forward_job()
        except Exception as e:
            print(f"[FF_WORKER] _fetch_forward_job error: {e}")
            await asyncio.sleep(2)
            continue
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

            # ── FIX: increment session forwarded counter ───────────────────
            # Read total from session dict (accurate) not from job document.
            uid = job.get("user_id")
            s = FF_SESSIONS.get(uid) if uid else None
            if s and s.get("session_id") == session_id:
                s["forwarded"] = s.get("forwarded", 0) + 1

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

    BUG FIXES vs previous version:
    ─────────────────────────────────────────────────────────────────────────
    1. "Still showing transferring after all done":
       Old code counted `remaining = count_documents()` which raced with
       deletes from multiple concurrent workers.  A worker deleting the last
       document could race with another still checking — both would see
       remaining > 0.
       Fix: use session["forwarded"] >= session["total"] as the completion
       check.  This is updated atomically in the asyncio single-thread and
       needs no DB round-trip.

    2. Inaccurate total in progress:
       Old code read total from job["total"] which was 0 until the scan
       finished and stamped it.  Early jobs always showed "0/0".
       Fix: read total from FF_SESSIONS[uid]["total"] which is updated live
       during the scan.  Fall back to job["total"] only if session is gone.

    3. Per-session done counter was never reset between sessions:
       Fix: counter is reset in enqueue_forward_jobs() when a new session
       starts, so the first _PROGRESS_EVERY files always show an update.

    4. Completion message could be sent before scan finished (total still 0):
       Fix: only send completion if session["scan_done"] is True.
    ─────────────────────────────────────────────────────────────────────────
    """
    session_id = job.get("session_id")
    if not session_id or session_id in CANCELLED_SESSIONS:
        return

    # Already completed by another worker for this session
    if session_id in _session_completed:
        return

    # ── Read counters from session dict (authoritative) ───────────────────
    uid = job.get("user_id")
    s   = FF_SESSIONS.get(uid) if uid else None

    if s and s.get("session_id") == session_id:
        forwarded  = s.get("forwarded", 0)
        total      = s.get("total", 0)
        scan_done  = s.get("scan_done", False)
    else:
        # Session was cleaned up (e.g. cancelled); fall back to DB count
        forwarded  = _session_done_count.get(session_id, 0) + 1
        total      = job.get("total", 0)
        scan_done  = True  # if session is gone, assume scan finished

    _session_done_count[session_id] = forwarded

    # ── Completion check ──────────────────────────────────────────────────
    # Only complete if: scan has finished AND all files have been forwarded
    is_complete = scan_done and total > 0 and forwarded >= total

    if is_complete:
        # Guard: only the first worker to reach this point sends the final msg.
        if session_id in _session_completed:
            return
        _session_completed.add(session_id)

        # Cleanup session
        if uid and uid in FF_SESSIONS and FF_SESSIONS[uid].get("session_id") == session_id:
            FF_SESSIONS.pop(uid, None)
        _session_done_count.pop(session_id, None)
        CANCELLED_SESSIONS.discard(session_id)

        try:
            await _edit_with_retry(
                client,
                job["chat_id"],
                job["ui_msg"],
                (
                    "✅ <b>Forwarding completed</b>\n\n"
                    f"📤 <b>Source:</b> {job['source_title']}\n"
                    f"📥 <b>Destination:</b> {job['destination_title']}\n\n"
                    f"📦 <b>Files forwarded:</b> <code>{forwarded}</code>\n"
                    f"🗂 <b>Total detected:</b> <code>{total}</code>"
                ),
            )
        except Exception:
            pass

        # Delayed cleanup so any late-arriving workers still see it and bail
        async def _cleanup_session():
            await asyncio.sleep(30)
            _session_completed.discard(session_id)

        asyncio.create_task(_cleanup_session())
        return

    # ── Rate-limit intermediate updates ───────────────────────────────────
    if forwarded % _PROGRESS_EVERY != 0:
        return

    # Don't overwrite a completion message that was just sent
    if session_id in _session_completed:
        return

    # Build progress bar
    pct      = int((forwarded / total) * 100) if total > 0 else 0
    bar_fill = int(pct / 10)
    bar      = "▓" * bar_fill + "░" * (10 - bar_fill)
    frame    = ANIM_FRAMES[int(time.time()) % len(ANIM_FRAMES)]

    text = (
        f"📤 <b>{job['source_title']}</b>\n"
        f"         ⬇️⬇️⬇️\n"
        f"📥 <b>{job['destination_title']}</b>\n\n"
        f"{frame}\n"
        f"[{bar}] <code>{pct}%</code>\n"
        f"📦 <b>Forwarded:</b> <code>{forwarded}</code> / <code>{total if total > 0 else '?'}</code>"
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
        forwarded = s.get("forwarded", 0)
        total     = s.get("total", 0)

        # Clean up pending DB jobs for this session
        await forward_queue.delete_many({"session_id": session_id})
        _session_done_count.pop(session_id, None)
        _session_completed.discard(session_id)

        await query.message.edit_text(
            "🛑 <b>Forwarding cancelled</b>\n\n"
            f"📦 <b>Files sent:</b> <code>{forwarded}</code>\n"
            f"🗂 <b>Total detected:</b> <code>{total}</code>"
        )
    else:
        await query.message.edit_text("🛑 Cancelled.")
