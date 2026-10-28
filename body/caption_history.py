"""
caption_history.py -- keeps a record of every caption the bot edits so an
admin can export real failures of the smart-filename engine and share them.

Design rules (so this can never hurt the caption workers):
  * record_history() only builds a small dict and does queue.put_nowait().
  * a single background task bulk-writes the queue (every ~5 s or 200 docs).
  * every DB error is logged and swallowed.
  * rows are upserted on (channel_id, file_unique_id, engine_version) so a
    reprocessed file never creates duplicates.
  * text is capped at 4000 chars, no user ids are stored, and a TTL index
    removes old rows (HISTORY_TTL_DAYS, default 60).

Admin-only commands (private chat):
  /history_stats
  /history_export [days] [all|unsure|changed]
  /history_clear
  /wrong <correct output>      (reply to a processed / forwarded message)
  /original                    (reply to any file -> its caption BEFORE the bot edited it)
"""
import asyncio
import io
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from info import ADMIN, CP_CH
from body.database import db, get_dump_origin
from body.smart_text import ENGINE_VERSION

logger = logging.getLogger("captionbot.history")

HISTORY_TTL_DAYS = int(os.getenv("HISTORY_TTL_DAYS", "60"))
MAX_TEXT = 4000
FLUSH_EVERY_S = 5.0
FLUSH_BATCH = 200
QUEUE_MAX = 5000
EXPORT_MAX_ROWS = 5000
UNSURE_FLAGS = ["low_confidence", "conflict", "omitted_language"]

history_col = db.caption_history

_queue = None            # created in on_bot_start (needs the running loop)
_stats = {"queued": 0, "written": 0, "dropped": 0, "errors": 0}


# ───────────────────────────── building a record ────────────────────────────

def _cap(value):
    if value is None:
        return ""
    return str(value)[:MAX_TEXT]


def build_history_doc(job, meta, new_caption, smart_file_name=""):
    """Pure function: job + analysis metadata -> the document to store."""
    meta = meta or {}
    msg_id = job.get("message_id")
    uid = job.get("file_unique_id") or ("msg%s" % msg_id)
    flags = [f for f in (meta.get("flags") or []) if f in UNSURE_FLAGS]

    def fld(name):
        f = meta.get(name) or {}
        return {k: (_cap(v) if isinstance(v, str) else v) for k, v in f.items()}

    plain = _cap(job.get("default_caption_plain"))
    html = _cap(job.get("default_caption"))
    doc_html = {"original_caption_html": html} if html and html != plain else {}

    return {
        **doc_html,
        "channel_id": job.get("chat_id"),
        "message_id": msg_id,
        "file_unique_id": uid,
        "original_filename": _cap(job.get("original_file_name")),
        "original_caption": _cap(job.get("default_caption_plain")),
        "new_caption": _cap(new_caption),
        "smart_file_name": _cap(meta.get("smart_file_name") or smart_file_name),
        "parsed": {
            "title": fld("title"),
            "year": fld("year"),
            "audio": fld("audio"),
            "subtitle": fld("subtitle"),
        },
        "flags": flags,
        "engine_version": meta.get("engine_version") or ENGINE_VERSION,
        "ts": datetime.now(timezone.utc),
    }


def record_history(job, meta, new_caption, smart_file_name=""):
    """Non-blocking and exception-proof; safe to call from a worker."""
    try:
        q = _queue
        if q is None:
            return
        q.put_nowait(build_history_doc(job, meta, new_caption, smart_file_name))
        _stats["queued"] += 1
    except asyncio.QueueFull:
        _stats["dropped"] += 1
    except Exception as e:                     # never break the worker
        _stats["errors"] += 1
        logger.debug("record_history failed: %s", e)


# ───────────────────────────── background writer ────────────────────────────

async def _write_batch(batch):
    if not batch:
        return
    try:
        from pymongo import UpdateOne
        ops = []
        for d in batch:
            key = {
                "channel_id": d["channel_id"],
                "file_unique_id": d["file_unique_id"],
                "engine_version": d["engine_version"],
            }
            ops.append(UpdateOne(key, {"$set": d, "$setOnInsert": {"reported": False}}, upsert=True))
        await history_col.bulk_write(ops, ordered=False)
        _stats["written"] += len(batch)
    except Exception as e:
        _stats["errors"] += 1
        logger.warning("caption_history: bulk write of %d rows failed: %s", len(batch), e)


