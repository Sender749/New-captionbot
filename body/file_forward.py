import asyncio
import time
import uuid
import re
import os
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from pyrogram.errors import FloodWait
from pyrogram.enums import ParseMode
from body.database import (
    forward_queue, enqueue_forward, forward_done, forward_retry,
    fetch_forward_job_for_session, FORWARD_COOLDOWN,
    is_dump_skip, get_user_channels,
)
from body.state import (
    FF_SESSIONS, CANCELLED_SESSIONS,
    _USER_FORWARD_TASKS,
)
from info import FF_CH, ADMIN

FORWARD_DELAY  = 0.3   # seconds between sends inside one session
MAX_CONCURRENT = 2     # parallel sends per session (keeps Telegram happy)

ANIM_FRAMES = [
    "🔄 Transferring files",
    "🔄 Transferring files.",
    "🔄 Transferring files..",
    "🔄 Transferring files...",
]

MSG_LINK_RE = re.compile(
    r'(?:https?://)?t\.me/(?:c/(\d+)|([A-Za-z0-9_]+))/(\d+)', re.I,
)
MD_LINK_RE  = re.compile(r'\[([^\]]+)\]\([^)]+\)', re.I)
URL_RE      = re.compile(r'(https?://\S+|t\.me/\S+)', re.I)
HTML_TAG_RE = re.compile(r'<[^>]+>')
MENTION_RE  = re.compile(r'@\w+', re.I)


# ─────────────────────────────────────────────
#  Startup hook
# ─────────────────────────────────────────────
def on_bot_start(client: Client):
    # Per-session model: no global workers needed at startup
    pass


# ─────────────────────────────────────────────
#  Input parsers / validators
# ─────────────────────────────────────────────
def _parse_single(text: str):
    text = text.strip()
    m    = MSG_LINK_RE.search(text)
    if m:
        cid    = int(f"-100{m.group(1)}") if m.group(1) else None
        msg_id = int(m.group(3))
        return cid, msg_id
    if text.isdigit():
        return None, int(text)
    return None, None


def parse_forward_input(raw: str) -> dict:
    parts = re.split(r'\s*-\s*(?=\S)', raw, maxsplit=1)
    if len(parts) == 2:
        sh1, s_id = _parse_single(parts[0])
        sh2, e_id = _parse_single(parts[1])
        if s_id is None or e_id is None:
            return {"error": "❌ Could not parse start or end message."}
        if s_id > e_id:
            return {"error": "❌ Start ID must be less than end ID."}
        return {"skip_id": s_id - 1, "end_id": e_id,
                "src_hint": sh1 or sh2, "error": None}
    if raw.strip() == "0":
        return {"skip_id": 0, "end_id": None, "src_hint": None, "error": None}
    sh, mid = _parse_single(raw.strip())
    if mid is None:
        return {"error": "❌ Invalid message link or ID.\n\nSend a link, a message ID, or <code>0</code> for all."}
    return {"skip_id": mid, "end_id": None, "src_hint": sh, "error": None}


async def validate_msg_in_channel(client: Client, channel_id: int, msg_id: int) -> bool:
    try:
        msg = await client.get_messages(channel_id, msg_id)
        return msg is not None and not getattr(msg, "empty", True)
    except Exception:
        return False


def clean_text(text: str) -> str:
    if not text: return ""
    text = MD_LINK_RE.sub(r'\1', text)
    text = HTML_TAG_RE.sub('', text)
    text = URL_RE.sub('', text)
    text = MENTION_RE.sub('', text)
    return re.sub(r'\s+', ' ', text).strip()


# ─────────────────────────────────────────────
#  Callback: source channel selected
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
        (x["channel_title"] for x in s["channels"] if x["channel_id"] == src), str(src)
    )
    # Remove source from destination list
    s["channels"] = [x for x in s["channels"] if x["channel_id"] != src]
    s["step"]     = "dst"

    kb = [[InlineKeyboardButton(x["channel_title"],
                                callback_data=f"ff_dst_{x['channel_id']}")]
          for x in s["channels"]]
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")])
    await query.message.edit_text(
        f"✅ <b>Source:</b> {s['source_title']}\n\n"
        "📥 <b>Select DESTINATION channel</b>\n"
        "<i>(files will be forwarded TO here)</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb),
    )


