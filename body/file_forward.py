# ─────────────────────────────────────────────────────────────────────────────
#  FILE FORWARD MODULE  —  /file_forward command
#
#  Flow:
#    1. /file_forward  →  show source channel list (admin channels) + Cancel
#    2. Select source  →  show destination channel list + Back + Cancel
#    3. Select dest    →  ask for skip/range input + Cancel
#         • "0"            → forward ALL files
#         • "2500" / link  → forward from that msg ID onwards
#         • "100 - 500"    → forward only that ID range
#    4. Forwarding starts → live progress bar + Cancel button
#    5. On completion     → completed summary message
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import re
import time
import uuid

from collections import defaultdict

from pyrogram import Client, filters
from pyrogram.errors import (
    ChatAdminRequired,
    ChannelPrivate,
    FloodWait,
    MessageNotModified,
)
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from body.database import (
    enqueue_forward_bulk,
    forward_done,
    forward_queue,
    forward_retry,
    get_user_channels,
)
from info import ADMIN, FF_CH


# ─────────────────────────────────────────────────────────────────────────────
#  CONSTANTS
# ─────────────────────────────────────────────────────────────────────────────

MAX_FORWARD_PER_PAIR = 1        # concurrent forwards per (src, dst) pair
FORWARD_DELAY        = 0.8      # seconds between each copy_message call
FORWARD_EXECUTORS    = 6        # number of parallel worker coroutines

# All Telegram media types considered "files"
MEDIA_TYPES = (
    "video", "document", "audio", "photo",
    "voice", "video_note", "animation", "sticker",
)

# ─────────────────────────────────────────────────────────────────────────────
#  RUNTIME STATE
# ─────────────────────────────────────────────────────────────────────────────

FF_SESSIONS:        dict = {}          # uid  → session dict
CANCELLED_SESSIONS: set  = set()       # session_id strings

FORWARD_ACTIVE:   dict = defaultdict(int)   # (src, dst) → active count
FORWARD_COOLDOWN: dict = {}                  # (src, dst) → resume timestamp

_LAST_PROGRESS_EDIT: dict = {}              # session_id → last edit timestamp

# ─────────────────────────────────────────────────────────────────────────────
#  REGEX
# ─────────────────────────────────────────────────────────────────────────────

_MSG_LINK_RE  = re.compile(r"(?:https?://)?t\.me/(?:c/\d+|[A-Za-z0-9_]+)/(\d+)")
_USERNAME_RE  = re.compile(r"@\w+",                     flags=re.IGNORECASE)
_URL_RE       = re.compile(r"(https?://\S+|t\.me/\S+)", flags=re.IGNORECASE)
_HTML_TAG_RE  = re.compile(r"<[^>]+>")
_MD_LINK_RE   = re.compile(r"\[([^\]]+)\]\([^)]+\)")


# ─────────────────────────────────────────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────────────────────────────────────────

def extract_msg_id(text: str):
    """Extract a Telegram message ID from a plain integer or message link."""
    if not text:
        return None
    m = _MSG_LINK_RE.search(text)
    if m:
        return int(m.group(1))
    t = text.strip()
    if t.isdigit():
        return int(t)
    return None


def clean_text(text: str) -> str:
    """Strip links, usernames, HTML/Markdown from a string."""
    if not text:
        return ""
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _HTML_TAG_RE.sub("", text)
    text = _URL_RE.sub("", text)
    text = _USERNAME_RE.sub("", text)
    return re.sub(r"\s+", " ", text).strip()


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s"


def _has_media(msg) -> bool:
    if msg is None or not getattr(msg, "id", 0):
        return False
    for t in MEDIA_TYPES:
        if getattr(msg, t, None) is not None:
            return True
    return bool(getattr(msg, "media", None))


def _progress_text(session: dict, done: int, errors: int,
                   total: int, elapsed: float, complete: bool = False) -> str:
    src = session.get("source_title", "Unknown")
    dst = session.get("destination_title", "Unknown")
    BAR = 10
    filled = int(BAR * done / total) if total > 0 else 0
    bar    = "█" * filled + "░" * (BAR - filled)
    pct    = int(100 * done / total) if total > 0 else 0

    if complete:
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


