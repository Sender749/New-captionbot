import sys, time, os, re, asyncio
from typing import Tuple, List, Optional
from pyrogram import Client, filters, errors, enums
from pyrogram.types import (
    InlineKeyboardButton, InlineKeyboardMarkup,
    ChatMemberUpdated, CallbackQuery,
)
from pyrogram.errors import ChatAdminRequired, RPCError, FloodWait
from pyrogram.enums import ParseMode
from info import *
from Script import script
from body.database import *
from body.state import (
    _USER_CAPTION_TASKS, _USER_FORWARD_TASKS,
    FF_SESSIONS, CANCELLED_SESSIONS,
)
from collections import defaultdict
from imdb import IMDb

ia = IMDb()
DEFAULT_EDIT_DELAY = 0.5

bot_data = {
    "caption_set":       {},
    "block_words_set":   {},
    "suffix_set":        {},
    "prefix_set":        {},
    "replace_words_set": {},
    "url_set":           {},
}

# ─────────────────────────────────────────────
#  Per-user caption worker management
# ─────────────────────────────────────────────
def _ensure_user_caption_worker(client: Client, user_id: int):
    task = _USER_CAPTION_TASKS.get(user_id)
    if task is None or task.done():
        task = asyncio.create_task(_user_caption_worker(client, user_id))
        _USER_CAPTION_TASKS[user_id] = task


async def _user_caption_worker(client: Client, user_id: int):
    """Dedicated worker draining ONE user's caption queue, then exits."""
    idle = 0
    while True:
        job = await fetch_caption_job_for_user(user_id)
        if not job:
            idle += 1
            if idle >= 6:
                _USER_CAPTION_TASKS.pop(user_id, None)
                return
            await asyncio.sleep(0.5)
            continue
        idle = 0
        await _process_caption_job(client, job)


async def global_caption_worker(client: Client):
    """Small fallback pool — catches orphan jobs. Runs forever."""
    while True:
        job = await fetch_any_caption_job()
        if not job:
            await asyncio.sleep(1)
            continue
        await _process_caption_job(client, job)


async def _process_caption_job(client: Client, job: dict):
    ch = job["chat_id"]
    try:
        url_buttons = job.get("url_buttons", [])
        markup = (
            InlineKeyboardMarkup([
                [InlineKeyboardButton(btn["text"], url=btn["url"]) for btn in row]
                for row in url_buttons
            ]) if url_buttons else None
        )
        await client.edit_message_caption(
            chat_id=ch, message_id=job["message_id"],
            caption=job["caption"], parse_mode=ParseMode.HTML,
            reply_markup=markup,
        )
        if not await is_dump_skip(ch):
            try:
                orig = await client.get_messages(ch, job["message_id"])
                fname = None
                for t in ("document", "video", "audio", "voice"):
                    obj = getattr(orig, t, None)
                    if obj:
                        fname = getattr(obj, "file_name", None)
                        break
                fname = remove_emojis(clean_text(fname or "File"))
                await client.copy_message(
                    chat_id=CP_CH, from_chat_id=ch,
                    message_id=job["message_id"], caption=fname,
                )
            except Exception:
                pass
        await mark_done(job["_id"])
        await asyncio.sleep(DEFAULT_EDIT_DELAY)
    except FloodWait as e:
        wait = e.value + 2
        CHANNEL_COOLDOWN[ch] = time.time() + wait
        await reschedule(job["_id"], delay=wait)
    except errors.MessageNotModified:
        await mark_done(job["_id"])
    except Exception:
        if job.get("retries", 0) >= 5:
            await mark_done(job["_id"])
        else:
            await reschedule(job["_id"], delay=10)
    finally:
        CHANNEL_ACTIVE[ch] = max(0, CHANNEL_ACTIVE[ch] - 1)


# ─────────────────────────────────────────────
#  Channel admin join event
# ─────────────────────────────────────────────
@Client.on_chat_member_updated()
async def when_added_as_admin(client, update):
    try:
        new  = update.new_chat_member
        chat = update.chat
        if not new or not getattr(new, "user", None) or not new.user.is_self:
            return
        owner = getattr(update, "from_user", None)
        if not owner:
            return
        oid, oname = owner.id, owner.first_name or "Unknown"
        await add_user_channel(oid, chat.id, chat.title or "Unnamed")
        await set_channel_title_cache(chat.id, chat.title or "Unnamed")
        if not await get_channel_caption(chat.id):
            await set_block_words(chat.id, "")
            await set_prefix(chat.id, "")
            await set_suffix(chat.id, "")
            await set_replace_words(chat.id, "")
            await set_link_remover_status(chat.id, False)
            await set_emoji_remover_status(chat.id, False)
        try:
            msg = await client.send_message(
                oid,
                f"✅ Bot added to <b>{chat.title}</b>.\nManage via /settings.",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("⚙️ Open Settings", callback_data="settings_cb")
                ]]),
            )
            asyncio.create_task(_auto_delete(msg, 60))
            ch_link = (f"<a href='https://t.me/{chat.username}'>{chat.title}</a>"
                       if getattr(chat, "username", None) else f"{chat.title} (Private)")
            try:
                await client.send_message(
                    LOG_CH,
                    script.NEW_CHANNEL_TXT.format(
                        owner_name=oname, owner_id=oid,
                        channel_name=ch_link, channel_id=chat.id,
                    ),
                    disable_web_page_preview=True,
                )
            except Exception:
                pass
        except Exception as e:
            print(f"[WARN] notify {oid}: {e}")
    except Exception as e:
        print(f"[ERROR] when_added_as_admin: {e}")


