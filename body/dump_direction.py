import logging
import time
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from body.database import (
    chnl_ids,
    get_channel_cached,
    set_channel_title_cache,
    get_channel_title_cached,
    is_dump_skip,
    get_dump_destination,
    set_dump_destination,
    clear_dump_destination,
)
from info import ADMIN, CP_CH

logger = logging.getLogger("captionbot.dump_direction")

_ADMIN_FILTER = filters.user(ADMIN)

CHANNELS_PER_PAGE = 10

# ── Lightweight cache of "channels the bot is admin in" ──────────────────────
# Same idea as admin_channels.py's channel-list cache: avoids re-scanning
# chnl_ids on every Next/Back tap, so pagination feels instant.
_DEST_LIST_CACHE: dict = {"data": None, "ts": 0.0}
_DEST_LIST_TTL   = 30  # seconds


async def _get_all_registered_channels(force: bool = False) -> list:
    """
    Every channel the bot is registered as admin in — i.e. every doc in
    chnl_ids, which when_added_as_admin() (Caption.py) populates the
    moment the bot is made admin somewhere. This is the bot's own record
    of "channels I administer", built from its DB — deliberately NOT a
    live scan of all Telegram chats the bot account can see.
    """
    now = time.time()
    if not force and _DEST_LIST_CACHE["data"] is not None and (now - _DEST_LIST_CACHE["ts"]) < _DEST_LIST_TTL:
        return _DEST_LIST_CACHE["data"]

    result = []
    async for doc in chnl_ids.find({}, {"chnl_id": 1, "_title": 1}):
        cid = doc.get("chnl_id")
        if cid is None:
            continue
        result.append({
            "channel_id": cid,
            "channel_title": doc.get("_title") or str(cid),
        })
    result.sort(key=lambda c: c["channel_title"].casefold())

    _DEST_LIST_CACHE["data"] = result
    _DEST_LIST_CACHE["ts"]   = now
    return result


async def _resolve_title(client: Client, channel_id: int) -> str:
    """Cached title lookup with a live get_chat() fallback, same pattern
    used by admin_channels.py's channel-detail screen."""
    doc = await get_channel_cached(channel_id)
    title = doc.get("_title")
    if title:
        return title
    try:
        chat = await client.get_chat(channel_id)
        title = getattr(chat, "title", None) or str(channel_id)
        await set_channel_title_cache(channel_id, title)
    except Exception:
        title = str(channel_id)
    return title


# ── /dump_change command ──────────────────────────────────────────────────────

@Client.on_message(filters.private & _ADMIN_FILTER & filters.command("dump_change"))
async def dump_change_cmd(client: Client, message):
    if len(message.command) != 2:
        return await message.reply_text(
            "❌ <b>Usage:</b>\n<code>/dump_change -100xxxxxxxxxx</code>\n\n"
            "Pass the <b>source</b> channel whose edited-file dump copies "
            "you want to redirect to another channel.",
        )
    try:
        source_id = int(message.command[1])
    except ValueError:
        return await message.reply_text("❌ Invalid channel ID.")

    logger.info(f"[DUMP_CHANGE] admin={message.from_user.id} opened source={source_id}")
    try:
        await _render_dump_change(client, message, source_id, page=0, force_refresh=True)
    except Exception as e:
        logger.exception(f"[DUMP_CHANGE] render failed source={source_id}: {e}")
        try:
            await message.reply_text(f"❌ Failed to load dump settings:\n<code>{e}</code>")
        except Exception:
            pass


# ── Renderer (shared by the command and every callback below) ────────────────

