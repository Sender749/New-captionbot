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

# ── regex (was missing – caused NameError on every skip input) ───────────────
MSG_LINK_RE = re.compile(
    r'(?:https?://)?t\.me/(?:c/(\d+)|([A-Za-z0-9_]+))/(\d+)',
    flags=re.IGNORECASE
)

FORWARD_DELAY         = 0.3
FORWARD_EXECUTORS     = 10
PROGRESS_UPDATE_EVERY = 5

PAIR_COOLDOWN    = {}               # (src,dst) -> unblock time
PAIR_ACTIVE      = defaultdict(int) # active workers per pair
PAIR_MAX_WORKERS = 2
PRIORITY_PAIRS   = set()

FF_SESSIONS        = {}
CANCELLED_SESSIONS = set()


# ─── helpers ──────────────────────────────────────────────────────────────────
def _log(tag: str, msg: str):
    print(f"[{tag}] {msg}", flush=True)

def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'(https?://\S+|t\.me/\S+)', '', text, flags=re.IGNORECASE)
    text = re.sub(r'@\w+', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s+', ' ', text)
    return text.strip()


# ─── startup ──────────────────────────────────────────────────────────────────
async def _recover_loop():
    while True:
        try:
            await recover_stuck_forward_jobs(timeout=600)
        except Exception as e:
            _log("FF_RECOVER_ERR", str(e))
        await asyncio.sleep(300)

def on_bot_start(client: Client):
    _log("FF_START", f"Launching {FORWARD_EXECUTORS} forward workers")
    for _ in range(FORWARD_EXECUTORS):
        asyncio.create_task(forward_worker(client))
    asyncio.create_task(_recover_loop())


# ─── input parsing ────────────────────────────────────────────────────────────
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
        src_hint2, end_id   = _parse_single(parts[1])
        if start_id is None or end_id is None:
            return {"error": "Could not parse start or end message reference."}
        if start_id > end_id:
            return {"error": "Start message ID must be less than end message ID."}
        return {"skip_id": start_id - 1, "end_id": end_id,
                "src_hint": src_hint1 or src_hint2, "error": None}
    else:
        if raw.strip() == "0":
            return {"skip_id": 0, "end_id": None, "src_hint": None, "error": None}
        src_hint, msg_id = _parse_single(raw.strip())
        if msg_id is None:
            return {"error": "Invalid input.\n\nSend a message link, a message ID, or 0 to forward all."}
        return {"skip_id": msg_id, "end_id": None, "src_hint": src_hint, "error": None}


async def validate_msg_in_channel(client: Client, channel_id: int, msg_id: int) -> bool:
    try:
        msg = await client.get_messages(channel_id, msg_id)
        # Default must be False: valid messages have .empty=False,
        # deleted/non-existent messages have .empty=True
        result = msg is not None and not getattr(msg, 'empty', False)
        _log("FF_VALIDATE", f"channel={channel_id} msg={msg_id} valid={result}")
        return result
    except Exception as e:
        _log("FF_VALIDATE_ERR", f"channel={channel_id} msg={msg_id} err={e}")
        return False


# ─── callback: source ─────────────────────────────────────────────────────────
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
    _log("FF_SRC", f"uid={uid} src={src} '{s['source_title']}'")
    kb = [[InlineKeyboardButton(x["channel_title"], callback_data=f"ff_dst_{x['channel_id']}")]
          for x in s["channels"]]
    kb.append([InlineKeyboardButton("Cancel", callback_data="ff_cancel")])
    await query.message.edit_text(
        " <b>Select DESTINATION channel</b>",
        reply_markup=InlineKeyboardMarkup(kb)
    )


# ─── callback: destination ────────────────────────────────────────────────────
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
    s["expires"] = time.time() + 900
    _log("FF_DST", f"uid={uid} dst={dst} '{s['destination_title']}' — waiting for skip input")
    await query.message.edit_text(
        " <b>Enter forwarding range</b>\n\n"
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
            [[InlineKeyboardButton("Cancel", callback_data="ff_cancel")]]
        ),
        disable_web_page_preview=True
    )