async def _auto_delete(msg, delay):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass


# ─────────────────────────────────────────────
#  UI helpers
# ─────────────────────────────────────────────
@Client.on_callback_query(filters.regex(r"^settings_cb$"))
async def settings_button_handler(client, query):
    await query.answer()
    loading = await query.message.edit_text("⚙️ Loading your channels…")
    await _send_settings(client, user=query.from_user, send_func=loading.edit_text)


@Client.on_callback_query(filters.regex("^help$"))
async def help_callback(client, query):
    await query.answer()
    me = await client.get_me()
    await query.message.edit_text(
        script.HELP_TEXT,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Add to channel",
                                  url=f"https://t.me/{me.username}?startchannel=true")],
            [InlineKeyboardButton("⬅️ Back", callback_data="start")],
        ]),
        disable_web_page_preview=True,
    )


@Client.on_callback_query(filters.regex("^start$"))
async def back_start(client, query):
    await query.answer()
    await _start_ui(client, chat_id=query.message.chat.id,
                    mention=query.from_user.mention, edit=query.message)


@Client.on_callback_query(filters.regex("^about_cb$"))
async def about_callback(client, query):
    await query.answer()
    bot = await client.get_me()
    await query.message.edit_text(
        script.ABOUT_TXT.format(bot_name=bot.first_name, bot_username=bot.username),
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("🌐 Owner", url="https://t.me/Navex_69"),
            InlineKeyboardButton("⬅️ Back", callback_data="start"),
        ]]),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


@Client.on_callback_query(filters.regex("^close_msg$"))
async def close_msg(client, query):
    await query.answer()
    try:
        await query.message.delete()
    except Exception:
        pass


async def _start_ui(client, *, chat_id, mention, edit=None):
    me = await client.get_me()
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Add to channel",
                              url=f"https://t.me/{me.username}?startchannel=true")],
        [InlineKeyboardButton("📂 Help", callback_data="help"),
         InlineKeyboardButton("⚙ Settings", callback_data="settings_cb")],
        [InlineKeyboardButton("ℹ️ About", callback_data="about_cb")],
    ])
    txt = script.START_TXT.format(mention=mention)
    if edit:
        await edit.edit_text(txt, reply_markup=kb, disable_web_page_preview=True)
    else:
        await client.send_message(chat_id, txt, reply_markup=kb,
                                  disable_web_page_preview=True)


async def _send_settings(client, *, user, send_func):
    uid      = user.id
    channels = await get_user_channels(uid)
    if not channels:
        me = await client.get_me()
        return await send_func(
            "You haven't added me to any channel yet.\n\n➕ Add me as admin first.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("➕ Add me",
                                     url=f"https://t.me/{me.username}?startchannel=true"),
            ]]),
        )

    async def _chk(ch):
        cid = ch.get("channel_id")
        ttl = ch.get("channel_title", str(cid))
        try:
            m = await client.get_chat_member(cid, "me")
            if _is_admin(m):
                try:
                    ttl = (await client.get_chat(cid)).title or ttl
                except Exception:
                    pass
                return {"ok": True, "channel_id": cid, "channel_title": ttl}
            await users.update_one({"_id": uid}, {"$pull": {"channels": {"channel_id": cid}}})
            return {"ok": False, "title": ttl}
        except (ChatAdminRequired, RPCError):
            await users.update_one({"_id": uid}, {"$pull": {"channels": {"channel_id": cid}}})
            return {"ok": False, "title": ttl}
        except Exception:
            return {"ok": True, "channel_id": cid, "channel_title": ttl}

    results = await asyncio.gather(*[_chk(ch) for ch in channels])
    valid   = [r for r in results if r["ok"]]
    removed = [r["title"] for r in results if not r["ok"]]

    if removed:
        try:
            await send_func("⚠️ Removed (no longer admin):\n" +
                            "\n".join(f"• {t}" for t in removed))
        except Exception:
            pass
    if not valid:
        return await send_func("No active channels where I'm admin.")

    btns = [[InlineKeyboardButton(ch["channel_title"],
                                   callback_data=f"chinfo_{ch['channel_id']}")]
             for ch in valid]
    btns.append([InlineKeyboardButton("❌ Close", callback_data="close_msg")])
    await send_func("📋 Your channels — tap to manage:",
                    reply_markup=InlineKeyboardMarkup(btns))