# ─────────────────────────────────────────────
#  Callback: destination channel selected
# ─────────────────────────────────────────────
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
        (x["channel_title"] for x in s["channels"] if x["channel_id"] == dst), str(dst)
    )
    s["step"]    = "skip"
    s["chat_id"] = query.message.chat.id
    s["msg_id"]  = query.message.id
    s["expires"] = time.time() + 900   # 15-min timeout

    await query.message.edit_text(
        f"✅ <b>Source:</b> {s['source_title']}\n"
        f"✅ <b>Destination:</b> {s['destination_title']}\n\n"
        "⏭ <b>Enter forwarding range</b>\n\n"
        "<blockquote expandable>"
        "<b>Options:</b>\n"
        "• <code>0</code> — forward <b>ALL</b> files\n"
        "• <code>msg_link</code> or <code>id</code> — start <b>AFTER</b> this message\n"
        "• <code>start - end</code> — forward a specific <b>RANGE</b>\n\n"
        "<b>Examples:</b>\n"
        "<code>0</code>\n"
        "<code>https://t.me/c/1234/100</code>\n"
        "<code>100 - 500</code>"
        "</blockquote>\n\n"
        "⏰ Session expires in <b>15 minutes</b>",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")
        ]]),
    )


# ─────────────────────────────────────────────
#  Entry point — called from Caption.py after range parsed
# ─────────────────────────────────────────────
async def enqueue_forward_jobs(client: Client, uid: int):
    """
    Shows instant scan UI, then launches isolated per-user task.
    Returns immediately so calling handler stays non-blocking.
    """
    s = FF_SESSIONS.get(uid)
    if not s:
        return
    if "session_id" not in s:
        s["session_id"] = str(uuid.uuid4())
    s.setdefault("total", 0)

    # Immediate feedback — user sees action right away
    try:
        await client.edit_message_text(
            s["chat_id"], s["msg_id"],
            f"📤 <b>{s['source_title']}</b>\n"
            f"         ⬇️⬇️⬇️\n"
            f"📥 <b>{s['destination_title']}</b>\n\n"
            "🔍 <b>Scanning for files…</b>",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")
            ]]),
        )
    except Exception:
        pass

    # Kill any previous task for this user
    old = _USER_FORWARD_TASKS.get(uid)
    if old and not old.done():
        old.cancel()

    task = asyncio.create_task(_run_forward_session(client, uid))
    _USER_FORWARD_TASKS[uid] = task


