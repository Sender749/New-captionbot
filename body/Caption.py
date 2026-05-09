import sys, time, os, re, asyncio
from typing import Tuple, List, Optional
from pyrogram import Client, filters, errors, enums
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ChatMemberUpdated, CallbackQuery
from pyrogram.errors import ChatAdminRequired, RPCError, FloodWait
from pyrogram.enums import ParseMode
from info import *
from Script import script
from body.database import *  
from body.file_forward import *
from collections import deque, defaultdict
from imdb import IMDb
from body.database import _CHANNEL_CACHE as CHANNEL_CACHE, CHANNEL_ACTIVE, CHANNEL_COOLDOWN, DEFAULT_MAX_WORKERS

ia = IMDb()
MESSAGE_LINK_RE = re.compile(r"(?:https?://)?t\.me/(?:c/\d+|[A-Za-z0-9_]+)/(\d+)")
DEFAULT_EDIT_DELAY = 0.3                 # per channel
bot_data = {
    "caption_set": {},
    "block_words_set": {},
    "suffix_set": {},
    "prefix_set": {},
    "replace_words_set": {},
    "url_set": {}
}

def extract_msg_id_from_text(text: str) -> int | None:
    if not text:
        return None
    m = MESSAGE_LINK_RE.search(text)
    if m:
        return int(m.group(1))
    if text.isdigit():
        return int(text)
    return None

async def animate_loading(msg):
    frames = [
        "⚙️ Loading your channels",
        "⚙️ Loading your channels.",
        "⚙️ Loading your channels..",
        "⚙️ Loading your channels...",
    ]
    while True:
        for f in frames:
            try:
                await msg.edit_text(f)
            except:
                return
            await asyncio.sleep(0.6)

@Client.on_chat_member_updated()
async def when_added_as_admin(client, chat_member_update):
    try:
        new = chat_member_update.new_chat_member
        chat = chat_member_update.chat
        if not new or not getattr(new, "user", None) or not new.user.is_self:
            return
        owner = getattr(chat_member_update, "from_user", None)
        if not owner:
            print(f"[INFO] Bot added manually to: {chat.title}")
            return
        owner_id = owner.id
        owner_name = owner.first_name or "Unknown User"
        await add_user_channel(owner_id, chat.id, chat.title or "Unnamed Channel")
        await set_channel_title_cache(chat.id, chat.title or "Unnamed Channel")
        existing = await get_channel_caption(chat.id)
        if not existing:
            await set_block_words(chat.id, "")
            await set_prefix(chat.id, "")
            await set_suffix(chat.id, "")
            await set_replace_words(chat.id, "")
            await set_link_remover_status(chat.id, False)
            await set_emoji_remover_status(chat.id, False)
        try:
            msg = await client.send_message(
                owner_id,
                f"✅ Bot added to <b>{chat.title}</b>.\nYou can manage it anytime using /settings.",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("⚙️ Open Settings", callback_data="settings_cb")]
                ])
            )
            print(f"[NEW] Added to {chat.title} by {owner_name} ({owner_id})")
            try:
                if chat.username:
                    channel_link = f"https://t.me/{chat.username}"
                    channel_name_clickable = f"<a href='{channel_link}'>{chat.title}</a>"
                else:
                    channel_name_clickable = f"{chat.title} (Private Channel)"
                log_text = script.NEW_CHANNEL_TXT.format(
                    owner_name=owner_name,
                    owner_id=owner_id,
                    channel_name=channel_name_clickable,
                    channel_id=chat.id
                )
                await client.send_message(LOG_CH, log_text, disable_web_page_preview=True)
            except Exception as e:
                print(f"[WARN] Failed to send log message: {e}")
            asyncio.create_task(auto_delete_message(msg, 60))
        except Exception as e:
            print(f"[WARN] Could not notify user: {e}")
    except Exception as e:
        print(f"[ERROR] when_added_as_admin: {e}")

async def auto_delete_message(msg, delay: int):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except:
        pass

@Client.on_callback_query(filters.regex(r"^settings_cb$"))
async def settings_button_handler(client: Client, query: CallbackQuery):
    await query.answer()
    loading = await query.message.edit_text("⚙️ Loading your channels...")
    await user_settings(
        client,
        user=query.from_user,
        send_func=loading.edit_text
    )

@Client.on_callback_query(filters.regex("^help$"))
async def help_callback(client, query: CallbackQuery):
    await query.answer()
    bot_me = await client.get_me()
    bot_username = bot_me.username
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕️ Add me to your channel ➕️", url=f"https://t.me/{bot_username}?startchannel=true")],
        [InlineKeyboardButton("⬅️ Back", callback_data="start")]
    ])
    await query.message.edit_text(
        text=script.HELP_TEXT,
        reply_markup=keyboard,
        disable_web_page_preview=True
    )

@Client.on_callback_query(filters.regex("^start$"))
async def back_to_start(client: Client, query: CallbackQuery):
    await query.answer()
    await show_start_ui(
        client,
        chat_id=query.message.chat.id,
        mention=query.from_user.mention,
        edit_message=query.message
    )

async def show_start_ui(
    client: Client,
    *,
    chat_id: int,
    mention: str,
    edit_message=None
):
    bot_me = await client.get_me()
    bot_username = bot_me.username or BOT_USERNAME
    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕️ Add me to your channel ➕️", url=f"https://t.me/{bot_username}?startchannel=true")],
            [InlineKeyboardButton("📂Help", callback_data="help"), InlineKeyboardButton("⚙ Settings", callback_data="settings_cb")],
            [InlineKeyboardButton("ℹ️ About", callback_data="about_cb")],
        ]
    )
    text = script.START_TXT.format(mention=mention)
    if edit_message:
        await edit_message.edit_text(
            text=text,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )
    else:
        await client.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=keyboard,
            disable_web_page_preview=True
        )

@Client.on_callback_query(filters.regex("^about_cb$"))
async def about_callback(client: Client, query: CallbackQuery):
    await query.answer()
    bot = await client.get_me()
    text = script.ABOUT_TXT.format(
        bot_name=bot.first_name,
        bot_username=bot.username
    )
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("🌐 Owner", url="https://t.me/Navex_69"),InlineKeyboardButton("⬅️ Back", callback_data="start")]
    ])
    await query.message.edit_text(
        text=text,
        reply_markup=keyboard,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )

# ---------------- Commands ----------------
@Client.on_message(filters.command("start") & filters.private)
async def start_cmd(client, message):
    try:
        user = message.from_user
        user_id = int(user.id)
        user_name = user.first_name or "Unknown User"
        username = user.username
        is_new_user = await insert_user_check_new(user_id)
        await show_start_ui(
            client,
            chat_id=message.chat.id,
            mention=user.mention
        )
        if is_new_user:
            try:
                if username:
                    user_clickable = f"<a href='https://t.me/{username}'>{user_name}</a>"
                else:
                    user_clickable = f"{user_name}"
                log_text = script.NEW_USER_TXT.format(user=user_clickable, user_id=user_id)
                await client.send_message(LOG_CH, log_text, disable_web_page_preview=True)
            except Exception as e:
                print(f"[ERROR] log new user: {e}")
    except Exception as e:
        print(f"[ERROR] start_cmd failed: {e}")

@Client.on_message(filters.private & filters.user(ADMIN) & filters.command("dump_skip"))
async def dump_skip_cmd(client, message):
    if len(message.command) != 2:
        return await message.reply_text(
            "❌ Usage:\n`/dump_skip -100xxxxxxxxxx`",
            parse_mode=ParseMode.MARKDOWN
        )
    try:
        channel_id = int(message.command[1])
    except ValueError:
        return await message.reply_text("❌ Invalid channel ID")
    await set_dump_skip(channel_id, True)
    text = "✅ <b>Dump skip enabled</b>\n\n"
    text += await format_dump_skip_list(client)
    await message.reply_text(text, parse_mode=ParseMode.HTML)