# ─────────────────────────────────────────────
#  Commands
# ─────────────────────────────────────────────
@Client.on_message(filters.command("start") & filters.private)
async def start_cmd(client, message):
    u = message.from_user
    is_new = await insert_user_check_new(u.id)
    await _start_ui(client, chat_id=message.chat.id, mention=u.mention)
    if is_new:
        try:
            link = (f"<a href='https://t.me/{u.username}'>{u.first_name}</a>"
                    if u.username else u.first_name)
            await client.send_message(
                LOG_CH, script.NEW_USER_TXT.format(user=link, user_id=u.id),
                disable_web_page_preview=True,
            )
        except Exception:
            pass


@Client.on_message(filters.command("settings") & filters.private)
async def settings_cmd(client, message):
    loading = await message.reply_text("⚙️ Loading your channels…")
    await _send_settings(client, user=message.from_user, send_func=loading.edit_text)


@Client.on_message(filters.private & filters.command("file_forward"))
async def ff_start_cmd(client, message):
    uid      = message.from_user.id
    channels = await get_user_channels(uid)
    if not channels:
        return await message.reply_text(
            "❌ No admin channels found.\nAdd me to a channel first."
        )
    FF_SESSIONS[uid] = {"step": "src", "channels": channels, "expires": None}
    kb = [[InlineKeyboardButton(ch["channel_title"],
                                callback_data=f"ff_src_{ch['channel_id']}")]
          for ch in channels]
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")])
    await message.reply_text(
        "📤 <b>Select SOURCE channel</b>\n"
        "<i>(files will be forwarded FROM here)</i>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(kb),
    )


# ─────────────────────────────────────────────
#  Admin commands
# ─────────────────────────────────────────────
@Client.on_message(filters.private & filters.user(ADMIN) & filters.command("dump_skip"))
async def dump_skip_cmd(client, message):
    if len(message.command) != 2:
        return await message.reply_text("❌ Usage: `/dump_skip -100xxxxxxxxxx`",
                                        parse_mode=ParseMode.MARKDOWN)
    try:
        cid = int(message.command[1])
    except ValueError:
        return await message.reply_text("❌ Invalid channel ID.")
    await set_dump_skip(cid, True)
    await message.reply_text(
        "✅ <b>Dump skip enabled</b>\n\n" + await _fmt_dump_list(client),
        parse_mode=ParseMode.HTML,
    )


@Client.on_message(filters.private & filters.user(ADMIN) & filters.command("remove_dump"))
async def remove_dump_cmd(client, message):
    if len(message.command) != 2:
        return await message.reply_text("❌ Usage: `/remove_dump -100xxxxxxxxxx`",
                                        parse_mode=ParseMode.MARKDOWN)
    try:
        cid = int(message.command[1])
    except ValueError:
        return await message.reply_text("❌ Invalid channel ID.")
    await remove_dump_skip(cid)
    await message.reply_text(
        "🗑 <b>Dump skip removed</b>\n\n" + await _fmt_dump_list(client),
        parse_mode=ParseMode.HTML,
    )


@Client.on_message(filters.private & filters.user(ADMIN) & filters.command("stats"))
async def bot_stats(client, message):
    cap_p  = await queue_col.count_documents({"status": "pending"})
    cap_r  = await queue_col.count_documents({"status": "processing"})
    fwd_p  = await forward_queue.count_documents({"status": "pending"})
    fwd_r  = await forward_queue.count_documents({"status": "processing"})
    uc     = await total_user()
    acap   = sum(1 for t in _USER_CAPTION_TASKS.values() if not t.done())
    afwd   = sum(1 for t in _USER_FORWARD_TASKS.values() if not t.done())
    await message.reply_text(
        "📊 <b>BOT STATS</b>\n\n"
        f"👤 Users: <code>{uc}</code>\n"
        f"📝 Caption pending: <code>{cap_p}</code>\n"
        f"📝 Caption active: <code>{cap_r}</code>\n"
        f"📦 Forward pending: <code>{fwd_p}</code>\n"
        f"📦 Forward active: <code>{fwd_r}</code>\n"
        f"🔄 Caption sessions: <code>{acap}</code>\n"
        f"🚚 Forward sessions: <code>{afwd}</code>",
        parse_mode=ParseMode.HTML,
    )


