import asyncio
import html
import logging
import time
from datetime import datetime

from pyrogram import Client, filters
from pyrogram.enums import ParseMode
from pyrogram.errors import MessageNotModified
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from info import ADMIN
from body.database import (
    CAPTION_WORKERS,
    DEFAULT_EDIT_DELAY,
    chnl_ids,
    forward_queue,
    member_forward_queue,
    queue_col,
    total_user,
    users,
)
from body import stats_tracker as st

logger = logging.getLogger("captionbot.stats_panel")

PERIOD_NAMES = {
    "today": "Today",
    "yesterday": "Yesterday",
    "7d": "Last 7 Days",
    "30d": "Last 30 Days",
    "all": "All Time",
}
PERIOD_SHORT = {
    "today": "Today",
    "yesterday": "Yesterday",
    "7d": "7 Days",
    "30d": "30 Days",
    "all": "All Time",
}
PREVIOUS_NAMES = {
    "today": "yesterday",
    "yesterday": "the day before",
    "7d": "the previous 7 days",
    "30d": "the previous 30 days",
}
COMMAND_ALIASES = {
    "today": "today",
    "yesterday": "yesterday",
    "7": "7d",
    "7d": "7d",
    "week": "7d",
    "30": "30d",
    "30d": "30d",
    "month": "30d",
    "all": "all",
}
LINE = "━━━━━━━━━━━━━━━━━━━━━━━━"
PAGE_SIZE = 8
MAX_TITLE = 38
ADMIN_CHECK_LIMIT = 25


def esc(value):
    return html.escape(str(value), quote=False)


def num(value):
    return f"{int(value):,}"


def short(text, limit=MAX_TITLE):
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def trend(current, previous):
    if previous is None:
        return ""
    if previous == 0:
        return "  🆕" if current > 0 else ""
    change = (current - previous) / previous * 100
    if abs(change) < 0.5:
        return "  ➖ 0%"
    arrow = "🔺" if change > 0 else "🔻"
    return f"  {arrow} {abs(change):.0f}%"


def channel_link(channel_id, title):
    cid = str(channel_id)
    internal = cid[4:] if cid.startswith("-100") else cid.lstrip("-")
    return f'<a href="https://t.me/c/{internal}/1">{esc(short(title))}</a>'


def user_link(user_id, label=None):
    return f'<a href="tg://user?id={user_id}">{esc(label if label is not None else user_id)}</a>'


def nice_date(key):
    return st.parse_day(key).strftime("%d %b %Y")


def period_caption(period, start, end):
    if period == "all":
        return PERIOD_NAMES[period]
    if start == end:
        return f"{PERIOD_NAMES[period]} — {nice_date(start)}"
    return f"{PERIOD_NAMES[period]} — {nice_date(start)} → {nice_date(end)}"


