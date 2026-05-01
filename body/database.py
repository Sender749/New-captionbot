import motor.motor_asyncio
from info import *
from typing import Optional
import time
from collections import defaultdict

# ─────────────────────────────────────────────
#  MongoDB client
# ─────────────────────────────────────────────
client  = motor.motor_asyncio.AsyncIOMotorClient(MONGO_DB)
db      = client.captions_with_chnl
chnl_ids       = db.chnl_ids
users          = db.users
user_channels  = db.user_channels
queue_col      = db.caption_queue
forward_queue  = db.forward_queue

# ─────────────────────────────────────────────
#  In-memory rate-limiter state
#  (per-channel / per-pair — safe because a
#   single asyncio process owns these dicts)
# ─────────────────────────────────────────────
CHANNEL_ACTIVE   = defaultdict(int)   # channel_id  -> active caption workers
CHANNEL_COOLDOWN = {}                 # channel_id  -> unblock timestamp
FORWARD_COOLDOWN = {}                 # (src,dst)   -> unblock timestamp

# Caps — how many concurrent edits per channel at once
DEFAULT_MAX_WORKERS  = 4
# How many forward jobs may run simultaneously for one (src,dst) pair
MAX_FORWARD_PER_PAIR = 2

# Used by bot.py / Caption.py (imported from here for backward compat)
_CHANNEL_CACHE = {}
CACHE_TTL      = 120   # seconds

# ─────────────────────────────────────────────
#  Index bootstrap
# ─────────────────────────────────────────────
async def ensure_queue_indexes():
    await queue_col.create_index([("user_id", 1), ("status", 1), ("ts", 1)])
    await queue_col.create_index([("chat_id", 1)])
    await queue_col.create_index([("status", 1), ("ts", 1)])

async def ensure_forward_indexes():
    await forward_queue.create_index([("user_id", 1), ("status", 1), ("ts", 1)])
    await forward_queue.create_index([("session_id", 1)])
    await forward_queue.create_index([("status", 1), ("ts", 1)])
    await forward_queue.create_index([("src", 1), ("dst", 1)])

# ─────────────────────────────────────────────
#  Caption queue  (per-user fair-pick)
# ─────────────────────────────────────────────
async def enqueue_caption(job: dict):
    """Insert a caption-edit job.  job must contain chat_id and user_id."""
    await queue_col.insert_one({
        **job,
        "status":  "pending",
        "retries": 0,
        "ts":      time.time(),
    })

async def fetch_caption_job_for_user(user_id: int):
    """
    Claim one pending caption job that belongs to *user_id*.
    Respects per-channel concurrency cap and FloodWait cooldowns.
    Returns the job doc or None.
    """
    now = time.time()
    cursor = queue_col.find(
        {"user_id": user_id, "status": "pending"}
    ).sort("ts", 1)
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

async def fetch_any_caption_job():
    """
    Claim one pending caption job from any user (global fallback worker).
    Same concurrency rules apply.
    """
    now = time.time()
    cursor = queue_col.find({"status": "pending"}).sort("ts", 1)
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
        {"$set": {"status": "pending", "ts": time.time() + delay},
         "$inc": {"retries": 1}},
    )

async def recover_stuck_jobs(timeout=300):
    await queue_col.update_many(
        {"status": "processing", "started": {"$lt": time.time() - timeout}},
        {"$set": {"status": "pending"}},
    )

# ─────────────────────────────────────────────
#  Forward queue  (per-session)
# ─────────────────────────────────────────────
async def enqueue_forward(job: dict):
    await forward_queue.insert_one({
        **job,
        "status":  "pending",
        "retries": 0,
        "ts":      time.time(),
    })

async def fetch_forward_job_for_session(session_id: str):
    """
    Claim one pending forward job that belongs to *session_id*.
    Respects per-(src,dst) concurrency and FloodWait cooldowns.
    """
    now = time.time()
    cursor = forward_queue.find(
        {"session_id": session_id, "status": "pending"}
    ).sort("ts", 1)
    async for job in cursor:
        key = (job["src"], job["dst"])
        if FORWARD_COOLDOWN.get(key, 0) > now:
            continue
        updated = await forward_queue.find_one_and_update(
            {"_id": job["_id"], "status": "pending"},
            {"$set": {"status": "processing", "started": now}},
        )
        if updated is None:
            continue
        return job
    return None

async def forward_done(job_id):
    await forward_queue.delete_one({"_id": job_id})

async def forward_retry(job_id, delay):
    await forward_queue.update_one(
        {"_id": job_id},
        {"$set": {"status": "pending", "ts": time.time() + delay},
         "$inc": {"retries": 1}},
    )

# ─────────────────────────────────────────────
#  Dump-skip
# ─────────────────────────────────────────────
async def set_dump_skip(channel_id: int, status: bool):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$set": {"dump_skip": bool(status)}},
        upsert=True,
    )

async def remove_dump_skip(channel_id: int):
    await chnl_ids.update_one({"chnl_id": channel_id}, {"$unset": {"dump_skip": ""}})

async def is_dump_skip(channel_id: int) -> bool:
    doc = await chnl_ids.find_one({"chnl_id": channel_id})
    return bool(doc.get("dump_skip", False)) if doc else False

async def get_all_dump_skip_channels():
    return [doc async for doc in chnl_ids.find({"dump_skip": True})]

