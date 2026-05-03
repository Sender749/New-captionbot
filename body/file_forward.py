import asyncio
import time
import uuid, re, os
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait
from pyrogram.enums import ParseMode
from body.database import (
    forward_queue, enqueue_forward, forward_done, forward_retry,
    fetch_forward_job_for_session, is_dump_skip, get_user_channels,
    get_maintenance,
)
from body.state import FF_SESSIONS, CANCELLED_SESSIONS, _USER_FORWARD_TASKS
from collections import defaultdict
from info import FF_CH, ADMIN

# ─────────────────────────────────────────────
#  Original global state
# ─────────────────────────────────────────────
FORWARD_ACTIVE   = defaultdict(int)   # (src, dst) -> active
FORWARD_COOLDOWN = {}                 # (src, dst) -> unblock time
MAX_FORWARD_PER_PAIR = 1
FORWARD_DELAY        = 0.3
FORWARD_EXECUTORS    = 12             # kept for reference; no longer used directly

ANIM_FRAMES = [
    "🔄 Transferring files",
    "🔄 Transferring files.",
    "🔄 Transferring files..",
    "🔄 Transferring files...",
]

USERNAME_RE = re.compile(r'@\w+', flags=re.IGNORECASE)
URL_RE      = re.compile(r'(https?://\S+|t\.me/\S+)', flags=re.IGNORECASE)
HTML_TAG_RE = re.compile(r'<[^>]+>')
MD_LINK_RE  = re.compile(r'\[([^\]]+)\]\([^)]+\)')
MSG_LINK_RE = re.compile(
    r'(?:https?://)?t\.me/(?:c/(\d+)|([A-Za-z0-9_]+))/(\d+)',
    flags=re.IGNORECASE,
)


# ─────────────────────────────────────────────
#  Startup hook
# ─────────────────────────────────────────────
def on_bot_start(client: Client):
    # Per-session model: no global workers needed.
    # Kept as a hook so bot.py's plugin scanner still calls it cleanly.
    pass


# ─────────────────────────────────────────────
#  Helpers  (originals)
# ─────────────────────────────────────────────
def clean_text(text: str) -> str:
    if not text:
        return ""
    text = MD_LINK_RE.sub(r'\1', text)
    text = HTML_TAG_RE.sub('', text)
    text = URL_RE.sub('', text)
    text = USERNAME_RE.sub('', text)
    return re.sub(r'\s+', ' ', text).strip()


def _parse_single(text: str):
    text = text.strip()
    m    = MSG_LINK_RE.search(text)
    if m:
        numeric_cid = m.group(1)
        msg_id      = int(m.group(3))
        if numeric_cid:
            return int(f"-100{numeric_cid}"), msg_id
        return None, msg_id
    if text.isdigit():
        return None, int(text)
    return None, None


def parse_forward_input(raw: str):
    parts = re.split(r'\s*-\s*(?=\S)', raw, maxsplit=1)
    if len(parts) == 2:
        sh1, start_id = _parse_single(parts[0])
        sh2, end_id   = _parse_single(parts[1])
        if start_id is None or end_id is None:
            return {"error": "❌ Could not parse start or end message reference."}
        if start_id > end_id:
            return {"error": "❌ Start message ID must be less than end message ID."}
        return {"skip_id": start_id - 1, "end_id": end_id,
                "src_hint": sh1 or sh2, "error": None}
    if raw.strip() == "0":
        return {"skip_id": 0, "end_id": None, "src_hint": None, "error": None}
    sh, mid = _parse_single(raw.strip())
    if mid is None:
        return {"error": "❌ Invalid message link or ID.\n\nSend a Telegram message link, a message ID, or 0 to forward all."}
    return {"skip_id": mid, "end_id": None, "src_hint": sh, "error": None}


async def validate_msg_in_channel(client: Client, channel_id: int, msg_id: int) -> bool:
    try:
        msg = await client.get_messages(channel_id, msg_id)
        return msg is not None and not getattr(msg, 'empty', True)
    except Exception:
        return False


# ─────────────────────────────────────────────
#  Callbacks — source / destination selection
# ─────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^ff_src_(-?\d+)$"))
async def ff_src(client, query):
    await query.answer()
    uid = query.from_user.id
    s   = FF_SESSIONS.get(uid)
    if not s:
        return await query.message.edit_text("⚠️ Session expired. Use /file_forward again.")
    src = int(query.matches[0].group(1))
    s["source"]       = src
    s["source_title"] = next(
        x["channel_title"] for x in s["channels"] if x["channel_id"] == src
    )
    s["channels"] = [x for x in s["channels"] if x["channel_id"] != src]
    s["step"]     = "dst"
    kb = [[InlineKeyboardButton(x["channel_title"],
                                callback_data=f"ff_dst_{x['channel_id']}")]
          for x in s["channels"]]
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")])
    await query.message.edit_text(
        "📥 **Select DESTINATION channel**",
        reply_markup=InlineKeyboardMarkup(kb),
    )


