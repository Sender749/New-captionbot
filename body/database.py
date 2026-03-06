import motor.motor_asyncio
from info import *
from typing import Optional
import time
from pymongo import ReturnDocument
from collections import defaultdict

# -------- Caption Scheduler State (GLOBAL) --------
CHANNEL_ACTIVE = defaultdict(int)
CHANNEL_COOLDOWN = {}
DEFAULT_MAX_WORKERS = 2

_CHANNEL_CACHE = {}
_CHAT_TITLE_CACHE = {}
CACHE_TTL = 120
CHAT_TITLE_TTL = 300

client = motor.motor_asyncio.AsyncIOMotorClient(
    MONGO_DB,
    serverSelectionTimeoutMS=5000,
    connectTimeoutMS=5000,
    socketTimeoutMS=10000,
    maxPoolSize=50,
    minPoolSize=5,
)
db = client.captions_with_chnl
chnl_ids = db.chnl_ids
users = db.users
user_channels = db.user_channels
queue_col = db.caption_queue

# ---------------- Chat Title Cache ----------------
def get_cached_chat_title(channel_id: int) -> Optional[str]:
    now = time.time()
    cached = _CHAT_TITLE_CACHE.get(channel_id)
    if cached and now - cached["ts"] < CHAT_TITLE_TTL:
        return cached["title"]
    return None

def set_cached_chat_title(channel_id: int, title: str):
    _CHAT_TITLE_CACHE[channel_id] = {"title": title, "ts": time.time()}

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
    doc = await get_channel_cached(channel_id)
    return bool(doc.get("dump_skip", False))

async def get_all_dump_skip_channels():
    cursor = chnl_ids.find({"dump_skip": True})
    return [doc async for doc in cursor]

# ---------------- Queue System for Caption ----------------
async def ensure_queue_indexes():
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
    now = time.time()
    blocked = [ch for ch, ts in CHANNEL_COOLDOWN.items() if ts > now]
    blocked += [ch for ch, active in CHANNEL_ACTIVE.items() if active >= DEFAULT_MAX_WORKERS]

    query = {"status": "pending"}
    if blocked:
        query["chat_id"] = {"$nin": blocked}

    job = await queue_col.find_one_and_update(
        query,
        {"$set": {"status": "processing", "started": now}},
        sort=[("ts", 1)],
        return_document=ReturnDocument.AFTER
    )
    if job:
        CHANNEL_ACTIVE[job["chat_id"]] += 1
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
    await queue_col.update_many(
        {"status": "processing", "started": {"$lt": time.time() - timeout}},
        {"$set": {"status": "pending"}}
    )

# ---------------- User functions ----------------
async def insert_user(user_id: int):
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
    cursor = users.find({}, {"_id": 1})
    return [{"_id": u["_id"]} async for u in cursor]

async def insert_user_check_new(user_id: int) -> bool:
    try:
        result = await users.update_one(
            {"_id": user_id},
            {"$setOnInsert": {"channels": []}},
            upsert=True
        )
        return result.upserted_id is not None
    except Exception as e:
        print(f"[ERROR] insert_user_check_new: {e}")
        return False

# ---------------- Channel functions ----------------
async def add_user_channel(user_id: int, channel_id: int, channel_title: str):
    await users.update_one({"_id": user_id}, {"$pull": {"channels": {"channel_id": channel_id}}})
    await users.update_one(
        {"_id": user_id},
        {"$push": {"channels": {"channel_id": channel_id, "channel_title": channel_title}}},
        upsert=True
    )

async def get_user_channels(user_id):
    data = await users.find_one({"_id": user_id}, {"channels": 1})
    return data.get("channels", []) if data else []

# ---------------- Caption functions ----------------
async def addCap(chnl_id: int, caption: str):
    await chnl_ids.insert_one({"chnl_id": chnl_id, "caption": caption})

async def updateCap(chnl_id: int, caption: str):
    await chnl_ids.update_one({"chnl_id": chnl_id}, {"$set": {"caption": caption}})

async def get_channel_caption(chnl_id: int):
    return await chnl_ids.find_one({"chnl_id": chnl_id})

async def delete_channel_caption(chnl_id: int):
    await chnl_ids.delete_one({"chnl_id": chnl_id})

# ---------------- Blocked Words ----------------
async def set_block_words(chnl_id: int, raw_text: str):
    await chnl_ids.update_one({"chnl_id": chnl_id}, {"$set": {"block_words": raw_text}}, upsert=True)
    _CHANNEL_CACHE.pop(chnl_id, None)