def human_duration(seconds):
    seconds = int(seconds)
    days, rem = divmod(seconds, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def now_stamp():
    return datetime.now(st.LOCAL_TZ).strftime("%d %b %Y, %H:%M:%S")


def callback(view, period, page=0):
    return f"st:{view}:{period}:{page}"


def build_keyboard(view, period, page=0, pages=1):
    rows = []
    if view in ("ch", "ow") and pages > 1:
        rows.append(
            [
                InlineKeyboardButton("⬅️ Prev", callback_data=callback(view, period, max(0, page - 1))),
                InlineKeyboardButton(f"{page + 1}/{pages}", callback_data="st:noop"),
                InlineKeyboardButton("Next ➡️", callback_data=callback(view, period, min(pages - 1, page + 1))),
            ]
        )
    if view in ("ov", "ch", "ow"):
        def pbtn(p):
            mark = "✅ " if p == period else ""
            return InlineKeyboardButton(mark + PERIOD_SHORT[p], callback_data=callback(view, p, 0))

        rows.append([pbtn("today"), pbtn("yesterday"), pbtn("7d")])
        rows.append([pbtn("30d"), pbtn("all")])
    elif view == "dy":
        rows.append(
            [
                InlineKeyboardButton(("✅ " if period != "30d" else "") + "7 Days", callback_data=callback("dy", "7d")),
                InlineKeyboardButton(("✅ " if period == "30d" else "") + "30 Days", callback_data=callback("dy", "30d")),
            ]
        )
    elif view == "sy":
        rows.append([InlineKeyboardButton("🛡 Admin Channels", callback_data=callback("ad", "all"))])
    nav_period = period if view in ("ov", "ch", "ow") else "today"
    rows.append(
        [
            InlineKeyboardButton("📊 Overview", callback_data=callback("ov", nav_period)),
            InlineKeyboardButton("📡 Channels", callback_data=callback("ch", nav_period)),
            InlineKeyboardButton("👤 Owners", callback_data=callback("ow", nav_period)),
        ]
    )
    rows.append(
        [
            InlineKeyboardButton("📈 Daily", callback_data=callback("dy", "7d")),
            InlineKeyboardButton("⚙️ System", callback_data=callback("sy", "all")),
            InlineKeyboardButton("🔄 Refresh", callback_data=callback(view, period, page)),
        ]
    )
    return InlineKeyboardMarkup(rows)


async def header(client, title, subtitle):
    me = await client.get_me()
    return (
        f"📊 <b>{title}</b>\n"
        f"🤖 <b>{esc(me.first_name)}</b> (@{esc(me.username)})\n"
        f"🗓 <b>{esc(subtitle)}</b>\n"
        f"{LINE}\n\n"
    )


def footer():
    return f"\n{LINE}\n🕒 <i>Updated {now_stamp()} · {st.tz_label()}</i>"


async def user_names(client, user_ids):
    names = {}
    ids = [u for u in user_ids if u]
    if not ids:
        return names
    try:
        for u in await client.get_users(ids):
            names[u.id] = u.first_name or str(u.id)
        return names
    except Exception:
        pass
    for uid in ids:
        try:
            u = await client.get_users(uid)
            names[uid] = u.first_name or str(uid)
        except Exception:
            continue
    return names


async def render_overview(client, period):
    start, end = st.range_for(period)
    rows = await st.channel_totals(start, end)
    cur = st.summarize(rows)
    prev_range = st.previous_range(period)
    prev = None
    if prev_range:
        prev = st.summarize(await st.channel_totals(*prev_range))

    def p(field):
        return prev[field] if prev else None

    avg = cur["edits"] / cur["channels"] if cur["channels"] else 0
    dump_rate = (cur["dumps"] / cur["edits"] * 100) if cur["edits"] else 0

    text = await header(client, "BOT STATISTICS", period_caption(period, start, end))
    text += (
        "📝 <b>CAPTION REPORT</b>\n"
        f"  • Files edited      : <code>{num(cur['edits'])}</code>{trend(cur['edits'], p('edits'))}\n"
        f"  • Sent to dump      : <code>{num(cur['dumps'])}</code>{trend(cur['dumps'], p('dumps'))}\n"
        f"  • Dump rate         : <code>{dump_rate:.1f}%</code>\n"
        f"  • Failed / dropped  : <code>{num(cur['failed'])}</code>{trend(cur['failed'], p('failed'))}\n"
        f"  • FloodWaits        : <code>{num(cur['floodwaits'])}</code>{trend(cur['floodwaits'], p('floodwaits'))}\n"
        f"  • Active channels   : <code>{num(cur['channels'])}</code>{trend(cur['channels'], p('channels'))}\n"
        f"  • Avg / channel     : <code>{avg:.1f}</code>\n\n"
    )

    top = [r for r in rows if r["edits"] > 0][:5]
    text += "🏆 <b>TOP CHANNELS</b>\n"
    if top:
        owners = await st.owner_map([r["channel_id"] for r in top])
        for i, r in enumerate(top, 1):
            title = await st.channel_title(r["channel_id"], owners)
            owner = owners.get(r["channel_id"])
            owner_txt = f" · 👤 {user_link(owner[0])}" if owner else ""
            text += (
                f"  {i}. {channel_link(r['channel_id'], title)}\n"
                f"      📝 <code>{num(r['edits'])}</code> · 📤 <code>{num(r['dumps'])}</code>{owner_txt}\n"
            )
    else:
        text += "  No edits recorded for this period.\n"

    if prev_range:
        text += f"\n<i>🔺🔻 compared with {PREVIOUS_NAMES[period]}</i>\n"
    else:
        since = await st.first_day()
        text += f"\n<i>📌 Tracking since {nice_date(since) if since else 'today'}</i>\n"

    users_count = await total_user()
    channel_docs = await chnl_ids.count_documents({})
    dump_skip = await chnl_ids.count_documents({"dump_skip": True})
    text += (
        f"\n👥 <b>BOT TOTALS</b>\n"
        f"  • Total users     : <code>{num(users_count)}</code>\n"
        f"  • Total channels  : <code>{num(channel_docs)}</code>\n"
        f"  • Dump-skip chans : <code>{num(dump_skip)}</code>\n"
    )
    text += footer()
    return text, build_keyboard("ov", period)


async def render_channels(client, period, page):
    start, end = st.range_for(period)
    rows = [r for r in await st.channel_totals(start, end) if r["edits"] > 0 or r["dumps"] > 0]
    pages = max(1, (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(0, page), pages - 1)
    chunk = rows[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    text = await header(client, "CHANNEL REPORT", period_caption(period, start, end))
    summary = st.summarize(rows)
    text += (
        f"📡 Channels: <code>{num(len(rows))}</code> · "
        f"📝 <code>{num(summary['edits'])}</code> · 📤 <code>{num(summary['dumps'])}</code>\n\n"
    )
    if not chunk:
        text += "No channel activity recorded for this period.\n"
    else:
        owners = await st.owner_map([r["channel_id"] for r in chunk])
        for i, r in enumerate(chunk, page * PAGE_SIZE + 1):
            title = await st.channel_title(r["channel_id"], owners)
            owner = owners.get(r["channel_id"])
            owner_txt = user_link(owner[0]) if owner else "unknown"
            text += (
                f"<b>{i}.</b> {channel_link(r['channel_id'], title)}\n"
                f"    🆔 <code>{r['channel_id']}</code>\n"
                f"    👤 Added by: {owner_txt} · <code>{owner[0] if owner else '-'}</code>\n"
                f"    📝 <code>{num(r['edits'])}</code> edited · 📤 <code>{num(r['dumps'])}</code> dumped"
                f" · ❌ <code>{num(r['failed'])}</code>\n\n"
            )
    text += footer()
    return text, build_keyboard("ch", period, page, pages)


async def render_owners(client, period, page):
    start, end = st.range_for(period)
    rows = [r for r in await st.channel_totals(start, end) if r["edits"] > 0 or r["dumps"] > 0]
    owners = await st.owner_map([r["channel_id"] for r in rows])
    grouped = {}
    for r in rows:
        owner = owners.get(r["channel_id"])
        key = owner[0] if owner else None
        g = grouped.setdefault(key, {"owner": key, "edits": 0, "dumps": 0, "failed": 0, "channels": 0})
        g["edits"] += r["edits"]
        g["dumps"] += r["dumps"]
        g["failed"] += r["failed"]
        g["channels"] += 1
    ranked = sorted(grouped.values(), key=lambda g: (g["edits"], g["dumps"]), reverse=True)
    pages = max(1, (len(ranked) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(0, page), pages - 1)
    chunk = ranked[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]
    names = await user_names(client, [g["owner"] for g in chunk])

    text = await header(client, "OWNER REPORT", period_caption(period, start, end))
    text += f"👥 Active owners: <code>{num(len(ranked))}</code>\n\n"
    if not chunk:
        text += "No owner activity recorded for this period.\n"
    for i, g in enumerate(chunk, page * PAGE_SIZE + 1):
        if g["owner"] is None:
            who = "❓ Unknown owner"
            uid_txt = "-"
        else:
            who = "👤 " + user_link(g["owner"], names.get(g["owner"], g["owner"]))
            uid_txt = f"{g['owner']}"
        text += (
            f"<b>{i}.</b> {who}\n"
            f"    🆔 <code>{uid_txt}</code> · 📡 <code>{num(g['channels'])}</code> channel(s)\n"
            f"    📝 <code>{num(g['edits'])}</code> edited · 📤 <code>{num(g['dumps'])}</code> dumped"
            f" · ❌ <code>{num(g['failed'])}</code>\n\n"
        )
    text += footer()
    return text, build_keyboard("ow", period, page, pages)


async def render_daily(client, period):
    span = 30 if period == "30d" else 7
    today = st.day_key()
    start = st.shift_day(today, -(span - 1))
    series = await st.daily_series(start, today)
    days = [st.shift_day(start, i) for i in range(span)]

    lines = [f"{'Date':<7}{'Edit':>7}{'Dump':>7}{'Fail':>6}{'Ch':>5}"]
    total_edits = total_dumps = total_failed = 0
    best = None
    for d in reversed(days):
        r = series.get(d, {})
        edits, dumps = r.get("edits", 0), r.get("dumps", 0)
        failed, chans = r.get("failed", 0), r.get("channels", 0)
        total_edits += edits
        total_dumps += dumps
        total_failed += failed
        if best is None or edits > best[1]:
            best = (d, edits)
        label = st.parse_day(d).strftime("%d %b")
        lines.append(f"{label:<7}{edits:>7}{dumps:>7}{failed:>6}{chans:>5}")
    lines.append("-" * 32)
    lines.append(f"{'Total':<7}{total_edits:>7}{total_dumps:>7}{total_failed:>6}")

    text = await header(client, "DAILY REPORT", f"Last {span} days — {nice_date(start)} → {nice_date(today)}")
    text += "<pre>" + esc("\n".join(lines)) + "</pre>\n"
    text += (
        f"📊 Avg / day: <code>{total_edits / span:.1f}</code> edits · "
        f"<code>{total_dumps / span:.1f}</code> dumps\n"
    )
    if best and best[1] > 0:
        text += f"🏅 Best day: <b>{nice_date(best[0])}</b> — <code>{num(best[1])}</code> edits\n"
    text += footer()
    return text, build_keyboard("dy", "30d" if span == 30 else "7d")


async def render_system(client):
    cap_pending = await queue_col.count_documents({"status": "pending"})
    cap_processing = await queue_col.count_documents({"status": "processing"})
    fwd_pending = await forward_queue.count_documents({"status": "pending"})
    fwd_processing = await forward_queue.count_documents({"status": "processing"})
    mf_pending = await member_forward_queue.count_documents({"status": "pending"})
    mf_processing = await member_forward_queue.count_documents({"status": "processing"})
    users_count = await total_user()
    channel_docs = await chnl_ids.count_documents({})
    dump_skip = await chnl_ids.count_documents({"dump_skip": True})
    owners_count = await users.count_documents({"channels.0": {"$exists": True}})
    since = await st.first_day()
    state = st.tracker_state()

    text = await header(client, "SYSTEM STATUS", "Live")
    text += (
        "📝 <b>CAPTION QUEUE</b>\n"
        f"  • Pending    : <code>{num(cap_pending)}</code>\n"
        f"  • Processing : <code>{num(cap_processing)}</code>\n\n"
        "📦 <b>FORWARD QUEUE</b>\n"
        f"  • Pending    : <code>{num(fwd_pending)}</code>\n"
        f"  • Processing : <code>{num(fwd_processing)}</code>\n\n"
        "📥 <b>MEMBER FORWARD QUEUE</b>\n"
        f"  • Pending    : <code>{num(mf_pending)}</code>\n"
        f"  • Processing : <code>{num(mf_processing)}</code>\n\n"
        "👥 <b>USERS &amp; CHANNELS</b>\n"
        f"  • Total users        : <code>{num(users_count)}</code>\n"
        f"  • Users with channels: <code>{num(owners_count)}</code>\n"
        f"  • DB channel docs    : <code>{num(channel_docs)}</code>\n"
        f"  • Dump-skip channels : <code>{num(dump_skip)}</code>\n\n"
        "🛠 <b>ENGINE</b>\n"
        f"  • Caption workers : <code>{CAPTION_WORKERS}</code>\n"
        f"  • Base edit delay : <code>{DEFAULT_EDIT_DELAY}s</code>\n"
        f"  • Uptime          : <code>{human_duration(time.time() - st.STARTED_AT)}</code>\n"
        f"  • Stats tracking  : <code>{nice_date(since) if since else 'not started'}</code>\n"
        f"  • Stats unsaved   : <code>{num(st.pending_buffer_size())}</code>"
        f" · write errors <code>{state['errors']}</code>\n"
    )
    text += footer()
    return text, build_keyboard("sy", "all")


async def _check_admin(client, channel_id, title, sem):
    async with sem:
        try:
            member = await client.get_chat_member(channel_id, "me")
            status = getattr(member, "status", "")
            status = str(getattr(status, "value", status)).lower()
            if status not in ("administrator", "creator", "owner"):
                return None
            try:
                chat = await client.get_chat(channel_id)
                link = f"https://t.me/{chat.username}" if getattr(chat, "username", None) else None
                return (chat.title or title, channel_id, link)
            except Exception:
                return (title, channel_id, None)
        except Exception:
            return None


async def render_admin_channels(client):
    docs = await users.aggregate(
        [
            {"$unwind": "$channels"},
            {"$group": {"_id": "$channels.channel_id", "title": {"$first": "$channels.channel_title"}}},
        ]
    ).to_list(length=None)
    sem = asyncio.Semaphore(5)
    results = await asyncio.gather(
        *[_check_admin(client, d["_id"], d.get("title") or str(d["_id"]), sem) for d in docs[:ADMIN_CHECK_LIMIT]]
    )
    found = [r for r in results if r]

    text = await header(client, "ADMIN CHANNELS", f"Checked {min(len(docs), ADMIN_CHECK_LIMIT)} of {len(docs)} channels")
    text += f"🛡 Bot is admin in: <code>{num(len(found))}</code> channel(s)\n\n"
    for i, (title, cid, link) in enumerate(found, 1):
        name = f'<a href="{link}">{esc(short(title))}</a>' if link else esc(short(title))
        text += f"  {i}. {name} <code>({cid})</code>\n"
    if not found:
        text += "None found.\n"
    text += footer()
    return text, build_keyboard("sy", "all")


async def render(client, view, period, page):
    if view == "ch":
        return await render_channels(client, period, page)
    if view == "ow":
        return await render_owners(client, period, page)
    if view == "dy":
        return await render_daily(client, period)
    if view == "sy":
        return await render_system(client)
    if view == "ad":
        return await render_admin_channels(client)
    return await render_overview(client, period)


@Client.on_message(filters.private & filters.user(ADMIN) & filters.command("stats"))
async def stats_command(client, message):
    period = "today"
    if len(message.command) > 1:
        period = COMMAND_ALIASES.get(message.command[1].lower(), "today")
    loading = await message.reply_text("📊 Building report…")
    try:
        text, kb = await render(client, "ov", period, 0)
        await loading.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)
    except Exception as e:
        logger.exception("stats command failed")
        await loading.edit_text(f"❌ Error fetching stats:\n<code>{esc(e)}</code>", parse_mode=ParseMode.HTML)


@Client.on_callback_query(filters.regex(r"^st:noop$"))
async def stats_noop(client, query):
    await query.answer()


@Client.on_callback_query(filters.regex(r"^st:(ov|ch|ow|dy|sy|ad):(today|yesterday|7d|30d|all):(\d+)$") & filters.user(ADMIN))
async def stats_callback(client, query):
    view, period, page = query.matches[0].group(1), query.matches[0].group(2), int(query.matches[0].group(3))
    try:
        await query.answer("Loading…" if view == "ad" else None)
    except Exception:
        pass
    try:
        text, kb = await render(client, view, period, page)
        await query.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=kb, disable_web_page_preview=True)
    except MessageNotModified:
        pass
    except Exception as e:
        logger.exception("stats callback failed")
        try:
            await query.answer(f"Error: {str(e)[:150]}", show_alert=True)
        except Exception:
            pass