@Client.on_message(filters.private & filters.user(ADMIN) & filters.command("queue"))
async def queue_status(client, message):
    cap_p = await queue_col.count_documents({"status": "pending"})
    cap_r = await queue_col.count_documents({"status": "processing"})
    fwd_p = await forward_queue.count_documents({"status": "pending"})
    fwd_r = await forward_queue.count_documents({"status": "processing"})

    cap_lines = []
    async for row in queue_col.aggregate([
        {"$match": {"status": "pending"}},
        {"$group": {"_id": "$chat_id", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}}, {"$limit": 10},
    ]):
        try:
            name = (await client.get_chat(row["_id"])).title
        except Exception:
            name = str(row["_id"])
        eta = int((row["n"] / DEFAULT_MAX_WORKERS) * DEFAULT_EDIT_DELAY)
        cap_lines.append(
            f"• <b>{name}</b>  jobs: <code>{row['n']}</code>  "
            f"ETA: ~{eta // 60}m {eta % 60}s"
        )

    fwd_lines = []
    async for row in forward_queue.aggregate([
        {"$match": {"status": "pending"}},
        {"$group": {"_id": "$user_id", "n": {"$sum": 1}}},
        {"$sort": {"n": -1}}, {"$limit": 10},
    ]):
        eta = int(row["n"] * 0.3)
        fwd_lines.append(
            f"• user <code>{row['_id']}</code>  jobs: <code>{row['n']}</code>  "
            f"ETA: ~{eta // 60}m {eta % 60}s"
        )

    text = (
        f"📊 <b>QUEUE STATUS</b>\n\n"
        f"📝 <b>Caption</b>  pending <code>{cap_p}</code>  active <code>{cap_r}</code>\n"
    )
    text += ("\n".join(cap_lines) if cap_lines else "✅ Queue empty") + "\n\n"
    text += (
        f"📦 <b>Forward</b>  pending <code>{fwd_p}</code>  active <code>{fwd_r}</code>\n"
    )
    text += "\n".join(fwd_lines) if fwd_lines else "✅ No forward tasks"
    await message.reply_text(text, parse_mode=enums.ParseMode.HTML)


@Client.on_message(filters.private & filters.user(ADMIN) & filters.command("broadcast"))
async def broadcast(client, message):
    if not message.reply_to_message:
        return await message.reply_text("↩ Reply to a message to broadcast.")
    st  = await message.reply_text("📡 Starting broadcast…")
    all_u = await getid()
    tot   = await total_user()
    ok = blk = dead = fail = 0
    for u in all_u:
        await asyncio.sleep(0.2)
        try:
            await message.reply_to_message.copy(u["_id"])
            ok += 1
        except errors.InputUserDeactivated:
            dead += 1; await delete_user(u["_id"])
        except errors.UserIsBlocked:
            blk += 1; await delete_user(u["_id"])
        except Exception:
            fail += 1
        try:
            await st.edit(f"📡 <b>Broadcast</b>\n"
                          f"Total {tot} | Done {ok} | Blocked {blk} | "
                          f"Dead {dead} | Failed {fail}")
        except errors.FloodWait as e:
            await asyncio.sleep(e.value)
    await st.edit(f"✅ <b>Done</b>\n"
                  f"Total {tot} | Done {ok} | Blocked {blk} | "
                  f"Dead {dead} | Failed {fail}")


@Client.on_message(filters.private & filters.user(ADMIN) & filters.command("restart"))
async def restart_bot(client, message):
    await (await message.reply_text("🔄 Restarting…")).edit("✅ Restarted.")
    os.execl(sys.executable, sys.executable, *sys.argv)


@Client.on_message(filters.command("reset") & filters.user(ADMIN))
async def reset_db(client, message):
    await message.reply_text("⚠️ Deleting all records…")
    await users.delete_many({})
    await chnl_ids.delete_many({})
    await user_channels.delete_many({})
    _CHANNEL_CACHE.clear()
    await message.reply_text("✅ Done.")


@Client.on_message(filters.private & filters.user(ADMIN) & filters.command("admin"))
async def admin_help(client, message):
    await message.reply_text(
        script.ADMIN_HELP_TEXT.format(workers=8, delay=DEFAULT_EDIT_DELAY),
        parse_mode=ParseMode.HTML,
    )