# ─── enqueue all jobs for a session ───────────────────────────────────────────
async def enqueue_forward_jobs(client: Client, uid: int):
    s = FF_SESSIONS[uid]
    if "session_id" not in s:
        s["session_id"] = str(uuid.uuid4())
    session_id = s["session_id"]
    src      = s["source"]
    dst      = s["destination"]
    start_id = int(s["skip"]) + 1
    end_id   = s.get("end_id")

    _log("FF_ENQUEUE", f"session={session_id} src={src} dst={dst} start_id={start_id} end_id={end_id}")

    s["total"]     = 0
    s["forwarded"] = 0
    s["errors"]    = []
    start_ts       = time.time()

    # ── smart scan: jump over deleted-message gaps quickly ────────────────────
    effective_start = start_id
    try:
        latest_id = 0
        async for m in client.get_chat_history(src, limit=1):
            latest_id = m.id
        _log("FF_SCAN", f"latest_id={latest_id} start_id={start_id}")

        if latest_id > 0 and start_id < latest_id - 200:
            lo, hi = start_id, latest_id
            iters  = 0
            while hi - lo > 200:
                mid   = (lo + hi) // 2
                iters += 1
                try:
                    probe = await client.get_messages(src, mid)
                    if probe and not getattr(probe, 'empty', False):
                        hi = mid
                    else:
                        lo = mid + 1
                except Exception as be:
                    _log("FF_BINSEARCH_ERR", f"mid={mid} {be}")
                    lo = mid + 1
            effective_start = lo
            _log("FF_SCAN", f"binary search {iters} steps → effective_start={effective_start}")
    except Exception as e:
        _log("FF_SCAN_ERR", f"smart-start failed: {e}")
        effective_start = start_id

    msg_id              = effective_start
    consecutive_missing = 0
    MAX_CONSECUTIVE_MISSING = 500

    while True:
        if end_id is not None and msg_id > end_id:
            break
        try:
            msg = await client.get_messages(src, msg_id)
        except Exception as e:
            _log("FF_GETMSG_ERR", f"msg_id={msg_id} {e}")
            msg = None

        if not msg or getattr(msg, 'empty', False):
            consecutive_missing += 1
            if consecutive_missing >= MAX_CONSECUTIVE_MISSING:
                _log("FF_SCAN", f"Stopped at msg_id={msg_id} after {MAX_CONSECUTIVE_MISSING} missing")
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
            "start_time":        start_ts,
        })
        s["total"] += 1
        if s["total"] % 100 == 0:
            _log("FF_SCAN", f"Enqueued {s['total']} jobs so far (current msg_id={msg_id})")
        msg_id += 1

    _log("FF_ENQUEUE", f"Scan done: enqueued={s['total']}")

    # Update all jobs in this session with the real total
    await forward_queue.update_many(
        {"session_id": session_id},
        {"$set": {"total": s["total"]}}
    )

    try:
        await client.edit_message_text(
            s["chat_id"],
            s["msg_id"],
            (
                f" <b>Source:</b> {s['source_title']}\n"
                f" <b>Destination:</b> {s['destination_title']}\n"
                f" <b>Total files found:</b> <code>{s['total']}</code>\n\n"
                " Starting transfer…"
            ),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Cancel", callback_data="ff_cancel")]]
            )
        )
    except Exception as e:
        _log("FF_ENQUEUE_MSG_ERR", str(e))


