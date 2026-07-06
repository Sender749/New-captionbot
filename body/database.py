import motor.motor_asyncio
import logging
from info import *
from typing import Optional
import time
from collections import defaultdict

logger = logging.getLogger("captionbot.database")

# ════════════════════════════════════════════════════════
#  WORKER / EXECUTOR TUNING  (Koyeb free tier safe)
#  Koyeb free: 512 MB RAM, 0.1 vCPU shared
#  Keep total asyncio tasks ≤ 30 so event loop stays snappy
# ════════════════════════════════════════════════════════

# Caption workers  ── how many concurrent caption-edit tasks
CAPTION_WORKERS      = 6     # was 30; 6 is enough with fair scheduling

# Forward workers  ── how many concurrent file-forward tasks
FORWARD_WORKERS      = 4     # was 12; 4 avoids flooding Telegram

# Max pending docs scanned per fetch_channel_job() call — keeps polling cheap
# even when thousands of jobs are queued across busy/cooling-down channels.
_FETCH_SCAN_LIMIT    = 200

# Max parallel edits per channel at once (rate-limit headroom)
DEFAULT_MAX_WORKERS  = 2     # per channel concurrency cap

# How many forward jobs run simultaneously for a single src→dst pair
MAX_FORWARD_PER_PAIR = 1     # keep 1 – Telegram throttles hard per chat

# Seconds to sleep between edits (caption) / forwards
DEFAULT_EDIT_DELAY   = 0.5   # caption edit cooldown per worker
FORWARD_DELAY        = 0.8   # forward cooldown per worker (generous)

# ════════════════════════════════════════════════════════
#  Per-channel scheduler state  (in-memory, reset on restart)
# ════════════════════════════════════════════════════════
CHANNEL_ACTIVE   = defaultdict(int)   # channel_id  -> active caption workers
CHANNEL_COOLDOWN = {}                 # channel_id  -> FloodWait unblock time

# ════════════════════════════════════════════════════════
#  Channel settings cache  (avoids repeated DB reads)
# ════════════════════════════════════════════════════════
_CHANNEL_CACHE = {}
CACHE_TTL = 120   # seconds

# ════════════════════════════════════════════════════════
#  MongoDB connection
# ════════════════════════════════════════════════════════
_mongo_client = motor.motor_asyncio.AsyncIOMotorClient(
    MONGO_DB,
    serverSelectionTimeoutMS=10_000,
    connectTimeoutMS=10_000,
    socketTimeoutMS=30_000,
    maxPoolSize=10,
    minPoolSize=1,
)
db              = _mongo_client.captions_with_chnl
chnl_ids        = db.chnl_ids
users           = db.users
user_channels   = db.user_channels
queue_col       = db.caption_queue
forward_queue   = db.forward_queue

# ── NEW: global admin forwarding progress ────────────────────────────────────
# Stores per-channel forwarding resume state:
#   { channel_id: int, last_msg_id: int, total_forwarded: int, updated_at: float }
global_ff_progress = db.global_ff_progress


# ════════════════════════════════════════════════════════
#  Index setup  (called once at startup)
# ════════════════════════════════════════════════════════
async def ensure_queue_indexes():
    await queue_col.create_index([("status", 1), ("ts", 1)])
    await queue_col.create_index([("chat_id", 1)])
    await queue_col.create_index(
        [("chat_id", 1), ("ts", 1)],
        partialFilterExpression={"status": "pending"},
        background=True,
    )


async def ensure_forward_indexes():
    await forward_queue.create_index([("status", 1), ("ts", 1)])
    await forward_queue.create_index([("src", 1)])
    await forward_queue.create_index([("dst", 1)])
    await forward_queue.create_index([("session_id", 1)])
    await forward_queue.create_index([("user_id", 1)])
    await forward_queue.create_index(
        [("src", 1), ("dst", 1), ("ts", 1)],
        partialFilterExpression={"status": "pending"},
        background=True,
    )


async def ensure_global_ff_indexes():
    """Create indexes for the global (admin) forwarding progress collection."""
    await global_ff_progress.create_index(
        [("channel_id", 1)],
        unique=True,
        background=True,
    )


