import asyncio
import logging
import os
import time
from datetime import datetime, timedelta, timezone

from pymongo import UpdateOne

from body.database import db, users, get_channel_title_cached

logger = logging.getLogger("captionbot.stats")

TZ_OFFSET_MIN = int(os.getenv("STATS_TZ_OFFSET_MIN", "330"))
LOCAL_TZ = timezone(timedelta(minutes=TZ_OFFSET_MIN))
FLUSH_EVERY_S = 3.0
DAY_MIN = "0000-00-00"
DAY_MAX = "9999-99-99"
FIELDS = ("edits", "dumps", "failed", "floodwaits")

stats_col = db.stats_daily

STARTED_AT = time.time()
_buffer = {}
_state = {"flushed": 0, "errors": 0}


def tz_label():
    sign = "+" if TZ_OFFSET_MIN >= 0 else "-"
    hours, minutes = divmod(abs(TZ_OFFSET_MIN), 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def day_key(ts=None):
    stamp = time.time() if ts is None else ts
    return datetime.fromtimestamp(stamp, LOCAL_TZ).strftime("%Y-%m-%d")


def parse_day(key):
    return datetime.strptime(key, "%Y-%m-%d")


def shift_day(key, delta):
    return (parse_day(key) + timedelta(days=delta)).strftime("%Y-%m-%d")


def _record(channel_id, field):
    try:
        slot = _buffer.setdefault((day_key(), int(channel_id)), {})
        slot[field] = slot.get(field, 0) + 1
    except Exception as e:
        logger.debug("stats record failed: %s", e)


def record_edit(channel_id):
    _record(channel_id, "edits")


def record_dump(channel_id):
    _record(channel_id, "dumps")


def record_failure(channel_id):
    _record(channel_id, "failed")


def record_floodwait(channel_id):
    _record(channel_id, "floodwaits")


def pending_buffer_size():
    return sum(sum(slot.values()) for slot in _buffer.values())


def tracker_state():
    return dict(_state)


async def _flush():
    global _buffer
    if not _buffer:
        return
    snapshot, _buffer = _buffer, {}
    ops = []
    for (day, channel_id), counts in snapshot.items():
        inc = {k: v for k, v in counts.items() if v}
        if not inc:
            continue
        ops.append(
            UpdateOne(
                {"day": day, "channel_id": channel_id},
                {"$inc": inc, "$setOnInsert": {"created": datetime.now(timezone.utc)}},
                upsert=True,
            )
        )
    if not ops:
        return
    try:
        await stats_col.bulk_write(ops, ordered=False)
        _state["flushed"] += len(ops)
    except Exception as e:
        _state["errors"] += 1
        logger.warning("stats flush of %d rows failed: %s", len(ops), e)
        for key, counts in snapshot.items():
            slot = _buffer.setdefault(key, {})
            for field, value in counts.items():
                slot[field] = slot.get(field, 0) + value


async def _flusher():
    while True:
        await asyncio.sleep(FLUSH_EVERY_S)
        try:
            await _flush()
        except Exception as e:
            _state["errors"] += 1
            logger.warning("stats flusher error: %s", e)


async def _ensure_indexes():
    try:
        await stats_col.create_index(
            [("day", 1), ("channel_id", 1)], unique=True, name="uniq_day_channel"
        )
        await stats_col.create_index([("channel_id", 1), ("day", 1)], name="channel_day")
    except Exception as e:
        logger.warning("stats index creation failed: %s", e)


def on_bot_start(client):
    asyncio.create_task(_flusher(), name="stats_flusher")
    asyncio.create_task(_ensure_indexes(), name="stats_indexes")
    logger.info("stats tracker ready (%s)", tz_label())


def range_for(period):
    today = day_key()
    if period == "today":
        return today, today
    if period == "yesterday":
        yesterday = shift_day(today, -1)
        return yesterday, yesterday
    if period == "7d":
        return shift_day(today, -6), today
    if period == "30d":
        return shift_day(today, -29), today
    return DAY_MIN, DAY_MAX


def previous_range(period):
    if period == "all":
        return None
    start, end = range_for(period)
    span = (parse_day(end) - parse_day(start)).days + 1
    return shift_day(start, -span), shift_day(start, -1)


async def first_day():
    doc = await stats_col.find_one({}, sort=[("day", 1)], projection={"day": 1})
    return doc["day"] if doc else None


async def channel_totals(start, end):
    pipeline = [
        {"$match": {"day": {"$gte": start, "$lte": end}}},
        {
            "$group": {
                "_id": "$channel_id",
                "edits": {"$sum": "$edits"},
                "dumps": {"$sum": "$dumps"},
                "failed": {"$sum": "$failed"},
                "floodwaits": {"$sum": "$floodwaits"},
                "days": {"$sum": 1},
            }
        },
        {"$sort": {"edits": -1, "dumps": -1}},
    ]
    rows = await stats_col.aggregate(pipeline).to_list(length=None)
    return [
        {
            "channel_id": r["_id"],
            "edits": r.get("edits", 0),
            "dumps": r.get("dumps", 0),
            "failed": r.get("failed", 0),
            "floodwaits": r.get("floodwaits", 0),
            "days": r.get("days", 0),
        }
        for r in rows
    ]


def summarize(rows):
    total = {f: sum(r[f] for r in rows) for f in FIELDS}
    total["channels"] = sum(1 for r in rows if r["edits"] > 0)
    return total


async def daily_series(start, end):
    pipeline = [
        {"$match": {"day": {"$gte": start, "$lte": end}}},
        {
            "$group": {
                "_id": "$day",
                "edits": {"$sum": "$edits"},
                "dumps": {"$sum": "$dumps"},
                "failed": {"$sum": "$failed"},
                "floodwaits": {"$sum": "$floodwaits"},
                "channels": {
                    "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$edits", 0]}, 0]}, 1, 0]}
                },
            }
        },
    ]
    rows = await stats_col.aggregate(pipeline).to_list(length=None)
    return {r["_id"]: r for r in rows}


async def owner_map(channel_ids):
    wanted = {int(c) for c in channel_ids}
    result = {}
    if not wanted:
        return result
    cursor = users.find({"channels.channel_id": {"$in": list(wanted)}}, {"channels": 1})
    async for doc in cursor:
        for ch in doc.get("channels", []):
            cid = ch.get("channel_id")
            if cid in wanted and cid not in result:
                result[cid] = (doc["_id"], ch.get("channel_title") or "")
    return result


async def channel_title(channel_id, owners=None):
    if owners and channel_id in owners and owners[channel_id][1]:
        return owners[channel_id][1]
    try:
        title = await get_channel_title_cached(channel_id)
    except Exception:
        title = ""
    return title or str(channel_id)
