import asyncio
import html
import logging
import re
import time
from datetime import datetime, timezone

from pyrogram import Client, filters
from pyrogram.errors import FloodWait
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from info import ADMIN, REVIEW_CH, REVIEW_PER_MIN, REVIEW_DEDUP_S
from body.database import db
from body.caption_history import history_col, UNSURE_FLAGS, MAX_TEXT
from body.corrections_store import parse_correction, save_correction

logger = logging.getLogger("captionbot.review")

reviews_col = db.caption_reviews

QUEUE_MAX = 2000
REVIEW_TTL_DAYS = 30
PROMPT_TTL_S = 3600
SEND_RETRIES = 3
RECENT_MAX = 5000
CARD_LIMIT = 3900

_queue = None
_prompts = {}
_recent = {}
_stats = {"queued": 0, "deduped": 0, "dropped": 0, "sent": 0, "errors": 0}

CB_RE = r"^rv_(ok|wr):(-?\d+):(\d+)$"


def _clip(value, n):
    value = str(value or "")
    return value if len(value) <= n else value[: n - 1] + "…"


def enqueue_review(job, meta):
    try:
        q = _queue
        if q is None or not REVIEW_CH:
            return
        meta = meta or {}
        if meta.get("corrected") or not meta.get("sig"):
            return
        flags = [f for f in (meta.get("flags") or []) if f in UNSURE_FLAGS]
        if not flags:
            return
        title = str((meta.get("title") or {}).get("value") or "").casefold()
        ident = title or str(job.get("original_file_name") or "")[:60].casefold()
        key = (job.get("chat_id"), ident, tuple(flags))
        now = time.monotonic()
        last = _recent.get(key)
        if last is not None and now - last < REVIEW_DEDUP_S:
            _stats["deduped"] += 1
            return
        if len(_recent) >= RECENT_MAX:
            for k in [k for k, t in _recent.items() if now - t >= REVIEW_DEDUP_S]:
                _recent.pop(k, None)
            if len(_recent) >= RECENT_MAX:
                _recent.clear()
        _recent[key] = now
        q.put_nowait({
            "cid": job.get("chat_id"),
            "mid": job.get("message_id"),
            "sig": meta["sig"],
            "filename": _clip(job.get("original_file_name"), 300),
            "caption": _clip(job.get("default_caption_plain"), 600),
            "bot_output": _clip(meta.get("smart_file_name"), 600),
            "flags": flags,
            "engine_version": meta.get("engine_version") or "",
        })
        _stats["queued"] += 1
    except asyncio.QueueFull:
        _stats["dropped"] += 1
    except Exception as e:
        _stats["errors"] += 1
        logger.debug("enqueue_review failed: %s", e)


def _post_link(cid, mid):
    s = str(cid)
    if s.startswith("-100") and mid:
        return "https://t.me/c/%s/%s" % (s[4:], mid)
    return ""


def build_card_text(item, with_file=True):
    lines = [
        "🔎 <b>Review needed</b> · <code>%s</code>" % html.escape(", ".join(item["flags"])),
        "<b>File:</b> <code>%s</code>" % html.escape(item["filename"] or "-"),
        "<b>Original caption:</b> %s" % html.escape(_clip(item["caption"], 300) or "-"),
        "<b>Bot caption:</b> <code>%s</code>" % html.escape(item["bot_output"] or "-"),
    ]
    if not with_file:
        lines.append("⚠️ The file could not be copied here.")
    link = _post_link(item["cid"], item["mid"])
    if link:
        lines.append('<a href="%s">Open post</a>' % link)
    return _clip("\n".join(lines), CARD_LIMIT)


def build_card_buttons(cid, mid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ OK", callback_data="rv_ok:%s:%s" % (cid, mid)),
        InlineKeyboardButton("❌ Wrong", callback_data="rv_wr:%s:%s" % (cid, mid)),
    ]])


async def _claim(item):
    doc = {
        "cid": item["cid"],
        "mid": item["mid"],
        "sig": item["sig"],
        "filename": item["filename"],
        "caption": item["caption"],
        "bot_output": item["bot_output"],
        "flags": item["flags"],
        "engine_version": item["engine_version"],
        "status": "pending",
        "ts": datetime.now(timezone.utc),
    }
    res = await reviews_col.update_one(
        {"cid": item["cid"], "mid": item["mid"]}, {"$setOnInsert": doc}, upsert=True
    )
    return res.upserted_id is not None