# ════════════════════════════════════════════════════════
#  Caption queue helpers
# ════════════════════════════════════════════════════════
async def enqueue_caption(job: dict):
    await queue_col.insert_one({
        **job,
        "status":  "pending",
        "retries": 0,
        "ts":      time.time(),
    })


async def fetch_channel_job():
    """
    Fair-pick: scan pending caption jobs oldest-first.
    Skip channels that are cooled down or at concurrency cap.
    Atomically claim the first eligible job.

    The scan is capped at _FETCH_SCAN_LIMIT documents per call. Without a
    cap, once thousands of jobs pile up for channels that are all
    momentarily at their concurrency cap / cooldown, every single worker
    would re-scan the *entire* pending backlog on every 0.5s poll just to
    find nothing eligible -- wasted DB round-trips that get worse the more
    files are queued, right when the queue needs to be draining fastest.
    Capping the scan means a worker simply tries again next poll instead of
    walking the whole collection every time; nothing is ever skipped since
    finished jobs are deleted and paused ones are already excluded here.
    """
    now = time.time()
    cursor = queue_col.find({"status": "pending"}).sort("ts", 1).limit(_FETCH_SCAN_LIMIT)
    async for job in cursor:
        ch = job["chat_id"]
        if CHANNEL_COOLDOWN.get(ch, 0) > now:
            continue
        if CHANNEL_ACTIVE[ch] >= DEFAULT_MAX_WORKERS:
            continue
        CHANNEL_ACTIVE[ch] += 1
        updated = await queue_col.find_one_and_update(
            {"_id": job["_id"], "status": "pending"},
            {"$set": {"status": "processing", "started": now}},
        )
        if updated is None:
            CHANNEL_ACTIVE[ch] = max(0, CHANNEL_ACTIVE[ch] - 1)
            continue
        return job
    return None


async def mark_done(job_id):
    await queue_col.delete_one({"_id": job_id})


async def reschedule(job_id, delay=5):
    await queue_col.update_one(
        {"_id": job_id},
        {
            "$set": {"status": "pending", "ts": time.time() + delay},
            "$inc": {"retries": 1},
        },
    )


async def recover_stuck_jobs(timeout=300):
    cutoff = time.time() - timeout
    r1 = await queue_col.update_many(
        {"status": "processing", "started": {"$lt": cutoff}},
        {"$set": {"status": "pending"}},
    )
    if r1.modified_count:
        logger.info(f"[RECOVER] Reset {r1.modified_count} stuck caption job(s)")
    r2 = await forward_queue.update_many(
        {"status": "processing", "started": {"$lt": cutoff}},
        {"$set": {"status": "pending"}},
    )
    if r2.modified_count:
        logger.info(f"[RECOVER] Reset {r2.modified_count} stuck forward job(s)")


# ════════════════════════════════════════════════════════
#  Forward queue helpers
# ════════════════════════════════════════════════════════
async def enqueue_forward(job: dict):
    await forward_queue.insert_one({
        **job,
        "status":  "pending",
        "retries": 0,
        "ts":      time.time(),
    })


async def forward_done(job_id):
    await forward_queue.delete_one({"_id": job_id})


async def forward_retry(job_id, delay: float):
    await forward_queue.update_one(
        {"_id": job_id},
        {
            "$set": {"status": "pending", "ts": time.time() + delay},
            "$inc": {"retries": 1},
        },
    )


# ════════════════════════════════════════════════════════
#  Global (admin) forward progress helpers  ── NEW
# ════════════════════════════════════════════════════════
async def get_global_ff_progress(channel_id: int) -> dict:
    """Return saved forwarding progress for a channel, or empty dict."""
    doc = await global_ff_progress.find_one({"channel_id": channel_id})
    return doc or {}


async def save_global_ff_progress(
    channel_id: int, last_msg_id: int, total_forwarded: int
):
    """Upsert the last forwarded message id and cumulative count for a channel."""
    await global_ff_progress.update_one(
        {"channel_id": channel_id},
        {
            "$set": {
                "last_msg_id":     last_msg_id,
                "total_forwarded": total_forwarded,
                "updated_at":      time.time(),
            }
        },
        upsert=True,
    )


async def reset_global_ff_progress(channel_id: int):
    """Delete saved progress for a channel (use before a fresh full-forward)."""
    await global_ff_progress.delete_one({"channel_id": channel_id})