# ─── job scheduler ────────────────────────────────────────────────────────────
async def fetch_forward_fair_job():
    now = time.time()

    # Priority pass (post-FloodWait pairs)
    cursor = forward_queue.find({"status": "pending"}).sort("ts", 1)
    async for job in cursor:
        key = (job["src"], job["dst"])
        if key not in PRIORITY_PAIRS:
            continue
        if PAIR_COOLDOWN.get(key, 0) > now:
            continue
        if PAIR_ACTIVE[key] >= PAIR_MAX_WORKERS:
            continue
        PRIORITY_PAIRS.discard(key)
        PAIR_ACTIVE[key] += 1
        await forward_queue.update_one(
            {"_id": job["_id"], "status": "pending"},
            {"$set": {"status": "processing", "started": now}}
        )
        return job

    # Normal pass
    cursor = forward_queue.find({"status": "pending"}).sort("ts", 1)
    async for job in cursor:
        key = (job["src"], job["dst"])
        if PAIR_COOLDOWN.get(key, 0) > now:
            continue
        if PAIR_ACTIVE[key] >= PAIR_MAX_WORKERS:
            continue
        PAIR_ACTIVE[key] += 1
        await forward_queue.update_one(
            {"_id": job["_id"], "status": "pending"},
            {"$set": {"status": "processing", "started": now}}
        )
        return job

    return None


# ─── worker ───────────────────────────────────────────────────────────────────
async def forward_worker(client: Client):
    while True:
        job = await fetch_forward_fair_job()
        if not job:
            await asyncio.sleep(0.5)
            continue

        key        = (job["src"], job["dst"])
        session_id = job.get("session_id")
        msg_id     = job.get("msg_id")

        try:
            if session_id in CANCELLED_SESSIONS:
                _log("FF_WORKER", f"Cancelled — skipping msg={msg_id}")
                await forward_done(job["_id"])
                continue

            _log("FF_WORKER", f"Copying msg={msg_id} src={job['src']} dst={job['dst']}")
            await client.copy_message(
                chat_id=job["dst"],
                from_chat_id=job["src"],
                message_id=msg_id
            )
            _log("FF_WORKER", f"Done msg={msg_id}")

            job_user = job.get("user_id")
            if job_user != ADMIN:
                try:
                    await client.copy_message(
                        chat_id=FF_CH,
                        from_chat_id=job["src"],
                        message_id=msg_id
                    )
                except Exception as e:
                    _log("FF_DUMP_FAIL", f"msg={msg_id} {e}")

            await forward_done(job["_id"])
            await update_forward_progress(client, job, success=True)
            await asyncio.sleep(FORWARD_DELAY)

        except FloodWait as e:
            wait = int(e.value) + 2
            _log("FF_FLOOD", f"FloodWait {wait}s for pair {key}")
            PAIR_COOLDOWN[key] = time.time() + wait
            PRIORITY_PAIRS.add(key)
            await forward_retry(job["_id"], wait)

        except Exception as e:
            _log("FF_WORKER_ERR", f"msg={msg_id} {e}")
            await forward_done(job["_id"])
            await update_forward_progress(client, job, success=False)

        finally:
            PAIR_ACTIVE[key] = max(0, PAIR_ACTIVE[key] - 1)


# ─── progress ─────────────────────────────────────────────────────────────────
SESSION_STATS = defaultdict(lambda: {"forwarded": 0, "errors": [], "start_time": None, "total": 0})

def _fmt_duration(seconds: float) -> str:
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m {s}s"