# ─────────────────────────────────────────────
#  Channel media → auto-caption
# ─────────────────────────────────────────────
@Client.on_message(filters.channel & filters.media)
async def reCap(client, msg):
    if msg.edit_date or not msg.media:
        return
    cid  = msg.chat.id
    dcap = msg.caption or ""

    fname = fsize = None
    for ft in ("video", "audio", "document", "voice"):
        obj = getattr(msg, ft, None)
        if obj:
            fname = getattr(obj, "file_name", None) or (
                "Voice Message" if ft == "voice" else "File"
            )
            fname = fname.replace("_", " ").replace(".", " ")
            fsize = get_size(getattr(obj, "file_size", 0))
            break
    if not fname:
        return

    doc = await get_channel_cached(cid)
    tpl = doc.get("caption")
    if not tpl:
        return

    langs  = extract_audio_languages(f"{fname} {dcap}")
    year   = extract_year(dcap) or extract_year(fname) or ""

    try:
        raw   = normalize_series_name(fname)
        info  = parse_file_info(raw, dcap)
        smart = build_smart_filename(raw, dcap) if "{smart_file_name}" in tpl else ""
        cap   = tpl.format(
            file_name=raw, smart_file_name=smart, file_size=fsize,
            default_caption=dcap,
            language=" + ".join(langs) if langs else "",
            year=year or info.get("year", ""),
            title=info.get("title", ""), season=info.get("season", ""),
            episode=info.get("episode", ""), audio=info.get("audio", ""),
            subtitle=info.get("subtitle", ""), quality=info.get("quality", ""),
            resolution=info.get("resolution", ""), source=info.get("source", ""),
            vcodec=info.get("vcodec", ""), acodec=info.get("acodec", ""),
            extension=info.get("extension", ""), duration="", empty="",
        )
    except Exception:
        cap = tpl

    bw = doc.get("block_words", "")
    rw = doc.get("replace_words") or ""
    if bw:
        cap = apply_block_words(cap, bw)
    if rw:
        pairs = parse_replace_pairs(rw)
        if pairs:
            cap = apply_replacements(cap, pairs)
    if doc.get("link_remover"):
        cap = strip_links_only(cap)
    if doc.get("prefix"):
        cap = f"{doc['prefix']}\n{cap}".strip()
    if doc.get("suffix"):
        cap = f"{cap}\n{doc['suffix']}".strip()
    if doc.get("emoji_remover"):
        cap = remove_emojis(cap)
    cap = cap.strip()
    if "<" in cap and ">" in cap:
        cap = _sanitize_html(cap)

    udoc = await users.find_one({"channels.channel_id": cid})
    uid  = udoc["_id"] if udoc else None
    await enqueue_caption({
        "chat_id": cid, "message_id": msg.id,
        "caption": cap, "url_buttons": doc.get("url_buttons", []),
        "user_id": uid,
    })
    if uid:
        _ensure_user_caption_worker(client, uid)


