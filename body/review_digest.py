import asyncio
import html
import logging
import re
from collections import Counter, OrderedDict
from datetime import datetime, timedelta, timezone

from info import REVIEW_CH, REVIEW_DIGEST_AT
from body.database import db
from body.caption_history import history_col, UNSURE_FLAGS
from body.corrections_store import count_corrections
from body.review_flow import pending_review_count
from body.smart_text import ENGINE_VERSION

logger = logging.getLogger("captionbot.digest")

WINDOW_HOURS = 24
TOP_PATTERNS = 15
FETCH_LIMIT = 5000
CHUNK = 3800
PATTERN_LEN = 90

SEP_RE = re.compile(r"[._\-\[\]\(\)\{\}|]+")
SE_RE = re.compile(r"\bs\d{1,2}\s*e\d{1,3}\b", re.I)
RES_RE = re.compile(r"\b\d{3,4}p\b", re.I)
YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")
NUM_RE = re.compile(r"\d+")
SPACE_RE = re.compile(r"\s+")


def pattern_of(doc):
    name = doc.get("original_filename") or str(doc.get("original_caption") or "").split("\n")[0]
    s = SPACE_RE.sub(" ", SEP_RE.sub(" ", str(name))).strip()
    title = SPACE_RE.sub(" ", SEP_RE.sub(" ", str(((doc.get("parsed") or {}).get("title") or {}).get("value") or ""))).strip()
    if title:
        s = re.sub(re.escape(title), "<T>", s, flags=re.I)
    s = SE_RE.sub("<SE>", s)
    s = RES_RE.sub("<R>", s)
    s = YEAR_RE.sub("<Y>", s)
    s = NUM_RE.sub("#", s)
    return SPACE_RE.sub(" ", s).strip().casefold()[:PATTERN_LEN]


def group_patterns(docs):
    groups = OrderedDict()
    for d in docs:
        flags = tuple(sorted(f for f in (d.get("flags") or []) if f in UNSURE_FLAGS))
        fixes = tuple(sorted((d.get("field_fixes") or {}).keys()))
        key = (pattern_of(d), flags, fixes)
        g = groups.get(key)
        if g is None:
            groups[key] = {"count": 1, "example": d.get("original_filename") or "", "flags": flags, "fixes": fixes, "pattern": key[0]}
        else:
            g["count"] += 1
    return sorted(groups.values(), key=lambda g: g["count"], reverse=True)


def build_digest_text(docs, total, pending, corrections, now=None):
    now = now or datetime.now(timezone.utc)
    flag_counts = Counter(f for d in docs for f in (d.get("flags") or []) if f in UNSURE_FLAGS)
    reported = sum(1 for d in docs if d.get("reported"))
    fix_counts = Counter(k for d in docs for k in (d.get("field_fixes") or {}).keys())
    flagged = sum(1 for d in docs if d.get("flags"))
    lines = [
        "📊 <b>Caption digest</b> · %s UTC · engine %s" % (now.strftime("%Y-%m-%d %H:%M"), html.escape(ENGINE_VERSION)),
        "Edited (last %dh): <b>%d</b> · flagged: <b>%d</b> · reported wrong: <b>%d</b>" % (WINDOW_HOURS, total, flagged, reported),
    ]
    if flag_counts:
        lines.append("Flags: " + ", ".join("%s %d" % (k, v) for k, v in flag_counts.most_common()))
    if fix_counts:
        lines.append("Fields fixed: " + ", ".join("%s %d" % (k, v) for k, v in fix_counts.most_common()))
    lines.append("Awaiting review: <b>%d</b> · saved corrections (all time): <b>%d</b>" % (pending, corrections))
    patterns = group_patterns(docs)[:TOP_PATTERNS]
    if patterns:
        lines.append("")
        lines.append("<b>Top patterns</b>")
        for i, g in enumerate(patterns, 1):
            tags = ", ".join(list(g["flags"]) + ["fixed:" + f for f in g["fixes"]]) or "reported"
            lines.append("%d. <b>%d×</b> <code>%s</code>" % (i, g["count"], html.escape(tags)))
            lines.append("    <code>%s</code>" % html.escape(g["pattern"] or "-"))
            if g["example"]:
                lines.append("    e.g. <code>%s</code>" % html.escape(g["example"][:120]))
    return "\n".join(lines)


def split_chunks(text, size=CHUNK):
    out, cur = [], ""
    for line in text.split("\n"):
        if cur and len(cur) + len(line) + 1 > size:
            out.append(cur)
            cur = line
        else:
            cur = line if not cur else cur + "\n" + line
    if cur:
        out.append(cur)
    return out


def parse_time(value, default=(3, 30)):
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", str(value or ""))
    if not m:
        return default
    h, mi = int(m.group(1)), int(m.group(2))
    return (h, mi) if h < 24 and mi < 60 else default


def seconds_until(hour, minute, now=None):
    now = now or datetime.now(timezone.utc)
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def _fetch():
    since = datetime.now(timezone.utc) - timedelta(hours=WINDOW_HOURS)
    base = {"ts": {"$gte": since}}
    total = await history_col.count_documents(base)
    query = {**base, "$or": [{"flags.0": {"$exists": True}}, {"reported": True}]}
    proj = {"flags": 1, "original_filename": 1, "original_caption": 1, "parsed.title": 1, "reported": 1, "field_fixes": 1}
    docs = await history_col.find(query, proj).sort("ts", -1).limit(FETCH_LIMIT).to_list(FETCH_LIMIT)
    return total, docs


async def post_digest(client):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    meta = db.review_meta
    state = await meta.find_one({"_id": "digest"})
    if state and state.get("day") == today:
        return False
    total, docs = await _fetch()
    if not total:
        return False
    text = build_digest_text(docs, total, await pending_review_count(), await count_corrections())
    for chunk in split_chunks(text):
        await client.send_message(REVIEW_CH, chunk, disable_web_page_preview=True)
        await asyncio.sleep(1)
    await meta.update_one({"_id": "digest"}, {"$set": {"day": today}}, upsert=True)
    return True


async def _scheduler(client):
    hour, minute = parse_time(REVIEW_DIGEST_AT)
    while True:
        await asyncio.sleep(seconds_until(hour, minute))
        try:
            await post_digest(client)
        except Exception as e:
            logger.warning("digest failed: %s", e)
        await asyncio.sleep(61)


def on_bot_start(client):
    if not REVIEW_CH:
        logger.info("caption digest: disabled (REVIEW_CH not set)")
        return
    asyncio.create_task(_scheduler(client), name="caption_review_digest")
    logger.info("caption digest: scheduled daily at %s UTC", REVIEW_DIGEST_AT)