async def _with_retry(factory):
    for _ in range(SEND_RETRIES):
        try:
            return await factory()
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
    raise RuntimeError("flood wait retries exhausted")


async def _post_card(client, item):
    if not await _claim(item):
        return
    copied = None
    try:
        try:
            copied = await _with_retry(lambda: client.copy_message(REVIEW_CH, item["cid"], item["mid"]))
        except Exception as e:
            logger.warning("review file copy failed ch=%s msg=%s: %s", item["cid"], item["mid"], e)
        kwargs = {"reply_to_message_id": copied.id} if copied else {}
        sent = await _with_retry(lambda: client.send_message(
            REVIEW_CH,
            build_card_text(item, with_file=copied is not None),
            reply_markup=build_card_buttons(item["cid"], item["mid"]),
            disable_web_page_preview=True,
            **kwargs,
        ))
    except Exception:
        try:
            await reviews_col.delete_one({"cid": item["cid"], "mid": item["mid"]})
        except Exception:
            pass
        if copied:
            try:
                await client.delete_messages(REVIEW_CH, copied.id)
            except Exception:
                pass
        raise
    _stats["sent"] += 1
    try:
        await reviews_col.update_one(
            {"cid": item["cid"], "mid": item["mid"]},
            {"$set": {"card_chat": sent.chat.id, "card_id": sent.id, "file_id": copied.id if copied else None}},
        )
    except Exception:
        pass


async def _sender(client):
    gap = 60.0 / max(1, REVIEW_PER_MIN)
    while True:
        item = await _queue.get()
        try:
            await _post_card(client, item)
        except Exception as e:
            _stats["errors"] += 1
            logger.warning("review card failed ch=%s msg=%s: %s", item.get("cid"), item.get("mid"), e)
        await asyncio.sleep(gap)


async def _ensure_indexes():
    try:
        await reviews_col.create_index([("cid", 1), ("mid", 1)], unique=True, name="uniq_post")
        await reviews_col.create_index("ts", expireAfterSeconds=REVIEW_TTL_DAYS * 86400, name="ttl_ts")
        await reviews_col.create_index([("status", 1), ("ts", -1)], name="status_ts")
    except Exception as e:
        logger.warning("caption_reviews: index setup failed: %s", e)


def on_bot_start(client):
    global _queue
    if not REVIEW_CH:
        logger.info("caption review: disabled (REVIEW_CH not set)")
        return
    _queue = asyncio.Queue(maxsize=QUEUE_MAX)
    asyncio.create_task(_sender(client), name="caption_review_sender")
    asyncio.create_task(_ensure_indexes(), name="caption_review_indexes")
    logger.info("caption review: ready (channel %s, %d/min)", REVIEW_CH, REVIEW_PER_MIN)


async def pending_review_count():
    try:
        return await reviews_col.count_documents({"status": "pending"})
    except Exception:
        return 0


async def _finish_card(client, chat_id, card_id, footer):
    try:
        msg = await client.get_messages(chat_id, card_id)
        body = msg.text.html if msg and msg.text else ""
        await client.edit_message_text(chat_id, card_id, _clip(body + "\n\n" + footer, CARD_LIMIT),
                                       disable_web_page_preview=True)
    except Exception as e:
        logger.debug("finish card failed: %s", e)


@Client.on_callback_query(filters.regex(CB_RE) & ~filters.user(ADMIN))
async def review_denied_cb(client, query):
    await query.answer("Admins only.", show_alert=True)