# ─────────────────────────────────────────────
#  Private text handler — all settings flows
# ─────────────────────────────────────────────
@Client.on_message(filters.private)
async def capture_user_input(client, message):
    uid = message.from_user.id
    active = (
        set(bot_data["caption_set"])
        | set(bot_data["block_words_set"])
        | set(bot_data["replace_words_set"])
        | set(bot_data["prefix_set"])
        | set(bot_data["suffix_set"])
        | set(bot_data.get("url_set", {}))
        | set(FF_SESSIONS)
    )
    if uid not in active:
        return

    text = (
        message.text.html if message.text else
        message.caption.html if message.caption else ""
    )
    if not text.strip():
        return

    async def _done(instr_id, msg_text, back_cb):
        try:
            await client.delete_messages(uid, message.id)
        except Exception:
            pass
        await client.edit_message_text(
            chat_id=uid, message_id=instr_id,
            text=msg_text, parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("↩ Back", callback_data=back_cb)
            ]]),
        )

    if uid in bot_data["caption_set"]:
        s = bot_data["caption_set"].pop(uid)
        await updateCap(s["channel_id"], text)
        return await _done(s["instr_msg_id"], "✅ <b>Caption updated!</b>",
                           f"back_to_captionmenu_{s['channel_id']}")

    if uid in bot_data["block_words_set"]:
        s   = bot_data["block_words_set"].pop(uid)
        old = await get_block_words(s["channel_id"])
        await set_block_words(s["channel_id"],
                              f"{old.rstrip()}\n{text.strip()}" if old else text.strip())
        return await _done(s["instr_msg_id"], "✅ <b>Blocked words updated!</b>",
                           f"back_to_blockwords_{s['channel_id']}")

    if uid in bot_data["replace_words_set"]:
        s   = bot_data["replace_words_set"].pop(uid)
        old = await get_replace_words(s["channel_id"])
        await set_replace_words(s["channel_id"],
                                f"{old.rstrip()}\n{text.strip()}" if old else text.strip())
        return await _done(s["instr_msg_id"], "✅ <b>Replace words updated!</b>",
                           f"back_to_replace_{s['channel_id']}")

    if uid in bot_data["prefix_set"]:
        s = bot_data["prefix_set"].pop(uid)
        _, old = await get_suffix_prefix(s["channel_id"])
        await set_prefix(s["channel_id"],
                         f"{old.rstrip()}\n{text.strip()}" if old else text.strip())
        return await _done(s["instr_msg_id"], "✅ <b>Prefix updated!</b>",
                           f"back_to_suffixprefix_{s['channel_id']}")

    if uid in bot_data["suffix_set"]:
        s = bot_data["suffix_set"].pop(uid)
        old, _ = await get_suffix_prefix(s["channel_id"])
        await set_suffix(s["channel_id"],
                         f"{old.rstrip()}\n{text.strip()}" if old else text.strip())
        return await _done(s["instr_msg_id"], "✅ <b>Suffix updated!</b>",
                           f"back_to_suffixprefix_{s['channel_id']}")

    if uid in bot_data.get("url_set", {}):
        s    = bot_data["url_set"].pop(uid)
        rows = []
        for line in text.strip().splitlines():
            row = []
            for part in [p.strip() for p in line.split("|") if p.strip()]:
                m = re.findall(r'"([^"]+)"', part)
                if len(m) == 2:
                    row.append({"text": m[0], "url": m[1]})
            if row:
                rows.append(row)
        if not rows:
            return await message.reply_text(
                '❌ Invalid format.\n\nExample:\n'
                '<code>"Btn1" "url1" | "Btn2" "url2"</code>',
                parse_mode=ParseMode.HTML,
            )
        await set_url_buttons(s["channel_id"], rows)
        return await _done(s["instr_msg_id"], "✅ <b>URL buttons updated!</b>",
                           f"seturl_{s['channel_id']}")

    # ── File-forward range input ──────────────────────────────────────────
    if uid in FF_SESSIONS:
        from body.file_forward import (
            parse_forward_input, validate_msg_in_channel, enqueue_forward_jobs,
        )
        s = FF_SESSIONS[uid]
        if s.get("expires") and s["expires"] < time.time():
            FF_SESSIONS.pop(uid, None)
            return await message.reply_text(
                "⏰ Session expired. Use /file_forward to start again."
            )
        if s.get("step") != "skip":
            return

        parsed = parse_forward_input((message.text or "").strip())
        if parsed.get("error"):
            return await message.reply_text(parsed["error"], parse_mode=ParseMode.HTML)

        skip_id  = parsed["skip_id"]
        end_id   = parsed["end_id"]
        src_hint = parsed["src_hint"]
        src_ch   = s["source"]

        if src_hint is not None and src_hint != src_ch:
            return await message.reply_text(
                "❌ <b>Wrong channel!</b>\n"
                "The link doesn't match the selected source channel.",
                parse_mode=ParseMode.HTML,
            )
        if skip_id > 0 and not await validate_msg_in_channel(client, src_ch, skip_id):
            return await message.reply_text("❌ Start message not found in source channel.")
        if end_id is not None and not await validate_msg_in_channel(client, src_ch, end_id):
            return await message.reply_text("❌ End message not found in source channel.")

        s["skip"]   = skip_id
        s["end_id"] = end_id
        s["step"]   = "queue"

        try:
            await message.delete()
        except Exception:
            pass
        prog = await client.send_message(
            s.get("chat_id", uid),
            "🔍 <b>Scanning channel for files…</b>",
            parse_mode=ParseMode.HTML,
        )
        s["msg_id"]  = prog.id
        s["chat_id"] = prog.chat.id
        # Fire-and-forget — UI stays snappy
        await enqueue_forward_jobs(client, uid)


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────
ALLOWED_TAGS = {"b","i","u","s","code","pre","a","spoiler","blockquote"}

def _sanitize_html(text: str) -> str:
    def repl(m):
        return m.group(0) if m.group(1).casefold() in ALLOWED_TAGS else ""
    return re.sub(r"</?\\s*([a-zA-Z0-9]+)(?:\\s[^>]*)?>", repl, text)

def _is_admin(m) -> bool:
    if not m: return False
    s = getattr(m, "status", "")
    try:
        if hasattr(s, "value"): s = str(s.value)
    except Exception:
        s = str(s)
    return s.lower() in ("administrator", "creator", "owner")

async def _fmt_dump_list(client) -> str:
    items = await get_all_dump_skip_channels()
    if not items:
        return "📭 <b>No dump-skip channels</b>"
    lines = ["📌 <b>Dump-skip channels:</b>"]
    for doc in items:
        cid = doc["chnl_id"]
        try:
            title = (await client.get_chat(cid)).title
        except Exception:
            title = "Unknown"
        lines.append(f"• <b>{title}</b>  <code>{cid}</code>")
    return "\n".join(lines)

def get_size(size: int) -> str:
    units = ["Bytes","KB","MB","GB","TB"]
    i = 0
    while size >= 1024.0 and i < len(units) - 1:
        size /= 1024.0; i += 1
    return "%.2f %s" % (size, units[i])