async def get_block_words(chnl_id: int) -> str:
    doc = await get_channel_cached(chnl_id)
    return doc.get("block_words", "")

async def delete_block_words(chnl_id: int):
    await chnl_ids.update_one({"chnl_id": chnl_id}, {"$unset": {"block_words": ""}})
    _CHANNEL_CACHE.pop(chnl_id, None)

# ---------------- Suffix & Prefix ----------------
async def set_suffix(channel_id: int, suffix: str):
    await chnl_ids.update_one({"chnl_id": channel_id}, {"$set": {"suffix": suffix}}, upsert=True)
    _CHANNEL_CACHE.pop(channel_id, None)

async def set_prefix(channel_id: int, prefix: str):
    await chnl_ids.update_one({"chnl_id": channel_id}, {"$set": {"prefix": prefix}}, upsert=True)
    _CHANNEL_CACHE.pop(channel_id, None)

async def get_suffix_prefix(channel_id: int):
    data = await get_channel_cached(channel_id)
    return data.get("suffix", ""), data.get("prefix", "")

async def delete_suffix(channel_id: int):
    await chnl_ids.update_one({"chnl_id": channel_id}, {"$unset": {"suffix": ""}})
    _CHANNEL_CACHE.pop(channel_id, None)

async def delete_prefix(channel_id: int):
    await chnl_ids.update_one({"chnl_id": channel_id}, {"$unset": {"prefix": ""}})
    _CHANNEL_CACHE.pop(channel_id, None)

# ---------------- Link remover ----------------
async def get_link_remover_status(channel_id: int) -> bool:
    doc = await get_channel_cached(channel_id)
    return bool(doc.get("link_remover", False))

async def set_link_remover_status(channel_id: int, status: bool):
    await chnl_ids.update_one({"chnl_id": channel_id}, {"$set": {"link_remover": bool(status)}}, upsert=True)
    _CHANNEL_CACHE.pop(channel_id, None)

# ---------------- Replace words ----------------
async def get_replace_words(channel_id: int) -> Optional[str]:
    doc = await get_channel_cached(channel_id)
    return doc.get("replace_words")

async def set_replace_words(channel_id: int, text: str):
    await chnl_ids.update_one({"chnl_id": channel_id}, {"$set": {"replace_words": text}}, upsert=True)
    _CHANNEL_CACHE.pop(channel_id, None)

async def delete_replace_words_db(channel_id: int):
    await chnl_ids.update_one({"chnl_id": channel_id}, {"$unset": {"replace_words": ""}})
    _CHANNEL_CACHE.pop(channel_id, None)

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

def invalidate_channel_cache(channel_id: int):
    _CHANNEL_CACHE.pop(channel_id, None)

# ---------------- Emoji remover ----------------
async def get_emoji_remover_status(channel_id: int) -> bool:
    doc = await get_channel_cached(channel_id)
    return bool(doc.get("emoji_remover", False))

async def set_emoji_remover_status(channel_id: int, status: bool):
    await chnl_ids.update_one({"chnl_id": channel_id}, {"$set": {"emoji_remover": bool(status)}}, upsert=True)
    _CHANNEL_CACHE.pop(channel_id, None)

# ---------------- URL Buttons ----------------
async def set_url_buttons(channel_id: int, buttons: list):
    await chnl_ids.update_one({"chnl_id": channel_id}, {"$set": {"url_buttons": buttons}}, upsert=True)
    _CHANNEL_CACHE.pop(channel_id, None)

async def get_url_buttons(channel_id: int) -> list:
    doc = await get_channel_cached(channel_id)
    return doc.get("url_buttons", [])

async def delete_url_buttons(channel_id: int):
    await chnl_ids.update_one({"chnl_id": channel_id}, {"$unset": {"url_buttons": ""}})
    _CHANNEL_CACHE.pop(channel_id, None)

# ---------------- Delete Channel (full wipe) ----------------
async def delete_all_channel_data(user_id: int, channel_id: int):
    """Remove channel from user's list and wipe ALL its settings from DB."""
    await users.update_one({"_id": user_id}, {"$pull": {"channels": {"channel_id": channel_id}}})
    await chnl_ids.delete_one({"chnl_id": channel_id})
    _CHANNEL_CACHE.pop(channel_id, None)
    _CHAT_TITLE_CACHE.pop(channel_id, None)