async def update_forward_progress(client: Client, job, success: bool = True):
    session = job.get("session_id")
    if session in CANCELLED_SESSIONS:
        return

    stats = SESSION_STATS[session]
    if stats["start_time"] is None:
        stats["start_time"] = job.get("start_time", time.time())

    job_total = job.get("total", 0)
    if job_total > stats["total"]:
        stats["total"] = job_total

    if success:
        stats["forwarded"] += 1
    else:
        stats["errors"].append(job.get("msg_id", "?"))

    remaining = await forward_queue.count_documents(
        {"session_id": session, "status": {"$in": ["pending", "processing"]}}
    )

    elapsed   = time.time() - (stats["start_time"] or time.time())
    forwarded = stats["forwarded"]
    total     = stats["total"] or job.get("total", 0)
    errors    = stats["errors"]
    err_count = len(errors)

    speed_str = ""
    if elapsed > 0 and forwarded > 0:
        rate = forwarded / elapsed
        if remaining > 0:
            eta       = remaining / rate
            speed_str = f" <b>Speed:</b> {rate:.1f} files/s  |  ETA: {_fmt_duration(eta)}\n"

    if remaining == 0:
        total_time = _fmt_duration(elapsed)
        err_text   = ""
        if errors:
            err_ids = ", ".join(str(e) for e in errors[:10])
            if len(errors) > 10:
                err_ids += f" … +{len(errors)-10} more"
            err_text = f"\n <b>Failed ({err_count}):</b> <code>{err_ids}</code>"
        text = (
            " <b>Forwarding Completed!</b>\n\n"
            f" <b>Source:</b> {job['source_title']}\n"
            f" <b>Destination:</b> {job['destination_title']}\n"
            f" <b>Total Forwarded:</b> <code>{forwarded}</code> / <code>{total}</code>\n"
            f" <b>Total Time:</b> {total_time}"
            f"{err_text}"
        )
        _log("FF_PROGRESS", f"session={session} COMPLETED {forwarded}/{total}")
        try:
            await client.edit_message_text(job["chat_id"], job["ui_msg"], text)
        except Exception as e:
            _log("FF_PROGRESS_ERR", str(e))
        SESSION_STATS.pop(session, None)
        return

    if forwarded % PROGRESS_UPDATE_EVERY != 0:
        return

    progress_bar = ""
    if total > 0:
        pct          = forwarded / total
        filled       = int(pct * 10)
        progress_bar = "" * filled + "" * (10 - filled) + f" {int(pct*100)}%\n"

    err_text = f" <b>Errors so far:</b> <code>{err_count}</code>\n" if err_count else ""
    text = (
        " <b>Forwarding in Progress…</b>\n\n"
        f" <b>Source:</b> {job['source_title']}\n"
        f" <b>Destination:</b> {job['destination_title']}\n\n"
        f" {progress_bar}"
        f" <b>Forwarded:</b> <code>{forwarded}</code> / <code>{total}</code>\n"
        f" <b>Elapsed:</b> {_fmt_duration(elapsed)}\n"
        f"{speed_str}"
        f"{err_text}"
    )
    try:
        await client.edit_message_text(
            job["chat_id"], job["ui_msg"], text,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Cancel", callback_data="ff_cancel")]]
            )
        )
    except Exception as e:
        _log("FF_PROGRESS_ERR", str(e))


# ─── cancel ───────────────────────────────────────────────────────────────────
@Client.on_callback_query(filters.regex("^ff_cancel$"))
async def ff_cancel(client, query):
    uid = query.from_user.id
    s   = FF_SESSIONS.pop(uid, None)
    if not s:
        await query.message.edit_text(" Nothing to cancel.")
        return
    session_id = s.get("session_id")
    if session_id:
        CANCELLED_SESSIONS.add(session_id)
        stats      = SESSION_STATS.pop(session_id, {})
        forwarded  = stats.get("forwarded", 0)
        errors     = stats.get("errors", [])
        start_time = stats.get("start_time")
        elapsed    = _fmt_duration(time.time() - start_time) if start_time else "N/A"
        total      = s.get("total", 0)
        _log("FF_CANCEL", f"session={session_id} forwarded={forwarded}/{total}")
        await forward_queue.delete_many({"session_id": session_id})
        err_text = f"\n <b>Errors:</b> <code>{len(errors)}</code>" if errors else ""
        await query.message.edit_text(
            " <b>Forwarding Cancelled</b>\n\n"
            f" <b>Source:</b> {s.get('source_title','?')}\n"
            f" <b>Destination:</b> {s.get('destination_title','?')}\n\n"
            f" <b>Forwarded:</b> <code>{forwarded}</code>\n"
            f" <b>Total found:</b> <code>{total}</code>\n"
            f" <b>Time elapsed:</b> {elapsed}"
            f"{err_text}"
        )
    else:
        await query.message.edit_text(" Cancelled.")