def extract_year(text: str) -> Optional[str]:
    m = re.search(r'\b(19\d{2}|20\d{2})\b', text or "")
    return m.group(1) if m else None

MD_LINK_RE = re.compile(r'\[([^\]]+)\]\([^)]+\)', re.I)
HTML_A_RE  = re.compile(r'<a\s+[^>]*href=["\'](?:https?://|tg://)[^"\']+["\'][^>]*>(.*?)</a>',
                        re.I | re.S)
TG_LINK_RE = re.compile(r'\[([^\]]+)\]\(tg://user\?id=\d+\)', re.I)
URL_RE     = re.compile(r'(https?://[^\s]+|www\.[^\s]+|t\.me/[^\s/]+(?:/[^\s]+)?)', re.I)
MENTION_RE = re.compile(r'@\w+', re.I)
EMOJI_RE   = re.compile(
    "[\U0001F300-\U0001F9FF\U00002600-\U000027BF\U0001FA00-\U0001FA9F"
    "\U0001F1E0-\U0001F1FF\U00002702-\U000027B0]+", re.UNICODE,
)

LANG_LIST = [
    "Hindi","English","Tamil","Telugu","Malayalam","Kannada","Marathi","Gujarati",
    "Bengali","Punjabi","Urdu","Japanese","Korean","Chinese","Spanish","French",
    "German","Italian","Russian",
]
LANG_CODE_MAP = {
    "hin":"Hindi","eng":"English","tam":"Tamil","tel":"Telugu","mal":"Malayalam",
    "kan":"Kannada","mar":"Marathi","guj":"Gujarati","ben":"Bengali","pan":"Punjabi",
    "urd":"Urdu","jpn":"Japanese","kor":"Korean","chi":"Chinese","spa":"Spanish",
    "fre":"French","ger":"German","ita":"Italian","rus":"Russian",
}
QUALITY_LIST     = ["2160p","4K","1080p","720p","480p","360p"]
SOURCE_LIST      = ["WEB-DL","WEBRip","BluRay","Blu-Ray","HDRip","DVDRip","HDTV","AMZN","NF","DSNP"]
VIDEO_CODEC_LIST = ["HEVC","x265","x264","AV1","H.264","H.265"]
EXT_LIST         = ["mkv","mp4","avi","webm","mov"]
ESUB_RE = re.compile(r'\bE\.?Subs?\b', re.I)
HSUB_RE = re.compile(r'\bH\.?Subs?\b', re.I)
SUB_RE  = re.compile(r'\b(?:M\.?Subs?|MSub|Subs?|Subtitles?)\b', re.I)

def normalize_series_name(name: str) -> str:
    if not name: return ""
    name = re.sub(r'\.(mkv|mp4|avi|webm)$', '', name, flags=re.I)
    name = re.sub(r'[._\-]+', ' ', name)
    return re.sub(r'\s+', ' ', name).strip().title()

def extract_audio_languages(text: str) -> list:
    found = []
    for lang in LANG_LIST:
        if re.search(rf'\b{re.escape(lang)}\b', text, re.I):
            found.append(lang)
    if not found:
        for code, lang in LANG_CODE_MAP.items():
            if re.search(rf'\b{code}\b', text, re.I) and lang not in found:
                found.append(lang)
    return list(dict.fromkeys(found))

def _qry(text: str, lst: list) -> str:
    for q in lst:
        if re.search(rf'\b{re.escape(q)}\b', text, re.I): return q
    return ""

def extract_quality(text): return _qry(text, QUALITY_LIST)
def extract_source(text):  return _qry(text, SOURCE_LIST)
def extract_vcodec(text):  return _qry(text, VIDEO_CODEC_LIST)

def extract_acodec(text: str) -> str:
    m = re.search(r'\b(DD5\.1|DD\+|DDP5\.1|DDP|DTS-HD|DTS|Atmos|AAC|AC3|MP3)(?:[- ]\d+Kbps)?\b',
                  text, re.I)
    return m.group(1).upper() if m else ""

def extract_ext(text: str) -> str:
    m = re.search(r'\.(mkv|mp4|avi|webm|mov)\b', text, re.I)
    if m: return m.group(1).lower()
    for e in EXT_LIST:
        if re.search(rf'\b{e}\b', text, re.I): return e.lower()
    return ""

def extract_subtitle_tag(text: str) -> str:
    if ESUB_RE.search(text): return "ESub"
    if HSUB_RE.search(text): return "HSub"
    if SUB_RE.search(text):  return "MSub"
    return ""

