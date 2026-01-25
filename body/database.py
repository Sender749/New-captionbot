import motor.motor_asyncio
from info import *
from typing import Optional
import time
from pymongo import ReturnDocument

_CHANNEL_CACHE = {}
CACHE_TTL = 30  # seconds
client = motor.motor_asyncio.AsyncIOMotorClient(MONGO_DB)
db = client.captions_with_chnl
chnl_ids = db.chnl_ids
users = db.users
user_channels = db.user_channels 
queue_col = db.caption_queue
forward_queue = db.forward_queue

# ---------------- Queue System for Forwarding ----------------
async def ensure_forward_indexes():
    await forward_queue.create_index([("status", 1), ("ts", 1)])
    await forward_queue.create_index([("src", 1)])
    await forward_queue.create_index([("dst", 1)])

async def enqueue_forward(job: dict):
    await forward_queue.insert_one({
        **job,
        "status": "pending",
        "retries": 0,
        "ts": time.time()
    })

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

async def forward_done(job_id):
    await forward_queue.delete_one({"_id": job_id})

async def forward_retry(job_id, delay):
    await forward_queue.update_one(
        {"_id": job_id},
        {"$set": {"status": "pending", "ts": time.time() + delay},
         "$inc": {"retries": 1}}
    )

# ---------------- Dump skip functions ----------------

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

# ---------------- Queue System for Caption ----------------
async def ensure_queue_indexes():
    await queue_col.create_index([("status", 1), ("ts", 1)])
    await queue_col.create_index([("chat_id", 1)])

async def enqueue_caption(job: dict):
    await queue_col.insert_one({
        **job,
        "status": "pending",
        "retries": 0,
        "ts": time.time()
    })

async def fetch_channel_job():
    now = time.time()
    cursor = queue_col.find(
        {"status": "pending"}
    ).sort("ts", 1)
    async for job in cursor:
        ch = job["chat_id"]
        if CHANNEL_COOLDOWN.get(ch, 0) > now:
            continue
        if CHANNEL_ACTIVE[ch] >= DEFAULT_MAX_WORKERS:
            continue
        CHANNEL_ACTIVE[ch] += 1
        await queue_col.update_one(
            {"_id": job["_id"], "status": "pending"},
            {"$set": {"status": "processing", "started": now}}
        )
        return job
    return None

async def mark_done(job_id):
    await queue_col.delete_one({"_id": job_id})
    
async def reschedule(job_id, delay=5):
    await queue_col.update_one(
        {"_id": job_id},
        {"$set": {"status": "pending", "ts": time.time() + delay},
         "$inc": {"retries": 1}}
    )

async def recover_stuck_jobs(timeout=300):
    await queue_col.update_many(
        {
            "status": "processing",
            "started": {"$lt": time.time() - timeout}
        },
        {"$set": {"status": "pending"}}
    )

# ---------------- User functions ----------------
async def insert_user(user_id: int):
    """Add user to DB if not exists"""
    try:
        await users.update_one({"_id": user_id}, {"$setOnInsert": {"channels": []}}, upsert=True)
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
            return False  # User already exists
        await users.update_one(
            {"_id": user_id},
            {"$setOnInsert": {"channels": []}},
            upsert=True
        )
        return True  
    except Exception as e:
        print(f"[ERROR] in insert_user_check_new: {e}")
        return False


# ---------------- Channel functions ----------------
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


# ---------------- Caption functions ----------------
async def addCap(chnl_id: int, caption: str):
    dets = {"chnl_id": chnl_id, "caption": caption}
    await chnl_ids.insert_one(dets)

async def updateCap(chnl_id: int, caption: str):
    await chnl_ids.update_one({"chnl_id": chnl_id}, {"$set": {"caption": caption}})

async def get_channel_caption(chnl_id: int):
    return await chnl_ids.find_one({"chnl_id": chnl_id})

async def delete_channel_caption(chnl_id: int):
    await chnl_ids.delete_one({"chnl_id": chnl_id})