async def _flusher():
    while True:
        batch = []
        try:
            batch.append(await _queue.get())
            deadline = time.monotonic() + FLUSH_EVERY_S
            while len(batch) < FLUSH_BATCH:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                try:
                    batch.append(await asyncio.wait_for(_queue.get(), left))
                except asyncio.TimeoutError:
                    break
            await _write_batch(batch)
        except asyncio.CancelledError:
            while not _queue.empty():
                batch.append(_queue.get_nowait())
            await _write_batch(batch)
            raise
        except Exception as e:
            _stats["errors"] += 1
            logger.warning("caption_history flusher error: %s", e)
            await asyncio.sleep(1)


async def _ensure_indexes():
    try:
        await history_col.create_index(
            [("channel_id", 1), ("file_unique_id", 1), ("engine_version", 1)],
            unique=True, name="uniq_file_engine",
        )
        await history_col.create_index([("reported", 1), ("ts", -1)], name="reported_ts")
        ttl = HISTORY_TTL_DAYS * 86400
        try:
            await history_col.create_index("ts", expireAfterSeconds=ttl, name="ttl_ts")
        except Exception:
            # TTL value changed via env var -> update the existing index in place
            await db.command({"collMod": "caption_history",
                              "index": {"name": "ttl_ts", "expireAfterSeconds": ttl}})
    except Exception as e:
        logger.warning("caption_history: index setup failed: %s", e)


def on_bot_start(client):
    """Called by bot.py once the event loop is running."""
    global _queue
    _queue = asyncio.Queue(maxsize=QUEUE_MAX)
    asyncio.create_task(_flusher(), name="caption_history_flusher")
    asyncio.create_task(_ensure_indexes(), name="caption_history_indexes")
    logger.info("caption_history: ready (TTL %d days, engine %s)", HISTORY_TTL_DAYS, ENGINE_VERSION)


# ───────────────────────────── export formatting ────────────────────────────

def _iso(ts):
    return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)


def _desc(f, kind):
    if not f:
        return "-"
    if kind == "list":
        langs = ", ".join(f.get("langs") or []) or "-"
        extra = f.get("tag")
        txt = langs + (" [%s]" % extra if extra else "")
    else:
        txt = f.get("value") or "-"
    conf = "/".join(x for x in (f.get("conf"), f.get("source")) if x)
    return "%s (%s)" % (txt, conf) if conf else txt


def format_txt_block(i, d):
    p = d.get("parsed") or {}
    cap = (d.get("original_caption") or "").replace("\n", "\n         ")
    lines = [
        "#%d%s  ch=%s msg=%s  engine=%s  %s" % (
            i, "  [REPORTED]" if d.get("reported") else "", d.get("channel_id"),
            d.get("message_id"), d.get("engine_version"), _iso(d.get("ts"))),
        "FILE:    %s" % (d.get("original_filename") or "-"),
        "CAPTION: %s" % (cap or "-"),
        "OUTPUT:  %s" % (d.get("new_caption") or "-").replace("\n", "\n         "),
        "SMART:   %s" % (d.get("smart_file_name") or "-"),
        "PARSED:  title=%s | year=%s | audio=%s | sub=%s" % (
            _desc(p.get("title"), "v"), _desc(p.get("year"), "v"),
            _desc(p.get("audio"), "list"), _desc(p.get("subtitle"), "list")),
        "FLAGS:   %s" % (", ".join(d.get("flags") or []) or "-"),
    ]
    if d.get("expected_output"):
        lines.append("EXPECTED: %s" % d["expected_output"])
    return "\n".join(lines)


def export_files(docs):
    """-> (jsonl_bytes, txt_bytes)."""
    jl, blocks = [], []
    for i, d in enumerate(docs, 1):
        row = {k: v for k, v in d.items() if k != "_id"}
        row["ts"] = _iso(row.get("ts"))
        if row.get("reported_at"):
            row["reported_at"] = _iso(row["reported_at"])
        jl.append(json.dumps(row, ensure_ascii=False))
        blocks.append(format_txt_block(i, d))
    return ("\n".join(jl) + "\n").encode("utf-8"), ("\n\n----\n\n".join(blocks) + "\n").encode("utf-8")


def parse_export_args(args):
    """['7', 'unsure'] -> (7, 'unsure').  Defaults: 7 days, mode 'all'."""
    days, mode = 7, "all"
    for a in args:
        a = a.lower()
        if a.isdigit():
            days = max(1, min(int(a), 3650))
        elif a in ("all", "unsure", "changed"):
            mode = a
    return days, mode