async def _render_dump_change(client: Client, target, source_id: int, page: int = 0, force_refresh: bool = False):
    is_query = hasattr(target, "data")   # True → CallbackQuery

    src_title  = await _resolve_title(client, source_id)
    skip_state = await is_dump_skip(source_id)
    dest_id    = await get_dump_destination(source_id)

    if dest_id:
        dest_title = await _resolve_title(client, dest_id)
        dest_text  = f"{dest_title} (<code>{dest_id}</code>)"
    else:
        dest_text = "Default (CP_CH dump channel)"

    try:
        channels = await _get_all_registered_channels(force=force_refresh)
    except Exception as e:
        logger.exception(f"[DUMP_CHANGE] failed to fetch channel list: {e}")
        text = f"❌ <b>Failed to load channel list.</b>\n<code>{e}</code>"
        if is_query:
            await target.message.edit_text(text)
        else:
            await target.reply_text(text)
        return

    # A channel can't dump into itself.
    candidates = [c for c in channels if c["channel_id"] != source_id]

    total_pages = max(1, (len(candidates) + CHANNELS_PER_PAGE - 1) // CHANNELS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))
    start = page * CHANNELS_PER_PAGE
    page_channels = candidates[start:start + CHANNELS_PER_PAGE]

    kb = [[InlineKeyboardButton("♻️ Default (CP_CH)", callback_data=f"dchg_def_{source_id}")]]

    for ch in page_channels:
        kb.append([InlineKeyboardButton(
            f"📢 {ch['channel_title']}",
            callback_data=f"dchg_sel_{source_id}_{ch['channel_id']}",
        )])

    if not candidates:
        kb.append([InlineKeyboardButton("— No other channels available —", callback_data="noop")])
    elif len(candidates) > CHANNELS_PER_PAGE:
        nav_row = []
        if page > 0:
            nav_row.append(InlineKeyboardButton("⬅️", callback_data=f"dchg_pg_{source_id}_{page - 1}"))
        nav_row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="noop"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton("➡️", callback_data=f"dchg_pg_{source_id}_{page + 1}"))
        kb.append(nav_row)

    kb.append([InlineKeyboardButton("❌", callback_data="close_msg")])

    text = (
        "📡 <b>Dump Direction Settings</b>\n\n"
        f"📢 <b>Channel:</b> {src_title}\n"
        f"🆔 <b>Channel ID:</b> <code>{source_id}</code>\n"
        f"🗑 <b>Dump skip:</b> {'✅ Enabled' if skip_state else '❌ Disabled'}\n"
        f"📥 <b>Current dump destination:</b> {dest_text}\n\n"
        "Select a channel below to forward this channel's edited files "
        "there instead of the default dump channel, or tap "
        "<b>♻️ Default</b> to cancel any redirect."
    )

    if is_query:
        await target.message.edit_text(text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))


# ── Pagination ─────────────────────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^dchg_pg_(-?\d+)_(\d+)$") & _ADMIN_FILTER)
async def dchg_page_nav(client: Client, query):
    await query.answer()
    source_id = int(query.matches[0].group(1))
    page      = int(query.matches[0].group(2))
    try:
        await _render_dump_change(client, query, source_id, page=page)
    except Exception as e:
        logger.exception(f"[DUMP_CHANGE] pagination failed source={source_id} page={page}: {e}")
        await query.answer(f"❌ Failed to load page: {e}", show_alert=True)


# ── Select a destination channel ──────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^dchg_sel_(-?\d+)_(-?\d+)$") & _ADMIN_FILTER)
async def dchg_select(client: Client, query):
    source_id = int(query.matches[0].group(1))
    dest_id   = int(query.matches[0].group(2))
    try:
        await set_dump_destination(source_id, dest_id)
        logger.info(f"[DUMP_CHANGE] admin={query.from_user.id} source={source_id} -> dest={dest_id}")
        await query.answer("✅ Dump destination updated")
        await _render_dump_change(client, query, source_id, page=0)
    except Exception as e:
        logger.exception(f"[DUMP_CHANGE] select failed source={source_id} dest={dest_id}: {e}")
        await query.answer(f"❌ Failed to save: {e}", show_alert=True)


# ── Reset to default (CP_CH) ──────────────────────────────────────────────────

@Client.on_callback_query(filters.regex(r"^dchg_def_(-?\d+)$") & _ADMIN_FILTER)
async def dchg_default(client: Client, query):
    source_id = int(query.matches[0].group(1))
    try:
        await clear_dump_destination(source_id)
        logger.info(f"[DUMP_CHANGE] admin={query.from_user.id} source={source_id} reset to default")
        await query.answer("♻️ Reset to default dump channel")
        await _render_dump_change(client, query, source_id, page=0)
    except Exception as e:
        logger.exception(f"[DUMP_CHANGE] reset failed source={source_id}: {e}")
        await query.answer(f"❌ Failed to reset: {e}", show_alert=True)