@Client.on_message(filters.private & filters.user(ADMIN) & filters.command("remove_dump"))
async def remove_dump_cmd(client, message):
    if len(message.command) != 2:
        return await message.reply_text(
            "❌ Usage:\n`/remove_dump -100xxxxxxxxxx`",
            parse_mode=ParseMode.MARKDOWN
        )
    try:
        channel_id = int(message.command[1])
    except ValueError:
        return await message.reply_text("❌ Invalid channel ID")
    await remove_dump_skip(channel_id)
    text = "🗑 <b>Dump skip removed</b>\n\n"
    text += await format_dump_skip_list(client)
    await message.reply_text(text, parse_mode=ParseMode.HTML)

@Client.on_message(filters.private & filters.command("file_forward"))
async def ff_start(client, message):
    uid = message.from_user.id
    channels = await get_user_channels(uid)
    if not channels:
        return await message.reply_text("❌ No admin channels found.")
    FF_SESSIONS[uid] = {
        "step": "src",
        "channels": channels,
        "expires": None  
    }
    kb = [[InlineKeyboardButton(ch["channel_title"], callback_data=f"ff_src_{ch['channel_id']}")] for ch in channels]
    kb.append([InlineKeyboardButton("❌ Cancel", callback_data="ff_cancel")])
    await message.reply_text("📤 **Select SOURCE channel**", reply_markup=InlineKeyboardMarkup(kb))
        
@Client.on_message(filters.private & filters.user(ADMIN) & filters.command("admin"))
async def admin_help(client, message):
    text = "⚙️ Scheduler: Per-channel & per-session isolated\nFloodWait-safe"
    await message.reply_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )

@Client.on_message(filters.private & filters.user(ADMIN) & filters.command("stats"))
async def bot_stats(client, message):
    pending = await queue_col.count_documents({"status": "pending"})
    processing = await queue_col.count_documents({"status": "processing"})
    users_count = await total_user()
    text = (
        "📊 <b>BOT STATS</b>\n\n"
        f"• Users: <code>{users_count}</code>\n"
        f"• Pending Jobs: <code>{pending}</code>\n"
        f"• Processing Jobs: <code>{processing}</code>\n"
    )
    await message.reply_text(text, parse_mode=ParseMode.HTML)

@Client.on_message(filters.private & filters.user(ADMIN) & filters.command(["broadcast"]))
async def broadcast(client, message):
    if (message.reply_to_message):
        silicon = await message.reply_text("Getting all ids from database.. Please wait")
        all_users = await getid()
        tot = await total_user()
        success = failed = deactivated = blocked = 0
        await silicon.edit("ʙʀᴏᴀᴅᴄᴀsᴛɪɴɢ...")
        for user in all_users:
            try:
                await asyncio.sleep(0.2)
                await message.reply_to_message.copy(user["_id"])
                success += 1
            except errors.InputUserDeactivated:
                deactivated += 1
                await delete_user(user["_id"])
            except errors.UserIsBlocked:
                blocked += 1
                await delete_user(user["_id"])
            except Exception:
                failed += 1
                await delete_user(user["_id"])
                pass
            try:
                await silicon.edit(
                    f"<u>ʙʀᴏᴀᴅᴄᴀsᴛ ᴘʀᴏᴄᴇssɪɴɢ</u>\n\n"
                    f"• ᴛᴏᴛᴀʟ ᴜsᴇʀs: {tot}\n• sᴜᴄᴄᴇssғᴜʟ: {success}\n• ʙʟᴏᴄᴋᴇᴅ ᴜsᴇʀs: {blocked}\n• ᴅᴇʟᴇᴛᴇᴅ ᴀᴄᴄᴏᴜɴᴛs: {deactivated}\n• ᴜɴsᴜᴄᴄᴇssғᴜʟ: {failed}"
                )
            except errors.FloodWait as e:
                await asyncio.sleep(e.value)
        await silicon.edit(
            f"<u>ʙʀᴏᴀᴅᴄᴀsᴛ ᴄᴏᴍᴘʟᴇᴛᴇᴅ</u>\n\n"
            f"• ᴛᴏᴛᴀʟ ᴜsᴇʀs: {tot}\n• sᴜᴄᴄᴇssғᴜʟ: {success}\n• ʙʟᴏᴄᴋᴇᴅ ᴜsᴇʀs: {blocked}\n• ᴅᴇʟᴇᴛᴇᴅ ᴀᴄᴄᴏᴜɴᴛs: {deactivated}\n• ᴜɴsᴜᴄᴄᴇssғᴜʟ: {failed}"
        )


@Client.on_message(filters.private & filters.user(ADMIN) & filters.command("restart"))
async def restart_bot(client, message):
    silicon = await client.send_message(
        chat_id=message.chat.id,
        text="**🔄 𝙿𝚁𝙾𝙲𝙴𝚂𝚂𝙴𝚂 𝚂𝚃𝙾𝙿𝙿ᴇᴅ. 𝙱𝙾𝚃 𝙸𝚂 𝚁𝙴𝚂𝚃𝙰𝚁𝚃𝙸𝙽𝙶...**",
    )
    await asyncio.sleep(3)
    await silicon.edit("**✅️ 𝙱𝙾𝚃 𝙸𝚂 𝚁𝙴𝚂𝚃𝙰𝚁𝚃𝙴𝙳. 𝙽𝙾𝚆 𝚈𝙾𝚄 𝙲𝙰𝙽 𝚄𝚂𝙴 𝙼𝙴**")
    os.execl(sys.executable, sys.executable, *sys.argv)

@Client.on_message(filters.command("settings") & filters.private)
async def settings_cmd(client, message):
    loading = await message.reply_text("⚙️ Loading your channels...")
    await user_settings(client, user=message.from_user, send_func=loading.edit_text)

async def user_settings(client: Client,*,user,send_func,):
    user_id = user.id
    channels = await get_user_channels(user_id)
    if not channels:
        bot = await client.get_me()
        bot_username = bot.username or BOT_USERNAME
        return await send_func(
            "You haven’t added me to any channels yet!\n\n"
            "➕ Add me as admin in your channel by below buttonx. 👇",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("➕ Add me to your channel",url=f"https://t.me/{bot_username}?startchannel=true")]]
            ),
            disable_web_page_preview=True
        )
    valid_channels = []
    removed_titles = []
    async def check_channel(ch):
        ch_id = ch.get("channel_id")
        ch_title = ch.get("channel_title", str(ch_id))
        try:
            member = await client.get_chat_member(ch_id, "me")
            if _is_admin_member(member):
                try:
                    chat = await client.get_chat(ch_id)
                    ch_title = getattr(chat, "title", ch_title)
                except:
                    pass
                return {"valid": True, "channel_id": ch_id, "channel_title": ch_title}
            else:
                await users.update_one({"_id": user_id}, {"$pull": {"channels": {"channel_id": ch_id}}})
                return {"valid": False, "title": ch_title}
        except (ChatAdminRequired, errors.RPCError):
            await users.update_one({"_id": user_id}, {"$pull": {"channels": {"channel_id": ch_id}}})
            return {"valid": False, "title": ch_title}
        except Exception:
            return {"valid": True, "channel_id": ch_id, "channel_title": ch_title}
    results = await asyncio.gather(*[check_channel(ch) for ch in channels])
    for res in results:
        if res["valid"]:
            valid_channels.append(res)
        else:
            removed_titles.append(res["title"])
    if removed_titles:
        removed_text = "• " + "\n• ".join(removed_titles)
        await send_func(f"⚠️ Removed (no admin/access):\n{removed_text}")
    if not valid_channels:
        return await send_func("No active channels where I am admin.")
    buttons = [[InlineKeyboardButton(ch["channel_title"], callback_data=f"chinfo_{ch['channel_id']}")] for ch in valid_channels]
    buttons.append([InlineKeyboardButton("❌ Close", callback_data="close_msg")])
    await send_func("📋 Your added channels:", reply_markup=InlineKeyboardMarkup(buttons))

