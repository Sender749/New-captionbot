import motor.motor_asyncio
from info import *
from typing import Optional
import time
from pymongo import ReturnDocument
from collections import defaultdict

# -------- Caption Scheduler State (GLOBAL) --------
CHANNEL_ACTIVE = defaultdict(int)   # channel_id -> active workers
CHANNEL_COOLDOWN = {}               # channel_id -> unblock timestamp
DEFAULT_MAX_WORKERS = 4             # concurrent edits per channel

_CHANNEL_CACHE = {}
CACHE_TTL = 120  # seconds

client = motor.motor_asyncio.AsyncIOMotorClient(
    MONGO_DB,
    maxPoolSize=50,          # allow more concurrent DB ops
    minPoolSize=5,
    serverSelectionTimeoutMS=5000,
)
db = client.captions_with_chnl
chnl_ids = db.chnl_ids
users = db.users
user_channels = db.user_channels
queue_col = db.caption_queue
forward_queue = db.forward_queue

# ─────────────────────────────────────────────────
#  CAPTION QUEUE
# ─────────────────────────────────────────────────

async def ensure_queue_indexes():
    """Create indexes that make queue polling fast."""
    await queue_col.create_index([("status", 1), ("ts", 1)])
    await queue_col.create_index([("chat_id", 1), ("status", 1)])

async def enqueue_caption(job: dict):
    await queue_col.insert_one({
        **job,
        "status": "pending",
        "retries": 0,
        "ts": time.time()
    })

async def fetch_channel_job():
    """
    Fair-pick: atomically claim one pending caption job whose channel
    is not on cooldown and has spare worker capacity.
    Uses find_one_and_update to avoid race conditions between workers.
    """
    now = time.time()

    # Build exclusion list in-memory (fast, no extra DB round-trip)
    blocked_channels = set()
    for ch, until in list(CHANNEL_COOLDOWN.items()):
        if until > now:
            blocked_channels.add(ch)
    for ch, active in CHANNEL_ACTIVE.items():
        if active >= DEFAULT_MAX_WORKERS:
            blocked_channels.add(ch)

    query = {"status": "pending"}
    if blocked_channels:
        query["chat_id"] = {"$nin": list(blocked_channels)}

    job = await queue_col.find_one_and_update(
        query,
        {"$set": {"status": "processing", "started": now}},
        sort=[("ts", 1)],
        return_document=ReturnDocument.AFTER,
    )
    if job:
        ch = job["chat_id"]
        CHANNEL_ACTIVE[ch] += 1
    return job

async def mark_done(job_id):
    await queue_col.delete_one({"_id": job_id})

async def reschedule(job_id, delay=5):
    await queue_col.update_one(
        {"_id": job_id},
        {"$set": {"status": "pending", "ts": time.time() + delay},
         "$inc": {"retries": 1}}
    )

async def recover_stuck_jobs(timeout=300):
    """Reset any jobs stuck in 'processing' state at startup."""
    result = await queue_col.update_many(
        {
            "status": "processing",
            "started": {"$lt": time.time() - timeout}
        },
        {"$set": {"status": "pending"}}
    )
    if result.modified_count:
        print(f"[RECOVERY] Reset {result.modified_count} stuck caption jobs")

# ─────────────────────────────────────────────────
#  FORWARD QUEUE
# ─────────────────────────────────────────────────

async def ensure_forward_indexes():
    await forward_queue.create_index([("status", 1), ("ts", 1)])
    await forward_queue.create_index([("src", 1)])
    await forward_queue.create_index([("dst", 1)])
    await forward_queue.create_index([("session_id", 1)])
    await forward_queue.create_index([("user_id", 1), ("status", 1)])

async def enqueue_forward(job: dict):
    await forward_queue.insert_one({
        **job,
        "status": "pending",
        "retries": 0,
        "ts": time.time()
    })

async def forward_done(job_id):
    await forward_queue.delete_one({"_id": job_id})

async def forward_retry(job_id, delay):
    await forward_queue.update_one(
        {"_id": job_id},
        {"$set": {"status": "pending", "ts": time.time() + delay},
         "$inc": {"retries": 1}}
    )

async def recover_stuck_forward_jobs(timeout=600):
    """Reset any forward jobs stuck in 'processing' at startup."""
    result = await forward_queue.update_many(
        {
            "status": "processing",
            "started": {"$lt": time.time() - timeout}
        },
        {"$set": {"status": "pending"}}
    )
    if result.modified_count:
        print(f"[RECOVERY] Reset {result.modified_count} stuck forward jobs")

