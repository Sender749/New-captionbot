import motor.motor_asyncio
import logging
import random
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

# ── Dump-origin tracking ──────────────────────────────────────────────────────
# Maps a message posted in CP_CH back to the channel (and message) it was
# originally copied from, so /id can answer "where did this dumped file
# come from" when replied to a forward of that CP_CH message.
#   { cp_ch_msg_id: int, origin_channel_id: int, origin_message_id: int, ts: float }
dump_origin_map = db.dump_origin_map


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


async def ensure_dump_origin_indexes():
    """
    Create indexes for the dump-origin tracking collection.

    This used to be keyed by cp_ch_msg_id alone (unique), which was safe
    only because every dump copy went to the single CP_CH chat and
    Telegram message ids are unique per-chat. Now that dump copies can be
    redirected to a different destination channel per source channel
    (see set_dump_destination), the same message id can legitimately show
    up in two different destination chats -- so the key must include the
    destination chat id too.

    Two migration hazards had to be handled explicitly, both of which
    would otherwise crash the bot on EVERY startup (index build throws ->
    uncaught -> Bot().run() dies -> supervisord respawns -> same crash
    forever):

    1. A leftover legacy single-field unique index on cp_ch_msg_id is
       dropped first.
    2. Any pre-existing documents from before this feature only have
       cp_ch_msg_id, not dest_chat_id/dest_msg_id. Building a UNIQUE
       index on the new fields while 2+ such documents exist fails
       immediately: Mongo treats a missing field as null, and a unique
       index only tolerates a single null pair. Those old documents are
       migrated (or dropped if unmigratable) to the new schema before
       the new index is built.

    The whole function is defensive on top of that: if anything here
    still goes wrong, it's logged and swallowed rather than allowed to
    take the entire bot down. Dump-origin tracking is a "nice to have"
    for /id lookups, not something worth crash-looping the bot over.
    """
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

        # Anything still missing the new fields (corrupt/unmigratable) would
        # still collide on the unique index -- it's disposable tracking
        # data, so just drop it instead of blocking startup over it.
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
        # Auto-expire mappings after 60 days so this collection doesn't grow forever.
        await dump_origin_map.create_index(
            [("ts", 1)],
            expireAfterSeconds=60 * 24 * 3600,
            background=True,
        )
    except Exception as e:
        # Never let index setup crash the whole bot -- worst case /id
        # lookups just run unindexed (slower, still correct) until this
        # is fixed manually.
        logger.error(f"[DUMP_ORIGIN] index creation failed, continuing without it: {e}")


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
    Fair-pick across ALL channels, with a cheap fast path for the common
    case and a guaranteed-fair fallback for the adversarial case.

    -- THE BUG THIS FIXES ------------------------------------------------
    The old version only ever looked at the globally-oldest
    _FETCH_SCAN_LIMIT (200) pending docs. That's fine when jobs are spread
    evenly, but if ONE channel has a big backlog (say 1500 files) queued
    before any other channel, every one of those 1500 jobs has an OLDER
    timestamp than a second channel's jobs queued afterwards. The moment
    that first channel hits a FloodWait cooldown, or is simply busy at its
    2-worker cap, the oldest-200 window is entirely made of ITS jobs --
    every one of them gets skipped, the scan finds nothing, and the
    second/third channel's jobs are never even looked at, even though they
    have free capacity. Effectively one busy/flood-waited channel could
    freeze every other channel's editing.

    -- THE FIX -------------------------------------------------------------
    1) FAST PATH (_fetch_from_oldest_window): unchanged behaviour --
       scan the oldest _FETCH_SCAN_LIMIT pending docs, skip
       cooling-down/at-cap channels, claim the first eligible one. This is
       cheap and handles the normal case (queue not dominated by one
       stuck channel) in a single indexed query, same as before.
    2) FALLBACK (_fetch_fair_across_channels): only runs when the fast
       path finds nothing eligible. It looks at every DISTINCT channel
       that currently has pending jobs, filters out channels that are
       cooling down or at their concurrency cap, and then -- for each
       remaining channel -- claims that channel's own oldest pending job
       (indexed via the (chat_id, ts) partial index, so it's cheap per
       channel). This guarantees no channel can be starved just because
       another channel has a much bigger backlog sitting in front of it.

    In the common case only step 1 ever runs. Step 2 only kicks in during
    exactly the scenario that used to cause starvation, and resolves it.
    """
    now = time.time()
    job = await _fetch_from_oldest_window(now)
    if job is not None:
        return job
    return await _fetch_fair_across_channels(now)


async def _fetch_from_oldest_window(now: float):
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
        if CHANNEL_COOLDOWN.get(ch, 0) <= now and CHANNEL_ACTIVE[ch] < DEFAULT_MAX_WORKERS
    ]
    if not eligible:
        return None

    # Shuffle so that across many polls / many workers, every eligible
    # channel gets an even shot at being picked first -- not always the
    # same channel-id ordering every time.
    random.shuffle(eligible)

    for ch in eligible:
        CHANNEL_ACTIVE[ch] += 1
        try:
            job = await queue_col.find_one_and_update(
                {"chat_id": ch, "status": "pending"},
                {"$set": {"status": "processing", "started": now}},
                sort=[("ts", 1)],
            )
        except Exception as e:
            logger.warning(f"[FAIR_FETCH] claim failed ch={ch}: {e}")
            job = None
        if job is not None:
            return job
        # Nothing claimable for this channel right now (another worker
        # beat us to its only remaining eligible doc, or a race with a
        # status change) -- release and try the next eligible channel.
        CHANNEL_ACTIVE[ch] = max(0, CHANNEL_ACTIVE[ch] - 1)
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
#  Dump-origin tracking helpers
# ════════════════════════════════════════════════════════
async def save_dump_origin(
    dest_chat_id: int, dest_msg_id: int, origin_channel_id: int, origin_message_id: int
):
    """
    Records that `dest_msg_id` inside `dest_chat_id` (the dump channel a
    file was actually copied to -- CP_CH by default, or an admin-selected
    custom destination) originally came from origin_channel_id /
    origin_message_id, so /id can answer "where did this come from".
    """
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


# ════════════════════════════════════════════════════════
#  Restart-recovery helpers
# ════════════════════════════════════════════════════════
async def reset_all_processing_to_pending():
    """
    Immediately requeues every in-flight ("processing") job in both the
    caption and forward queues back to "pending".

    Used by /restart so in-flight work is never abandoned waiting on
    recover_stuck_jobs()'s normal 5-minute stuck-timeout — right after the
    process comes back up, workers pick these jobs straight back up from
    where the bot left off, instead of starting over.

    Returns (caption_jobs_requeued, forward_jobs_requeued).
    """
    r1 = await queue_col.update_many(
        {"status": "processing"},
        {"$set": {"status": "pending", "ts": time.time()}},
    )
    r2 = await forward_queue.update_many(
        {"status": "processing"},
        {"$set": {"status": "pending", "ts": time.time()}},
    )
    return r1.modified_count, r2.modified_count


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
#  Per-channel dump DESTINATION helpers  ── /dump_change
#  (which chat an individual source channel's edited-file dump copies
#  get forwarded to; unset/None = default CP_CH)
# ════════════════════════════════════════════════════════
async def set_dump_destination(channel_id: int, dest_channel_id: int):
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$set": {"dump_dest": dest_channel_id}},
        upsert=True,
    )
    if channel_id in _CHANNEL_CACHE:
        _CHANNEL_CACHE[channel_id]["data"]["dump_dest"] = dest_channel_id


async def clear_dump_destination(channel_id: int):
    """Resets a channel back to the default CP_CH dump destination."""
    await chnl_ids.update_one(
        {"chnl_id": channel_id},
        {"$unset": {"dump_dest": ""}},
    )
    if channel_id in _CHANNEL_CACHE:
        _CHANNEL_CACHE[channel_id]["data"].pop("dump_dest", None)


async def get_dump_destination(channel_id: int) -> Optional[int]:
    """Returns the custom dump destination chat id for this channel, or
    None if it's still using the default CP_CH dump channel."""
    doc = await get_channel_cached(channel_id)
    return doc.get("dump_dest")


async def get_all_dump_destinations():
    cursor = chnl_ids.find({"dump_dest": {"$exists": True}})
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