# ─────────────────────────────────────────────
#  Per-user isolated forward session
# ─────────────────────────────────────────────
async def _run_forward_session(client: Client, uid: int):
    """
    Fully isolated coroutine for one user's forwarding job.
    No other user is affected regardless of file count or speed.
    """
    s = FF_SESSIONS.get(uid)
    if not s:
        return

    session_id = s["session_id"]
    src        = s["source"]
    dst        = s["destination"]
    start_id   = int(s.get("skip", 0)) + 1
    end_id     = s.get("end_id")

    # ── Phase 1: scan & enqueue ──────────────────────────────────────────
    total       = 0
    cur_id      = start_id
    consec_miss = 0
    MAX_MISS    = 500

    while True:
        if session_id in CANCELLED_SESSIONS:
            _USER_FORWARD_TASKS.pop(uid, None)
            return
        if end_id is not None and cur_id > end_id:
            break

        await asyncio.sleep(0)   # yield to keep UI and other tasks alive
        try:
            msg = await client.get_messages(src, cur_id)
        except FloodWait as e:
            await asyncio.sleep(int(e.value) + 1)
            continue
        except Exception:
            msg = None

        if not msg or getattr(msg, "empty", True):
            consec_miss += 1
            if consec_miss >= MAX_MISS:
                break
            cur_id += 1
            continue

        consec_miss = 0
        if not msg.media:
            cur_id += 1
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
        total  += 1
        cur_id += 1

        # Refresh progress every 50 files during scan
        if total % 50 == 0:
            try:
                await client.edit_message_text(
                    s["chat_id"], s["msg_id"],
                    f"📤 <b>{s['source_title']}</b>\n"
                    f"         ⬇️⬇️⬇️\n"
                    f"📥 <b>{s['destination_title']}</b>\n\n"
                    f"🔍 Scanning… found <b>{total}</b> files so far",
                    parse_mode=ParseMode.HTML,
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")
                    ]]),
                )
            except Exception:
                pass

    # Stamp real total on all queued jobs
    if total > 0:
        await forward_queue.update_many(
            {"session_id": session_id, "total": 0},
            {"$set": {"total": total}},
        )
    s["total"] = total

    if session_id in CANCELLED_SESSIONS:
        _USER_FORWARD_TASKS.pop(uid, None)
        return

    if total == 0:
        try:
            await client.edit_message_text(
                s["chat_id"], s["msg_id"],
                "ℹ️ <b>No media files found</b> in the selected range.",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        FF_SESSIONS.pop(uid, None)
        _USER_FORWARD_TASKS.pop(uid, None)
        return

    try:
        await client.edit_message_text(
            s["chat_id"], s["msg_id"],
            f"📤 <b>{s['source_title']}</b>\n"
            f"         ⬇️⬇️⬇️\n"
            f"📥 <b>{s['destination_title']}</b>\n\n"
            f"📦 Found <b>{total}</b> files — starting transfer…",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")
            ]]),
        )
    except Exception:
        pass

    # ── Phase 2: process jobs with bounded concurrency ───────────────────
    sem  = asyncio.Semaphore(MAX_CONCURRENT)
    done = 0

    async def _send_one(job):
        nonlocal done
        async with sem:
            if session_id in CANCELLED_SESSIONS:
                await forward_done(job["_id"])
                return
            key = (job["src"], job["dst"])
            try:
                msg = await client.get_messages(job["src"], job["msg_id"])
                if msg and not getattr(msg, "empty", True):
                    await _forward_with_thumb(client, job["src"], job["dst"], msg)

                    is_admin = (
                        (uid in ADMIN) if isinstance(ADMIN, (list, tuple, set))
                        else (uid == ADMIN)
                    )
                    if not is_admin and FF_CH:
                        try:
                            fname = None
                            for t in ("document", "video", "audio", "voice"):
                                obj = getattr(msg, t, None)
                                if obj:
                                    fname = getattr(obj, "file_name", None)
                                    break
                            await client.copy_message(
                                chat_id=int(FF_CH),
                                from_chat_id=job["src"],
                                message_id=job["msg_id"],
                                caption=clean_text(fname or "File"),
                            )
                        except Exception as de:
                            print(f"[FF_DUMP] {de}")

                await forward_done(job["_id"])
                done += 1
                await asyncio.sleep(FORWARD_DELAY)

                # Update progress every 5 files (avoid edit flood)
                if done % 5 == 0 or done == total:
                    await _update_progress(client, s, session_id, done, total)

            except FloodWait as e:
                wait = int(e.value) + 2
                FORWARD_COOLDOWN[key] = time.time() + wait
                await forward_retry(job["_id"], wait)
                await asyncio.sleep(wait)
            except Exception as ex:
                print(f"[FF_ERR] uid={uid} msg={job.get('msg_id')}: {ex}")
                await forward_done(job["_id"])

    # Dispatch all jobs as concurrent tasks bounded by semaphore
    while True:
        if session_id in CANCELLED_SESSIONS:
            break
        job = await fetch_forward_job_for_session(session_id)
        if not job:
            await asyncio.sleep(0.5)
            job = await fetch_forward_job_for_session(session_id)
            if not job:
                break
        asyncio.create_task(_send_one(job))
        await asyncio.sleep(0.05)   # micro-yield between dispatches

    # Drain semaphore — wait for all in-flight tasks to finish
    for _ in range(MAX_CONCURRENT):
        async with sem:
            pass

    _USER_FORWARD_TASKS.pop(uid, None)

    if session_id not in CANCELLED_SESSIONS:
        try:
            await client.edit_message_text(
                s["chat_id"], s["msg_id"],
                "✅ <b>Forwarding complete!</b>\n\n"
                f"📤 <b>From:</b> {s['source_title']}\n"
                f"📥 <b>To:</b> {s['destination_title']}\n"
                f"📦 <b>Files transferred:</b> <code>{done}</code> / <code>{total}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
        FF_SESSIONS.pop(uid, None)


async def _update_progress(client, s, session_id, done, total):
    if session_id in CANCELLED_SESSIONS:
        return
    pct   = int((done / total) * 100) if total else 0
    bar   = "█" * (pct // 10) + "░" * (10 - pct // 10)
    frame = ANIM_FRAMES[int(time.time()) % len(ANIM_FRAMES)]
    try:
        await client.edit_message_text(
            s["chat_id"], s["msg_id"],
            f"📤 <b>{s['source_title']}</b>\n"
            f"         ⬇️⬇️⬇️\n"
            f"📥 <b>{s['destination_title']}</b>\n\n"
            f"{frame}\n"
            f"<code>[{bar}] {pct}%</code>\n"
            f"📦 <b>{done}</b> / <b>{total}</b> files",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")
            ]]),
        )
    except Exception:
        pass


