import asyncio
import time
import uuid, re, os
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait
from body.database import *
from collections import defaultdict

FORWARD_ACTIVE = defaultdict(int)        # (src, dst) -> active
FORWARD_COOLDOWN = {}                    # (src, dst) -> unblock time

MAX_FORWARD_PER_PAIR = 2
FORWARD_DELAY = 1
FORWARD_EXECUTORS = 4

FF_SESSIONS = {}
CANCELLED_SESSIONS = set()
USERNAME_RE = re.compile(r'@\w+', flags=re.IGNORECASE)
URL_RE = re.compile(r'(https?://\S+|t\.me/\S+)', flags=re.IGNORECASE)
HTML_TAG_RE = re.compile(r'<[^>]+>')
MD_LINK_RE = re.compile(r'\[([^\]]+)\]\([^)]+\)')
# Matches t.me/c/CHANNEL_ID/MSG_ID or t.me/USERNAME/MSG_ID
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

# ---------- START WORKERS ----------
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


def _parse_single(text: str):
    """
    Parse a single msg reference (link or int).
    Returns (channel_id_or_None, msg_id_or_None).
    """
    text = text.strip()
    m = MSG_LINK_RE.search(text)
    if m:
        numeric_cid = m.group(1)   # c/XXXXXXXX style
        msg_id = int(m.group(3))
        if numeric_cid:
            return int(f"-100{numeric_cid}"), msg_id
        return None, msg_id
    if text.isdigit():
        return None, int(text)
    return None, None


def parse_forward_input(raw: str):
    """
    Parse the user's skip/range input.
    Supports:
      - "0"                      -> skip=0, end=None (all)
      - "123"                    -> skip=123, end=None
      - "link"                   -> skip from link, end=None
      - "start - end" (links/ids)-> start and end msg ids

    Returns dict with keys:
      skip_id   (int) - forward starts AFTER this id (0 = from beginning)
      end_id    (int|None) - last msg id to forward (None = no limit)
      src_hint  (int|None) - channel id extracted from link (for validation)
      error     (str|None) - human-readable error if parse failed
    """
    # check for range notation: split by " - "
    parts = re.split(r'\s*-\s*(?=\S)', raw, maxsplit=1)
    if len(parts) == 2:
        # start-end range
        src_hint1, start_id = _parse_single(parts[0])
        src_hint2, end_id = _parse_single(parts[1])
        if start_id is None or end_id is None:
            return {"error": "❌ Could not parse start or end message reference."}
        if start_id > end_id:
            return {"error": "❌ Start message ID must be less than end message ID."}
        src_hint = src_hint1 or src_hint2
        return {"skip_id": start_id - 1, "end_id": end_id, "src_hint": src_hint, "error": None}
    else:
        # single reference
        if raw.strip() == "0":
            return {"skip_id": 0, "end_id": None, "src_hint": None, "error": None}
        src_hint, msg_id = _parse_single(raw.strip())
        if msg_id is None:
            return {"error": "❌ Invalid message link or ID.\n\nSend a Telegram message link, a message ID, or 0 to forward all."}
        return {"skip_id": msg_id, "end_id": None, "src_hint": src_hint, "error": None}


async def validate_msg_in_channel(client: Client, channel_id: int, msg_id: int) -> bool:
    """Check that msg_id actually belongs to channel_id."""
    try:
        msg = await client.get_messages(channel_id, msg_id)
        return msg is not None and not getattr(msg, 'empty', True)
    except Exception:
        return False


# ---------- SOURCE ----------
@Client.on_callback_query(filters.regex(r"^ff_src_(-?\d+)$"))
async def ff_src(client, query):
    uid = query.from_user.id
    s = FF_SESSIONS.get(uid)
    if not s:
        return
    src = int(query.matches[0].group(1))
    s["source"] = src
    s["source_title"] = next(x["channel_title"] for x in s["channels"] if x["channel_id"] == src)
    s["channels"] = [x for x in s["channels"] if x["channel_id"] != src]
    s["step"] = "dst"
    kb = [[InlineKeyboardButton(x["channel_title"], callback_data=f"ff_dst_{x['channel_id']}")] for x in s["channels"]]
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")])
    await query.message.edit_text(
        "📥 **Select DESTINATION channel**",
        reply_markup=InlineKeyboardMarkup(kb)
    )

# ---------- DEST ----------
@Client.on_callback_query(filters.regex(r"^ff_dst_(-?\d+)$"))
async def ff_dst(client, query):
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
        disable_web_page_preview=True
    )