@Client.on_callback_query(filters.regex(CB_RE) & filters.user(ADMIN))
async def review_button_cb(client, query):
    m = re.match(CB_RE, query.data)
    action, cid, mid = m.group(1), int(m.group(2)), int(m.group(3))
    who = html.escape(query.from_user.first_name or str(query.from_user.id))
    rv = await reviews_col.find_one({"cid": cid, "mid": mid})
    if not rv:
        await query.answer("This review expired.", show_alert=True)
        return
    if rv.get("status") in ("ok", "corrected"):
        await query.answer("Already reviewed.", show_alert=True)
        return
    if action == "ok":
        await reviews_col.update_one({"cid": cid, "mid": mid}, {"$set": {"status": "ok"}})
        await query.answer("Marked OK")
        await _finish_card(client, query.message.chat.id, query.message.id, "✅ Marked OK by %s" % who)
        return
    await _drop_prompts(client, cid, mid)
    prompt_text = (
        "✏️ <b>Reply to this message</b> with the correct caption.\n\n"
        "Bot caption: <code>%s</code>\n\n"
        "Send the full caption line, or fields like\n"
        "<code>audio=Hindi + English; sub=ESub</code>\n"
        "(fields: title, year, audio, sub). Reply <code>cancel</code> to stop."
    ) % html.escape(rv.get("bot_output") or "-")
    try:
        prompt = await client.send_message(
            query.message.chat.id, _clip(prompt_text, CARD_LIMIT), reply_to_message_id=query.message.id
        )
    except Exception as e:
        logger.warning("review prompt failed: %s", e)
        await query.answer("Could not post the prompt here.", show_alert=True)
        return
    _prompts[prompt.id] = {
        "cid": cid, "mid": mid, "sig": rv.get("sig"),
        "filename": rv.get("filename", ""), "caption": rv.get("caption", ""),
        "bot_output": rv.get("bot_output", ""), "engine_version": rv.get("engine_version", ""),
        "card_chat": query.message.chat.id, "card_id": query.message.id,
        "prompt_chat": prompt.chat.id, "prompt_id": prompt.id,
        "who": who, "ts": time.monotonic(),
    }
    await query.answer("Reply to the new message below the card.")


async def _drop_prompts(client, cid, mid):
    now = time.monotonic()
    for pid, p in list(_prompts.items()):
        if (p["cid"] == cid and p["mid"] == mid) or now - p["ts"] > PROMPT_TTL_S:
            _prompts.pop(pid, None)
            try:
                await client.delete_messages(p["prompt_chat"], p["prompt_id"])
            except Exception:
                pass


def _has_prompt(_, __, message):
    rid = getattr(message, "reply_to_message_id", None)
    p = _prompts.get(rid) if rid else None
    if not p or message.chat.id != p["prompt_chat"]:
        return False
    if time.monotonic() - p["ts"] > PROMPT_TTL_S:
        _prompts.pop(rid, None)
        return False
    user = getattr(message, "from_user", None)
    if user is not None and user.id not in ADMIN:
        return False
    return True


_prompt_filter = filters.create(_has_prompt)


async def _edit_prompt(client, p, text):
    try:
        await client.edit_message_text(p["prompt_chat"], p["prompt_id"], text)
    except Exception as e:
        logger.debug("edit prompt failed: %s", e)


async def _store_correction(client, message, p, kind, value):
    ok = await save_correction(
        p["sig"], kind, value,
        channel_id=p["cid"], message_id=p["mid"],
        filename=p["filename"], caption=p["caption"], bot_output=p["bot_output"],
        engine_version=p["engine_version"], via="review_button",
    )
    if not ok:
        await message.reply_text("❌ Could not save that right now. Reply again or reply <code>cancel</code>.")
        return
    _prompts.pop(p["prompt_id"], None)
    shown = value if kind == "name" else "; ".join("%s=%s" % (k, v) for k, v in value.items())
    try:
        await reviews_col.update_one({"cid": p["cid"], "mid": p["mid"]}, {"$set": {"status": "corrected"}})
        update = {"reported": True, "expected_output": shown[:MAX_TEXT], "reported_at": datetime.now(timezone.utc)}
        if kind == "fields":
            update["field_fixes"] = value
        await history_col.update_one({"channel_id": p["cid"], "message_id": p["mid"]}, {"$set": update})
    except Exception as e:
        logger.warning("review correction bookkeeping failed: %s", e)
    await _finish_card(client, p["card_chat"], p["card_id"],
                       "❌ Corrected by %s: <code>%s</code>" % (p["who"], html.escape(_clip(shown, 400))))
    await _edit_prompt(client, p, "✅ Saved. The next time this exact file name and caption arrive, the bot will use it. "
                                  "The post that is already edited is not changed.")


@Client.on_message(filters.chat(REVIEW_CH) & filters.text & filters.reply & _prompt_filter, group=-5)
async def review_correction_text(client, message):
    p = _prompts.get(message.reply_to_message_id)
    text = (message.text or "").strip()
    try:
        if p is None:
            pass
        elif text.lower() == "cancel":
            _prompts.pop(p["prompt_id"], None)
            await _edit_prompt(client, p, "Cancelled.")
        else:
            kind, value = parse_correction(text)
            if kind is None:
                await message.reply_text("❌ %s\nReply again, or reply <code>cancel</code>." % html.escape(value))
            else:
                await _store_correction(client, message, p, kind, value)
    except Exception as e:
        logger.warning("review correction failed: %s", e)
    message.stop_propagation()