# ─────────────────────────────────────────────
#  Forward with thumbnail preservation
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
                file_name=f"/tmp/ffthumb_{msg.id}.jpg",
            )
            await client.send_video(
                chat_id=dst, video=media_obj.file_id, caption=caption,
                thumb=thumb_path,
                duration=getattr(media_obj, "duration", 0),
                width=getattr(media_obj, "width", 0),
                height=getattr(media_obj, "height", 0),
                supports_streaming=True, parse_mode=None,
            )
        elif media_type == "animation" and has_thumb:
            thumb_path = await client.download_media(
                media_obj.thumbs[0].file_id,
                file_name=f"/tmp/ffthumb_{msg.id}.jpg",
            )
            await client.send_animation(
                chat_id=dst, animation=media_obj.file_id,
                caption=caption, thumb=thumb_path, parse_mode=None,
            )
        elif media_type == "document" and has_thumb:
            thumb_path = await client.download_media(
                media_obj.thumbs[0].file_id,
                file_name=f"/tmp/ffthumb_{msg.id}.jpg",
            )
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
#  Cancel callback
# ─────────────────────────────────────────────
@Client.on_callback_query(filters.regex("^ff_cancel$"))
async def ff_cancel(client, query):
    await query.answer("🛑 Cancelling…")
    uid  = query.from_user.id
    s    = FF_SESSIONS.pop(uid, None)

    # Stop running task
    task = _USER_FORWARD_TASKS.pop(uid, None)
    if task and not task.done():
        task.cancel()

    if not s:
        try:
            await query.message.edit_text("❌ Nothing to cancel.")
        except Exception:
            pass
        return

    sid = s.get("session_id")
    if sid:
        CANCELLED_SESSIONS.add(sid)
        remaining = await forward_queue.count_documents({"session_id": sid})
        sent      = max(s.get("total", 0) - remaining, 0)
        await forward_queue.delete_many({"session_id": sid})
        try:
            await query.message.edit_text(
                "🛑 <b>Forwarding cancelled</b>\n\n"
                f"📤 <b>From:</b> {s.get('source_title', '?')}\n"
                f"📥 <b>To:</b> {s.get('destination_title', '?')}\n"
                f"📦 <b>Files sent:</b> <code>{sent}</code> / "
                f"<code>{s.get('total', '?')}</code>",
                parse_mode=ParseMode.HTML,
            )
        except Exception:
            pass
    else:
        try:
            await query.message.edit_text("🛑 Cancelled.")
        except Exception:
            pass
