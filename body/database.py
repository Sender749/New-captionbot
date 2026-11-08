import motor.motor_asyncio
import logging
import random
from info import *
from typing import Optional
import time
from collections import defaultdict

logger = logging.getLogger("captionbot.database")


CAPTION_WORKERS      = 10

FORWARD_WORKERS      = 4

_FETCH_SCAN_LIMIT    = 200

DEFAULT_MAX_WORKERS  = 2

MAX_FORWARD_PER_PAIR = 1

DEFAULT_EDIT_DELAY   = 0.5
FORWARD_DELAY        = 0.8

MEMBER_FORWARD_WORKERS      = 2
MAX_MEMBER_FORWARD_PER_PAIR = 1
MEMBER_FORWARD_DELAY        = 1.5

MAX_EDIT_DELAY       = 6.0
EASE_DOWN_AFTER      = 20
EASE_DOWN_STEP       = 0.1
BACKOFF_MULTIPLIER   = 1.8

CHANNEL_DELAY  = defaultdict(lambda: DEFAULT_EDIT_DELAY)
CHANNEL_STREAK = defaultdict(int)


def get_channel_delay(chat_id) -> float:
    return CHANNEL_DELAY[chat_id]


def note_edit_success(chat_id):
    CHANNEL_STREAK[chat_id] += 1
    if CHANNEL_STREAK[chat_id] >= EASE_DOWN_AFTER:
        CHANNEL_DELAY[chat_id] = max(
            DEFAULT_EDIT_DELAY, round(CHANNEL_DELAY[chat_id] - EASE_DOWN_STEP, 3)
        )
        CHANNEL_STREAK[chat_id] = 0


def note_edit_floodwait(chat_id):
    CHANNEL_DELAY[chat_id] = min(
        MAX_EDIT_DELAY, max(CHANNEL_DELAY[chat_id] * BACKOFF_MULTIPLIER, DEFAULT_EDIT_DELAY * 1.2)
    )
    CHANNEL_STREAK[chat_id] = 0


def get_channel_cap(chat_id) -> int:
    if CHANNEL_DELAY[chat_id] > DEFAULT_EDIT_DELAY:
        return 1
    return DEFAULT_MAX_WORKERS

CHANNEL_ACTIVE   = defaultdict(int)
CHANNEL_COOLDOWN = {}

_CHANNEL_CACHE = {}
CACHE_TTL = 120

_mongo_client = motor.motor_asyncio.AsyncIOMotorClient(
    MONGO_DB,
    serverSelectionTimeoutMS=10_000,
    connectTimeoutMS=10_000,
    socketTimeoutMS=30_000,
    maxPoolSize=20,
    minPoolSize=1,
)
db              = _mongo_client.captions_with_chnl
chnl_ids        = db.chnl_ids
users           = db.users
user_channels   = db.user_channels
queue_col       = db.caption_queue
forward_queue   = db.forward_queue

member_forward_queue = db.member_forward_queue

global_ff_progress = db.global_ff_progress

dump_origin_map = db.dump_origin_map


async def ensure_queue_indexes():
    await queue_col.create_index([("status", 1), ("ts", 1)])
    await queue_col.create_index([("chat_id", 1)])
    await queue_col.create_index([("chat_id", 1), ("message_id", 1)])
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


async def ensure_member_forward_indexes():
    await member_forward_queue.create_index([("status", 1), ("ts", 1)])
    await member_forward_queue.create_index([("src", 1)])
    await member_forward_queue.create_index([("dst", 1)])
    await member_forward_queue.create_index([("session_id", 1)])
    await member_forward_queue.create_index([("user_id", 1)])
    await member_forward_queue.create_index(
        [("src", 1), ("dst", 1), ("ts", 1)],
        partialFilterExpression={"status": "pending"},
        background=True,
    )


async def ensure_global_ff_indexes():
    await global_ff_progress.create_index(
        [("channel_id", 1)],
        unique=True,
        background=True,
    )