# ════════════════════════════════════════════════════════
#  Dump-skip helpers
# ════════════════════════════════════════════════════════
async def set_dump_skip(channel_id: int, status: bool):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$set": {"dump_skip": bool(status)}},
        upsert=True,
    )


async def remove_dump_skip(channel_id: int):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$unset": {"dump_skip": ""}},
    )


async def is_dump_skip(channel_id: int) -> bool:
    doc = await get_channel_cached(channel_id)
    return bool(doc.get("dump_skip", False))


async def get_all_dump_skip_channels():
    cursor = chnl_ids.find({"dump_skip": True})
    return [doc async for doc in cursor]


# ════════════════════════════════════════════════════════
#  Channel settings cache
# ════════════════════════════════════════════════════════
async def get_channel_cached(channel_id: int) -> dict:
    now = time.time()
    cached = _CHANNEL_CACHE.get(channel_id)
    if cached and now - cached["ts"] < CACHE_TTL:
        return cached["data"]
    doc = await chnl_ids.find_one({"chnl_id": channel_id}) or {}
    _CHANNEL_CACHE[channel_id] = {"data": doc, "ts": now}
    return doc


async def set_channel_title_cache(channel_id: int, title: str):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$set": {"_title": title}},
        upsert=True,
    )
    if channel_id in _CHANNEL_CACHE:
        _CHANNEL_CACHE[channel_id]["data"]["_title"] = title


async def get_channel_title_cached(channel_id: int) -> str:
    doc = await get_channel_cached(channel_id)
    return doc.get("_title", str(channel_id))


# ════════════════════════════════════════════════════════
#  User helpers
# ════════════════════════════════════════════════════════
async def insert_user(user_id: int):
    try:
        await users.update_one(
            {"_id": user_id},
            {"$setOnInsert": {"channels": []}},
            upsert=True,
        )
    except Exception:
        pass


async def insert_user_check_new(user_id: int) -> bool:
    try:
        user = await users.find_one({"_id": user_id})
        if user:
            return False
        await users.update_one(
            {"_id": user_id},
            {"$setOnInsert": {"channels": []}},
            upsert=True,
        )
        return True
    except Exception as e:
        logger.warning(f"[ERROR] insert_user_check_new: {e}")
        return False


async def total_user():
    return await users.count_documents({})


async def get_all_users():
    return users.find({})


async def delete_user(user_id):
    await users.delete_one({"_id": user_id})


async def getid():
    return [{"_id": u["_id"]} async for u in users.find({})]


# ════════════════════════════════════════════════════════
#  Channel helpers
# ════════════════════════════════════════════════════════
async def add_user_channel(user_id: int, channel_id: int, channel_title: str):
    await users.update_one(
        {"_id": user_id},
        {"$pull": {"channels": {"channel_id": channel_id}}},
    )
    await users.update_one(
        {"_id": user_id},
        {"$push": {"channels": {"channel_id": channel_id, "channel_title": channel_title}}},
        upsert=True,
    )


async def get_user_channels(user_id):
    data = await users.find_one({"_id": user_id})
    return data.get("channels", []) if data else []


# ════════════════════════════════════════════════════════
#  Caption CRUD
# ════════════════════════════════════════════════════════
async def addCap(chnl_id: int, caption: str):
    await chnl_ids.insert_one({"chnl_id": chnl_id, "caption": caption})


async def updateCap(chnl_id: int, caption: str):
    await chnl_ids.update_one(
        {"chnl_id": chnl_id},
        {"$set": {"caption": caption}},
        upsert=True,
    )
    if chnl_id in _CHANNEL_CACHE:
        _CHANNEL_CACHE[chnl_id]["data"]["caption"] = caption


async def get_channel_caption(chnl_id: int):
    return await chnl_ids.find_one({"chnl_id": chnl_id})


async def delete_channel_caption(chnl_id: int):
    await chnl_ids.update_one({"chnl_id": chnl_id}, {"$unset": {"caption": ""}})
    if chnl_id in _CHANNEL_CACHE:
        _CHANNEL_CACHE[chnl_id]["data"].pop("caption", None)