async def _fetch_for_export(days, mode):
    since = datetime.now(timezone.utc) - timedelta(days=days)
    base = {"ts": {"$gte": since}}
    if mode == "unsure":
        base["$or"] = [{"flags": {"$in": UNSURE_FLAGS}}, {"reported": True}]
    elif mode == "changed":
        pipe = [
            {"$match": {"ts": {"$gte": since}}},
            {"$group": {"_id": {"c": "$channel_id", "f": "$file_unique_id"},
                        "outs": {"$addToSet": "$new_caption"}}},
            {"$match": {"outs.1": {"$exists": True}}},
            {"$limit": 2000},
        ]
        keys = [r["_id"] async for r in history_col.aggregate(pipe)]
        if not keys:
            return []
        base["$or"] = [{"channel_id": k["c"], "file_unique_id": k["f"]} for k in keys]
    reported = await history_col.find({**base, "reported": True}).sort("ts", -1).limit(EXPORT_MAX_ROWS).to_list(EXPORT_MAX_ROWS)
    room = EXPORT_MAX_ROWS - len(reported)
    rest = []
    if room > 0:
        rest = await history_col.find({**base, "reported": {"$ne": True}}).sort("ts", -1).limit(room).to_list(room)
    return reported + rest


# ───────────────────────────── admin commands ───────────────────────────────

_ADMIN_PM = filters.private & filters.user(ADMIN)


@Client.on_message(filters.command("history_stats") & _ADMIN_PM)
async def history_stats_cmd(client, message):
    try:
        total = await history_col.count_documents({})
        reported = await history_col.count_documents({"reported": True})
        parts = []
        for f in UNSURE_FLAGS:
            parts.append("  • %s: <code>%d</code>" % (f, await history_col.count_documents({"flags": f})))
        versions = await history_col.distinct("engine_version")
        newest = await history_col.find_one({}, sort=[("ts", -1)])
        oldest = await history_col.find_one({}, sort=[("ts", 1)])
        await message.reply_text(
            "🗂 <b>Caption history</b>\n"
            "  • Records: <code>%d</code>\n  • Reported (/wrong): <code>%d</code>\n%s\n"
            "  • Engine versions: <code>%s</code>\n"
            "  • Oldest: <code>%s</code>\n  • Newest: <code>%s</code>\n"
            "  • TTL: <code>%d days</code>\n"
            "  • Writer: queued <code>%d</code>, written <code>%d</code>, dropped <code>%d</code>, errors <code>%d</code>"
            % (total, reported, "\n".join(parts), ", ".join(versions) or "-",
               _iso(oldest["ts"]) if oldest else "-", _iso(newest["ts"]) if newest else "-",
               HISTORY_TTL_DAYS, _stats["queued"], _stats["written"], _stats["dropped"], _stats["errors"])
        )
    except Exception as e:
        await message.reply_text("❌ history_stats failed: <code>%s</code>" % e)


@Client.on_message(filters.command("history_export") & _ADMIN_PM)
async def history_export_cmd(client, message):
    days, mode = parse_export_args(message.command[1:])
    try:
        docs = await _fetch_for_export(days, mode)
        if not docs:
            await message.reply_text("📭 No records for the last %d day(s) in mode <code>%s</code>." % (days, mode))
            return
        jl, txt = export_files(docs)
        stamp = int(time.time())
        for data, name in ((txt, "history_%s_%dd_%d.txt" % (mode, days, stamp)),
                           (jl, "history_%s_%dd_%d.jsonl" % (mode, days, stamp))):
            bio = io.BytesIO(data)
            bio.name = name
            await message.reply_document(bio, caption="%d record(s) · last %dd · %s" % (len(docs), days, mode))
    except Exception as e:
        await message.reply_text("❌ history_export failed: <code>%s</code>" % e)


@Client.on_message(filters.command("history_clear") & _ADMIN_PM)
async def history_clear_cmd(client, message):
    n = await history_col.estimated_document_count()
    await message.reply_text(
        "⚠️ Delete <b>all %d</b> caption-history record(s)? This cannot be undone." % n,
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes, delete all", callback_data="hist_clear_yes"),
            InlineKeyboardButton("✖ Cancel", callback_data="hist_clear_no"),
        ]]),
    )


@Client.on_callback_query(filters.regex(r"^hist_clear_(yes|no)$") & filters.user(ADMIN))
async def history_clear_cb(client, query):
    if query.data.endswith("no"):
        await query.message.edit_text("Cancelled — nothing was deleted.")
        return
    try:
        res = await history_col.delete_many({})
        await query.message.edit_text("🗑 Deleted <code>%d</code> record(s)." % res.deleted_count)
    except Exception as e:
        await query.message.edit_text("❌ Failed: <code>%s</code>" % e)