async def ensure_dump_origin_indexes():
    try:
        existing = await dump_origin_map.index_information()
        for name, spec in existing.items():
            if spec.get("key") == [("cp_ch_msg_id", 1)]:
                await dump_origin_map.drop_index(name)
                logger.info(f"[DUMP_ORIGIN] dropped legacy index {name}")
    except Exception as e:
        logger.warning(f"[DUMP_ORIGIN] legacy index cleanup skipped: {e}")

    try:
        migrated = 0
        cursor = dump_origin_map.find(
            {"cp_ch_msg_id": {"$exists": True}, "dest_msg_id": {"$exists": False}}
        )
        async for doc in cursor:
            await dump_origin_map.update_one(
                {"_id": doc["_id"]},
                {
                    "$set": {"dest_chat_id": CP_CH, "dest_msg_id": doc["cp_ch_msg_id"]},
                    "$unset": {"cp_ch_msg_id": ""},
                },
            )
            migrated += 1
        if migrated:
            logger.info(f"[DUMP_ORIGIN] migrated {migrated} legacy document(s) to the new schema")

        stale = await dump_origin_map.delete_many({"dest_msg_id": {"$exists": False}})
        if stale.deleted_count:
            logger.info(f"[DUMP_ORIGIN] dropped {stale.deleted_count} unmigratable legacy document(s)")
    except Exception as e:
        logger.warning(f"[DUMP_ORIGIN] legacy document migration skipped: {e}")

    try:
        await dump_origin_map.create_index(
            [("dest_chat_id", 1), ("dest_msg_id", 1)],
            unique=True,
            background=True,
        )
        await dump_origin_map.create_index(
            [("ts", 1)],
            expireAfterSeconds=60 * 24 * 3600,
            background=True,
        )
    except Exception as e:
        logger.error(f"[DUMP_ORIGIN] index creation failed, continuing without it: {e}")


async def enqueue_caption(job: dict):
    await queue_col.update_one(
        {
            "chat_id": job["chat_id"],
            "message_id": job["message_id"],
            "status": {"$in": ["pending", "processing"]},
        },
        {
            "$setOnInsert": {
                **{k: v for k, v in job.items() if k not in ("chat_id", "message_id")},
                "status": "pending",
                "retries": 0,
                "ts": time.time(),
            }
        },
        upsert=True,
    )


def _ready_filter(now: float) -> dict:
    return {
        "status": "pending",
        "$or": [{"retry_at": {"$exists": False}}, {"retry_at": {"$lte": now}}],
    }


async def fetch_channel_job():
    now = time.time()
    job = await _fetch_from_oldest_window(now)
    if job is not None:
        return job
    return await _fetch_fair_across_channels(now)


async def _fetch_from_oldest_window(now: float):
    cursor = queue_col.find(_ready_filter(now)).sort("ts", 1).limit(_FETCH_SCAN_LIMIT)
    async for job in cursor:
        ch = job["chat_id"]
        if CHANNEL_COOLDOWN.get(ch, 0) > now:
            continue
        if CHANNEL_ACTIVE[ch] >= get_channel_cap(ch):
            continue
        CHANNEL_ACTIVE[ch] += 1
        try:
            updated = await queue_col.find_one_and_update(
                {"_id": job["_id"], "status": "pending"},
                {"$set": {"status": "processing", "started": now}},
            )
        except Exception:
            CHANNEL_ACTIVE[ch] = max(0, CHANNEL_ACTIVE[ch] - 1)
            raise
        if updated is None:
            CHANNEL_ACTIVE[ch] = max(0, CHANNEL_ACTIVE[ch] - 1)
            continue
        return job
    return None


async def _fetch_fair_across_channels(now: float):
    try:
        channels = await queue_col.distinct("chat_id", {"status": "pending"})
    except Exception as e:
        logger.warning(f"[FAIR_FETCH] distinct(chat_id) failed: {e}")
        return None
    if not channels:
        return None

    eligible = [
        ch for ch in channels
        if CHANNEL_COOLDOWN.get(ch, 0) <= now and CHANNEL_ACTIVE[ch] < get_channel_cap(ch)
    ]
    if not eligible:
        return None

    random.shuffle(eligible)

    for ch in eligible:
        CHANNEL_ACTIVE[ch] += 1
        try:
            job = await queue_col.find_one_and_update(
                {"chat_id": ch, **_ready_filter(now)},
                {"$set": {"status": "processing", "started": now}},
                sort=[("ts", 1)],
            )
        except Exception as e:
            logger.warning(f"[FAIR_FETCH] claim failed ch={ch}: {e}")
            job = None
        if job is not None:
            return job
        CHANNEL_ACTIVE[ch] = max(0, CHANNEL_ACTIVE[ch] - 1)
    return None


async def mark_done(job_id):
    await queue_col.delete_one({"_id": job_id})