# ════════════════════════════════════════════════════════
#  Block words
# ════════════════════════════════════════════════════════
async def set_block_words(chnl_id: int, raw_text: str):
    await chnl_ids.update_one(
        {"chnl_id": chnl_id},
        {"$set": {"block_words": raw_text}},
        upsert=True,
    )
    if chnl_id in _CHANNEL_CACHE:
        _CHANNEL_CACHE[chnl_id]["data"]["block_words"] = raw_text


async def get_block_words(chnl_id: int) -> str:
    doc = await get_channel_cached(chnl_id)
    return doc.get("block_words", "")


async def delete_block_words(chnl_id: int):
    await chnl_ids.update_one({"chnl_id": chnl_id}, {"$unset": {"block_words": ""}})
    _CHANNEL_CACHE.pop(chnl_id, None)


# ════════════════════════════════════════════════════════
#  Suffix / Prefix
# ════════════════════════════════════════════════════════
async def set_suffix(channel_id: int, suffix: str):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$set": {"suffix": suffix}},
        upsert=True,
    )
    if channel_id in _CHANNEL_CACHE:
        _CHANNEL_CACHE[channel_id]["data"]["suffix"] = suffix


async def set_prefix(channel_id: int, prefix: str):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$set": {"prefix": prefix}},
        upsert=True,
    )
    if channel_id in _CHANNEL_CACHE:
        _CHANNEL_CACHE[channel_id]["data"]["prefix"] = prefix


async def get_suffix_prefix(channel_id: int):
    data = await get_channel_cached(channel_id)
    return data.get("suffix", ""), data.get("prefix", "")


async def delete_suffix(channel_id: int):
    await chnl_ids.update_one({"chnl_id": channel_id}, {"$unset": {"suffix": ""}})
    _CHANNEL_CACHE.pop(channel_id, None)


async def delete_prefix(channel_id: int):
    await chnl_ids.update_one({"chnl_id": channel_id}, {"$unset": {"prefix": ""}})
    _CHANNEL_CACHE.pop(channel_id, None)


# ════════════════════════════════════════════════════════
#  Link / Emoji remover
# ════════════════════════════════════════════════════════
async def get_link_remover_status(channel_id: int) -> bool:
    doc = await get_channel_cached(channel_id)
    return bool(doc.get("link_remover", False))


async def set_link_remover_status(channel_id: int, status: bool):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$set": {"link_remover": bool(status)}},
        upsert=True,
    )
    if channel_id in _CHANNEL_CACHE:
        _CHANNEL_CACHE[channel_id]["data"]["link_remover"] = bool(status)


async def get_emoji_remover_status(channel_id: int) -> bool:
    doc = await get_channel_cached(channel_id)
    return bool(doc.get("emoji_remover", False))


async def set_emoji_remover_status(channel_id: int, status: bool):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$set": {"emoji_remover": bool(status)}},
        upsert=True,
    )
    if channel_id in _CHANNEL_CACHE:
        _CHANNEL_CACHE[channel_id]["data"]["emoji_remover"] = bool(status)


# ════════════════════════════════════════════════════════
#  Replace words
# ════════════════════════════════════════════════════════
async def get_replace_words(channel_id: int) -> Optional[str]:
    doc = await get_channel_cached(channel_id)
    return doc.get("replace_words")


async def set_replace_words(channel_id: int, text: str):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$set": {"replace_words": text}},
        upsert=True,
    )
    if channel_id in _CHANNEL_CACHE:
        _CHANNEL_CACHE[channel_id]["data"]["replace_words"] = text


async def delete_replace_words_db(channel_id: int):
    await chnl_ids.update_one({"chnl_id": channel_id}, {"$unset": {"replace_words": ""}})
    _CHANNEL_CACHE.pop(channel_id, None)


# ════════════════════════════════════════════════════════
#  URL Buttons
# ════════════════════════════════════════════════════════
async def set_url_buttons(channel_id: int, buttons: list):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$set": {"url_buttons": buttons}},
        upsert=True,
    )
    if channel_id in _CHANNEL_CACHE:
        _CHANNEL_CACHE[channel_id]["data"]["url_buttons"] = buttons


async def get_url_buttons(channel_id: int) -> list:
    doc = await get_channel_cached(channel_id)
    return doc.get("url_buttons", [])


async def delete_url_buttons(channel_id: int):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$unset": {"url_buttons": ""}},
    )
    _CHANNEL_CACHE.pop(channel_id, None)
