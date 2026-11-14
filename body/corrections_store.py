import asyncio
import hashlib
import logging
import re
from datetime import datetime, timezone

from body.database import db

logger = logging.getLogger("captionbot.corrections")

corrections_col = db.caption_corrections

LOAD_LIMIT = 100000
LOAD_TIMEOUT_S = 25
MAX_NAME = 600
FIELD_KEYS = {"title": "title", "year": "year", "audio": "audio", "sub": "subtitle", "subtitle": "subtitle"}
FIELD_RE = re.compile(r"^\s*(title|year|audio|sub|subtitle)\s*=\s*(.*?)\s*$", re.I)
YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
AUDIO_SPLIT_RE = re.compile(r"\s*(?:\+|&|,|/)\s*")
FIELD_HELP = "Use field=value pairs only, for example: audio=Hindi + English; sub=ESub"

_OVERRIDES = {}


def make_sig(filename, caption):
    text = " ".join(str(filename or "").split()).casefold() + "\n" + " ".join(str(caption or "").split()).casefold()
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def parse_correction(text):
    text = (text or "").strip()
    if not text:
        return None, "Nothing to save."
    pieces = [p.strip() for p in re.split(r"[;\n]+", text) if p.strip()]
    if FIELD_RE.match(pieces[0]):
        fields = {}
        for piece in pieces:
            m = FIELD_RE.match(piece)
            if not m:
                return None, FIELD_HELP
            key = FIELD_KEYS[m.group(1).lower()]
            value = " ".join(m.group(2).split())
            if key == "subtitle" and value.lower() in ("none", "no", "-"):
                value = ""
            if not value and key != "subtitle":
                return None, "%s needs a value." % m.group(1).lower()
            if key == "year" and not YEAR_RE.match(value):
                return None, "year must look like 2019."
            fields[key] = value
        return "fields", fields
    name = " ".join(text.split())
    if len(name) > MAX_NAME:
        return None, "That is too long for a file name."
    return "name", name


def apply_fields(info, fields):
    if "title" in fields:
        info["title"] = fields["title"]
    if "year" in fields:
        info["year"] = fields["year"]
    if "audio" in fields:
        langs = [p for p in AUDIO_SPLIT_RE.split(fields["audio"]) if p]
        info["audio_langs"] = langs
        info["audio"] = " + ".join(langs)
    if "subtitle" in fields:
        info["subtitle"] = fields["subtitle"]
    return info


def get_correction(sig):
    return _OVERRIDES.get(sig) if sig else None


def _entry(doc):
    if doc.get("smart_name"):
        return {"smart_name": doc["smart_name"]}
    if doc.get("fields"):
        return {"fields": dict(doc["fields"])}
    return None


async def save_correction(sig, kind, value, channel_id=None, message_id=None,
                          filename="", caption="", bot_output="", engine_version="", via=""):
    if not sig or kind not in ("name", "fields"):
        return False
    doc = {
        "sig": sig,
        "smart_name": value if kind == "name" else "",
        "fields": value if kind == "fields" else {},
        "channel_id": channel_id,
        "message_id": message_id,
        "original_filename": str(filename or "")[:1000],
        "original_caption": str(caption or "")[:2000],
        "bot_output": str(bot_output or "")[:MAX_NAME],
        "engine_version": engine_version,
        "via": via,
        "ts": datetime.now(timezone.utc),
    }
    try:
        await corrections_col.update_one({"sig": sig}, {"$set": doc}, upsert=True)
    except Exception as e:
        logger.warning("save_correction failed: %s", e)
        return False
    entry = _entry(doc)
    if entry:
        _OVERRIDES[sig] = entry
    return True


async def count_corrections():
    try:
        return await corrections_col.count_documents({})
    except Exception:
        return len(_OVERRIDES)


async def _load():
    try:
        await corrections_col.create_index("sig", unique=True, name="uniq_sig")
    except Exception as e:
        logger.warning("caption_corrections: index setup failed: %s", e)
    loaded = {}
    cursor = corrections_col.find({}, {"sig": 1, "smart_name": 1, "fields": 1}).limit(LOAD_LIMIT)
    async for doc in cursor:
        entry = _entry(doc)
        if entry and doc.get("sig"):
            loaded[doc["sig"]] = entry
    _OVERRIDES.update(loaded)
    logger.info("caption_corrections: %d correction(s) loaded", len(loaded))


async def on_bot_start(client):
    try:
        await asyncio.wait_for(_load(), LOAD_TIMEOUT_S)
    except Exception as e:
        logger.warning("caption_corrections: load failed: %s", e)