async def _locate_origin(replied):
    """Find (channel_id, message_id) of the processed message a reply points at."""
    chat = getattr(replied, "forward_from_chat", None)
    mid = getattr(replied, "forward_from_message_id", None)
    if not (chat and mid):
        fo = getattr(replied, "forward_origin", None)
        chat = getattr(fo, "chat", None) or getattr(fo, "sender_chat", None)
        mid = getattr(fo, "message_id", None)
    if chat and mid:
        cid = chat.id
        origin = await get_dump_origin(cid, mid)        # a dump-channel copy?
        if origin.get("origin_channel_id"):
            return origin["origin_channel_id"], origin["origin_message_id"]
        return cid, mid
    txt = (getattr(replied, "text", None) or getattr(replied, "caption", None) or "")
    m = re.search(r"t\.me/c/(\d+)/(\d+)", txt)
    if m:
        return int("-100" + m.group(1)), int(m.group(2))
    m = re.search(r"(-100\d{6,})\s*[:/#]\s*(\d+)", txt)
    if m:
        return int(m.group(1)), int(m.group(2))
    return None, None


@Client.on_message(filters.command("wrong") & _ADMIN_PM)
async def wrong_cmd(client, message):
    expected = (message.text or "").split(None, 1)
    expected = expected[1].strip() if len(expected) > 1 else ""
    if not message.reply_to_message or not expected:
        await message.reply_text(
            "ℹ️ Reply to a <b>forwarded channel post</b> (or a message containing its t.me/c link) with\n"
            "<code>/wrong &lt;the correct output&gt;</code>"
        )
        return
    cid, mid = await _locate_origin(message.reply_to_message)
    if not cid:
        await message.reply_text("❌ I can't tell which channel post that is. Forward the post from the channel and reply to the forward.")
        return
    doc = await history_col.find_one({"channel_id": cid, "message_id": mid}, sort=[("ts", -1)])
    if not doc:
        await message.reply_text("❌ No history record for <code>%s</code> / <code>%s</code> (history keeps %d days)." % (cid, mid, HISTORY_TTL_DAYS))
        return
    await history_col.update_one(
        {"_id": doc["_id"]},
        {"$set": {"reported": True, "expected_output": expected[:MAX_TEXT],
                  "reported_at": datetime.now(timezone.utc)}},
    )
    await message.reply_text("✅ Saved. It will be listed first in <code>/history_export</code>.")


# ───────────────────────────── /original ────────────────────────────────────

def pick_original_text(doc):
    """-> (text, is_html).  Prefers the HTML form so links/bold survive."""
    html = (doc or {}).get("original_caption_html")
    if html:
        return html, True
    return (doc or {}).get("original_caption") or "", False


async def find_history_for_reply(replied):
    """Newest history record for the file/post a message points at, or None."""
    cid, mid = await _locate_origin(replied)        # exact channel post / dump copy first
    if cid and mid:
        doc = await history_col.find_one({"channel_id": cid, "message_id": mid}, sort=[("ts", -1)])
        if doc:
            return doc
    for attr in ("video", "document", "audio", "voice", "animation", "video_note"):
        media = getattr(replied, attr, None)
        uid = getattr(media, "file_unique_id", None) if media else None
        if uid:                                      # same file, whichever chat holds a copy
            doc = await history_col.find_one({"file_unique_id": uid}, sort=[("ts", -1)])
            if doc:
                return doc
    return None


@Client.on_message(filters.command("original") & _ADMIN_PM)
async def original_cmd(client, message):
    replied = message.reply_to_message
    if not replied:
        await message.reply_text("ℹ️ Reply to a file with <code>/original</code> to get its caption from before the bot edited it.")
        return
    try:
        doc = await find_history_for_reply(replied)
    except Exception as e:
        await message.reply_text("❌ Lookup failed: <code>%s</code>" % e)
        return
    if not doc:
        await message.reply_text(
            "❌ No record of that file. History only covers files edited after caption history "
            "was enabled, and is kept for %d days." % HISTORY_TTL_DAYS)
        return
    text, is_html = pick_original_text(doc)
    if not text.strip():
        await message.reply_text("ℹ️ That file had no caption before the edit.")
        return
    try:
        if is_html:
            await message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        else:
            await message.reply_text(text, parse_mode=ParseMode.DISABLED, disable_web_page_preview=True)
    except Exception:                                # e.g. HTML cut mid-tag -> plain text
        await message.reply_text(doc.get("original_caption") or text, parse_mode=ParseMode.DISABLED,
                                 disable_web_page_preview=True)