@Client.on_callback_query(filters.regex(r"^ff_dst_(-?\d+)$"))
async def ff_dst(client, query):
    await query.answer()
    uid = query.from_user.id
    s   = FF_SESSIONS.get(uid)
    if not s:
        return await query.message.edit_text("⚠️ Session expired. Use /file_forward again.")
    dst = int(query.matches[0].group(1))
    s["destination"]       = dst
    s["destination_title"] = next(
        x["channel_title"] for x in s["channels"] if x["channel_id"] == dst
    )
    s["step"]    = "skip"
    s["chat_id"] = query.message.chat.id
    s["msg_id"]  = query.message.id
    s["expires"] = time.time() + 900   # 15 minutes
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


# ─────────────────────────────────────────────
#  Scan & enqueue — per-user background task
# ─────────────────────────────────────────────
async def _scan_and_enqueue(client: Client, uid: int):
    """
    Background task: scans source channel and inserts jobs into DB.
    ISOLATED per user — no effect on other users' caption or forward tasks.
    """
    s = FF_SESSIONS.get(uid)
    if not s:
        return
    session_id = s["session_id"]
    src        = s["source"]
    dst        = s["destination"]
    start_id   = int(s["skip"]) + 1
    end_id     = s.get("end_id")

    s["total"] = 0
    msg_id     = start_id
    consec_miss= 0
    MAX_MISS   = 500

    while True:
        if end_id is not None and msg_id > end_id:
            break
        await asyncio.sleep(0)   # yield — keep other handlers running
        try:
            msg = await client.get_messages(src, msg_id)
        except FloodWait as e:
            await asyncio.sleep(int(e.value) + 1)
            continue
        except Exception:
            msg = None

        if not msg or getattr(msg, 'empty', True):
            consec_miss += 1
            if consec_miss >= MAX_MISS:
                break
            msg_id += 1
            continue

        consec_miss = 0
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
        msg_id     += 1

    # Stamp real total on all pending jobs for this session
    await forward_queue.update_many(
        {"session_id": session_id, "total": 0},
        {"$set": {"total": s["total"]}},
    )

    if session_id not in CANCELLED_SESSIONS:
        try:
            await client.edit_message_text(
                s["chat_id"], s["msg_id"],
                f"📤 <b>{s['source_title']}</b>\n"
                f"         ⬇️⬇️⬇️\n"
                f"📥 <b>{s['destination_title']}</b>\n\n"
                "🔄 Preparing files for transfer…",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]]
                ),
            )
        except Exception:
            pass

    # ── Phase 2: per-user forward worker ─────────────────────────────────
    await _run_user_forward_worker(client, uid)


async def _run_user_forward_worker(client: Client, uid: int):
    """
    Process all forward jobs for this user's session in an isolated coroutine.
    Uses the original forward_worker logic — FloodWait, dump copy, progress UI.
    """
    s = FF_SESSIONS.get(uid)
    if not s:
        _USER_FORWARD_TASKS.pop(uid, None)
        return
    session_id = s["session_id"]

    while True:
        job = await fetch_forward_job_for_session(session_id)
        if not job:
            await asyncio.sleep(1)
            job = await fetch_forward_job_for_session(session_id)
            if not job:
                break

        key = (job["src"], job["dst"])
        try:
            if session_id in CANCELLED_SESSIONS:
                await forward_done(job["_id"])
                continue

            msg = await client.get_messages(job["src"], job["msg_id"])
            await _forward_with_thumb(client, job["src"], job["dst"], msg)

            job_user = job.get("user_id")
            is_admin = (
                (job_user in ADMIN)
                if isinstance(ADMIN, (list, tuple, set))
                else (job_user == ADMIN)
            )
            if not is_admin and FF_CH:
                try:
                    fname = None
                    for t in ("document", "video", "audio", "voice"):
                        obj = getattr(msg, t, None)
                        if obj:
                            fname = getattr(obj, "file_name", None)
                            break
                    fname = clean_text(fname or "File")
                    await client.copy_message(
                        chat_id=int(FF_CH),
                        from_chat_id=job["src"],
                        message_id=job["msg_id"],
                        caption=fname,
                    )
                except Exception as e:
                    print(f"[FF_DUMP_FAIL] {e}")

            await forward_done(job["_id"])
            await _update_progress(client, job, session_id)
            await asyncio.sleep(FORWARD_DELAY)

        except FloodWait as e:
            wait  = int(e.value) + 2
            retries = job.get("retries", 0)
            wait += min(60, retries * 2)
            FORWARD_COOLDOWN[key] = time.time() + wait
            await forward_retry(job["_id"], wait)
            await asyncio.sleep(wait)
        except Exception as e:
            print(f"[FF_WORKER_ERR] uid={uid} msg={job.get('msg_id')}: {e}")
            await forward_done(job["_id"])

    _USER_FORWARD_TASKS.pop(uid, None)
    FF_SESSIONS.pop(uid, None)