async def reschedule(job_id, delay=5):
    await queue_col.update_one(
        {"_id": job_id},
        {
            "$set": {"status": "pending", "retry_at": time.time() + delay},
            "$inc": {"retries": 1},
        },
    )


async def reschedule_floodwait(job_id):
    await queue_col.update_one(
        {"_id": job_id},
        {"$set": {"status": "pending"}, "$inc": {"floodwait_retries": 1}},
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
    r3 = await member_forward_queue.update_many(
        {"status": "processing", "started": {"$lt": cutoff}},
        {"$set": {"status": "pending"}},
    )
    if r3.modified_count:
        logger.info(f"[RECOVER] Reset {r3.modified_count} stuck member-forward job(s)")


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


async def enqueue_member_forward(job: dict):
    await member_forward_queue.insert_one({
        **job,
        "status":  "pending",
        "retries": 0,
        "ts":      time.time(),
    })


async def member_forward_done(job_id):
    await member_forward_queue.delete_one({"_id": job_id})


async def member_forward_retry(job_id, delay: float):
    await member_forward_queue.update_one(
        {"_id": job_id},
        {
            "$set": {"status": "pending", "ts": time.time() + delay},
            "$inc": {"retries": 1},
        },
    )


async def get_global_ff_progress(channel_id: int) -> dict:
    doc = await global_ff_progress.find_one({"channel_id": channel_id})
    return doc or {}


async def save_global_ff_progress(
    channel_id: int, last_msg_id: int, total_forwarded: int
):
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
    await global_ff_progress.delete_one({"channel_id": channel_id})


async def save_dump_origin(
    dest_chat_id: int, dest_msg_id: int, origin_channel_id: int, origin_message_id: int
):
    await dump_origin_map.update_one(
        {"dest_chat_id": dest_chat_id, "dest_msg_id": dest_msg_id},
        {
            "$set": {
                "origin_channel_id": origin_channel_id,
                "origin_message_id": origin_message_id,
                "ts": time.time(),
            }
        },
        upsert=True,
    )


async def get_dump_origin(dest_chat_id: int, dest_msg_id: int) -> dict:
    return await dump_origin_map.find_one(
        {"dest_chat_id": dest_chat_id, "dest_msg_id": dest_msg_id}
    ) or {}


async def reset_all_processing_to_pending():
    r1 = await queue_col.update_many(
        {"status": "processing"},
        {"$set": {"status": "pending", "ts": time.time()}},
    )
    r2 = await forward_queue.update_many(
        {"status": "processing"},
        {"$set": {"status": "pending", "ts": time.time()}},
    )
    return r1.modified_count, r2.modified_count


async def set_dump_skip(channel_id: int, status: bool):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$set": {"dump_skip": bool(status)}},
        upsert=True,
    )
    if channel_id in _CHANNEL_CACHE:
        _CHANNEL_CACHE[channel_id]["data"]["dump_skip"] = bool(status)


async def remove_dump_skip(channel_id: int):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$unset": {"dump_skip": ""}},
    )
    if channel_id in _CHANNEL_CACHE:
        _CHANNEL_CACHE[channel_id]["data"].pop("dump_skip", None)


async def is_dump_skip(channel_id: int) -> bool:
    doc = await get_channel_cached(channel_id)
    return bool(doc.get("dump_skip", False))


async def get_all_dump_skip_channels():
    cursor = chnl_ids.find({"dump_skip": True})
    return [doc async for doc in cursor]


async def set_dump_destination(channel_id: int, dest_channel_id: int):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$set": {"dump_dest": dest_channel_id}},
        upsert=True,
    )
    if channel_id in _CHANNEL_CACHE:
        _CHANNEL_CACHE[channel_id]["data"]["dump_dest"] = dest_channel_id


async def clear_dump_destination(channel_id: int):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$unset": {"dump_dest": ""}},
    )
    if channel_id in _CHANNEL_CACHE:
        _CHANNEL_CACHE[channel_id]["data"].pop("dump_dest", None)


async def get_dump_destination(channel_id: int) -> Optional[int]:
    doc = await get_channel_cached(channel_id)
    return doc.get("dump_dest")


async def get_all_dump_destinations():
    cursor = chnl_ids.find({"dump_dest": {"$exists": True}})
    return [doc async for doc in cursor]


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


async def get_channel_owner(channel_id: int) -> Optional[int]:
    doc = await users.find_one({"channels.channel_id": channel_id}, {"_id": 1})
    return doc["_id"] if doc else None


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