def _cancel_kb():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")]]
    )


async def _edit(client, s: dict, text: str, cancel: bool = True):
    """Safely edit the session UI message."""
    markup = _cancel_kb() if cancel else None
    try:
        await client.edit_message_text(
            s["chat_id"], s["msg_id"], text,
            reply_markup=markup, disable_web_page_preview=True,
        )
    except (MessageNotModified, Exception) as e:
        print(f"[FF] _edit failed: {e}")


# ─────────────────────────────────────────────────────────────────────────────
#  BOT STARTUP  — spawn worker pool
# ─────────────────────────────────────────────────────────────────────────────

def on_bot_start(client):
    """Call this once after the Pyrogram client starts."""
    for _ in range(FORWARD_EXECUTORS):
        asyncio.create_task(forward_worker(client))


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 1 — /file_forward command
# ─────────────────────────────────────────────────────────────────────────────

@Client.on_message(filters.private & filters.command("file_forward"))
async def ff_start(client, message):
    uid      = message.from_user.id
    channels = await get_user_channels(uid)

    if not channels:
        return await message.reply_text(
            "❌ <b>No channels found.</b>\n\n"
            "Add me as an admin to at least <b>2 channels</b> first, "
            "then try again."
        )
    if len(channels) < 2:
        return await message.reply_text(
            "❌ <b>Need at least 2 channels.</b>\n\n"
            "Add me as an admin to a second channel and try again."
        )

    # Create session
    FF_SESSIONS[uid] = {
        "step":         "src",
        "all_channels": channels,
        "expires":      None,
    }

    kb = [
        [InlineKeyboardButton(ch["channel_title"],
                              callback_data=f"ff_src_{ch['channel_id']}")]
        for ch in channels
    ]
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")])

    await message.reply_text(
        "📤 <b>Select SOURCE channel</b>\n\n"
        "Choose the channel you want to <b>copy files from</b>:",
        reply_markup=InlineKeyboardMarkup(kb),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 2 — source channel selected
# ─────────────────────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^ff_src_(-?\d+)$"))
async def ff_src(client, query):
    uid = query.from_user.id
    s   = FF_SESSIONS.get(uid)
    if not s:
        return await query.answer("Session expired. Use /file_forward again.", show_alert=True)
    await query.answer()

    src               = int(query.matches[0].group(1))
    s["source"]       = src
    s["source_title"] = next(
        (x["channel_title"] for x in s["all_channels"] if x["channel_id"] == src),
        str(src),
    )

    dst_channels = [x for x in s["all_channels"] if x["channel_id"] != src]
    if not dst_channels:
        return await query.message.edit_text(
            "❌ Only one channel found.\n\n"
            "Add me as admin to at least one more channel."
        )
    s["step"] = "dst"

    kb = [
        [InlineKeyboardButton(x["channel_title"],
                              callback_data=f"ff_dst_{x['channel_id']}")]
        for x in dst_channels
    ]
    kb.append([InlineKeyboardButton("↩ Back (change source)", callback_data="ff_back_src")])
    kb.append([InlineKeyboardButton("❌ Cancel",              callback_data="ff_cancel")])

    await query.message.edit_text(
        f"📤 <b>Source:</b> {s['source_title']}\n\n"
        "📥 <b>Select DESTINATION channel</b>\n\n"
        "Choose the channel you want to <b>copy files to</b>:",
        reply_markup=InlineKeyboardMarkup(kb),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  BACK — return to source list
# ─────────────────────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^ff_back_src$"))
async def ff_back_src(client, query):
    uid = query.from_user.id
    s   = FF_SESSIONS.get(uid)
    if not s:
        return await query.answer("Session expired. Use /file_forward again.", show_alert=True)
    await query.answer()

    for key in ("source", "source_title", "destination", "destination_title"):
        s.pop(key, None)
    s["step"] = "src"

    kb = [
        [InlineKeyboardButton(ch["channel_title"],
                              callback_data=f"ff_src_{ch['channel_id']}")]
        for ch in s["all_channels"]
    ]
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")])

    await query.message.edit_text(
        "📤 <b>Select SOURCE channel</b>\n\n"
        "Choose the channel you want to <b>copy files from</b>:",
        reply_markup=InlineKeyboardMarkup(kb),
    )


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 3 — destination selected → ask for skip/range
# ─────────────────────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^ff_dst_(-?\d+)$"))
async def ff_dst(client, query):
    uid = query.from_user.id
    s   = FF_SESSIONS.get(uid)
    if not s:
        return await query.answer("Session expired. Use /file_forward again.", show_alert=True)
    await query.answer()

    dst                    = int(query.matches[0].group(1))
    s["destination"]       = dst
    s["destination_title"] = next(
        (x["channel_title"] for x in s["all_channels"] if x["channel_id"] == dst),
        str(dst),
    )
    s["step"]    = "skip"
    s["chat_id"] = query.message.chat.id
    s["msg_id"]  = query.message.id
    s["expires"] = time.time() + 900          # 15-minute window

    await query.message.edit_text(
        f"📤 <b>Source:</b> {s['source_title']}\n"
        f"📥 <b>Destination:</b> {s['destination_title']}\n\n"
        "⏭ <b>Send skip / range value:</b>\n\n"
        "• <code>0</code> — forward <b>all</b> files\n"
        "• <code>2500</code> or a message link — forward <b>from</b> that message onwards\n"
        "• <code>100 - 500</code> or two links — forward files in that <b>range only</b>\n\n"
        "⏰ Session expires in <b>15 minutes</b>",
        reply_markup=_cancel_kb(),
        disable_web_page_preview=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 3b — handle skip/range text input
#  Called from Caption.py's generic on_message handler.
# ─────────────────────────────────────────────────────────────────────────────

async def handle_ff_input(client, message, uid: int) -> bool:
    """
    Process a text message while the user's FF session is in the 'skip' step.
    Returns True if the message was consumed by the FF session.
    """
    s = FF_SESSIONS.get(uid)
    if not s or s.get("step") != "skip":
        return False

    # Session expiry
    if s.get("expires") and s["expires"] < time.time():
        FF_SESSIONS.pop(uid, None)
        await message.reply_text("⏰ Session expired. Start again with /file_forward")
        return True

    raw = (message.text or "").strip()

    # Silently delete the user's input to keep chat clean
    try:
        await message.delete()
    except Exception:
        pass

    # ── Try range format: "start - end"
    parts = re.split(r"\s*[-–]\s*|\n", raw, maxsplit=1)
    parts = [p.strip() for p in parts if p.strip()]

    if len(parts) == 2:
        id1 = extract_msg_id(parts[0])
        id2 = extract_msg_id(parts[1])
        if id1 is not None and id2 is not None:
            lo, hi = min(id1, id2), max(id1, id2)
            s["ff_mode"]     = "range"
            s["range_start"] = lo
            s["range_end"]   = hi
            s["step"]        = "queue"
            try:
                await client.edit_message_text(
                    s["chat_id"], s["msg_id"],
                    f"🔍 <b>Scanning source channel…</b>\n\n"
                    f"📌 Range: <code>{lo}</code> → <code>{hi}</code>\n"
                    "⏳ Counting media files, please wait…",
                    reply_markup=_cancel_kb(),
                )
            except Exception:
                pass
            asyncio.create_task(enqueue_forward_jobs(client, uid))
            return True

    # ── Single ID / link / "0"
    msg_id_val = extract_msg_id(raw)
    if msg_id_val is None:
        try:
            await client.edit_message_text(
                s["chat_id"], s["msg_id"],
                "❌ <b>Invalid input.</b> Please send one of:\n\n"
                "• <code>0</code> — forward all files\n"
                "• <code>2500</code> or a message link — forward from that message onwards\n"
                "• <code>100 - 500</code> or two links — forward a specific range",
                reply_markup=_cancel_kb(),
                disable_web_page_preview=True,
            )
        except Exception:
            pass
        return True

    s["ff_mode"] = "skip"
    s["skip"]    = int(msg_id_val)
    s["step"]    = "queue"

    label = "all files" if msg_id_val == 0 else f"message <code>{msg_id_val}</code> onwards"
    try:
        await client.edit_message_text(
            s["chat_id"], s["msg_id"],
            f"🔍 <b>Scanning source channel…</b>\n\n"
            f"⏭ Starting from {label}\n"
            "⏳ Counting media files, please wait…",
            reply_markup=_cancel_kb(),
        )
    except Exception:
        pass

    asyncio.create_task(enqueue_forward_jobs(client, uid))
    return True


# ─────────────────────────────────────────────────────────────────────────────
#  LAST-MESSAGE PROBE  (3 fallback methods)
# ─────────────────────────────────────────────────────────────────────────────

async def _get_last_msg_id(client, channel_id: int) -> int:
    # Method 1: get_chat_history
    try:
        async for msg in client.get_chat_history(channel_id, limit=1):
            if msg.id and msg.id > 0:
                print(f"[FF] last_id via get_chat_history: {msg.id} (ch={channel_id})")
                return msg.id
    except Exception as e:
        print(f"[FF] get_chat_history failed for {channel_id}: {e}")

    # Method 2: exponential probe + binary search
    print(f"[FF] falling back to probe search for channel {channel_id}")
    try:
        probe           = 1
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
                break
            await asyncio.sleep(0.05)

        if last_known_real == 0:
            return 1

        lo, hi = last_known_real, probe
        while hi - lo > 1:
            mid  = (lo + hi) // 2
            msgs = await client.get_messages(channel_id, mid)
            if not isinstance(msgs, list):
                msgs = [msgs]
            real = [m for m in msgs if m and getattr(m, "id", 0)]
            if real:
                lo = real[-1].id
            else:
                hi = mid
            await asyncio.sleep(0.05)

        print(f"[FF] last_id via probe/binary-search: {lo} (ch={channel_id})")
        return lo

    except Exception as e:
        print(f"[FF] probe search failed for {channel_id}: {e}")

    # Method 3: safe ceiling fallback
    print(f"[FF] using fallback ceiling 999999 for channel {channel_id}")
    return 999_999


# ─────────────────────────────────────────────────────────────────────────────
#  STEP 4 — scan source channel + bulk-enqueue copy jobs
# ─────────────────────────────────────────────────────────────────────────────

async def enqueue_forward_jobs(client, uid: int):
    """Scan the source channel for media, then bulk-enqueue copy jobs."""
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

    print(f"[FF] enqueue start uid={uid} src={src} dst={dst} "
          f"mode={ff_mode} skip={skip_id} range={range_start}-{range_end}")

    await _edit(client, s,
        "🔍 <b>Scanning source channel…</b>\n\n"
        "⏳ Fetching last message ID…"
    )

    # ── Determine scan bounds ─────────────────────────────────────────────
    if ff_mode == "range" and range_start is not None and range_end is not None:
        scan_from = range_start
        scan_to   = range_end
    else:
        scan_to   = await _get_last_msg_id(client, src)
        scan_from = max(1, skip_id + 1)

    if scan_to < 1:
        await _edit(client, s,
            "❌ <b>Could not determine channel message range.</b>\n\n"
            "Make sure the bot is admin in the source channel with read permissions.",
            cancel=False,
        )
        FF_SESSIONS.pop(uid, None)
        return

    if scan_from > scan_to:
        await _edit(client, s,
            f"❌ <b>Skip ID exceeds last message.</b>\n\n"
            f"Last message in channel: <code>{scan_to}</code>\n"
            f"Your skip value: <code>{skip_id}</code>\n\n"
            "Please send a smaller value.",
            cancel=False,
        )
        FF_SESSIONS.pop(uid, None)
        return

    total_range = scan_to - scan_from + 1

    await _edit(client, s,
        f"🔍 <b>Scanning source channel…</b>\n\n"
        f"📨 Range: <code>{scan_from}</code> → <code>{scan_to}</code> "
        f"(<code>{total_range}</code> IDs)\n"
        "⏳ Counting media files…"
    )

    # ── Batch-fetch ──────────────────────────────────────────────────────
    BATCH_SIZE     = 200
    media_ids      = []
    scanned        = 0
    last_ui_update = time.time()
    cur_id         = scan_from

    while cur_id <= scan_to:
        if session_id in CANCELLED_SESSIONS:
            print(f"[FF] scan cancelled uid={uid}")
            return

        batch_end    = min(cur_id + BATCH_SIZE - 1, scan_to)
        ids_to_fetch = list(range(cur_id, batch_end + 1))

        try:
            messages = await client.get_messages(src, ids_to_fetch)
        except FloodWait as e:
            print(f"[FF] FloodWait {e.value}s during scan id={cur_id}")
            await asyncio.sleep(e.value + 1)
            continue
        except (ChannelPrivate, ChatAdminRequired) as e:
            await _edit(client, s,
                f"❌ <b>Permission error:</b>\n<code>{e}</code>\n\n"
                "Bot needs admin access to read the source channel.",
                cancel=False,
            )
            FF_SESSIONS.pop(uid, None)
            return
        except Exception as e:
            print(f"[FF] get_messages error {cur_id}-{batch_end}: {e}")
            scanned += len(ids_to_fetch)
            cur_id   = batch_end + 1
            continue

        if not isinstance(messages, list):
            messages = [messages]

        for m in messages:
            if _has_media(m):
                media_ids.append(m.id)

        scanned += len(ids_to_fetch)
        cur_id   = batch_end + 1

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
                    f"🎞 Media found so far: <code>{len(media_ids)}</code>",
                    reply_markup=_cancel_kb(),
                )
            except (MessageNotModified, Exception):
                pass

    # ── Scan done ────────────────────────────────────────────────────────
    total       = len(media_ids)
    s["total"]  = total
    s["done"]   = 0
    s["errors"] = 0

    print(f"[FF] scan complete: scanned={scanned} IDs → found={total} media, uid={uid}")

    if total == 0:
        await _edit(client, s,
            f"❌ <b>No media files found</b> in the scanned range.\n\n"
            f"📊 <b>Scan summary:</b>\n"
            f"• IDs scanned: <code>{scanned}</code>\n"
            f"• Range: <code>{scan_from}</code> → <code>{scan_to}</code>\n\n"
            "Make sure the source channel contains photos, videos, documents, or audio.",
            cancel=False,
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
    await enqueue_forward_bulk(jobs)
    print(f"[FF] enqueued {len(jobs)} jobs uid={uid}")

    # Show initial progress bar
    elapsed = time.time() - start_time
    try:
        await client.edit_message_text(
            s["chat_id"], s["msg_id"],
            _progress_text(s, 0, 0, total, elapsed),
            reply_markup=_cancel_kb(),
        )
    except MessageNotModified:
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  WORKER POOL
# ─────────────────────────────────────────────────────────────────────────────

async def _fetch_job():
    """Atomically claim one pending job, respecting rate limits."""
    from pymongo import ReturnDocument
    now = time.time()
    job = await forward_queue.find_one_and_update(
        {"status": "pending"},
        {"$set": {"status": "processing", "started": now}},
        sort=[("ts", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if not job:
        return None

    key = (job["src"], job["dst"])
    if FORWARD_COOLDOWN.get(key, 0) > now or FORWARD_ACTIVE[key] >= MAX_FORWARD_PER_PAIR:
        await forward_queue.update_one(
            {"_id": job["_id"]},
            {"$set": {"status": "pending", "ts": now + 1.0}},
        )
        return None

    FORWARD_ACTIVE[key] += 1
    return job


async def forward_worker(client):
    """Long-running worker coroutine — processes copy jobs from MongoDB queue."""
    while True:
        job = await _fetch_job()
        if not job:
            await asyncio.sleep(0.3)
            continue

        key        = (job["src"], job["dst"])
        session_id = job.get("session_id")

        try:
            if session_id in CANCELLED_SESSIONS:
                await forward_done(job["_id"])
                continue

            # copy_message → no "Forwarded from" header
            await client.copy_message(
                chat_id=job["dst"],
                from_chat_id=job["src"],
                message_id=job["msg_id"],
            )
            print(f"[FF] ✓ msg={job['msg_id']} {job['src']} → {job['dst']}")

            # Admin dump-log copy to FF_CH
            admin_ids = ADMIN if isinstance(ADMIN, list) else [ADMIN]
            if job.get("user_id") not in admin_ids:
                try:
                    msg   = await client.get_messages(job["src"], job["msg_id"])
                    fname = None
                    for t in ("document", "video", "audio", "voice"):
                        obj = getattr(msg, t, None)
                        if obj:
                            fname = getattr(obj, "file_name", None)
                            break
                    await client.copy_message(
                        chat_id=FF_CH,
                        from_chat_id=job["src"],
                        message_id=job["msg_id"],
                        caption=clean_text(fname or "File"),
                    )
                except Exception as e:
                    print(f"[FF] dump_fail msg={job['msg_id']}: {e}")

            await forward_done(job["_id"])
            await _update_progress(client, job, error=False)
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
            await _update_progress(client, job, error=True)

        finally:
            FORWARD_ACTIVE[key] = max(0, FORWARD_ACTIVE[key] - 1)


# ─────────────────────────────────────────────────────────────────────────────
#  LIVE PROGRESS UPDATER
# ─────────────────────────────────────────────────────────────────────────────

async def _update_progress(client, job: dict, error: bool):
    session_id = job.get("session_id")
    if not session_id or session_id in CANCELLED_SESSIONS:
        return

    uid = job.get("user_id")
    s   = FF_SESSIONS.get(uid, {})

    if error:
        s["errors"] = s.get("errors", 0) + 1
    else:
        s["done"] = s.get("done", 0) + 1

    done    = s.get("done", 0)
    errors  = s.get("errors", 0)
    total   = job.get("total", 1)
    elapsed = time.time() - job.get("start_time", time.time())

    now  = time.time()
    last = _LAST_PROGRESS_EDIT.get(session_id, 0)

    remaining = await forward_queue.count_documents(
        {"session_id": session_id, "status": {"$in": ["pending", "processing"]}}
    )
    is_complete = remaining == 0

    # Throttle to once every 2 s (always fire on completion)
    if not is_complete and now - last < 2.0:
        return

    _LAST_PROGRESS_EDIT[session_id] = now

    if is_complete:
        text   = _progress_text(s, done, errors, total, elapsed, complete=True)
        markup = None
        _LAST_PROGRESS_EDIT.pop(session_id, None)
        FF_SESSIONS.pop(uid, None)
        CANCELLED_SESSIONS.discard(session_id)
        print(f"[FF] ✅ complete session={session_id} done={done} errors={errors}")
    else:
        text   = _progress_text(s, done, errors, total, elapsed)
        markup = _cancel_kb()

    try:
        await client.edit_message_text(
            job["chat_id"], job["ui_msg"], text, reply_markup=markup
        )
    except (MessageNotModified, Exception):
        pass


# ─────────────────────────────────────────────────────────────────────────────
#  CANCEL BUTTON
# ─────────────────────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex("^ff_cancel$"))
async def ff_cancel(client, query):
    uid = query.from_user.id
    await query.answer()

    s = FF_SESSIONS.pop(uid, None)
    if not s:
        try:
            await query.message.edit_text("❌ Nothing active to cancel.")
        except Exception:
            pass
        return

    session_id = s.get("session_id")
    elapsed    = time.time() - s.get("start_time", time.time())

    if session_id:
        CANCELLED_SESSIONS.add(session_id)
        await forward_queue.delete_many({"session_id": session_id})
        print(f"[FF] 🛑 cancel session={session_id} "
              f"done={s.get('done', 0)}/{s.get('total', 0)}")
        try:
            await query.message.edit_text(
                f"🛑 <b>Forwarding Cancelled</b>\n\n"
                f"📤 <b>Source:</b> {s.get('source_title', 'N/A')}\n"
                f"📥 <b>Destination:</b> {s.get('destination_title', 'N/A')}\n\n"
                f"📦 <b>Files Forwarded:</b> <code>{s.get('done', 0)}</code>\n"
                f"🗂 <b>Total Detected:</b> <code>{s.get('total', 0)}</code>\n"
                f"❌ <b>Errors:</b> <code>{s.get('errors', 0)}</code>\n"
                f"⏱ <b>Time Elapsed:</b> <code>{_fmt_duration(elapsed)}</code>"
            )
        except Exception:
            pass
    else:
        try:
            await query.message.edit_text("🛑 Cancelled.")
        except Exception:
            pass