# ---------------- Blocked Words functions ----------------
async def set_block_words(chnl_id: int, raw_text: str):
    await chnl_ids.update_one(
        {"chnl_id": chnl_id},
        {"$set": {"block_words": raw_text}},
        upsert=True
    )

async def get_block_words(chnl_id: int) -> str:
    doc = await chnl_ids.find_one({"chnl_id": chnl_id})
    return doc.get("block_words", "") if doc else ""

async def delete_block_words(chnl_id: int):
    """Delete all blocked words for a channel"""
    await chnl_ids.update_one({"chnl_id": chnl_id}, {"$unset": {"block_words": ""}})

# ---------------- Suffix & Prefix functions ----------------
async def set_suffix(channel_id: int, suffix: str):
    """Set suffix for a channel"""
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$set": {"suffix": suffix}},
        upsert=True
    )

async def set_prefix(channel_id: int, prefix: str):
    """Set prefix for a channel"""
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$set": {"prefix": prefix}},
        upsert=True
    )

async def get_suffix_prefix(channel_id: int):
    """Get suffix & prefix for a channel"""
    data = await chnl_ids.find_one({"chnl_id": channel_id})
    if data:
        return data.get("suffix", ""), data.get("prefix", "")
    return "", ""

async def delete_suffix(channel_id: int):
    await chnl_ids.update_one({"chnl_id": channel_id}, {"$unset": {"suffix": ""}})

async def delete_prefix(channel_id: int):
    await chnl_ids.update_one({"chnl_id": channel_id}, {"$unset": {"prefix": ""}})

# ---------------- Link remover ----------------
async def get_link_remover_status(channel_id: int) -> bool:
    doc = await chnl_ids.find_one({"chnl_id": channel_id})
    return bool(doc.get("link_remover", False)) if doc else False

async def set_link_remover_status(channel_id: int, status: bool):
    await chnl_ids.update_one({"chnl_id": channel_id}, {"$set": {"link_remover": bool(status)}}, upsert=True)

# ---------------- Replace words ----------------
async def get_replace_words(channel_id: int) -> Optional[str]:
    """Return stored replace words string (raw) or None."""
    doc = await chnl_ids.find_one({"chnl_id": channel_id})
    return doc.get("replace_words") if doc else None

async def set_replace_words(channel_id: int, text: str):
    """Store raw replace words text."""
    await chnl_ids.update_one({"chnl_id": channel_id}, {"$set": {"replace_words": text}}, upsert=True)

async def delete_replace_words_db(channel_id: int):
    await chnl_ids.update_one({"chnl_id": channel_id}, {"$unset": {"replace_words": ""}})

async def get_channel_title_fast(user_id: int, channel_id: int) -> str:
    user = await users.find_one(
        {"_id": user_id, "channels.channel_id": channel_id},
        {"channels.$": 1}
    )
    if user and "channels" in user and user["channels"]:
        return user["channels"][0].get("channel_title", str(channel_id))
    return str(channel_id)

async def get_channel_cached(channel_id: int):
    now = time.time()
    cached = _CHANNEL_CACHE.get(channel_id)
    if cached and now - cached["ts"] < CACHE_TTL:
        return cached["data"]
    doc = await chnl_ids.find_one({"chnl_id": channel_id}) or {}
    _CHANNEL_CACHE[channel_id] = {"data": doc, "ts": now}
    return doc

# ---------------- Emoji remover ----------------
async def get_emoji_remover_status(channel_id: int) -> bool:
    doc = await chnl_ids.find_one({"chnl_id": channel_id})
    return bool(doc.get("emoji_remover", False)) if doc else False

async def set_emoji_remover_status(channel_id: int, status: bool):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$set": {"emoji_remover": bool(status)}},
        upsert=True
    )

# ---------------- URL Buttons ----------------
async def set_url_buttons(channel_id: int, buttons: list):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$set": {"url_buttons": buttons}},
        upsert=True
    )

async def get_url_buttons(channel_id: int) -> list:
    doc = await chnl_ids.find_one({"chnl_id": channel_id})
    return doc.get("url_buttons", []) if doc else []

async def delete_url_buttons(channel_id: int):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$unset": {"url_buttons": ""}}
    )