# ---------- ENQUEUE ----------
async def enqueue_forward_jobs(client: Client, uid: int):
    s = FF_SESSIONS[uid]
    if "session_id" not in s:
        s["session_id"] = str(uuid.uuid4())
    session_id = s["session_id"]
    src = s["source"]
    dst = s["destination"]
    start_id = int(s["skip"]) + 1        # inclusive start
    end_id = s.get("end_id")             # None = no upper limit

    s["total"] = 0
    msg_id = start_id
    consecutive_missing = 0
    MAX_CONSECUTIVE_MISSING = 500
    while True:
        if end_id is not None and msg_id > end_id:
            break
        try:
            msg = await client.get_messages(src, msg_id)
        except Exception:
            msg = None
        if not msg or getattr(msg, 'empty', True):
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
        msg_id += 1
    await forward_queue.update_many(
        {"src": src, "dst": dst, "total": 0},
        {"$set": {"total": s["total"]}}
    )
    await client.edit_message_text(
        s["chat_id"],
        s["msg_id"],
        (
            f"📤 <b>{s['source_title']}</b>\n"
            f"         ⬇️⬇️⬇️\n"
            f"📥 <b>{s['destination_title']}</b>\n\n"
            "🔄 Preparing files for transfer…"
        ),
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]]))

# ================= FORWARD SCHEDULER STATE =================
async def fetch_forward_fair_job():
    now = time.time()
    cursor = forward_queue.find(
        {"status": "pending"}
    ).sort("ts", 1)
    async for job in cursor:
        key = (job["src"], job["dst"])
        if FORWARD_COOLDOWN.get(key, 0) > now:
            continue
        if FORWARD_ACTIVE[key] >= MAX_FORWARD_PER_PAIR:
            continue
        FORWARD_ACTIVE[key] += 1
        await forward_queue.update_one(
            {"_id": job["_id"], "status": "pending"},
            {"$set": {"status": "processing", "started": now}}
        )
        return job
    return None


async def _forward_with_thumb(client: Client, src: int, dst: int, msg) -> None:
    """
    Forward a media message preserving its original thumbnail.
    Falls back to copy_message if special handling is not needed.
    """
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
            # No thumb needed – fast copy_message path
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


# ================= IMPROVED FORWARD WORKER =================
async def forward_worker(client: Client):
    while True:
        job = await fetch_forward_fair_job()
        if not job:
            await asyncio.sleep(1)
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
            if job_user != ADMIN:
                try:
                    fname = None
                    for t in ("document", "video", "audio", "voice"):
                        obj = getattr(msg, t, None)
                        if obj:
                            fname = getattr(obj, "file_name", None)
                            break
                    if not fname:
                        fname = "File"
                    fname = clean_text(fname)
                    await client.copy_message(
                        chat_id=FF_CH,
                        from_chat_id=job["src"],
                        message_id=msg_id,
                        caption=fname
                    )
                except Exception as e:
                    print(f"[FF_DUMP_FAIL] {e}")
            await forward_done(job["_id"])
            await update_forward_progress(client, job)
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
            FORWARD_ACTIVE[key] -= 1

# ---------- PROGRESS ----------
async def update_forward_progress(client: Client, job):
    session = job.get("session_id")
    if session in CANCELLED_SESSIONS:
        return
    frame = ANIM_FRAMES[int(time.time()) % len(ANIM_FRAMES)]
    text = (
        f"📤 <b>{job['source_title']}</b>\n"
        f"         ⬇️⬇️⬇️\n"
        f"📥 <b>{job['destination_title']}</b>\n\n"
        f"{frame}"
    )
    try:
        await client.edit_message_text(
            job["chat_id"],
            job["ui_msg"],
            text,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]]))
    except:
        pass
    remaining = await forward_queue.count_documents({"session_id": session})
    if remaining == 0:
        try:
            await client.edit_message_text(
                job["chat_id"],
                job["ui_msg"],
                (
                    "✅ <b>Forwarding completed</b>\n\n"
                    f"📤 <b>Source:</b> {job['source_title']}\n"
                    f"📥 <b>Destination:</b> {job['destination_title']}\n\n"
                )
            )
        except:
            pass

# ---------- CANCEL ----------
@Client.on_callback_query(filters.regex("^ff_cancel$"))
async def ff_cancel(client, query):
    uid = query.from_user.id
    s = FF_SESSIONS.pop(uid, None)
    if not s:
        await query.message.edit_text("❌ Nothing to cancel.")
        return
    session_id = s.get("session_id")
    if session_id:
        CANCELLED_SESSIONS.add(session_id)
        remaining = await forward_queue.count_documents(
            {"session_id": session_id}
        )
        total = s.get("total", 0)
        sent = max(total - remaining, 0)
        await forward_queue.delete_many(
            {"session_id": session_id}
        )
        await query.message.edit_text(
            "🛑 <b>Forwarding cancelled</b>\n\n"
            f"📦 <b>Files sent:</b> <code>{sent}</code>\n"
            f"🗂 <b>Initially detected:</b> <code>{total}</code>"
        )
    else:
        await query.message.edit_text("🛑 Cancelled.")