def _extract_title_year(raw: str):
    text  = re.sub(r'[._]', ' ', raw)
    ym    = re.search(r'\b((?:19|20)\d{2})\b', text)
    year  = ym.group(1) if ym else ""
    tr    = text[:ym.start()] if ym else text
    tr    = re.sub(r'\s*\bS\d{1,3}\b.*$', '', tr, flags=re.I)
    tr    = re.sub(r'\s*\bEp?\.?\d{1,3}\b.*$', '', tr, flags=re.I)
    tr    = re.sub(
        r'\b(480p|720p|1080p|2160p|4k|web[- ]?dl|webrip|bluray|hdrip|'
        r'x264|x265|hevc|av1|esub|hsub|sub|dual|multi|audio|'
        r'hindi|english|tamil|telugu)\b', '', tr, flags=re.I,
    )
    tr    = re.sub(r'\([^)]{1,30}\)', '', tr)
    title = re.sub(r'\s{2,}', ' ', re.sub(r'[\[\]()\\-:,]+\s*$', '', tr.strip())).strip().title()
    return title, year

def _extract_season_ep(text: str):
    text = re.sub(r'[._]', ' ', text)
    s    = re.search(r'\bS(?:eason)?\s*0*(\d+)\b', text, re.I)
    season = f"S{int(s.group(1)):02d}" if s else ""
    r = re.search(r'\bEp?\.?\s*0*(\d+)\s*[-–to]+\s*0*(\d+)\b', text, re.I)
    if r:
        return season, f"Ep.{int(r.group(1)):02d}-{int(r.group(2)):02d}"
    e = re.search(r'\bEp?\.?\s*0*(\d+)\b', text, re.I)
    return season, (f"E{int(e.group(1)):02d}" if e else "")

def parse_file_info(filename: str, caption: str) -> dict:
    raw         = f"{filename} {caption}"
    title, year = _extract_title_year(raw)
    try:
        for r in ia.search_movie(title)[:5]:
            if str(r.get("year")) == year:
                title = r.get("title", title); break
    except Exception:
        pass
    season, ep = _extract_season_ep(raw)
    langs      = extract_audio_languages(raw)
    return {
        "title": title, "year": year, "season": season, "episode": ep,
        "audio": " + ".join(langs) if langs else "",
        "subtitle": extract_subtitle_tag(raw),
        "quality": extract_quality(raw), "resolution": extract_quality(raw),
        "source": extract_source(raw), "vcodec": extract_vcodec(raw),
        "acodec": extract_acodec(raw), "extension": extract_ext(raw),
    }

def build_smart_filename(filename: str, caption: str) -> str:
    info  = parse_file_info(filename, caption)
    parts = []
    if info["title"]:    parts.append(info["title"])
    se = f"{info['season']} {info['episode']}".strip()
    if se:               parts.append(se)
    if info["year"]:     parts.append(f"({info['year']})")
    for k in ("audio","subtitle","quality","source","vcodec","acodec","extension"):
        if info[k]:      parts.append(info[k])
    return " ".join(parts).strip()

def strip_links_only(text: str) -> str:
    if not text: return text
    text = MD_LINK_RE.sub(r'\1', text)
    text = TG_LINK_RE.sub(r'\1', text)
    text = HTML_A_RE.sub(r'\1', text)
    text = URL_RE.sub("", text)
    text = MENTION_RE.sub("", text)
    return re.sub(r'\s+', ' ', re.sub(r'\(\s*\)|\[\s*\]', '', text)).strip()

def apply_block_words(cap: str, raw: str) -> str:
    if not cap or not raw: return cap
    for w in [w.strip() for w in re.split(r"[,\n]+", raw) if w.strip()]:
        cap = cap.replace(w, "")
    lines = [l.rstrip() for l in cap.splitlines() if l.strip()]
    return re.sub(r"[ \t]{2,}", " ", "\n".join(lines)).strip()

def parse_replace_pairs(raw) -> List[Tuple[str, str]]:
    if not raw: return []
    if not isinstance(raw, str): raw = str(raw)
    pairs = []
    for item in [p.strip() for p in raw.replace('\n', ',').split(',') if p.strip()]:
        parts = item.split(None, 1)
        if len(parts) == 2:
            pairs.append((parts[0], parts[1]))
    return pairs

def apply_replacements(text: str, pairs: List[Tuple[str, str]]) -> str:
    for old, new in pairs:
        if not old: continue
        try:
            text = re.sub(re.escape(old), new, text, flags=re.I)
        except re.error:
            text = text.replace(old, new)
    return re.sub(r'[ \t]+', ' ', text).strip()

def remove_emojis(text: str) -> str:
    if not text: return text
    text = EMOJI_RE.sub("", text)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]{2,}", " ", text)).strip()

def clean_text(text: str) -> str:
    if not text: return ""
    text = MD_LINK_RE.sub(r'\1', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = URL_RE.sub('', text)
    text = MENTION_RE.sub('', text)
    return re.sub(r'\s+', ' ', text).strip()