# ─────────────────────────────────────────────────
#  DUMP SKIP
# ─────────────────────────────────────────────────

async def set_dump_skip(channel_id: int, status: bool):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$set": {"dump_skip": bool(status)}},
        upsert=True
    )

async def remove_dump_skip(channel_id: int):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$unset": {"dump_skip": ""}}
    )

async def is_dump_skip(channel_id: int) -> bool:
    doc = await chnl_ids.find_one({"chnl_id": channel_id})
    return bool(doc.get("dump_skip", False)) if doc else False

async def get_all_dump_skip_channels():
    cursor = chnl_ids.find({"dump_skip": True})
    return [doc async for doc in cursor]

# ─────────────────────────────────────────────────
#  USER FUNCTIONS
# ─────────────────────────────────────────────────

async def insert_user(user_id: int):
    try:
        await users.update_one(
            {"_id": user_id},
            {"$setOnInsert": {"channels": []}},
            upsert=True
        )
    except:
        pass

async def total_user():
    return await users.count_documents({})

async def get_all_users():
    return users.find({})

async def delete_user(user_id):
    await users.delete_one({"_id": user_id})

async def getid():
    users_list = []
    cursor = users.find({})
    async for user in cursor:
        users_list.append({"_id": user["_id"]})
    return users_list

async def insert_user_check_new(user_id: int) -> bool:
    try:
        user = await users.find_one({"_id": user_id})
        if user:
            return False
        await users.update_one(
            {"_id": user_id},
            {"$setOnInsert": {"channels": []}},
            upsert=True
        )
        return True
    except Exception as e:
        print(f"[ERROR] in insert_user_check_new: {e}")
        return False

# ─────────────────────────────────────────────────
#  CHANNEL FUNCTIONS
# ─────────────────────────────────────────────────

async def add_user_channel(user_id: int, channel_id: int, channel_title: str):
    await users.update_one(
        {"_id": user_id},
        {"$pull": {"channels": {"channel_id": channel_id}}}
    )
    await users.update_one(
        {"_id": user_id},
        {"$push": {"channels": {
            "channel_id": channel_id,
            "channel_title": channel_title
        }}},
        upsert=True
    )

async def get_user_channels(user_id):
    data = await users.find_one({"_id": user_id})
    return data.get("channels", []) if data else []

# ─────────────────────────────────────────────────
#  CAPTION FUNCTIONS
# ─────────────────────────────────────────────────

async def addCap(chnl_id: int, caption: str):
    dets = {"chnl_id": chnl_id, "caption": caption}
    await chnl_ids.insert_one(dets)

async def updateCap(chnl_id: int, caption: str):
    await chnl_ids.update_one(
        {"chnl_id": chnl_id},
        {"$set": {"caption": caption}},
        upsert=True
    )
    if chnl_id in _CHANNEL_CACHE:
        _CHANNEL_CACHE[chnl_id]["data"]["caption"] = caption

async def get_channel_caption(chnl_id: int):
    return await chnl_ids.find_one({"chnl_id": chnl_id})

async def delete_channel_caption(chnl_id: int):
    await chnl_ids.update_one(
        {"chnl_id": chnl_id},
        {"$unset": {"caption": ""}}
    )
    if chnl_id in _CHANNEL_CACHE:
        _CHANNEL_CACHE[chnl_id]["data"].pop("caption", None)

# ─────────────────────────────────────────────────
#  BLOCKED WORDS
# ─────────────────────────────────────────────────

async def set_block_words(chnl_id: int, raw_text: str):
    await chnl_ids.update_one(
        {"chnl_id": chnl_id},
        {"$set": {"block_words": raw_text}},
        upsert=True
    )
    if chnl_id in _CHANNEL_CACHE:
        _CHANNEL_CACHE[chnl_id]["data"]["block_words"] = raw_text

async def get_block_words(chnl_id: int) -> str:
    doc = await get_channel_cached(chnl_id)
    return doc.get("block_words", "")

async def delete_block_words(chnl_id: int):
    await chnl_ids.update_one(
        {"chnl_id": chnl_id},
        {"$unset": {"block_words": ""}}
    )
    _CHANNEL_CACHE.pop(chnl_id, None)

# ─────────────────────────────────────────────────
#  SUFFIX & PREFIX
# ─────────────────────────────────────────────────