@Client.on_callback_query(filters.regex("^close_msg$"))
async def close_message(client, query):
    await query.answer()
    try:
        await query.message.delete()
    except:
        pass
    
@Client.on_message(filters.command("reset") & filters.user(ADMIN))
async def reset_db(client, message):
    await message.reply_text("⚠️ This will delete all users, channels, captions, and settings from the database.\nProcessing...")

    await users.delete_many({})
    await chnl_ids.delete_many({})
    await user_channels.delete_many({})
    CHANNEL_CACHE.clear()

    await message.reply_text("✅ All database records have been deleted successfully!")

@Client.on_message(filters.private & filters.user(ADMIN) & filters.command("queue"))
async def queue_status(client, message):
    cap_pending = await queue_col.count_documents({"status": "pending"})
    cap_processing = await queue_col.count_documents({"status": "processing"})
    cap_pipeline = [
        {"$match": {"status": "pending"}},
        {"$group": {"_id": "$chat_id", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 10}
    ]
    cap_lines = []
    async for row in queue_col.aggregate(cap_pipeline):
        ch_id = row["_id"]
        count = row["count"]
        try:
            chat = await client.get_chat(ch_id)
            name = chat.title
        except:
            name = "Unknown"
        eta = int((count / DEFAULT_MAX_WORKERS) * DEFAULT_EDIT_DELAY)
        cap_lines.append(
            f"• <b>{name}</b>\n"
            f"  ├ ID: <code>{ch_id}</code>\n"
            f"  ├ Jobs: <code>{count}</code>\n"
            f"  └ ETA (channel): ~{eta//60}m {eta%60}s"
        )
    f_pending = await forward_queue.count_documents({"status": "pending"})
    f_processing = await forward_queue.count_documents({"status": "processing"})
    f_pipeline = [
        {"$match": {"status": "pending"}},
        {"$group": {
            "_id": {
                "src": "$src",
                "dst": "$dst"
            },
            "count": {"$sum": 1}
        }},
        {"$sort": {"count": -1}},
        {"$limit": 10}
    ]
    forward_lines = []
    async for row in forward_queue.aggregate(f_pipeline):
        src = row["_id"]["src"]
        dst = row["_id"]["dst"]
        count = row["count"]
        try:
            s_chat = await client.get_chat(src)
            s_name = s_chat.title
        except:
            s_name = "Unknown"
        try:
            d_chat = await client.get_chat(dst)
            d_name = d_chat.title
        except:
            d_name = "Unknown"
        eta = int(count * FORWARD_DELAY)
        forward_lines.append(
            f"• <b>{s_name}</b> ➜ <b>{d_name}</b>\n"
            f"  ├ Jobs: <code>{count}</code>\n"
            f"  └ ETA (pair): ~{eta//60}m {eta%60}s"
        )
    text = (
        "📊 <b>QUEUE STATUS</b>\n\n"
        "📝 <b>Caption Queue</b>\n"
        f"• Pending: <code>{cap_pending}</code>\n"
        f"• Processing: <code>{cap_processing}</code>\n"
    )
    if cap_lines:
        text += "🔥 <b>Top Busy Caption Channels</b>\n" + "\n".join(cap_lines) + "\n\n"
    else:
        text += "✅ No caption tasks\n\n"
    text += (
        "📦 <b>File Forward Queue</b>\n"
        f"• Pending: <code>{f_pending}</code>\n"
        f"• Processing: <code>{f_processing}</code>\n"
    )
    if forward_lines:
        text += "🚚 <b>Top Forward Sessions</b>\n" + "\n".join(forward_lines)
    else:
        text += "✅ No forward tasks"
    await message.reply_text(text, parse_mode=enums.ParseMode.HTML)

# ---------------- Auto Caption core ----------------
def sanitize_caption_html(text: str) -> str:
    if not text:
        return ""
    allowed_tags = {"b", "i", "u", "s", "code", "pre", "a", "spoiler", "blockquote"}
    def repl(match):
        tag = match.group(1).casefold()
        return match.group(0) if tag in allowed_tags else ""
    return re.sub(r"</?\s*([a-zA-Z0-9]+)(?:\s[^>]*)?>", repl, text)

async def caption_worker(client: Client):
    while True:
        job = await fetch_channel_job()
        if not job:
            await asyncio.sleep(0.5)
            continue
        ch = job["chat_id"]
        released = False
        try:
            await client.edit_message_caption(
                chat_id=ch,
                message_id=job["message_id"],
                caption=job["caption"],
                parse_mode=ParseMode.HTML,
                reply_markup=(InlineKeyboardMarkup([[InlineKeyboardButton(btn["text"], url=btn["url"]) for btn in row] for row in job.get("url_buttons", [])]) 
                              if job.get("url_buttons") else None
                             )
            )
            if not await is_dump_skip(ch):
                try:
                    original = await client.get_messages(ch, job["message_id"])
                    fname = None
                    for t in ("document", "video", "audio", "voice"):
                        obj = getattr(original, t, None)
                        if obj:
                            fname = getattr(obj, "file_name", None)
                            break
                    fname = clean_text(fname or "File")
                    fname = remove_emojis(fname)
                    await client.copy_message(
                        chat_id=CP_CH,
                        from_chat_id=ch,
                        message_id=job["message_id"],
                        caption=fname
                    )
                except:
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
            if not released:
                CHANNEL_ACTIVE[ch] = max(0, CHANNEL_ACTIVE[ch] - 1)
                released = True

@Client.on_message(filters.channel & filters.media)
async def reCap(client, msg):
    if msg.edit_date or not msg.media:
        return
    chnl_id = msg.chat.id
    default_caption = msg.caption or ""
    file_name = None
    file_size = None
    for file_type in ("video", "audio", "document", "voice"):
        obj = getattr(msg, file_type, None)
        if obj:
            file_name = getattr(obj, "file_name", None)
            if not file_name and file_type == "voice":
                file_name = "Voice Message"
            elif not file_name:
                file_name = "File"
            file_name = file_name.replace("_", " ").replace(".", " ")
            file_size = get_size(getattr(obj, "file_size", 0))
            break
    if not file_name:
        return
    cap_doc = await get_channel_cached(chnl_id)
    # Fetch channel settings
    cap_template = cap_doc.get("caption")
    if not cap_template:
        return
    link_remover_on = bool(cap_doc.get("link_remover", False))
    emoji_remover_on = bool(cap_doc.get("emoji_remover", False))
    blocked_words_raw = cap_doc.get("block_words", "")
    suffix = cap_doc.get("suffix", "") or ""
    prefix = cap_doc.get("prefix", "") or ""
    replace_raw = cap_doc.get("replace_words", None)
    url_buttons = cap_doc.get("url_buttons", [])
    # Keep original filename (with extension/dots) for smart metadata extraction
    original_file_name = ""
    for file_type in ("video", "audio", "document", "voice"):
        obj = getattr(msg, file_type, None)
        if obj:
            original_file_name = getattr(obj, "file_name", None) or ""
            break

    # Extract info from caption + filename (use original for better metadata extraction)
    combined_raw = f"{original_file_name} {default_caption}"
    audio_lang_list = extract_audio_languages(combined_raw)
    language = " + ".join(audio_lang_list) if audio_lang_list else ""
    year = extract_year(default_caption) or extract_year(original_file_name) or ""
    # Build caption
    try:
        raw_file_name = normalize_series_name(file_name)
        # Parse all metadata once – use original filename to preserve extension dots
        file_info = parse_file_info(original_file_name or raw_file_name, default_caption)
        smart_file_name = ""
        if "{smart_file_name}" in cap_template:
            smart_file_name = build_smart_filename(original_file_name or raw_file_name, default_caption)
        new_caption = cap_template.format(
            file_name=raw_file_name,
            smart_file_name=smart_file_name,
            file_size=file_size,
            default_caption=default_caption,
            language=language or file_info.get("audio", ""),
            year=year or file_info.get("year", ""),
            title=file_info.get("title", ""),
            season=file_info.get("season", ""),
            episode=file_info.get("episode", ""),
            audio=file_info.get("audio", ""),
            subtitle=file_info.get("subtitle", ""),
            quality=file_info.get("quality", ""),
            resolution=file_info.get("resolution", ""),
            source=file_info.get("source", ""),
            vcodec=file_info.get("vcodec", ""),
            acodec=file_info.get("acodec", ""),
            extension=file_info.get("extension", ""),
            duration="",
            empty="",
        )
    except Exception as e:
        new_caption = cap_template
    if blocked_words_raw:
        new_caption = apply_block_words(new_caption, blocked_words_raw)
    if replace_raw:
        replace_pairs = parse_replace_pairs(replace_raw)
        if replace_pairs:
            new_caption = apply_replacements(new_caption, replace_pairs)
    if link_remover_on:
        new_caption = strip_links_only(new_caption)
    if prefix:
        new_caption = f"{prefix}\n{new_caption}".strip()
    if suffix:
        new_caption = f"{new_caption}\n{suffix}".strip()
    if emoji_remover_on:
        new_caption = remove_emojis(new_caption)
    new_caption = new_caption.strip()
    if "<" in new_caption and ">" in new_caption:
        new_caption = sanitize_caption_html(new_caption)
    reply_markup = None
    if url_buttons:
        reply_markup = InlineKeyboardMarkup([
            [InlineKeyboardButton(btn["text"], url=btn["url"]) for btn in row]
            for row in url_buttons
        ])
    await enqueue_caption({
        "chat_id": msg.chat.id,
        "message_id": msg.id,
        "caption": new_caption,
        "url_buttons": url_buttons or [],
        "user_id": msg.from_user.id if msg.from_user else None
    })

# ═══════════════════════════════════════════════════════════════════
#  Smart File Name Helper  –  complete rewrite for movie/series/anime
# ═══════════════════════════════════════════════════════════════════

# ── Language tables ──────────────────────────────────────────────
LANG_LIST = [
    "Hindi", "English", "Tamil", "Telugu", "Malayalam", "Kannada",
    "Marathi", "Gujarati", "Bengali", "Punjabi", "Urdu",
    "Japanese", "Korean", "Chinese", "Spanish", "French", "German",
    "Italian", "Russian", "Arabic", "Dutch", "Portuguese", "Turkish",
]

LANG_CODE_MAP = {
    "hin": "Hindi", "eng": "English", "tam": "Tamil", "tel": "Telugu",
    "mal": "Malayalam", "kan": "Kannada", "mar": "Marathi", "guj": "Gujarati",
    "ben": "Bengali", "pan": "Punjabi", "urd": "Urdu",
    "jpn": "Japanese", "kor": "Korean", "chi": "Chinese",
    "spa": "Spanish", "fre": "French", "ger": "German",
    "ita": "Italian", "rus": "Russian", "ara": "Arabic",
    "dut": "Dutch",   "por": "Portuguese", "tur": "Turkish",
}

# Map lowercase full-word language → canonical name (for full-word matching in filenames)
_LANG_LOWER_MAP = {lang.lower(): lang for lang in LANG_LIST}

# ── Codec / Quality / Source tables ──────────────────────────────
QUALITY_LIST = ["2160p", "4K", "UHD", "1080p", "720p", "480p", "360p", "240p"]
SOURCE_LIST  = [
    "WEB-DL", "WEBRip", "BluRay", "Blu-Ray", "BDRip",
    "HDRip", "DVDRip", "HDTV", "AMZN", "NF", "DSNP",
    "HMAX", "ATVP", "PCOK", "SonyLIV", "ZEE5", "Hotstar", "JioCinema",
]
VIDEO_CODEC_LIST = ["HEVC", "x265", "x264", "AVC", "AV1", "H.264", "H.265", "VP9"]
AUDIO_CODEC_LIST = [
    "DD5.1", "DD+5.1", "DD+", "DDP5.1", "DDP", "Atmos",
    "DTS-HD", "DTS-X", "DTS", "TrueHD",
    "AAC5.1", "AAC", "AC3", "MP3", "FLAC", "OPUS",
]
EXT_LIST = ["mkv", "mp4", "avi", "webm", "mov", "m4v", "ts"]

# ── Sub / ESub patterns ──────────────────────────────────────────
ESUB_RE = re.compile(r'\bE\.?Subs?\b', re.I)
HSUB_RE = re.compile(r'\bH\.?Subs?\b', re.I)
MSUB_RE = re.compile(r'\bM\.?Subs?\b', re.I)
SUB_RE  = re.compile(r'\b(?:Subs?|Subtitles?)\b', re.I)

# ── "subtitle <lang>" full-word patterns ─────────────────────────
# Matches: "subtitle english", "subtitles hindi", "sub english", etc.
_SUB_LANG_RE = re.compile(
    r'\bsubtitles?\s+(' + '|'.join(re.escape(l) for l in LANG_LIST) + r')\b',
    re.I
)
# Also: "english subtitle/sub", "hindi sub" etc.
_LANG_SUB_RE = re.compile(
    r'\b(' + '|'.join(re.escape(l) for l in LANG_LIST) + r')\s+subtitles?\b',
    re.I
)

# ── Content-type keywords ─────────────────────────────────────────
_SERIES_RE   = re.compile(r'\b(?:Web\s*Series|TV\s*Series|Mini\s*Series|OTT\s*Series)\b', re.I)
_MOVIE_RE    = re.compile(r'\b(?:South\s*Movie|UnCut\s*Movie|Hindi\s*Movie|Movie)\b', re.I)
_UNCUT_RE    = re.compile(r'\bUnCut\b', re.I)
_SOUTH_RE    = re.compile(r'\bSouth\b', re.I)
_DUAL_RE     = re.compile(r'\bDual\s*Audio\b', re.I)
_MULTI_RE    = re.compile(r'\bMulti\s*(?:Audio|Lang(?:uage)?)?\b', re.I)
_COMPLETED_RE= re.compile(r'\bCompleted\b', re.I)
_HD_RE       = re.compile(r'\bHD\b', re.I)

# ── Helpers ───────────────────────────────────────────────────────
def _norm(text: str) -> str:
    return re.sub(r'\s+', ' ', text.lower()).strip()

def _clean_raw(text: str) -> str:
    """Replace dots/underscores with spaces for easier regex matching."""
    return re.sub(r'[._]', ' ', text)

# ── IMDB enrichment (best-effort, never blocks) ───────────────────
def imdb_enrich_title(title: str, year: str):
    if not title or not year or len(title) < 4:
        return title, year
    try:
        results = ia.search_movie(title)
        for r in results[:5]:
            if str(r.get("year")) == year:
                return r.get("title", title), year
    except Exception:
        pass
    return title, year

# ── Title + Year ─────────────────────────────────────────────────
def extract_title_year(raw: str):
    """
    Extract clean title and year from filename/caption.
    Handles movies, series (S01 E02), anime, etc.
    """
    text = _clean_raw(raw)

    # Find first plausible year (1900-2099)
    year_m = re.search(r'\b((?:19|20)\d{2})\b', text)
    year   = year_m.group(1) if year_m else ""
    cut    = year_m.start()  if year_m else len(text)

    title_raw = text[:cut]

    # Remove season/episode markers from the title portion
    title_raw = re.sub(r'\s*\bS(?:eason)?\s*\d{1,3}\b.*$', '', title_raw, flags=re.I)
    title_raw = re.sub(r'\s*\bEp?(?:isode)?\.?\s*\d{1,3}\b.*$', '', title_raw, flags=re.I)

    # Noise words that often bleed into the title area
    _TITLE_NOISE = (
        r'480p|720p|1080p|2160p|4k|uhd|web[\s\-]?dl|webrip|bluray|blu[\s\-]ray|bdrip|'
        r'hdrip|dvdrip|hdtv|amzn|nf|dsnp|hmax|'
        r'x264|x265|hevc|avc|av1|h\.264|h\.265|'
        r'esub|hsub|msub|sub|subtitle|'
        r'dual[\s]?audio|multi[\s]?audio|'
        r'uncut|south|bollywood|hollywood|'
        r'hindi|english|tamil|telugu|malayalam|kannada|punjabi|bengali|marathi|urdu|'
        r'japanese|korean|chinese|spanish|french|german|italian|russian'
    )
    title_raw = re.sub(rf'\b(?:{_TITLE_NOISE})\b', '', title_raw, flags=re.I)

    # Remove bracketed alternate titles / release group noise like "(Poojai)" "(Clear)"
    title_raw = re.sub(r'\([^)]{0,40}\)', '', title_raw)
    title_raw = re.sub(r'\[[^\]]{0,40}\]', '', title_raw)

    # Strip trailing punctuation / separators
    title_raw = re.sub(r'[\[\]()\-–:,|{}\s]+$', '', title_raw.strip())
    title_raw = re.sub(r'^[\[\]()\-–:,|{}\s]+', '', title_raw.strip())

    title = re.sub(r'\s{2,}', ' ', title_raw).strip()
    # Title-case but preserve short connectors
    title = title.title() if title else ""
    return title, year

# ── Season / Episode ─────────────────────────────────────────────
def extract_season_episode(text: str):
    """
    Returns (season_str, episode_str) as display strings.
    Handles:
      S01 E02 / S01E07 / Season 1 Episode 2
      S01 (Ep.01-09) / Ep.01-05 (range)
      Single Ep.07 / EP07 / E07
    """
    t = re.sub(r'[._]', ' ', text)
    season  = ""
    episode = ""

    # Season
    s_m = re.search(r'\bS(?:eason)?\s*0*(\d{1,3})\b', t, re.I)
    if s_m:
        season = f"S{int(s_m.group(1)):02d}"

    # Episode range: Ep.01-09 / (Ep 1-9) / E01-E09
    r_m = re.search(
        r'\bEp?(?:isode)?\.?\s*0*(\d{1,3})\s*[-–to]+\s*(?:Ep?\.?\s*)?0*(\d{1,3})\b',
        t, re.I
    )
    if r_m:
        episode = f"Ep.{int(r_m.group(1)):02d}-{int(r_m.group(2)):02d}"
        return season, episode

    # Single episode
    e_m = re.search(r'\bEp?(?:isode)?\.?\s*0*(\d{1,3})\b', t, re.I)
    if e_m:
        episode = f"E{int(e_m.group(1)):02d}"

    return season, episode

# ── Language vs Subtitle separation ──────────────────────────────
def _extract_subtitle_languages(text: str) -> set:
    """
    Return set of language names that appear explicitly as subtitle references.
    Patterns detected:
      • "subtitle english" / "subtitles hindi"
      • "english subtitle" / "hindi subs"
      • ESub / HSub / MSub (generic, no specific language)
    """
    sub_langs: set = set()
    for m in _SUB_LANG_RE.finditer(text):
        sub_langs.add(m.group(1).title())
    for m in _LANG_SUB_RE.finditer(text):
        sub_langs.add(m.group(1).title())
    return sub_langs

def extract_audio_languages(text: str) -> list:
    """
    Extract ALL languages present in the filename/caption.

    Audio languages and subtitle languages are kept SEPARATE in the output —
    subtitles get their own tag via extract_subtitle_tag(). This function
    always returns every language it finds regardless of whether any of them
    also appear in a subtitle reference, because a file can have both audio
    AND subtitles in the same language (e.g. English audio + English ESub).

    Handles:
      • Full words:  Hindi, English, Tamil …
      • 3-letter codes: Hin, Eng, Tam …
      • Dual-audio blocks: (Hindi + Telugu), {Hindi (Clear) + Telugu}
      • Standalone: "Hindi 480p", "ironman 2003 English hindi 480p subtitle english"
    """
    found: list = []

    # Match full language names (case-insensitive, whole-word)
    for lang in LANG_LIST:
        if re.search(rf'\b{re.escape(lang)}\b', text, re.I):
            found.append(lang)

    # Match 3-letter codes only if no full names found yet
    if not found:
        for code, lang in LANG_CODE_MAP.items():
            if re.search(rf'\b{code}\b', text, re.I) and lang not in found:
                found.append(lang)

    return list(dict.fromkeys(found))  # preserve order, dedupe

def extract_subtitle_tag(text: str) -> str:
    """
    Returns a subtitle tag from the filename/caption.
    Priority: ESub > HSub > MSub > Sub

    Also handles full-word patterns like "subtitle english" → ESub,
    and language-prefixed: "english sub" → ESub.
    """
    # Check explicit ESub/HSub/MSub/Sub tags first
    if ESUB_RE.search(text):
        return "ESub"
    if HSUB_RE.search(text):
        return "HSub"
    if MSUB_RE.search(text):
        return "MSub"

    # Check if any language is explicitly marked as a subtitle
    sub_langs = _extract_subtitle_languages(text)
    if sub_langs:
        # Determine tag type from explicit sub language context
        # "subtitle english" → ESub (English sub = External sub convention)
        # We just return "ESub" as the generic external subtitle marker
        return "ESub"

    if SUB_RE.search(text):
        return "MSub"
    return ""

# ── Individual field extractors ───────────────────────────────────
def extract_quality(text: str) -> str:
    """Returns resolution/quality tag like 1080p, 720p, 4K …"""
    for q in QUALITY_LIST:
        if re.search(rf'\b{re.escape(q)}\b', text, re.I):
            return q
    return ""

def extract_resolution(text: str) -> str:
    """Alias for extract_quality – for {resolution} placeholder."""
    return extract_quality(text)

def extract_source(text: str) -> str:
    """Returns source tag like WEB-DL, BluRay, HDRip …"""
    for s in SOURCE_LIST:
        if re.search(rf'\b{re.escape(s)}\b', text, re.I):
            return s
    return ""

def extract_video_codec(text: str) -> str:
    """Returns video codec like HEVC, x264, AV1 …"""
    for c in VIDEO_CODEC_LIST:
        if re.search(rf'\b{re.escape(c)}\b', text, re.I):
            return c
    return ""

def extract_audio_codec(text: str) -> str:
    """
    Extracts audio codec, including bitrate-suffixed patterns.
    Examples: DD5.1-224Kbps → DD5.1,  DDP5.1 → DDP5.1
    """
    m = re.search(
        r'\b(DD\+?5\.1|DDP5\.1|DD\+|DDP|DTS-HD|DTS-X|DTS|TrueHD|Atmos|'
        r'AAC5\.1|AAC|AC3|MP3|FLAC|OPUS)(?:[- ]\d+[Kk]bps)?\b',
        text, re.I
    )
    if m:
        return m.group(1).upper()
    return ""

def extract_extension(text: str) -> str:
    """Returns file extension like mkv, mp4, avi …"""
    m = re.search(r'\.(mkv|mp4|avi|webm|mov|m4v|ts)\b', text, re.I)
    if m:
        return m.group(1).lower()
    for e in EXT_LIST:
        if re.search(rf'\b{e}\b', text, re.I):
            return e.lower()
    return ""

# ── Dual-audio / multi label helper ──────────────────────────────
def _format_audio_label(langs: list, text: str) -> str:
    """
    Build the audio language string, preserving DD5.1-style annotations.
    Examples:
      ["Hindi", "Telugu"]  + has DD5.1  → "Hindi DD5.1-224Kbps + Telugu"
      ["Hindi", "Tamil"]   + no codec   → "Hindi + Tamil"
    """
    if not langs:
        return ""

    # Check if there is a bitrate-annotated codec in the raw text
    # e.g. "DD5.1-224Kbps"
    bitrate_m = re.search(
        r'\b(DD\+?5\.1|DDP5\.1|DD\+|DDP|DTS-HD|DTS-X|DTS|TrueHD|Atmos|AAC5\.1|AAC|AC3)'
        r'(?:[- ](\d+[Kk]bps))?\b',
        text, re.I
    )
    acodec_str = ""
    if bitrate_m:
        codec_part  = bitrate_m.group(1).upper()
        bitrate_part = bitrate_m.group(2)
        acodec_str  = f" {codec_part}-{bitrate_part}" if bitrate_part else f" {codec_part}"

    if len(langs) == 1:
        return f"{langs[0]}{acodec_str}"

    # First language gets the codec annotation (convention from examples)
    parts = [f"{langs[0]}{acodec_str}"] + langs[1:]
    return " + ".join(parts)

# ── Media-type detection ──────────────────────────────────────────
def detect_media_type(text: str) -> str:
    """Detect whether content is a movie, series, or anime."""
    if re.search(r'\bS\d{1,3}\s*(?:E\d|Ep)', text, re.I):
        return "series"
    if re.search(r'\bS\d{1,3}\b', text, re.I) and re.search(r'\bE\d{1,3}\b', text, re.I):
        return "series"
    if re.search(r'\bEp?\.?\s*\d{1,3}', text, re.I):
        return "series"
    if re.search(r'\bAnime\b', text, re.I):
        return "anime"
    return "movie"

# ── Master parser ─────────────────────────────────────────────────
def parse_file_info(filename: str, caption: str) -> dict:
    """
    Parse ALL metadata from filename + caption combined.
    Returns a dict with all individual fields for template placeholders.
    """
    raw = f"{filename} {caption}"

    title, year    = extract_title_year(raw)
    title, year    = imdb_enrich_title(title, year)
    season, episode = extract_season_episode(raw)
    audio_langs    = extract_audio_languages(raw)
    subtitle       = extract_subtitle_tag(raw)
    quality        = extract_quality(raw)
    source         = extract_source(raw)
    vcodec         = extract_video_codec(raw)
    acodec         = extract_audio_codec(raw)
    ext            = extract_extension(raw)

    audio_str = _format_audio_label(audio_langs, raw) if audio_langs else ""

    return {
        "title":      title,
        "year":       year,
        "season":     season,
        "episode":    episode,
        "audio":      audio_str,
        "subtitle":   subtitle,
        "quality":    quality,
        "resolution": quality,   # alias
        "source":     source,
        "vcodec":     vcodec,
        "acodec":     acodec,
        "extension":  ext,
    }

# ── Smart filename builder ────────────────────────────────────────
def build_smart_filename(filename: str, caption: str) -> str:
    """
    Build a clean, perfectly structured display caption from filename + caption.

    Output format (mirrors the given examples):
      Title (Year) (Audio) Dual Audio [UnCut] [South] [Media-Type Label] [HD] Resolution [ESub].ext

    Examples:
      Court - State Vs A Nobody (2025) (Hindi DD5.1-224Kbps + Telugu) Dual Audio UnCut South Movie HD 1080p ESub.mkv
      Sapne Vs Everyone S01 (Ep.01-05) (2023) Hindi Completed Web Series HEVC 480p ESub.mkv
      Campus Beats S06 E07 (2026) Hindi Web Series HEVC 480p ESub.mkv
      Loki S01 E02 Hindi Web Series HEVC 480p ESub.mkv
    """
    raw  = f"{filename} {caption}"
    info = parse_file_info(filename, caption)

    media_type = detect_media_type(raw)

    parts: list = []

    # 1. Title
    if info["title"]:
        parts.append(info["title"])

    # 2. Season + Episode  (for series/anime)
    if info["season"] or info["episode"]:
        se = f"{info['season']} {info['episode']}".strip()
        parts.append(se)

    # 3. Year in parentheses
    if info["year"]:
        parts.append(f"({info['year']})")

    # 4. Audio/language block
    #    If multiple languages → wrap in parentheses like the examples
    audio_langs = extract_audio_languages(raw)
    if audio_langs:
        audio_label = _format_audio_label(audio_langs, raw)
        if len(audio_langs) > 1:
            parts.append(f"({audio_label})")
        else:
            parts.append(audio_label)

    # 5. Dual Audio / Multi Audio label
    if _DUAL_RE.search(raw) and len(audio_langs) > 1:
        parts.append("Dual Audio")
    elif _MULTI_RE.search(raw) and len(audio_langs) > 2:
        parts.append("Multi Audio")

    # 6. UnCut flag
    if _UNCUT_RE.search(raw):
        parts.append("UnCut")

    # 7. South flag (South Movie)
    if _SOUTH_RE.search(raw) and media_type == "movie":
        parts.append("South")

    # 8. Completed flag (for web series)
    completed = bool(_COMPLETED_RE.search(raw))

    # 9. Media-type label
    if media_type == "series":
        if _SERIES_RE.search(raw):
            # Keep the exact label used in source (Web Series / TV Series …)
            m = _SERIES_RE.search(raw)
            lbl = re.sub(r'\s+', ' ', m.group(0).strip().title())
            if completed:
                parts.append(f"Completed {lbl}")
            else:
                parts.append(lbl)
        else:
            if completed:
                parts.append("Completed Web Series")
            else:
                parts.append("Web Series")
    elif media_type == "anime":
        parts.append("Anime")
    else:  # movie
        if _SOUTH_RE.search(raw):
            parts.append("Movie")   # "South Movie" already built as "South … Movie"
        elif _MOVIE_RE.search(raw):
            m = _MOVIE_RE.search(raw)
            parts.append(re.sub(r'\s+', ' ', m.group(0).strip().title()))
        # else: bare movie – no label needed

    # 10. HD flag
    if _HD_RE.search(raw):
        parts.append("HD")

    # 11. Video codec (HEVC, x264 …)
    if info["vcodec"]:
        parts.append(info["vcodec"])

    # 12. Resolution / Quality
    if info["quality"]:
        parts.append(info["quality"])

    # 13. Subtitle tag
    if info["subtitle"]:
        parts.append(info["subtitle"])

    # 14. Extension
    if info["extension"]:
        parts.append(f".{info['extension']}")

    # Join: extension is glued without space, everything else space-separated
    result = ""
    for p in parts:
        if p.startswith("."):
            result = result.rstrip() + p
        else:
            result = f"{result} {p}" if result else p

    return result.strip()


# ---------------- Helper functions ----------------
def _status_name(member_obj):
    status = getattr(member_obj, "status", "")
    try:
        if hasattr(status, "value"):
            return str(status.value).lower()
    except Exception:
        pass
    try:
        return str(status).lower()
    except Exception:
        return ""

def _is_admin_member(member_obj) -> bool:
    if not member_obj:
        return False
    status = getattr(member_obj, "status", "")
    try:
        if hasattr(status, "value"):
            status = str(status.value)
    except Exception:
        status = str(status)
    return str(status).lower() in ("administrator", "creator", "owner")

def get_size(size: int) -> str:
    units = ["Bytes", "KB", "MB", "GB", "TB"]
    i = 0
    while size >= 1024.0 and i < len(units) - 1:
        size /= 1024.0
        i += 1
    return "%.2f %s" % (size, units[i])
    
def extract_year(default_caption: str) -> Optional[str]:
    match = re.search(r'\b(19\d{2}|20\d{2})\b', default_caption or "")
    return match.group(1) if match else None
URL_RE = re.compile(
    r"(https?://[^\s]+|www\.[^\s]+|t\.me/[^\s/]+(?:/[^\s]+)?)",
    flags=re.IGNORECASE
)
MENTION_RE = re.compile(r'@\w+', flags=re.IGNORECASE)
MD_LINK_RE = re.compile(r'\[([^\]]+)\]\((?:https?:\/\/[^\)]+|tg:\/\/[^\)]+)\)', flags=re.IGNORECASE)
HTML_A_RE = re.compile(r'<a\s+[^>]*href=["\'](?:https?:\/\/|tg:\/)[^"\']+["\'][^>]*>(.*?)</a>', flags=re.IGNORECASE)
TG_USER_LINK_RE = re.compile(r'\[([^\]]+)\]\(tg:\/\/user\?id=\d+\)', flags=re.IGNORECASE)

EMOJI_LIST = [
    "😀","😃","😄","😁","😆","😅","😂","🤣","😊","😇","🙂","🙃","😉","😌","😍","🥰","😘","😗","😙","😚","😋","😜","😝","😛","🔐","🔒","🔓","🗝","🪪","🧾","📜","📝","📊","📈","📉","🗒","🗓","📅","⏰","⏳",
    "🤪","🤨","🧐","🤓","😎","🥸","🤩","🥳","😏","😒","😞","😔","😟","😕","🙁","☹️","😣","😖","😫","😩","🥺","😢","😭","💡","🔦","🕯","🧯","🛠","⚙️","🔧","🔩","🪛","🧲","📡","🛰","🖥","💻","📱",
    "😤","😠","😡","🤬","🤯","😳","🥵","🥶","😱","😨","😰","😥","😓","🤗","🤔","🫣","🤭","🫢","🤫","🤥","😶","😐","🌈","☀️","🌤","⛅","🌥","☁️","🌦","🌧","⛈","🌩","🌨","❄️","☃️","⛄","🌬","💨",
    "😑","😬","🙄","😯","😦","😧","😮","😲","🥱","😴","🤤","😪","😵","🤐","🥴","🤢","🤮","🤧","😷","🤒","🤕","🌍","🌎","🌏","🗺","🏔","⛰","🌋","🏕","🏖","🏜","🏝","🏞","🌅","🌄","🌠","🌌",
    "👍","👎","👌","✌️","🤞","🤟","🤘","🤙","🫶","👏","🙌","👐","🤲","🙏","✋","🖐","🖖","👋","🤚","🫱","🫲","🐙","🦑","🦀","🐡","🐠","🐬","🐳","🐋","🦈","🐊","🐅","🐆","🦓","🦍","🦧",
    "✍️","💪","🦾","🦿","❤️","🧡","💛","💚","💙","💜","🤎","🖤","🤍","💔","❣️","💕","💞","💓","💗","💖","💘","💝","🐔","🐧","🐦","🐤","🐺","🐗","🐴","🦄","🐝","🪲","🦋","🐌","🐞","🐢","🐍",
    "💯","✔️","✅","❌","❎","⚠️","🚫","⭕","❗","❓","🔥","💥","✨","🌟","⚡","💫","🎉","🎊","🎬","🎞","📽","🎥","📺","📼","🎧","🎵","🎶","🎼","🛑","🏁","🚦","🚥","🛣","🛤","🚧","🛞","🚲","🛵","🏍","🚗","🚙","🚕","🚌","🚎",
    "🍿","📀","💿","📌","📍","📎","📂","📁","📄","🗂","🗃","🔔","🔕","📢","📣","📯","👑","🎯","🏆","🥇","🥈","🥉","🎖","🏅","🎁","🎈","🎀","🪄","🎨","🧩","♟",
    "🚀","🛸","🚨","🧨","⬆️","⬇️","➡️","⬅️","🔁","🔄","⏩","⏪","⏭","⏮","👀","👁️","🧠","🫀","🫁","🦷","🦴","👅","👄","🚓","🚑","🚒","🚐","🚚","🚛","🚜","🚢","🛳","⛴","🛥","✈️","🛫","🛬","🪂"
    "🫠","🫡","🫥","🫨","🫤","🥹","🫶🏻","🫶🏽","🫶🏿","🤝","🤜","🤛","🫰","🫵","🫳","🫴","🐶","🐱","🐭","🐹","🐰","🦊","🐻","🐼","🐨","🐯","🦁","🐮","🐷","🐸","🐵",
    "🏳️","🏴","🏁","🚩","🏳️‍🌈","🏳️‍⚧️","🏴‍☠️",
]

def remove_emojis(text: str) -> str:
    if not text:
        return text
    for emo in EMOJI_LIST:
        text = text.replace(emo, "")
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

async def format_dump_skip_list(client: Client) -> str:
    items = await get_all_dump_skip_channels()
    if not items:
        return "📭 <b>No dump-skip channels</b>"
    lines = ["📌 <b>Dump-skip channels:</b>\n"]
    for doc in items:
        cid = doc["chnl_id"]
        try:
            chat = await client.get_chat(cid)
            title = chat.title
        except:
            title = "Unknown / Bot not in channel"
        lines.append(f"• <b>{title}</b>\n  <code>{cid}</code>")
    return "\n".join(lines)

def normalize_series_name(name: str) -> str:
    if not name:
        return ""
    name = re.sub(r'\.(mkv|mp4|avi|webm)$', '', name, flags=re.IGNORECASE)
    name = re.sub(r'[._\-]+', ' ', name)
    name = re.sub(r'\s+', ' ', name)
    return name.strip().title()

def strip_links_and_mentions_keep_text(text: str) -> str:
    if not text:
        return text
    text = MD_LINK_RE.sub(r'\1', text)
    text = TG_USER_LINK_RE.sub(r'\1', text)
    text = URL_RE.sub("", text)
    text = MENTION_RE.sub("", text)
    text = re.sub(r'[ 	]+', ' ', text) 
    return text

def strip_links_only(text: str) -> str:
    if not text:
        return text
    text = MD_LINK_RE.sub(r'\1', text)
    text = TG_USER_LINK_RE.sub(r'\1', text)
    text = HTML_A_RE.sub(r'\1', text)
    text = URL_RE.sub("", text)
    text = MENTION_RE.sub("", text)
    text = re.sub(r'\(\s*\)', '', text)   # ()
    text = re.sub(r'\[\s*\]', '', text)   # []
    text = re.sub(r'\{\s*\}', '', text)   # {}
    text = re.sub(r'\s+', ' ', text)
    return text.strip()

def apply_block_words(caption_html: str, raw_blocked: str) -> str:
    if not caption_html or not raw_blocked:
        return caption_html
    plain = caption_html
    blocked_items = [
        item.strip()
        for item in re.split(r"[,\n]+", raw_blocked)
        if item.strip()
    ]
    for item in blocked_items:
        plain = plain.replace(item, "")
    plain = "\n".join(line.rstrip() for line in plain.splitlines())
    plain = "\n".join(line for line in plain.splitlines() if line.strip())
    plain = re.sub(r"[ \t]{2,}", " ", plain)
    return plain.strip()

def parse_replace_pairs(raw):
    if not raw:
        return []
    # Convert list -> string (joined by commas)
    if isinstance(raw, list):
        raw = ','.join(map(str, raw))
    elif not isinstance(raw, str):
        raw = str(raw)
    raw = raw.replace('\n', ',')
    items = [p.strip() for p in raw.split(',') if p.strip()]
    pairs = []
    for item in items:
        parts = item.split(None, 1)
        if len(parts) == 2:
            pairs.append((parts[0], parts[1]))
    return pairs

def apply_replacements(text: str, pairs: List[Tuple[str, str]]) -> str:
    if not pairs or not text:
        return text
    new_text = text
    for old, new in pairs:
        if not old:
            continue
        try:
            pattern = re.compile(re.escape(old), flags=re.IGNORECASE)
            new_text = pattern.sub(new, new_text)
            if re.search(re.escape(old), new_text, flags=re.IGNORECASE):
                new_text = re.sub(re.escape(old), new, new_text, flags=re.IGNORECASE)
        except re.error:
            new_text = new_text.replace(old, new)
    new_text = re.sub(r'[ 	]+', ' ', new_text).strip()
    return new_text

# ---------------- Function Handler ----------------
@Client.on_message(filters.private)
async def capture_user_input(client, message):
    """
    Single handler for all user text input collected via bot_data sessions.

    BUG FIXES:
    1. Each session type is checked independently using its OWN key so a stale
       caption_set entry never intercepts a block_words_set input.
    2. block_words now REPLACES (not appends) so re-sending words doesn't
       accidentally treat them as a caption.
    3. Only the session that the user is ACTIVELY in is consumed — all other
       session keys for this user are cleared when any session starts
       (done in CallbackQuery.py) so cross-bleed is impossible.
    """
    user_id = message.from_user.id

    # Build the set of users who have an active session
    active_users = set()
    for key in ("caption_set", "block_words_set", "replace_words_set",
                "prefix_set", "suffix_set", "url_set"):
        active_users.update(bot_data.get(key, {}).keys())
    active_users.update(FF_SESSIONS.keys())
    if user_id not in active_users:
        return

    text = (
        message.text.html if message.text else
        message.caption.html if message.caption else
        ""
    )

    # ---------- CAPTION ----------
    # Use a separate text check inside each branch so an empty caption is still
    # allowed for block_words / FF steps that use raw message.text
    if user_id in bot_data.get("caption_set", {}):
        if not text.strip():
            return
        session      = bot_data["caption_set"].pop(user_id)
        channel_id   = session["channel_id"]
        instr_msg_id = session["instr_msg_id"]
        await updateCap(channel_id, text)
        try:
            await client.delete_messages(user_id, message.id)
        except Exception:
            pass
        await client.edit_message_text(
            chat_id=user_id,
            message_id=instr_msg_id,
            text="✅ Caption updated successfully!",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("↩ Back", callback_data=f"back_to_captionmenu_{channel_id}")]]
            ),
        )
        return

    # ---------- BLOCK WORDS ----------
    if user_id in bot_data.get("block_words_set", {}):
        if not text.strip():
            return
        session      = bot_data["block_words_set"].pop(user_id)
        channel_id   = session["channel_id"]
        instr_msg_id = session["instr_msg_id"]
        # REPLACE — not append — so re-sending words doesn't stack
        await set_block_words(channel_id, text.strip())
        try:
            await client.delete_messages(user_id, message.id)
        except Exception:
            pass
        await client.edit_message_text(
            chat_id=user_id,
            message_id=instr_msg_id,
            text="✅ Blocked words updated!",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("↩ Back", callback_data=f"back_to_blockwords_{channel_id}")]]
            ),
        )
        return

    # ---------- REPLACE WORDS ----------
    if user_id in bot_data.get("replace_words_set", {}):
        if not text.strip():
            return
        session      = bot_data["replace_words_set"].pop(user_id)
        channel_id   = session["channel_id"]
        instr_msg_id = session["instr_msg_id"]
        # REPLACE — not append
        await set_replace_words(channel_id, text.strip())
        try:
            await client.delete_messages(user_id, message.id)
        except Exception:
            pass
        await client.edit_message_text(
            chat_id=user_id,
            message_id=instr_msg_id,
            text="✅ Replace words updated!",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("↩ Back", callback_data=f"back_to_replace_{channel_id}")]]
            ),
        )
        return

    # ---------- PREFIX ----------
    if user_id in bot_data.get("prefix_set", {}):
        if not text.strip():
            return
        session      = bot_data["prefix_set"].pop(user_id)
        channel_id   = session["channel_id"]
        instr_msg_id = session["instr_msg_id"]
        # REPLACE — not append
        await set_prefix(channel_id, text.strip())
        try:
            await client.delete_messages(user_id, message.id)
        except Exception:
            pass
        await client.edit_message_text(
            chat_id=user_id,
            message_id=instr_msg_id,
            text="✅ Prefix updated!",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("↩ Back", callback_data=f"back_to_suffixprefix_{channel_id}")]]
            ),
        )
        return

    # ---------- SUFFIX ----------
    if user_id in bot_data.get("suffix_set", {}):
        if not text.strip():
            return
        session      = bot_data["suffix_set"].pop(user_id)
        channel_id   = session["channel_id"]
        instr_msg_id = session["instr_msg_id"]
        # REPLACE — not append
        await set_suffix(channel_id, text.strip())
        try:
            await client.delete_messages(user_id, message.id)
        except Exception:
            pass
        await client.edit_message_text(
            chat_id=user_id,
            message_id=instr_msg_id,
            text="✅ Suffix updated!",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("↩ Back", callback_data=f"back_to_suffixprefix_{channel_id}")]]
            ),
        )
        return

    # ---------- URL BUTTONS ----------
    if user_id in bot_data.get("url_set", {}):
        if not text.strip():
            return
        session      = bot_data["url_set"].pop(user_id)
        channel_id   = session["channel_id"]
        instr_msg_id = session["instr_msg_id"]
        rows  = []
        lines = text.strip().splitlines()
        for line in lines:
            row   = []
            parts = [p.strip() for p in line.split("|") if p.strip()]
            for part in parts:
                matched = re.findall(r'"([^"]+)"', part)
                if len(matched) == 2:
                    row.append({"text": matched[0], "url": matched[1]})
            if row:
                rows.append(row)
        if not rows:
            # Put session back so user can try again without re-navigating
            bot_data.setdefault("url_set", {})[user_id] = session
            await message.reply_text("❌ Invalid format. Please try again.")
            return
        await set_url_buttons(channel_id, rows)
        try:
            await client.delete_messages(user_id, message.id)
        except Exception:
            pass
        await client.edit_message_text(
            chat_id=user_id,
            message_id=instr_msg_id,
            text="✅ URL buttons updated successfully!",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("↩ Back", callback_data=f"seturl_{channel_id}")]]
            ),
        )
        return

    # ================= FILE FORWARD SKIP HANDLER =================
    if user_id in FF_SESSIONS:
        session = FF_SESSIONS[user_id]
        if session.get("expires") and session["expires"] < time.time():
            FF_SESSIONS.pop(user_id, None)
            await message.reply_text("⏰ Session expired.\nStart again using /file_forward")
            return
        if session.get("step") == "skip":
            raw    = (message.text or "").strip()
            parsed = parse_forward_input(raw)

            if parsed.get("error"):
                await message.reply_text(parsed["error"])
                return

            skip_id     = parsed["skip_id"]
            end_id      = parsed["end_id"]
            src_hint    = parsed["src_hint"]
            src_channel = session["source"]

            if src_hint is not None and src_hint != src_channel:
                await message.reply_text(
                    "❌ <b>Wrong channel!</b>\n\n"
                    "The message link you sent does not belong to the selected source channel.\n"
                    "Please send a link or ID from the correct source channel."
                )
                return

            if skip_id > 0:
                valid = await validate_msg_in_channel(client, src_channel, skip_id)
                if not valid:
                    await message.reply_text(
                        "❌ <b>Message not found!</b>\n\n"
                        "The start message ID/link does not exist in the source channel.\n"
                        "Please check and try again."
                    )
                    return

            if end_id is not None:
                valid_end = await validate_msg_in_channel(client, src_channel, end_id)
                if not valid_end:
                    await message.reply_text(
                        "❌ <b>End message not found!</b>\n\n"
                        "The end message ID/link does not exist in the source channel.\n"
                        "Please check and try again."
                    )
                    return

            session["skip"]   = skip_id
            session["end_id"] = end_id
            session["step"]   = "queue"
            try:
                await message.delete()
            except Exception:
                pass
            try:
                await client.delete_messages(session["chat_id"], session["msg_id"])
            except Exception:
                pass
            progress_msg = await client.send_message(
                session["chat_id"],
                "🚚 Preparing forwarding…"
            )
            session["msg_id"] = progress_msg.id
            await enqueue_forward_jobs(client, user_id)
            return