# ─────────────────────────────────────────────
#  Channel caption / settings
# ─────────────────────────────────────────────
async def get_channel_cached(channel_id: int) -> dict:
    now = time.time()
    cached = _CHANNEL_CACHE.get(channel_id)
    if cached and (now - cached["_ts"]) < CACHE_TTL:
        return cached
    doc = await chnl_ids.find_one({"chnl_id": channel_id}) or {}
    doc["_ts"] = now
    _CHANNEL_CACHE[channel_id] = doc
    return doc

async def set_channel_title_cache(channel_id: int, title: str):
    _CHANNEL_CACHE.pop(channel_id, None)
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$set": {"_title": title}},
        upsert=True,
    )

async def get_channel_caption(channel_id: int):
    doc = await get_channel_cached(channel_id)
    return doc.get("caption")

async def updateCap(channel_id: int, caption: str):
    _CHANNEL_CACHE.pop(channel_id, None)
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$set": {"caption": caption}},
        upsert=True,
    )

async def delete_channel_caption(channel_id: int):
    _CHANNEL_CACHE.pop(channel_id, None)
    await chnl_ids.update_one({"chnl_id": channel_id}, {"$unset": {"caption": ""}})

async def set_block_words(channel_id: int, words: str):
    _CHANNEL_CACHE.pop(channel_id, None)
    await chnl_ids.update_one(
        {"chnl_id": channel_id}, {"$set": {"block_words": words}}, upsert=True
    )

async def get_block_words(channel_id: int) -> str:
    doc = await get_channel_cached(channel_id)
    return doc.get("block_words", "")

async def delete_block_words(channel_id: int):
    _CHANNEL_CACHE.pop(channel_id, None)
    await chnl_ids.update_one({"chnl_id": channel_id}, {"$unset": {"block_words": ""}})

async def set_suffix(channel_id: int, text: str):
    _CHANNEL_CACHE.pop(channel_id, None)
    await chnl_ids.update_one(
        {"chnl_id": channel_id}, {"$set": {"suffix": text}}, upsert=True
    )

async def set_prefix(channel_id: int, text: str):
    _CHANNEL_CACHE.pop(channel_id, None)
    await chnl_ids.update_one(
        {"chnl_id": channel_id}, {"$set": {"prefix": text}}, upsert=True
    )

async def get_suffix_prefix(channel_id: int):
    doc = await get_channel_cached(channel_id)
    return doc.get("suffix", ""), doc.get("prefix", "")

async def delete_suffix(channel_id: int):
    _CHANNEL_CACHE.pop(channel_id, None)
    await chnl_ids.update_one({"chnl_id": channel_id}, {"$unset": {"suffix": ""}})

async def delete_prefix(channel_id: int):
    _CHANNEL_CACHE.pop(channel_id, None)
    await chnl_ids.update_one({"chnl_id": channel_id}, {"$unset": {"prefix": ""}})

async def set_replace_words(channel_id: int, text: str):
    _CHANNEL_CACHE.pop(channel_id, None)
    await chnl_ids.update_one(
        {"chnl_id": channel_id}, {"$set": {"replace_words": text}}, upsert=True
    )

async def get_replace_words(channel_id: int) -> str:
    doc = await get_channel_cached(channel_id)
    return doc.get("replace_words", "")

async def delete_replace_words_db(channel_id: int):
    _CHANNEL_CACHE.pop(channel_id, None)
    await chnl_ids.update_one({"chnl_id": channel_id}, {"$unset": {"replace_words": ""}})

async def set_link_remover_status(channel_id: int, status: bool):
    _CHANNEL_CACHE.pop(channel_id, None)
    await chnl_ids.update_one(
        {"chnl_id": channel_id}, {"$set": {"link_remover": status}}, upsert=True
    )

async def get_link_remover_status(channel_id: int) -> bool:
    doc = await get_channel_cached(channel_id)
    return bool(doc.get("link_remover", False))

async def set_emoji_remover_status(channel_id: int, status: bool):
    _CHANNEL_CACHE.pop(channel_id, None)
    await chnl_ids.update_one(
        {"chnl_id": channel_id}, {"$set": {"emoji_remover": status}}, upsert=True
    )

async def get_emoji_remover_status(channel_id: int) -> bool:
    doc = await get_channel_cached(channel_id)
    return bool(doc.get("emoji_remover", False))

async def set_url_buttons(channel_id: int, buttons: list):
    _CHANNEL_CACHE.pop(channel_id, None)
    await chnl_ids.update_one(
        {"chnl_id": channel_id}, {"$set": {"url_buttons": buttons}}, upsert=True
    )

async def delete_url_buttons(channel_id: int):
    _CHANNEL_CACHE.pop(channel_id, None)
    await chnl_ids.update_one({"chnl_id": channel_id}, {"$unset": {"url_buttons": ""}})

# ─────────────────────────────────────────────
#  User / channel management
# ─────────────────────────────────────────────
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
    result = await users.update_one(
        {"_id": user_id},
        {"$setOnInsert": {"channels": []}},
        upsert=True,
    )
    return result.upserted_id is not None

async def add_user_channel(user_id: int, channel_id: int, channel_title: str):
    await users.update_one(
        {"_id": user_id},
        {"$addToSet": {"channels": {"channel_id": channel_id, "channel_title": channel_title}}},
        upsert=True,
    )

async def get_user_channels(user_id: int) -> list:
    doc = await users.find_one({"_id": user_id})
    return doc.get("channels", []) if doc else []

async def getid():
    return [doc async for doc in users.find({}, {"_id": 1})]

async def total_user() -> int:
    return await users.count_documents({})

async def delete_user(user_id: int):
    await users.delete_one({"_id": user_id})