async def set_suffix(channel_id: int, suffix: str):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$set": {"suffix": suffix}},
        upsert=True
    )
    if channel_id in _CHANNEL_CACHE:
        _CHANNEL_CACHE[channel_id]["data"]["suffix"] = suffix

async def set_prefix(channel_id: int, prefix: str):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$set": {"prefix": prefix}},
        upsert=True
    )
    if channel_id in _CHANNEL_CACHE:
        _CHANNEL_CACHE[channel_id]["data"]["prefix"] = prefix

async def get_suffix_prefix(channel_id: int):
    data = await get_channel_cached(channel_id)
    return data.get("suffix", ""), data.get("prefix", "")

async def delete_suffix(channel_id: int):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$unset": {"suffix": ""}}
    )
    _CHANNEL_CACHE.pop(channel_id, None)

async def delete_prefix(channel_id: int):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$unset": {"prefix": ""}}
    )
    _CHANNEL_CACHE.pop(channel_id, None)

# ─────────────────────────────────────────────────
#  LINK REMOVER
# ─────────────────────────────────────────────────

async def get_link_remover_status(channel_id: int) -> bool:
    doc = await get_channel_cached(channel_id)
    return bool(doc.get("link_remover", False))

async def set_link_remover_status(channel_id: int, status: bool):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$set": {"link_remover": bool(status)}},
        upsert=True
    )
    if channel_id in _CHANNEL_CACHE:
        _CHANNEL_CACHE[channel_id]["data"]["link_remover"] = bool(status)

# ─────────────────────────────────────────────────
#  REPLACE WORDS
# ─────────────────────────────────────────────────

async def get_replace_words(channel_id: int) -> Optional[str]:
    doc = await get_channel_cached(channel_id)
    return doc.get("replace_words")

async def set_replace_words(channel_id: int, text: str):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$set": {"replace_words": text}},
        upsert=True
    )
    if channel_id in _CHANNEL_CACHE:
        _CHANNEL_CACHE[channel_id]["data"]["replace_words"] = text

async def delete_replace_words_db(channel_id: int):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$unset": {"replace_words": ""}}
    )
    _CHANNEL_CACHE.pop(channel_id, None)

# ─────────────────────────────────────────────────
#  CHANNEL TITLE CACHE
# ─────────────────────────────────────────────────

async def set_channel_title_cache(channel_id: int, title: str):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$set": {"_title": title}},
        upsert=True
    )
    if channel_id in _CHANNEL_CACHE:
        _CHANNEL_CACHE[channel_id]["data"]["_title"] = title

async def get_channel_title_cached(channel_id: int) -> str:
    doc = await get_channel_cached(channel_id)
    return doc.get("_title", str(channel_id))

async def get_channel_cached(channel_id: int):
    now = time.time()
    cached = _CHANNEL_CACHE.get(channel_id)
    if cached and now - cached["ts"] < CACHE_TTL:
        return cached["data"]
    doc = await chnl_ids.find_one({"chnl_id": channel_id}) or {}
    _CHANNEL_CACHE[channel_id] = {"data": doc, "ts": now}
    return doc

# ─────────────────────────────────────────────────
#  EMOJI REMOVER
# ─────────────────────────────────────────────────

async def get_emoji_remover_status(channel_id: int) -> bool:
    doc = await get_channel_cached(channel_id)
    return bool(doc.get("emoji_remover", False))

async def set_emoji_remover_status(channel_id: int, status: bool):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$set": {"emoji_remover": bool(status)}},
        upsert=True
    )
    if channel_id in _CHANNEL_CACHE:
        _CHANNEL_CACHE[channel_id]["data"]["emoji_remover"] = bool(status)

# ─────────────────────────────────────────────────
#  URL BUTTONS
# ─────────────────────────────────────────────────

async def set_url_buttons(channel_id: int, buttons: list):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$set": {"url_buttons": buttons}},
        upsert=True
    )
    if channel_id in _CHANNEL_CACHE:
        _CHANNEL_CACHE[channel_id]["data"]["url_buttons"] = buttons

async def get_url_buttons(channel_id: int) -> list:
    doc = await get_channel_cached(channel_id)
    return doc.get("url_buttons", [])

async def delete_url_buttons(channel_id: int):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$unset": {"url_buttons": ""}}
    )
    _CHANNEL_CACHE.pop(channel_id, None)