async def _update_progress(client: Client, job: dict, session_id: str):
    if session_id in CANCELLED_SESSIONS:
        return
    frame = ANIM_FRAMES[int(time.time()) % len(ANIM_FRAMES)]
    text  = (
        f"📤 <b>{job['source_title']}</b>\n"
        f"         ⬇️⬇️⬇️\n"
        f"📥 <b>{job['destination_title']}</b>\n\n"
        f"{frame}"
    )
    try:
        await client.edit_message_text(
            job["chat_id"], job["ui_msg"], text,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]]
            ),
        )
    except Exception:
        pass
    remaining = await forward_queue.count_documents({"session_id": session_id})
    if remaining == 0:
        try:
            await client.edit_message_text(
                job["chat_id"], job["ui_msg"],
                "✅ <b>Forwarding completed</b>\n\n"
                f"📤 <b>Source:</b> {job['source_title']}\n"
                f"📥 <b>Destination:</b> {job['destination_title']}\n",
            )
        except Exception:
            pass


async def enqueue_forward_jobs(client: Client, uid: int):
    """
    Entry point from Caption.py's message handler.
    Shows instant 'Scanning…' feedback and fires a per-user background task.
    Returns immediately — the handler is freed at once.
    """
    s = FF_SESSIONS.get(uid)
    if not s:
        return
    if "session_id" not in s:
        s["session_id"] = str(uuid.uuid4())

    try:
        await client.edit_message_text(
            s["chat_id"], s["msg_id"],
            f"📤 <b>{s['source_title']}</b>\n"
            f"         ⬇️⬇️⬇️\n"
            f"📥 <b>{s['destination_title']}</b>\n\n"
            "🔄 Scanning files…",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]]
            ),
        )
    except Exception:
        pass

    # Cancel any previous task for this user
    old = _USER_FORWARD_TASKS.get(uid)
    if old and not old.done():
        old.cancel()

    # Each user's scanning + forwarding runs fully isolated
    task = asyncio.create_task(_scan_and_enqueue(client, uid))
    _USER_FORWARD_TASKS[uid] = task


# ─────────────────────────────────────────────
#  Thumbnail-preserving forward  (original)
# ─────────────────────────────────────────────
async def _forward_with_thumb(client: Client, src: int, dst: int, msg) -> None:
    thumb_path = None
    try:
        media_type = media_obj = None
        for t in ("video", "document", "animation"):
            obj = getattr(msg, t, None)
            if obj:
                media_type = t
                media_obj  = obj
                break

        caption   = msg.caption or ""
        has_thumb = bool(getattr(media_obj, "thumbs", None)) if media_obj else False

        if media_type == "video" and has_thumb:
            thumb_path = await client.download_media(
                media_obj.thumbs[0].file_id,
                file_name=f"/tmp/thumb_ff_{msg.id}.jpg",
            )
            await client.send_video(
                chat_id=dst, video=media_obj.file_id, caption=caption,
                thumb=thumb_path,
                duration=getattr(media_obj, "duration", 0),
                width=getattr(media_obj, "width", 0),
                height=getattr(media_obj, "height", 0),
                supports_streaming=True, parse_mode=None,
            )
        elif media_type in ("document", "animation") and has_thumb:
            thumb_path = await client.download_media(
                media_obj.thumbs[0].file_id,
                file_name=f"/tmp/thumb_ff_{msg.id}.jpg",
            )
            if media_type == "animation":
                await client.send_animation(
                    chat_id=dst, animation=media_obj.file_id,
                    caption=caption, thumb=thumb_path, parse_mode=None,
                )
            else:
                await client.send_document(
                    chat_id=dst, document=media_obj.file_id,
                    caption=caption, thumb=thumb_path, parse_mode=None,
                )
        else:
            await client.copy_message(chat_id=dst, from_chat_id=src, message_id=msg.id)
    finally:
        if thumb_path:
            try:
                os.remove(thumb_path)
            except Exception:
                pass


# ─────────────────────────────────────────────
#  Cancel
# ─────────────────────────────────────────────
@Client.on_callback_query(filters.regex("^ff_cancel$"))
async def ff_cancel(client, query):
    await query.answer()
    uid = query.from_user.id

    # Stop the running task
    task = _USER_FORWARD_TASKS.pop(uid, None)
    if task and not task.done():
        task.cancel()

    s = FF_SESSIONS.pop(uid, None)
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
        await query.message.edit_text(
            "🛑 <b>Forwarding cancelled</b>\n\n"
            f"📦 <b>Files sent:</b> <code>{sent}</code>\n"
            f"🗂 <b>Initially detected:</b> <code>{total}</code>",
        )
    else:
        await query.message.edit_text("🛑 Cancelled.")
