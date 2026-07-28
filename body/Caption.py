import sys, time, os, re, asyncio, logging
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

logger = logging.getLogger("captionbot.caption")

_ia = None

def _get_ia():
    """Lazily create the IMDb() client on first use.

    Creating IMDb() at import time makes cinemagoer build its default
    S3/SQLite-backed access system, which calls sqlalchemy.create_engine()
    with a malformed 'sqlite://cinemagoer.db' URL on this
    environment/version combo and raises immediately -> the whole bot
    process crashes on import (exit status 1 right after startup, before
    it even connects to Telegram). Using the "http" backend and building
    it lazily avoids touching sqlalchemy entirely and keeps the crash
    contained (best-effort, silent on any error) instead of taking the
    whole bot down.
    """
    global _ia
    if _ia is None:
        _ia = IMDb("http")
    return _ia

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
                    [InlineKeyboardButton("⚙️ Open Settings", callback_data=f"chinfo_{chat.id}")]
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

_PENDING_AUTO_DELETE: dict = {}   # message_id -> asyncio.Task

async def auto_delete_message(msg, delay: int):
    """
    Schedules `msg` for deletion after `delay` seconds. The task is tracked
    by message id in _PENDING_AUTO_DELETE so it can be cancelled (see
    cancel_pending_auto_delete) if the user interacts with the message
    before the timer fires -- e.g. tapping "⚙️ Open Settings" on a
    channel-added notification. Previously this message always vanished
    after a fixed delay even while the user was actively using it to
    configure the channel's caption, which yanked the settings UI away
    mid-setup.
    """
    task = asyncio.current_task()
    if task is not None:
        _PENDING_AUTO_DELETE[msg.id] = task
    try:
        await asyncio.sleep(delay)
        await msg.delete()
    except asyncio.CancelledError:
        pass
    except Exception:
        pass
    finally:
        _PENDING_AUTO_DELETE.pop(msg.id, None)


def cancel_pending_auto_delete(message_id: int):
    """Cancel a scheduled auto-delete for `message_id`, if one is pending."""
    task = _PENDING_AUTO_DELETE.pop(message_id, None)
    if task and not task.done():
        task.cancel()

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

@Client.on_message(filters.private & filters.user(ADMIN) & filters.command("id"))
async def dump_origin_id_cmd(client, message):
    """
    Reply /id to a message forwarded FROM the CP_CH dump channel (or to a
    message inside CP_CH itself) to find out which channel that file
    originally came from — so it can be passed straight to /dump_skip.
    """
    reply = message.reply_to_message
    if not reply:
        return await message.reply_text(
            "❌ <b>Usage:</b> Forward a file from the dump channel "
            "(or open it inside the dump channel) and reply to it with /id.",
            parse_mode=ParseMode.HTML,
        )

    cp_ch_msg_id = None
    if reply.forward_from_chat and reply.forward_from_chat.id == CP_CH:
        cp_ch_msg_id = reply.forward_from_message_id
    elif reply.chat and reply.chat.id == CP_CH:
        cp_ch_msg_id = reply.id

    if not cp_ch_msg_id:
        return await message.reply_text(
            "❌ That message isn't a file forwarded from the dump channel."
        )

    origin = await get_dump_origin(cp_ch_msg_id)
    if not origin:
        return await message.reply_text(
            "❌ No origin info found for this file "
            "(it may predate this tracking feature)."
        )

    origin_channel_id = origin["origin_channel_id"]
    title = await get_channel_title_cached(origin_channel_id)
    skipped = await is_dump_skip(origin_channel_id)

    await message.reply_text(
        "📡 <b>Origin Channel Found</b>\n\n"
        f"📢 <b>Channel:</b> {title}\n"
        f"🆔 <b>Channel ID:</b> <code>{origin_channel_id}</code>\n"
        f"🗂 <b>Dump skip:</b> {'✅ Enabled' if skipped else '❌ Not enabled'}\n\n"
        f"Use <code>/dump_skip {origin_channel_id}</code> to stop dumping files from this channel.",
        parse_mode=ParseMode.HTML,
    )


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
    bot = await client.get_me()
    text = (
        f"👑 <b>Admin Panel — @{bot.username}</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

        "📊 <b>INFO &amp; MONITORING</b>\n"
        "┌ /admin — Show this admin command list\n"
        "├ /stats — Full bot statistics &amp; DB info\n"
        "└ /queue — Live caption &amp; forward queue status\n\n"

        "📢 <b>BROADCAST</b>\n"
        "┌ /broadcast — Broadcast a message to all users\n"
        "└   <i>(reply to any message to broadcast it)</i>\n\n"

        "🗃 <b>DUMP CHANNEL CONTROL</b>\n"
        "┌ /dump_skip <code>-100xxx</code> — Skip dump for a channel\n"
        "└ /remove_dump <code>-100xxx</code> — Remove dump skip for a channel\n\n"

        "🗄 <b>DATABASE</b>\n"
        "└ /reset — ⚠️ Wipe ALL users, channels &amp; settings from DB\n\n"

        "🔄 <b>BOT CONTROL</b>\n"
        "└ /restart — Restart the bot process\n\n"

        "📤 <b>FILE FORWARDING</b>\n"
        "├ /file_forward — Start a user file forward session\n"
        "│   <i>→ Pick source → destination → range (or 0 for all)</i>\n"
        "└ /channels — View all user-added channels &amp; bulk-forward files\n"
        "    <i>→ Shows channel info, who added it, file count,</i>\n"
        "    <i>   forwarding progress, start/continue/stop controls</i>\n\n"

        "⚙️ <b>CHANNEL SETTINGS</b>\n"
        "└ /settings — Manage your added channels\n\n"

        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔐 All commands above are admin-only.\n"
        "📋 /queue is available to all users (shows their own tasks)."
    )
    await message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

@Client.on_message(filters.private & filters.user(ADMIN) & filters.command("stats"))
async def bot_stats(client, message):
    loading = await message.reply_text("📊 Fetching stats…")
    try:
        # ── Queue counts ─────────────────────────────────────────
        cap_pending    = await queue_col.count_documents({"status": "pending"})
        cap_processing = await queue_col.count_documents({"status": "processing"})
        cap_done_today = await queue_col.count_documents({})   # total in queue (incl done)
        fwd_pending    = await forward_queue.count_documents({"status": "pending"})
        fwd_processing = await forward_queue.count_documents({"status": "processing"})

        # ── User / Channel DB counts ──────────────────────────────
        users_count    = await total_user()
        channels_count = await chnl_ids.count_documents({})
        dump_skip_count = await chnl_ids.count_documents({"dump_skip": True})

        # ── Channels where bot is admin ───────────────────────────
        all_channel_docs = await users.aggregate([
            {"$unwind": "$channels"},
            {"$group": {"_id": "$channels.channel_id",
                        "title": {"$first": "$channels.channel_title"}}}
        ]).to_list(length=None)

        admin_channels = []
        for doc in all_channel_docs[:30]:   # cap at 30 to avoid flood
            ch_id = doc["_id"]
            title = doc.get("title", str(ch_id))
            try:
                m = await client.get_chat_member(ch_id, "me")
                if _is_admin_member(m):
                    try:
                        chat = await client.get_chat(ch_id)
                        title = chat.title or title
                        link  = f"https://t.me/{chat.username}" if getattr(chat, "username", None) else None
                        admin_channels.append((title, ch_id, link))
                    except Exception:
                        admin_channels.append((title, ch_id, None))
            except Exception:
                pass

        # ── Bot info ──────────────────────────────────────────────
        bot     = await client.get_me()
        bot_name = bot.first_name
        bot_user = bot.username

        # ── Build text ────────────────────────────────────────────
        text = (
            f"📊 <b>BOT STATISTICS</b>\n"
            f"🤖 <b>{bot_name}</b>  (@{bot_user})\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

            "👥 <b>USERS &amp; CHANNELS</b>\n"
            f"  • Total users     : <code>{users_count}</code>\n"
            f"  • DB channel docs : <code>{channels_count}</code>\n"
            f"  • Dump-skip chans : <code>{dump_skip_count}</code>\n"
            f"  • Bot is admin in : <code>{len(admin_channels)}</code> channel(s)\n\n"

            "📝 <b>CAPTION QUEUE</b>\n"
            f"  • Pending   : <code>{cap_pending}</code>\n"
            f"  • Processing: <code>{cap_processing}</code>\n\n"

            "📦 <b>FORWARD QUEUE</b>\n"
            f"  • Pending   : <code>{fwd_pending}</code>\n"
            f"  • Processing: <code>{fwd_processing}</code>\n\n"
        )

        if admin_channels:
            text += "📡 <b>CHANNELS WHERE BOT IS ADMIN</b>\n"
            for i, (title, ch_id, link) in enumerate(admin_channels[:20], 1):
                if link:
                    text += f"  {i}. <a href='{link}'>{title}</a> <code>({ch_id})</code>\n"
                else:
                    text += f"  {i}. {title} <code>({ch_id})</code>\n"
            if len(admin_channels) > 20:
                text += f"  … and {len(admin_channels) - 20} more\n"
        else:
            text += "📡 <b>CHANNELS WHERE BOT IS ADMIN:</b> None found\n"

        text += "\n━━━━━━━━━━━━━━━━━━━━━━━━"

        await loading.edit_text(text, parse_mode=ParseMode.HTML,
                                 disable_web_page_preview=True)
    except Exception as e:
        await loading.edit_text(f"❌ Error fetching stats:\n<code>{e}</code>",
                                 parse_mode=ParseMode.HTML)

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
    """
    /restart — admin-only.

    Before restarting, immediately requeues every currently "processing"
    caption-edit and file-forward job back to "pending". Since those jobs
    (and any still-"pending" ones) live in MongoDB, not in memory, they
    survive the restart untouched — after the new process comes back up,
    the worker pools simply pick everything back up where the bot left
    off, instead of starting over or waiting up to 5 minutes for
    recover_stuck_jobs()'s normal stuck-job timeout to kick in.
    """
    silicon = await client.send_message(
        chat_id=message.chat.id,
        text="**🔄 𝚂𝚃𝙾𝙿𝙿𝙸𝙽𝙶 𝙰𝙻𝙻 𝚃𝙰𝚂𝙺𝚂 & 𝚁𝙴𝚂𝚃𝙰𝚁𝚃𝙸𝙽𝙶...**",
    )
    try:
        cap_requeued, fwd_requeued = await reset_all_processing_to_pending()
        logger.info(
            f"[RESTART] requeued {cap_requeued} caption job(s) and "
            f"{fwd_requeued} forward job(s) before restart"
        )
    except Exception as e:
        logger.warning(f"[RESTART] failed to requeue in-flight jobs: {e}")
        cap_requeued = fwd_requeued = 0

    await silicon.edit(
        "**✅️ 𝙰𝙻𝙻 𝚃𝙰𝚂𝙺𝚂 𝚂𝚃𝙾𝙿𝙿𝙴𝙳 & 𝚂𝙰𝙵𝙴𝙻𝚈 𝚁𝙴𝚀𝚄𝙴𝚄𝙴𝙳**\n\n"
        f"📋 Caption jobs requeued: `{cap_requeued}`\n"
        f"📦 Forward jobs requeued: `{fwd_requeued}`\n\n"
        "🔄 Restarting now — resuming exactly where I left off..."
    )
    await asyncio.sleep(1)
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
    """
    /queue — admin-only.

    Shows caption-edit and file-forward progress for ALL users (not just
    the caller), plus the global-forward (admin /channels) queue from
    admin_channels.py.
    """
    uid      = message.from_user.id
    is_admin = True

    loading = await message.reply_text("🔄 Fetching queue…")
    try:
        # ════════════════════════════════════════════════════
        #  CAPTION QUEUE  (all users)
        # ════════════════════════════════════════════════════
        cap_pending    = await queue_col.count_documents({"status": "pending"})
        cap_processing = await queue_col.count_documents({"status": "processing"})
        cap_match      = {"status": {"$in": ["pending", "processing"]}}

        cap_pipeline = [
            {"$match": cap_match},
            {"$group": {
                "_id":        "$chat_id",
                "pending":    {"$sum": {"$cond": [{"$eq": ["$status", "pending"]},    1, 0]}},
                "processing": {"$sum": {"$cond": [{"$eq": ["$status", "processing"]}, 1, 0]}},
                "user_id":    {"$first": "$user_id"},
            }},
            {"$sort": {"pending": -1}},
            {"$limit": 15 if is_admin else 10},
        ]

        cap_lines = []
        async for row in queue_col.aggregate(cap_pipeline):
            ch_id      = row["_id"]
            pending    = row["pending"]
            processing = row["processing"]
            total      = pending + processing
            row_uid    = row.get("user_id")

            try:
                chat    = await client.get_chat(ch_id)
                ch_name = chat.title or str(ch_id)
            except Exception:
                ch_name = str(ch_id)

            user_str = ""
            # Only show user info to admins (privacy) or when it's their own task
            if is_admin and row_uid:
                try:
                    u     = await client.get_users(row_uid)
                    uname = u.first_name or "Unknown"
                    utag  = f"@{u.username}" if u.username else f"ID:{row_uid}"
                    user_str = f"\n  ├ 👤 <a href='tg://user?id={row_uid}'>{uname}</a> ({utag})"
                except Exception:
                    user_str = f"\n  ├ 👤 ID: <code>{row_uid}</code>"

            eta = int((pending / max(DEFAULT_MAX_WORKERS, 1)) * DEFAULT_EDIT_DELAY)
            cap_lines.append(
                f"• <b>{ch_name}</b> <code>({ch_id})</code>"
                f"{user_str}\n"
                f"  ├ 📥 Total: <code>{total}</code>  "
                f"⏳ Pending: <code>{pending}</code>  "
                f"⚙️ Active: <code>{processing}</code>\n"
                f"  └ ⏱ ETA: ~{eta // 60}m {eta % 60}s"
            )

        # ════════════════════════════════════════════════════
        #  FILE FORWARD QUEUE  (all users' /file_forward sessions)
        # ════════════════════════════════════════════════════
        f_pending    = await forward_queue.count_documents({"status": "pending"})
        f_processing = await forward_queue.count_documents({"status": "processing"})
        fwd_match    = {"status": {"$in": ["pending", "processing"]}}

        f_pipeline = [
            {"$match": fwd_match},
            {"$group": {
                "_id":               "$session_id",
                "src":               {"$first": "$src"},
                "dst":               {"$first": "$dst"},
                "source_title":      {"$first": "$source_title"},
                "destination_title": {"$first": "$destination_title"},
                "user_id":           {"$first": "$user_id"},
                "total":             {"$first": "$total"},
                "pending":    {"$sum": {"$cond": [{"$eq": ["$status", "pending"]},    1, 0]}},
                "processing": {"$sum": {"$cond": [{"$eq": ["$status", "processing"]}, 1, 0]}},
            }},
            {"$sort": {"pending": -1}},
            {"$limit": 10},
        ]

        forward_lines = []
        async for row in forward_queue.aggregate(f_pipeline):
            src        = row["src"]
            dst        = row["dst"]
            pending    = row["pending"]
            processing = row["processing"]
            total_jobs = row.get("total", pending + processing)
            done_jobs  = max(0, total_jobs - pending - processing)
            row_uid    = row.get("user_id")
            src_name   = row.get("source_title") or str(src)
            dst_name   = row.get("destination_title") or str(dst)

            if not row.get("source_title"):
                try:
                    src_name = (await client.get_chat(src)).title or src_name
                except Exception:
                    pass
            if not row.get("destination_title"):
                try:
                    dst_name = (await client.get_chat(dst)).title or dst_name
                except Exception:
                    pass

            user_str = ""
            if is_admin and row_uid:
                try:
                    u     = await client.get_users(row_uid)
                    uname = u.first_name or "Unknown"
                    utag  = f"@{u.username}" if u.username else f"ID:{row_uid}"
                    user_str = f"\n  ├ 👤 <a href='tg://user?id={row_uid}'>{uname}</a> ({utag})"
                except Exception:
                    user_str = f"\n  ├ 👤 ID: <code>{row_uid}</code>"

            pct = int((done_jobs / total_jobs * 100)) if total_jobs > 0 else 0
            eta = int((pending + processing) * FORWARD_DELAY)
            forward_lines.append(
                f"• <b>{src_name}</b> ➜ <b>{dst_name}</b>"
                f"{user_str}\n"
                f"  ├ 📦 Total: <code>{total_jobs}</code>  "
                f"✅ Done: <code>{done_jobs}</code>  "
                f"⏳ Left: <code>{pending + processing}</code>  "
                f"[{pct}%]\n"
                f"  └ ⏱ ETA: ~{eta // 60}m {eta % 60}s"
            )

        # ════════════════════════════════════════════════════
        #  GLOBAL (ADMIN) FORWARD — /channels forwarding
        #  Only shown to admins
        # ════════════════════════════════════════════════════
        gff_lines = []
        if is_admin:
            try:
                from body.admin_channels import ADMIN_FF_SESSIONS, _gff_job_queue
                for sid, sess in list(ADMIN_FF_SESSIONS.items()):
                    forwarded  = sess.get("forwarded", 0)
                    total      = sess.get("total", 0)
                    src_title  = sess.get("channel_title", str(sess.get("channel_id", "?")))
                    dest_title = sess.get("dest_title", "Destination")
                    pct        = int((forwarded / total) * 100) if total > 0 else 0
                    q_size     = _gff_job_queue.qsize()
                    bar_filled = int(pct / 10)
                    bar        = "▓" * bar_filled + "░" * (10 - bar_filled)
                    gff_lines.append(
                        f"• <b>{src_title}</b> ➜ <b>{dest_title}</b>\n"
                        f"  ├ [{bar}] <code>{pct}%</code>\n"
                        f"  ├ 📦 Forwarded: <code>{forwarded}</code> / <code>{total}</code>  "
                        f"⏳ Queue: <code>{q_size}</code>\n"
                        f"  └ 🔑 Session: <code>{sid[:8]}…</code>"
                    )
            except Exception:
                pass

        # ════════════════════════════════════════════════════
        #  COMPOSE REPLY
        # ════════════════════════════════════════════════════
        scope_label = "📋 <b>QUEUE STATUS (All Users)</b>"

        text = (
            f"{scope_label}\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"

            "📝 <b>CAPTION QUEUE</b>\n"
            f"  • Pending   : <code>{cap_pending}</code>\n"
            f"  • Processing: <code>{cap_processing}</code>\n\n"
        )

        if cap_lines:
            text += "🔥 <b>Active Caption Tasks</b>\n" + "\n\n".join(cap_lines) + "\n\n"
        else:
            text += "✅ Caption queue is empty\n\n"

        text += (
            "📦 <b>FILE FORWARD QUEUE</b>\n"
            f"  • Pending   : <code>{f_pending}</code>\n"
            f"  • Processing: <code>{f_processing}</code>\n\n"
        )

        if forward_lines:
            text += "🚚 <b>Active Forward Sessions</b>\n" + "\n\n".join(forward_lines) + "\n\n"
        else:
            text += "✅ Forward queue is empty\n\n"

        text += "🌐 <b>GLOBAL FORWARD (/channels)</b>\n"
        if gff_lines:
            text += "\n\n".join(gff_lines)
        else:
            text += "✅ No active global-forward sessions"

        text += "\n\n━━━━━━━━━━━━━━━━━━━━━━━━"

        await loading.edit_text(text, parse_mode=ParseMode.HTML,
                                 disable_web_page_preview=True)
    except Exception as e:
        await loading.edit_text(f"❌ Error fetching queue:\n<code>{e}</code>",
                                 parse_mode=ParseMode.HTML)

# ---------------- Auto Caption core ----------------
def sanitize_caption_html(text: str) -> str:
    if not text:
        return ""
    allowed_tags = {"b", "i", "u", "s", "code", "pre", "a", "spoiler", "blockquote"}
    def repl(match):
        tag = match.group(1).casefold()
        return match.group(0) if tag in allowed_tags else ""
    return re.sub(r"</?\s*([a-zA-Z0-9]+)(?:\s[^>]*)?>", repl, text)

async def caption_worker(client: Client, worker_id: int = 0):
    """
    Single caption-queue consumer.

    IMPORTANT: previously `job = await fetch_channel_job()` sat OUTSIDE the
    try/except block below, so any transient error inside it (a Mongo
    cursor hiccup, a network blip, etc.) raised straight out of this
    function. Since bot.py only ever started ONE of these tasks, that single
    unhandled exception permanently killed the *only* consumer of the
    queue -- every job already enqueued after that point (often after only
    ~10-15 files) just sat in MongoDB as "pending" forever, with nothing
    left running to pick it up, and no restart happened until the process
    itself was restarted. There was also no logging at all here, so the
    failure was completely invisible in the Koyeb logs.

    Fixed by: (1) wrapping the fetch itself in its own try/except so a
    single bad poll never stops the loop, (2) logging every failure/retry
    so admins can see what's happening from `koyeb logs`, and (3) this
    function is now started multiple times in parallel (see
    `start_caption_workers` below) and auto-restarted by a supervisor if it
    ever does exit unexpectedly, so there's no more single point of failure.
    """
    logger.info(f"[CAP_WORKER_{worker_id}] started")
    while True:
        try:
            job = await fetch_channel_job()
        except Exception as e:
            logger.warning(f"[CAP_WORKER_{worker_id}] fetch_channel_job error: {e}")
            await asyncio.sleep(2)
            continue

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
                    dump_caption = sanitize_dump_caption(job.get("default_caption") or "")
                    dump_sent = await client.copy_message(
                        chat_id=CP_CH,
                        from_chat_id=ch,
                        message_id=job["message_id"],
                        caption=dump_caption,
                    )
                    await save_dump_origin(dump_sent.id, ch, job["message_id"])
                except Exception as e:
                    logger.debug(f"[CAP_WORKER_{worker_id}] dump-copy skipped ch={ch} msg={job['message_id']}: {e}")
            await mark_done(job["_id"])
            logger.info(f"[CAP_WORKER_{worker_id}] edited ch={ch} msg={job['message_id']}")
            await asyncio.sleep(DEFAULT_EDIT_DELAY)
        except FloodWait as e:
            wait = e.value + 2
            CHANNEL_COOLDOWN[ch] = time.time() + wait
            logger.warning(f"[CAP_WORKER_{worker_id}] FloodWait {wait}s ch={ch}")
            await reschedule(job["_id"], delay=wait)
        except errors.MessageNotModified:
            await mark_done(job["_id"])
        except Exception as e:
            if job.get("retries", 0) >= 5:
                await mark_done(job["_id"])
                logger.error(f"[CAP_WORKER_{worker_id}] giving up ch={ch} msg={job.get('message_id')} after 5 retries: {e}")
            else:
                logger.warning(f"[CAP_WORKER_{worker_id}] job failed ch={ch} msg={job.get('message_id')} (retry {job.get('retries', 0) + 1}/5): {e}")
                await reschedule(job["_id"], delay=10)
        finally:
            if not released:
                CHANNEL_ACTIVE[ch] = max(0, CHANNEL_ACTIVE[ch] - 1)
                released = True


def start_caption_workers(client: Client, count: int = None):
    """
    Spawns `count` (defaults to CAPTION_WORKERS from database.py) parallel
    caption_worker tasks, each wrapped in a self-healing supervisor: if a
    worker ever exits unexpectedly (should no longer happen given the fix
    above, but this is a safety net), it's logged and automatically
    restarted after a short delay instead of silently reducing capacity
    forever.
    """
    n = count or CAPTION_WORKERS

    async def _guarded(i):
        while True:
            try:
                await caption_worker(client, worker_id=i)
            except Exception as e:
                logger.exception(f"[CAP_WORKER_{i}] crashed unexpectedly, restarting in 3s: {e}")
                await asyncio.sleep(3)
            else:
                # caption_worker's own while-True loop should never return
                # normally; if it somehow does, restart it anyway.
                logger.warning(f"[CAP_WORKER_{i}] exited unexpectedly, restarting in 3s")
                await asyncio.sleep(3)

    for i in range(n):
        asyncio.create_task(_guarded(i), name=f"cap_worker_{i}")
    logger.info(f"[CAP] {n} caption workers started")

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
        # prefix/suffix are stored as a comma/newline-separated list of
        # entries (same canonical format as block/replace words) — render
        # each entry on its own line in the real caption rather than
        # showing the raw comma-joined storage string.
        prefix_display = "\n".join(_split_items(prefix))
        new_caption = f"{prefix_display}\n{new_caption}".strip()
    if suffix:
        suffix_display = "\n".join(_split_items(suffix))
        new_caption = f"{new_caption}\n{suffix_display}".strip()
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
        "default_caption": default_caption,
        "url_buttons": url_buttons or [],
        "user_id": msg.from_user.id if msg.from_user else None
    })

# ═══════════════════════════════════════════════════════════════════
#  Smart File Name Engine  –  professional media caption builder
#  Supports: Movies · Web Series · TV Shows · Anime · OTT originals
#
#  v2 — enhanced accuracy + new tags:
#    • Bigger quality / source / codec tables (WEB-DL, Theater Print,
#      HDCAM, HDTS, R5, Remux … · 144p→8K · HEVC/x265/x264/XviD … ·
#      DD/DDP/DTS/TrueHD/Atmos + bitrate)
#    • Website names, usernames (@handle), t.me links and ad phrases
#      ("join our channel", "subscribe now" …) are stripped BEFORE
#      parsing so they never leak into the Title or any tag.
#    • Context-aware language vs subtitle detection: "English Subtitle"
#      (spelled out, not just ESub) is correctly read as a SUBTITLE,
#      not an audio language — even when other real audio languages
#      (Hindi, Punjabi …) are present in the same text.
#    • Automatic "Dual Audio" / "Multi Audio" tag whenever 2 / 3+
#      languages are detected (no longer depends on the source text
#      explicitly saying "Dual Audio").
#    • "Combined Episodes" tag for batch ranges (S01 E01-05) and
#      "Complete Season" tag when only "S01" is present with no
#      episode number at all.
#    • "Remastered" tag + standalone audio bitrate tag (e.g. 224Kbps).
#    • Pure regex / no network calls on the hot path → stays fast even
#      for large (100k+ file) batches.
# ═══════════════════════════════════════════════════════════════════

# ── Language tables ──────────────────────────────────────────────
# Full canonical language names (order matters: longer/rarer first
# to avoid partial matches on shorter names)
LANG_LIST = [
    "Malayalam", "Kannada", "Marathi", "Gujarati", "Bengali",
    "Punjabi", "Bhojpuri", "Rajasthani", "Haryanvi", "Odia",
    "Assamese", "Maithili", "Santali", "Kashmiri", "Sindhi",
    "Konkani", "Manipuri", "Dogri", "Tulu",
    "Telugu", "Tamil", "Hindi", "English", "Urdu",
    "Japanese", "Korean", "Mandarin", "Chinese", "Cantonese",
    "Spanish", "French", "German", "Italian", "Russian",
    "Arabic", "Dutch", "Portuguese", "Turkish", "Thai",
    "Vietnamese", "Indonesian", "Malay", "Tagalog", "Filipino", "Sinhala",
    "Nepali", "Burmese",
]

# 3-letter ISO-ish codes found in filenames
LANG_CODE_MAP = {
    "hin": "Hindi",     "eng": "English",    "tam": "Tamil",
    "tel": "Telugu",    "mal": "Malayalam",   "kan": "Kannada",
    "mar": "Marathi",   "guj": "Gujarati",    "ben": "Bengali",
    "pan": "Punjabi",   "urd": "Urdu",        "bho": "Bhojpuri",
    "jpn": "Japanese",  "kor": "Korean",      "chi": "Chinese",
    "zho": "Mandarin",  "cmn": "Mandarin",
    "spa": "Spanish",   "fre": "French",      "ger": "German",
    "ita": "Italian",   "rus": "Russian",     "ara": "Arabic",
    "dut": "Dutch",     "por": "Portuguese",  "tur": "Turkish",
    "tha": "Thai",      "vie": "Vietnamese",  "ind": "Indonesian",
    "may": "Malay",     "sin": "Sinhala",     "nep": "Nepali",
    "bur": "Burmese",
}

# ── Quality / Resolution ─────────────────────────────────────────
# Listed longest-first so "2160p" is matched before "1080p" etc.
QUALITY_LIST = [
    "2160p", "1440p", "1080p", "900p", "720p",
    "576p", "540p", "480p", "360p", "240p", "216p", "144p",
    "8K", "4K UHD", "4K", "UHD",
]

# ── Source / Rip type ────────────────────────────────────────────
# Ordered: more specific first so e.g. "WEB-DL" wins over bare "WEB",
# "UHD BluRay" wins over "BluRay", etc.
SOURCE_LIST = [
    "UHD BluRay", "BluRay REMUX", "BDRemux", "Remux",
    "WEB-DL", "WEBRip", "WEB",
    "BluRay", "Blu-Ray", "BRRip", "BDRip",
    "HDRip", "DVDRip", "DVDScr", "DVD",
    "HDTS", "HDTC", "HDCAM", "HDTV",
    "Theater Print", "Theatrical Print", "TC Print",
    "PreDVD", "Pre-DVD", "PDVD",
    "R5", "TS", "TC", "SCR", "CAM",
    "AMZN", "DSNP", "NF", "HMAX", "ATVP", "PCOK",
    "SonyLIV", "ZEE5", "Hotstar", "JioCinema", "Voot", "ALTBalaji",
    "MXPlayer", "ErosNow",
]

# ── Video codecs ─────────────────────────────────────────────────
VIDEO_CODEC_LIST = [
    "HEVC", "x265", "H.265", "H265",
    "x264", "H.264", "H264", "AVC",
    "AV1", "VP9", "MPEG-4", "MPEG-2", "XviD", "DivX",
]

# ── Audio codecs ─────────────────────────────────────────────────
# Listed longest/most specific first to avoid partial matches
AUDIO_CODEC_LIST = [
    "TrueHD Atmos", "Dolby Atmos", "DTS-HD MA", "DTS-HD", "DTS-X", "DTS",
    "DD+5.1", "DDP5.1", "DD5.1", "DD+2.0", "DDP2.0", "DD2.0", "DD+", "DDP",
    "Atmos", "TrueHD",
    "AAC5.1", "AAC2.0", "AAC", "AC3", "EAC3",
    "MP3", "FLAC", "OPUS", "PCM",
]

# ── File extensions ──────────────────────────────────────────────
EXT_LIST = ["mkv", "mp4", "avi", "webm", "mov", "m4v", "ts", "wmv", "flv"]

# ── Sub / ESub compiled patterns ─────────────────────────────────
ESUB_RE = re.compile(r'\bE\.?Subs?\b', re.I)
HSUB_RE = re.compile(r'\bH\.?Subs?\b', re.I)
MSUB_RE = re.compile(r'\bM\.?Subs?\b', re.I)
# Generic "Sub / Subs / Subtitle / Subtitles" (not already E/H/M prefixed)
SUB_RE  = re.compile(r'\b(?<!E\.)(?<!H\.)(?<!M\.)Subs?(?:titles?)?\b', re.I)

# ── Subtitle-language relationship patterns (context aware) ──────
# These are used to tell audio languages and SUBTITLE languages apart
# even when the subtitle language is spelled out in full — e.g.
# "Hindi Punjabi English Subtitle" → Hindi & Punjabi = audio,
# English = subtitle only (NOT counted as a 3rd audio language).
_LANG_ALT = '|'.join(re.escape(l) for l in LANG_LIST)
_SUB_KEYWORD = r'(?:subtitles?|subs?)'

# NOTE: a repeated language token is only chained into the SAME
# subtitle-language group when an EXPLICIT separator (, + & / "and")
# connects it to the next one. Bare whitespace between two language
# words is deliberately NOT treated as a chain — "Hindi Punjabi
# English Subtitle" must read as Hindi+Punjabi = AUDIO and only the
# single word immediately touching "Subtitle" (English) as the
# subtitle language, which matches how these are conventionally named.
# Explicitly-joined lists ("English + Hindi Subtitle", "Subtitle:
# Eng, Hindi") are still fully captured.
_SEP = r'(?:\s*(?:,|\+|&|/|\band\b)\s*)'

_SUB_KEYWORD_RE = re.compile(rf'\b{_SUB_KEYWORD}\b', re.I)

# Language chain immediately AFTER the keyword: "Subtitle[:-] <lang> [<sep> <lang> ...]"
_LANG_CHAIN_AFTER_RE = re.compile(
    rf'\A\s*[:\-]?\s*((?:{_LANG_ALT})(?:{_SEP}(?:{_LANG_ALT})){{0,3}})',
    re.I
)
# Language chain immediately BEFORE the keyword: "<lang> [<sep> <lang> ...] Subtitle"
_LANG_CHAIN_BEFORE_RE = re.compile(
    rf'((?:{_LANG_ALT})(?:{_SEP}(?:{_LANG_ALT})){{0,3}})\s*\Z',
    re.I
)

def _scan_subtitle_context(text: str):
    """
    Single keyword-anchored pass that resolves the subtitle-language
    relationship correctly even in ambiguous cases where a language sits
    on BOTH sides of the "Subtitle" keyword — e.g. "Hindi Subtitle
    English" (Hindi = audio, English = subtitle; NOT the other way
    around, and Hindi must NOT be swallowed as a subtitle language just
    because it happens to sit next to the keyword).

    For every occurrence of the "Subtitle/Sub" keyword:
      1. Look at the language(s) immediately AFTER it first — this is
         the far more common convention ("Subtitle: English",
         "Sub English") and takes priority.
      2. Only if nothing follows, fall back to the language(s)
         immediately BEFORE it ("English Subtitle").
    This guarantees a language is never double-claimed by two keyword
    occurrences and that a keyword sandwiched between two different
    languages doesn't accidentally eat the audio-language one.

    Returns (spans, langs):
      spans — list of (start, end) character spans (in the SAME
              dot/underscore-normalized text) covering just the
              subtitle-language token(s), for excluding those specific
              occurrences from audio-language extraction.
      langs — set of canonical language names identified as subtitles.
    """
    text = _clean_raw(text)
    spans: list = []
    langs: set = set()

    for kw_m in _SUB_KEYWORD_RE.finditer(text):
        after_m = _LANG_CHAIN_AFTER_RE.match(text, kw_m.end())
        if after_m:
            start, end = after_m.start(1), after_m.end(1)
        else:
            before_m = _LANG_CHAIN_BEFORE_RE.search(text[:kw_m.start()])
            if not before_m:
                continue
            start, end = before_m.start(1), before_m.end(1)

        spans.append((start, end))
        chunk = text[start:end]
        for lang in LANG_LIST:
            if re.search(rf'\b{re.escape(lang)}\b', chunk, re.I):
                langs.add(lang)

    return spans, langs

# ── Content-type / label patterns ────────────────────────────────
# NOTE: [\s.]* (not just \s*) so dot-separated filenames like
# "Dual.Audio" / "Web.Series" / "Dolby.Vision" are recognized just as
# well as their space-separated caption forms.
_SERIES_RE    = re.compile(
    r'\b(?:Web[\s.]*Series|TV[\s.]*Series|Mini[\s.]*Series|OTT[\s.]*Series|'
    r'Limited[\s.]*Series|Drama[\s.]*Series|Short[\s.]*Series)\b', re.I
)
_ANIME_RE     = re.compile(r'\bAnime\b', re.I)
_UNCUT_RE     = re.compile(r'\bUnCut\b', re.I)
_SOUTH_RE     = re.compile(r'\bSouth\b', re.I)
_BOLLYWOOD_RE = re.compile(r'\bBollywood\b', re.I)
_HOLLYWOOD_RE = re.compile(r'\bHollywood\b', re.I)
_DUAL_RE      = re.compile(r'\bDual[\s.]*Audio\b', re.I)
_MULTI_RE     = re.compile(r'\bMulti[\s.]*(?:Audio|Lang(?:uage)?)?\b', re.I)
_COMPLETED_RE = re.compile(r'\bCompleted\b', re.I)
_HD_RE        = re.compile(r'\b(?:HD|FHD|Full[\s.]*HD)\b', re.I)
_HDR_RE       = re.compile(r'\b(?:HDR10\+|HDR10|HDR|Dolby[\s.]*Vision|DV)\b', re.I)
_REMASTER_RE  = re.compile(r'\bRe[\s.\-]?master(?:ed)?\b', re.I)
_BITRATE_RE   = re.compile(r'\b(\d{2,4})[\s\-]?[Kk]bps\b')

# ── Promotional / junk-noise stripper ─────────────────────────────
# Website names, @usernames, t.me links and common ad phrases must
# NEVER show up in the Title or any other smart-filename field.
_URL_RE       = re.compile(r'(?:https?://|www\.)\S+', re.I)
_TME_RE       = re.compile(r't\.me/\S+', re.I)
_MENTION_RE   = re.compile(r'@[A-Za-z0-9_]{3,}')
_DOMAIN_RE    = re.compile(
    r'\b[A-Za-z0-9-]{2,30}\.(?:com|net|in|co|org|me|xyz|info|live|cc|to|link|pw|icu|site)\b',
    re.I
)
_AD_PHRASE_RE = re.compile(
    r'\b(?:join[\s.]+(?:us|our|now)?|subscribe(?:[\s.]+now)?|follow[\s.]+us|'
    r'click[\s.]+here|visit[\s.]+(?:us|now)?|for[\s.]+more[\s.]+(?:movies|videos|updates)?|'
    r'download[\s.]+from|powered[\s.]+by|uploaded[\s.]+by|encoded[\s.]+by|'
    r'telegram[\s.]+channel|our[\s.]+channel|official[\s.]+channel)\b',
    re.I
)

def strip_promo_noise(text: str) -> str:
    """
    Remove URLs, @mentions, website/domain names and common ad phrases
    from raw text BEFORE any smart-filename parsing, so third-party
    website names, usernames and adverts never end up in the Title or
    in any extracted tag.
    """
    if not text:
        return text
    t = _URL_RE.sub(' ', text)
    t = _TME_RE.sub(' ', t)
    t = _MENTION_RE.sub(' ', t)
    t = _DOMAIN_RE.sub(' ', t)
    t = _AD_PHRASE_RE.sub(' ', t)
    return t

def _build_clean_raw(filename: str, caption: str) -> str:
    """Combine filename + caption and strip promo/ad noise once."""
    return strip_promo_noise(f"{filename or ''} {caption or ''}")

# ── Dynamic noise table for title cleanup ─────────────────────────
# Built once from the tables above so the title parser always stays
# in sync with whatever quality/source/codec/language tags exist —
# no more manually duplicated noise lists to keep updated by hand.
def _build_noise_pattern() -> str:
    tokens = []
    tokens += [re.escape(q) for q in QUALITY_LIST]
    tokens += [re.escape(s).replace(r'\ ', r'[\s.\-]*') for s in SOURCE_LIST]
    tokens += [re.escape(c) for c in VIDEO_CODEC_LIST]
    tokens += [re.escape(c) for c in AUDIO_CODEC_LIST]
    tokens += [re.escape(l.lower()) for l in LANG_LIST]
    tokens += [
        r'e\.?subs?', r'h\.?subs?', r'm\.?subs?', r'subs?', r'subtitles?',
        r'dual[\s]?audio', r'multi[\s]?audio', r'multi',
        r'uncut', r're[\s\-]?master(?:ed)?',
        r'south', r'bollywood', r'hollywood',
        r'hdr10\+', r'hdr10', r'hdr', r'dolby[\s]?vision',
        r'\d{2,4}[\s\-]?kbps',
        r'complete(?:d)?', r'full\s*hd', r'\bhd\b', r'\bfhd\b',
    ]
    return '|'.join(tokens)

_NOISE = _build_noise_pattern()

# ── Internal helpers ──────────────────────────────────────────────
def _norm(text: str) -> str:
    return re.sub(r'\s+', ' ', text.lower()).strip()

def _clean_raw(text: str) -> str:
    """Replace dots/underscores with spaces for easier token-level parsing."""
    return re.sub(r'[._]', ' ', text)

# ── IMDB enrichment ────────────────────────────────────────────────
# Disabled on the hot path by default: a live network lookup per file
# would serialize/stall the whole event loop and tank throughput on
# large (100k+ file) batches. The title cleanup below is already
# strong enough without it. Kept here (unused) in case a future
# background/cached enrichment pass is wanted.
ENABLE_IMDB_TITLE_ENRICH = False
_imdb_title_cache: dict = {}

def imdb_enrich_title(title: str, year: str):
    if not ENABLE_IMDB_TITLE_ENRICH:
        return title, year
    if not title or not year or len(title) < 3:
        return title, year
    cache_key = (title.lower(), year)
    if cache_key in _imdb_title_cache:
        return _imdb_title_cache[cache_key], year
    try:
        results = _get_ia().search_movie(title)
        for r in results[:5]:
            if str(r.get("year", "")) == year:
                clean = r.get("title", title)
                if clean:
                    _imdb_title_cache[cache_key] = clean
                    return clean, year
    except Exception:
        pass
    _imdb_title_cache[cache_key] = title
    return title, year

# ── Title + Year extractor ────────────────────────────────────────
def extract_title_year(raw: str):
    """
    Robustly extracts the show/movie title and release year.

    Strategy:
      1. Normalise separators (dots, underscores → spaces).
      2. Find the first 4-digit year (1900–2099) — everything before it
         is a candidate title.
      3. Strip season/episode markers, codec/quality/source/language
         noise and bracketed alt-title junk from the candidate.
      4. Title-case the result.

    Handles all common naming conventions:
      • Movie.Name.2024.1080p.BluRay.mkv
      • Show Name S03 E07 (2023) Hindi WEB-DL
      • Title (2020) (Hindi + Tamil) Dual Audio UnCut 720p
      • Anime.Name.S01.EP05.720p.HEVC
    """
    text = _clean_raw(raw)

    # Step 1: locate year
    year_m = re.search(r'\b((?:19|20)\d{2})\b', text)
    year   = year_m.group(1) if year_m else ""
    cut    = year_m.start()  if year_m else len(text)

    title_raw = text[:cut]

    # Step 2: strip season / episode markers from title zone
    title_raw = re.sub(r'\s*\bS(?:eason)?\s*\d{1,3}(?:E\d{1,3})+\b.*$', '', title_raw, flags=re.I)  # S01E07 / S01E07E08
    title_raw = re.sub(r'\s*\bS(?:eason)?\s*\d{1,3}\b.*$', '', title_raw, flags=re.I)
    title_raw = re.sub(r'\s*\bEp?(?:isode)?\.?\s*\d{1,3}\b.*$', '', title_raw, flags=re.I)

    # Step 3: strip known noise tokens (dynamically built from all tables)
    title_raw = re.sub(rf'\b(?:{_NOISE})\b', '', title_raw, flags=re.I)

    # Step 4: remove bracketed junk – "(Poojai)", "[Clear]", "{hin}"
    title_raw = re.sub(r'\([^)]{0,50}\)', '', title_raw)
    title_raw = re.sub(r'\[[^\]]{0,50}\]', '', title_raw)
    title_raw = re.sub(r'\{[^}]{0,50}\}', '', title_raw)

    # Step 5: strip leading/trailing punctuation
    title_raw = re.sub(r'[\[\]()\-–—:,|{}\s]+$', '', title_raw.strip())
    title_raw = re.sub(r'^[\[\]()\-–—:,|{}\s]+', '', title_raw.strip())

    title = re.sub(r'\s{2,}', ' ', title_raw).strip()
    title = title.title() if title else ""
    return title, year

# ── Season / Episode extractor ───────────────────────────────────
def extract_season_episode(text: str):
    """
    Returns (season_str, episode_str, season_tag) as clean display strings.

    Patterns handled (all case-insensitive):
      S01E07          →  S01, E07,        ""
      S01 E07         →  S01, E07,        ""
      S01E07E08       →  S01, E07-E08,    ""
      Season 2 Ep 5   →  S02, E05,        ""
      S01 (Ep.01-09)  →  S01, Ep.01-09,   "Combined Episodes"
      Ep.01-05        →  "",  Ep.01-05,   "Combined Episodes"
      EP05 / Ep 5     →  "",  E05,        ""
      S01 (no E##)    →  S01, "",         "Complete Season"

    season_tag:
      "Combined Episodes" — the file bundles a range of episodes
                             (e.g. S01 E01-05, S15 E06-15).
      "Complete Season"   — only a season number is present with no
                             episode marker at all → whole season.
      ""                  — a normal single-episode file (or a movie).
    """
    t = re.sub(r'[._]', ' ', text)
    season  = ""
    episode = ""

    # ── 1. Season + Episode RANGE together ────────────────────────
    # Matches: S01E01-E05 / S01 E01-05 / S15E06-15 / Season 1 Ep 1-9
    # (season and episode are captured from the SAME match, so this
    #  works even when there's no space/dot between "S01" and "E01" —
    #  a plain \bS..\b season regex can't see across that boundary).
    m = re.search(
        r'\bS(?:eason)?\s*0*(\d{1,3})\s*'
        r'E(?:p(?:isode)?)?\.?\s*0*(\d{1,3})\s*[-–to]+\s*'
        r'(?:E(?:p(?:isode)?)?\.?\s*)?0*(\d{1,3})\b',
        t, re.I
    )
    if m:
        season  = f"S{int(m.group(1)):02d}"
        episode = f"Ep.{int(m.group(2)):02d}-{int(m.group(3)):02d}"
        return season, episode, "Combined Episodes"

    # ── 2. Season + double-episode contiguous: S01E07E08 ─────────
    m = re.search(r'\bS(?:eason)?\s*0*(\d{1,3})\s*E(\d{1,3})E(\d{1,3})\b', t, re.I)
    if m:
        season  = f"S{int(m.group(1)):02d}"
        episode = f"E{int(m.group(2)):02d}-E{int(m.group(3)):02d}"
        return season, episode, "Combined Episodes"

    # ── 3. Season + single Episode together ───────────────────────
    # Handles contiguous "S01E02", spaced "S01 E02" and keyword form
    # "Season 2 Episode 5" in one shot.
    m = re.search(
        r'\bS(?:eason)?\s*0*(\d{1,3})\s*E(?:p(?:isode)?)?\.?\s*0*(\d{1,3})\b',
        t, re.I
    )
    if m:
        season  = f"S{int(m.group(1)):02d}"
        episode = f"E{int(m.group(2)):02d}"
        return season, episode, ""

    # ── 4. Episode-only RANGE (no season number at all) ───────────
    # Matches: Ep.01-09 / (Ep 1-9) / E01-09 / E01-E09
    m = re.search(
        r'\bEp?(?:isode)?\.?\s*0*(\d{1,3})\s*[-–to]+\s*(?:Ep?(?:isode)?\.?\s*)?0*(\d{1,3})\b',
        t, re.I
    )
    if m:
        episode = f"Ep.{int(m.group(1)):02d}-{int(m.group(2)):02d}"
        return season, episode, "Combined Episodes"

    # ── 5. Episode-only single (no season number at all) ──────────
    m = re.search(r'\bEp?(?:isode)?\.?\s*0*(\d{1,3})\b', t, re.I)
    if m:
        episode = f"E{int(m.group(1)):02d}"
        return season, episode, ""

    # ── 6. Season present alone, no episode marker anywhere ───────
    # e.g. "Show.Name.S03.Complete.1080p" → whole season file.
    m = re.search(r'\bS(?:eason)?\s*0*(\d{1,3})\b', t, re.I)
    if m:
        season = f"S{int(m.group(1)):02d}"
        return season, "", "Complete Season"

    return season, episode, ""

# ── Subtitle-language context ─────────────────────────────────────
def _subtitle_context_spans(text: str):
    """
    Return character spans (start, end) of the LANGUAGE portion of any
    subtitle-language relationship found by `_scan_subtitle_context`.
    Any language match whose position falls inside one of these spans
    is treated as a subtitle language, not an audio language.

    NOTE: spans are relative to the dot/underscore-normalized text
    (see `_clean_raw`) — the caller must run language matching against
    that SAME normalized string for the offsets to line up correctly.
    """
    spans, _ = _scan_subtitle_context(text)
    return spans

def _get_subtitle_languages(text: str) -> set:
    """
    Returns the set of language names explicitly paired with a subtitle
    reference (e.g. "English Subtitle", "Subtitle: Hindi, Punjabi").
    Used by extract_subtitle_tag to pick ESub/HSub/MSub, and by
    extract_audio_languages to make sure those mentions are NOT counted
    as audio languages.
    """
    _, langs = _scan_subtitle_context(text)
    return langs

# ── Language extractor ────────────────────────────────────────────
def extract_audio_languages(text: str) -> list:
    """
    Extract the AUDIO languages present in the filename + caption.

    • Context-aware: a language mention that only ever appears as part
      of a "<lang> Subtitle" / "Subtitle: <lang>" phrase (spelled out
      in full, not just ESub/HSub) is treated as a SUBTITLE language
      and excluded here — even while other real audio languages
      (Hindi, Punjabi, etc.) in the same text are still picked up.
    • If the same language ALSO appears elsewhere outside a subtitle
      context, it's still counted as audio (that occurrence wins).
    • Languages are returned IN THE ORDER THEY APPEAR in the text,
      so "Hindi + Telugu" always stays "Hindi + Telugu".
    • Full names are tried first; 3-letter ISO codes only as fallback.
    """
    text = _clean_raw(text)  # keep positions consistent with sub-span offsets
    sub_spans = _subtitle_context_spans(text)

    def _in_sub_span(pos: int) -> bool:
        return any(s <= pos < e for s, e in sub_spans)

    found_with_pos: list = []

    for lang in LANG_LIST:
        for m in re.finditer(rf'\b{re.escape(lang)}\b', text, re.I):
            if _in_sub_span(m.start()):
                continue
            found_with_pos.append((m.start(), lang))
            break  # first non-subtitle occurrence is enough for this language

    # Fallback: 3-letter codes (only if zero full names found)
    if not found_with_pos:
        for code, lang in LANG_CODE_MAP.items():
            m = re.search(rf'\b{re.escape(code)}\b', text, re.I)
            if m and not _in_sub_span(m.start()) and lang not in [l for _, l in found_with_pos]:
                found_with_pos.append((m.start(), lang))

    # Sort by position of first appearance → preserves source order
    found_with_pos.sort(key=lambda x: x[0])

    # Deduplicate while keeping order (same lang matched at two positions)
    seen: set = set()
    result: list = []
    for _, lang in found_with_pos:
        if lang not in seen:
            seen.add(lang)
            result.append(lang)
    return result

# ── Subtitle tag extractor ────────────────────────────────────────
def extract_subtitle_tag(text: str) -> str:
    """
    Returns the subtitle presence tag.
    Priority: ESub > HSub > MSub > Sub

    Also promotes a spelled-out "<lang> Subtitle" mention:
      English  → ESub
      Hindi    → HSub
      2+ langs → MSub
    """
    if ESUB_RE.search(text):
        return "ESub"
    if HSUB_RE.search(text):
        return "HSub"
    if MSUB_RE.search(text):
        return "MSub"
    sub_langs = _get_subtitle_languages(text)
    if sub_langs:
        if len(sub_langs) > 1:
            return "MSub"
        if "English" in sub_langs:
            return "ESub"
        if "Hindi" in sub_langs:
            return "HSub"
        return "MSub"
    if SUB_RE.search(text):
        return "MSub"
    return ""

# ── Individual placeholder extractors ────────────────────────────
def extract_quality(text: str) -> str:
    """Returns resolution tag: 2160p, 1080p, 720p, 480p, 4K …"""
    for q in QUALITY_LIST:
        if re.search(rf'\b{re.escape(q)}\b', text, re.I):
            return q
    return ""

def extract_resolution(text: str) -> str:
    """Alias of extract_quality (for {resolution} placeholder)."""
    return extract_quality(text)

def extract_source(text: str) -> str:
    """
    Returns the rip/source tag.
    Examples: WEB-DL, BluRay, HDRip, Theater Print, AMZN, NF …
    """
    for s in SOURCE_LIST:
        pattern = re.escape(s).replace(r'\ ', r'[\s.\-]*')
        if re.search(rf'\b{pattern}\b', text, re.I):
            return s
    return ""

def extract_video_codec(text: str) -> str:
    """Returns video codec: HEVC, x264, AV1 …"""
    for c in VIDEO_CODEC_LIST:
        if re.search(rf'\b{re.escape(c)}\b', text, re.I):
            return c
    return ""

def extract_audio_codec(text: str) -> str:
    """
    Extracts audio codec including optional bitrate suffix.
    Examples:  DD5.1-224Kbps → DD5.1   |   DDP5.1 → DDP5.1
               TrueHD Atmos  → TrueHD Atmos
    """
    for codec in AUDIO_CODEC_LIST:
        pattern = re.escape(codec)
        m = re.search(
            rf'\b{pattern}(?:[- ]\d+[Kk]bps)?\b',
            text, re.I
        )
        if m:
            return codec  # return canonical casing from list
    return ""

def extract_bitrate(text: str) -> str:
    """Returns standalone audio bitrate tag, e.g. '224Kbps'."""
    m = _BITRATE_RE.search(text)
    return f"{m.group(1)}Kbps" if m else ""

def extract_extension(text: str) -> str:
    """Returns lowercase file extension: mkv, mp4, avi …"""
    # Prefer dot-prefixed match (most reliable)
    m = re.search(r'\.(' + '|'.join(EXT_LIST) + r')\b', text, re.I)
    if m:
        return m.group(1).lower()
    # Fallback: bare word at end or surrounded by spaces
    for e in EXT_LIST:
        if re.search(rf'(?<![.\w]){re.escape(e)}(?![.\w])', text, re.I):
            return e.lower()
    return ""

# ── Audio label formatter ─────────────────────────────────────────
def _format_audio_label(langs: list, text: str) -> str:
    """
    Formats the audio language block, preserving codec+bitrate annotation
    on the first (primary) language. Falls back to a bare bitrate tag
    (e.g. "224Kbps") when a bitrate is present but no codec name is.

    Examples:
      ["Hindi", "Telugu"]  + "DD5.1-224Kbps" → "Hindi DD5.1-224Kbps + Telugu"
      ["Hindi", "Tamil"]   + no codec         → "Hindi + Tamil"
      ["Hindi"]            + "DDP5.1"         → "Hindi DDP5.1"
      ["Hindi"]            + "224Kbps" only    → "Hindi 224Kbps"
    """
    if not langs:
        return ""

    # Look for bitrate-annotated audio codec in raw text
    m = re.search(
        r'\b(TrueHD\s+Atmos|Dolby\s+Atmos|DTS[\s\-]HD(?:\s+MA)?|DTS[\s\-]X|DTS|'
        r'DD\+?5\.1|DDP5\.1|DD\+?2\.0|DDP2\.0|DD\+|DDP|Atmos|TrueHD|'
        r'AAC5\.1|AAC2\.0|AAC|AC3|EAC3|MP3|FLAC|OPUS)'
        r'(?:[- ](\d{2,4}[Kk]bps))?\b',
        text, re.I
    )
    acodec_str = ""
    if m:
        codec_part   = m.group(1)
        bitrate_part = m.group(2)
        acodec_str   = f" {codec_part}-{bitrate_part}" if bitrate_part else f" {codec_part}"
    else:
        br_m = _BITRATE_RE.search(text)
        if br_m:
            acodec_str = f" {br_m.group(1)}Kbps"

    if len(langs) == 1:
        return f"{langs[0]}{acodec_str}"

    # Primary language gets codec annotation; rest are plain
    parts = [f"{langs[0]}{acodec_str}"] + langs[1:]
    return " + ".join(parts)

# ── Media-type detector ───────────────────────────────────────────
def detect_media_type(text: str) -> str:
    """
    Detects content type: 'series', 'anime', or 'movie'.

    Series signals: S01E02, S01 E02, Season 1 Episode 2,
                    Ep.01-09 (batch), EP07, Web Series label,
                    or a bare season marker with no episode at all
                    (S03.Complete style batch dumps)
    Anime signals:  'Anime' keyword — checked FIRST, since anime also
                    commonly uses S01E07-style numbering and would
                    otherwise get misread as a generic "series".
    """
    # Explicit "Anime" keyword wins immediately — stronger, more
    # specific signal than the generic S/E numbering heuristics below.
    if _ANIME_RE.search(text):
        return "anime"
    # Explicit series label wins immediately
    if _SERIES_RE.search(text):
        return "series"
    # Standard SxxExx or S01 E02 patterns
    if re.search(r'\bS\d{1,3}\s*E\d{1,3}\b', text, re.I):
        return "series"
    if re.search(r'\bS\d{1,3}\b', text, re.I) and re.search(r'\bE\d{1,3}\b', text, re.I):
        return "series"
    # Ep-only (no season number) – batch or standalone episode
    if re.search(r'\bEp?(?:isode)?\.?\s*\d{1,3}\b', text, re.I):
        return "series"
    # Season keyword without S-prefix
    if re.search(r'\bSeason\s*\d{1,3}\b', text, re.I):
        return "series"
    # Bare season marker only, no episode marker anywhere (e.g.
    # "Show.Name.S03.Complete.1080p") — still a series, whole season.
    if re.search(r'\bS\d{1,3}\b', text, re.I):
        return "series"
    return "movie"

# ── Master metadata parser ────────────────────────────────────────
def parse_file_info(filename: str, caption: str) -> dict:
    """
    Parse all metadata from filename + caption combined (with promo/ad
    noise stripped first). Returns a flat dict used directly by the
    {placeholder} template engine.
    """
    raw = _build_clean_raw(filename, caption)

    title, year          = extract_title_year(raw)
    title, year          = imdb_enrich_title(title, year)  # no-op unless explicitly enabled
    season, episode, stag = extract_season_episode(raw)
    audio_langs          = extract_audio_languages(raw)
    subtitle             = extract_subtitle_tag(raw)
    quality              = extract_quality(raw)
    source                = extract_source(raw)
    vcodec                = extract_video_codec(raw)
    acodec                = extract_audio_codec(raw)
    bitrate               = extract_bitrate(raw)
    ext                   = extract_extension(raw)
    audio_str             = _format_audio_label(audio_langs, raw) if audio_langs else ""

    return {
        "title":       title,
        "year":        year,
        "season":      season,
        "episode":     episode,
        "season_tag":  stag,          # "Combined Episodes" | "Complete Season" | ""
        "audio":       audio_str,
        "audio_langs": audio_langs,
        "subtitle":    subtitle,
        "quality":     quality,
        "resolution":  quality,       # alias
        "source":      source,
        "vcodec":      vcodec,
        "acodec":      acodec,
        "bitrate":     bitrate,
        "extension":   ext,
    }

# ── Smart caption builder ─────────────────────────────────────────
def build_smart_filename(filename: str, caption: str) -> str:
    """
    Build a professional, fully structured media caption from filename + caption.

    Output order:
      Title  [S## E##/Ep.##-##]  [Combined Episodes / Complete Season]  (Year)
      (Lang1 [Codec-Bitrate] + Lang2)  [Dual/Multi Audio]
      [UnCut]  [Remastered]  [South / Bollywood / Hollywood]  [MediaLabel]
      [HD/FHD]  [HDR]  [Source]  [VCodec]  Quality
      [ESub/HSub/MSub]  [.ext]

    Examples:
      Court - State Vs A Nobody (2025) (Hindi DD5.1-224Kbps + Telugu) Dual Audio UnCut South Movie HD 1080p ESub.mkv
      Sapne Vs Everyone S01 (Ep.01-05) (Combined Episodes) (2023) Hindi Web Series HEVC 480p ESub.mkv
      Loki S01 E02 Hindi Web Series HEVC 480p ESub.mkv
      Salaar Part 1 Ceasefire (2024) (Hindi + Telugu) Dual Audio UnCut South Movie HEVC 720p ESub.mkv
      My Hero Academia S06 E07 (2023) Japanese + English Anime HEVC 1080p ESub.mkv
      Panchayat S03 (Complete Season) (2024) Hindi Web Series WEB-DL 1080p ESub.mkv
    """
    raw         = _build_clean_raw(filename, caption)
    info        = parse_file_info(filename, caption)
    media_type  = detect_media_type(raw)
    audio_langs = info.get("audio_langs") or extract_audio_languages(raw)
    parts: list = []

    # ── 1. Title ─────────────────────────────────────────────────
    if info["title"]:
        parts.append(info["title"])

    # ── 2. Season + Episode ──────────────────────────────────────
    if info["season"] or info["episode"]:
        se = f"{info['season']} {info['episode']}".strip()
        parts.append(se)

    # ── 3. Combined-episode / Complete-season tag ────────────────
    if info["season_tag"] and media_type != "movie":
        parts.append(f"({info['season_tag']})")

    # ── 4. Year ──────────────────────────────────────────────────
    if info["year"]:
        parts.append(f"({info['year']})")

    # ── 5. Audio / Language block ────────────────────────────────
    if audio_langs:
        audio_label = _format_audio_label(audio_langs, raw)
        # Wrap multi-language in parentheses (matches real-world conventions)
        if len(audio_langs) > 1:
            parts.append(f"({audio_label})")
        else:
            parts.append(audio_label)

    # ── 6. Dual / Multi Audio label ──────────────────────────────
    # Automatic — based on how many languages were actually detected —
    # with the explicit "Dual/Multi Audio" text in the source treated
    # as an additional (not required) signal.
    explicit_dual  = bool(_DUAL_RE.search(raw))
    explicit_multi = bool(_MULTI_RE.search(raw))
    if len(audio_langs) >= 3 or explicit_multi:
        parts.append("Multi Audio")
    elif len(audio_langs) == 2 or explicit_dual:
        parts.append("Dual Audio")

    # ── 7. UnCut ─────────────────────────────────────────────────
    if _UNCUT_RE.search(raw):
        parts.append("UnCut")

    # ── 8. Remastered ────────────────────────────────────────────
    if _REMASTER_RE.search(raw):
        parts.append("Remastered")

    # ── 9. Regional / industry label ─────────────────────────────
    if media_type == "movie":
        if _SOUTH_RE.search(raw):
            parts.append("South")
        elif _BOLLYWOOD_RE.search(raw):
            parts.append("Bollywood")
        elif _HOLLYWOOD_RE.search(raw):
            parts.append("Hollywood")

    # ── 10. Completed (for finished series) ───────────────────────
    completed = bool(_COMPLETED_RE.search(raw))

    # ── 11. Media-type label ──────────────────────────────────────
    if media_type == "series":
        series_label = "Web Series"
        s_m = _SERIES_RE.search(raw)
        if s_m:
            # Preserve the exact label from the source text
            series_label = re.sub(r'[\s.]+', ' ', s_m.group(0).strip()).title()
        if completed:
            parts.append(f"Completed {series_label}")
        else:
            parts.append(series_label)
    elif media_type == "anime":
        parts.append("Anime")
    else:
        # Movie – add "Movie" label when regional or explicit flag is present
        if _SOUTH_RE.search(raw) or _BOLLYWOOD_RE.search(raw) or _HOLLYWOOD_RE.search(raw):
            parts.append("Movie")
        elif re.search(r'\bMovie\b', raw, re.I):
            parts.append("Movie")

    # ── 12. HD / FHD flag ────────────────────────────────────────
    hd_m = _HD_RE.search(raw)
    if hd_m:
        parts.append(re.sub(r'[\s.]+', ' ', hd_m.group(0).strip()))

    # ── 13. HDR flag(s) ───────────────────────────────────────────
    # A file can legitimately mention more than one HDR-type format
    # separately (e.g. "HDR10 ... Dolby Vision") — collect all distinct
    # mentions instead of keeping only the first.
    hdr_labels: list = []
    for hdr_m in _HDR_RE.finditer(raw):
        label = re.sub(r'[\s.]+', ' ', hdr_m.group(0).strip())
        if label.upper() not in [l.upper() for l in hdr_labels]:
            hdr_labels.append(label)
    if hdr_labels:
        parts.append(" ".join(hdr_labels))

    # ── 14. Source / Rip type ────────────────────────────────────
    if info["source"]:
        parts.append(info["source"])

    # ── 15. Video codec ──────────────────────────────────────────
    if info["vcodec"]:
        parts.append(info["vcodec"])

    # ── 16. Resolution / Quality ─────────────────────────────────
    if info["quality"]:
        parts.append(info["quality"])

    # ── 17. Subtitle tag ─────────────────────────────────────────
    if info["subtitle"]:
        parts.append(info["subtitle"])

    # ── 18. Extension (glued with dot, no space) ─────────────────
    if info["extension"]:
        parts.append(f".{info['extension']}")

    # Join: extension is attached without a space; everything else space-joined
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

DUMP_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"   # symbols/pictographs, emoticons, transport, supplemental symbols
    "\U00002600-\U000027BF"   # misc symbols, dingbats (includes 👉-style arrows' neighbours)
    "\U00002190-\U000021FF"   # arrows
    "\U00002B00-\U00002BFF"   # misc symbols/arrows
    "\U0001F1E6-\U0001F1FF"   # regional indicators (flag letters)
    "\U0000FE0F"              # variation selector
    "\U0000200D"              # zero-width joiner
    "]+",
    flags=re.UNICODE,
)

def sanitize_dump_caption(text: str) -> str:
    """
    Cleans a file's original/default caption before it's sent to the CP_CH
    dump channel: strips @usernames, links/websites (http, www, t.me),
    markdown/HTML links, and emojis -- leaving the rest of the original
    caption text untouched (no smart-filename rebuilding of any kind).
    """
    if not text:
        return ""
    text = strip_links_only(text)
    text = remove_emojis(text)
    text = DUMP_EMOJI_RE.sub("", text)
    # Drop now-empty lines left behind by stripped links/mentions
    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())
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

# ═══════════════════════════════════════════════════════════════════
#  Append / Delete-specific engine for: Block Words · Replace Words ·
#  Prefix · Suffix
#
#  All four settings share ONE canonical storage format: a comma
#  (or newline) separated list of items, e.g. "spam, fake, scam" or
#  "old1 new1, old2 new2" for replace pairs. This lets all four reuse
#  the exact same append / de-dupe / delete-specific machinery instead
#  of four separate hand-rolled implementations.
#
#  Behaviour change from before: sending new words/phrases now ADDS to
#  whatever is already saved (case-insensitive de-duplicated) instead
#  of silently overwriting it. The existing "Delete ALL" button still
#  wipes everything; a new "Delete Specific" flow lets the user paste
#  back one or more existing items (shown to them in a copy-pasteable,
#  comma-separated list) to remove just those.
# ═══════════════════════════════════════════════════════════════════

WORD_KIND_LABELS = {
    "block":   "blocked word(s)",
    "replace": "replace-word pair(s)",
    "prefix":  "prefix entrie(s)",
    "suffix":  "suffix entrie(s)",
}

def _split_items(raw) -> List[str]:
    """Split a stored/typed block into individual items on commas or newlines."""
    if not raw:
        return []
    if not isinstance(raw, str):
        raw = str(raw)
    return [item.strip() for item in re.split(r'[,\n]+', raw) if item.strip()]

def format_items_preview(raw) -> str:
    """
    Comma-joined, copy-paste-friendly preview of stored items.
    Used in every settings menu AND the delete-specific picker so the
    user always sees (and can copy from) the exact same canonical form
    — commas, not bullet points.
    """
    return ", ".join(_split_items(raw))

def merge_items(old_raw: str, new_raw: str) -> Tuple[str, List[str], List[str]]:
    """
    Append items from new_raw onto old_raw, case-insensitively
    de-duplicated, preserving the original order and NEVER dropping
    existing items.

    Returns (merged_raw, added_items, duplicate_items).
    """
    old_items = _split_items(old_raw)
    new_items = _split_items(new_raw)
    seen = {i.lower() for i in old_items}
    merged = list(old_items)
    added, duplicates = [], []
    for item in new_items:
        key = item.lower()
        if key in seen:
            duplicates.append(item)
            continue
        merged.append(item)
        seen.add(key)
        added.append(item)
    return ", ".join(merged), added, duplicates

def remove_items(old_raw: str, remove_raw: str) -> Tuple[str, List[str], List[str]]:
    """
    Remove the specific item(s) the user pasted back from the stored
    list (case-insensitive exact-item match).

    Returns (new_raw, removed_items, not_found_items).
    """
    old_items    = _split_items(old_raw)
    remove_items_ = _split_items(remove_raw)
    remove_lower = {i.lower() for i in remove_items_}
    old_lower    = {i.lower() for i in old_items}

    kept, removed = [], []
    for item in old_items:
        if item.lower() in remove_lower:
            removed.append(item)
        else:
            kept.append(item)
    not_found = [r for r in remove_items_ if r.lower() not in old_lower]
    return ", ".join(kept), removed, not_found

def validate_word_input(kind: str, raw: str) -> Tuple[bool, Optional[str], str]:
    """
    Validates and normalizes user-submitted text BEFORE it's merged and
    saved as a caption setting.

    kind: 'block' | 'replace' | 'prefix' | 'suffix'
    Returns (is_valid, warning_message_or_None, cleaned_raw)
      - is_valid=False  → nothing should be saved; warning_message
        explains why and the caller should let the user try again.
      - is_valid=True with a warning_message → saved, but some entries
        were skipped/invalid and the user should be told which.
    """
    items = _split_items(raw)
    if not items:
        return False, "❌ Empty input. Please send at least one word/phrase.", ""

    too_long = [i for i in items if len(i) > 300]
    if too_long:
        return False, (
            "❌ One of the entries is too long (max 300 characters). "
            "Please shorten it and try again."
        ), ""

    if kind == "replace":
        valid, invalid = [], []
        for item in items:
            parts = item.split(None, 1)
            if len(parts) == 2:
                valid.append(item)
            else:
                invalid.append(item)
        if not valid:
            return False, (
                "❌ Invalid format. Each entry must be:\n"
                "<code>old_word new_word</code>\n\n"
                "Separate multiple entries with a comma or a new line."
            ), ""
        warning = None
        if invalid:
            warning = (
                f"⚠️ Skipped {len(invalid)} invalid entr{'y' if len(invalid) == 1 else 'ies'} "
                f"(missing the replacement word): {', '.join(invalid)}"
            )
        return True, warning, ", ".join(valid)

    # block / prefix / suffix — any non-empty item is acceptable
    return True, None, ", ".join(items)

async def _get_kind_raw(channel_id: int, kind: str) -> str:
    """Fetch the currently-saved raw value for a given setting kind."""
    if kind == "block":
        return await get_block_words(channel_id)
    if kind == "replace":
        return await get_replace_words(channel_id) or ""
    if kind == "prefix":
        _, prefix = await get_suffix_prefix(channel_id)
        return prefix or ""
    if kind == "suffix":
        suffix, _ = await get_suffix_prefix(channel_id)
        return suffix or ""
    return ""

async def _save_kind_raw(channel_id: int, kind: str, raw: str):
    """Persist the raw value for a given setting kind."""
    if kind == "block":
        await set_block_words(channel_id, raw)
    elif kind == "replace":
        await set_replace_words(channel_id, raw)
    elif kind == "prefix":
        await set_prefix(channel_id, raw)
    elif kind == "suffix":
        await set_suffix(channel_id, raw)


# is a catch-all for plain-text session input (captions, block words,
# suffixes, etc.). Pyrogram stops checking further handlers in the same
# group once one handler's filter matches -- so if this matched commands
# too, any command defined in a plugin module loaded after Caption.py
# (e.g. /channels in body/admin_channels.py) would be silently swallowed
# here and never even reach its own handler, with nothing printed to the
# log since this function simply falls through when no session is active.
_NOT_A_COMMAND = filters.create(
    lambda _, __, m: not (m.text or m.caption or "").startswith("/")
)


@Client.on_message(filters.private & _NOT_A_COMMAND)
async def capture_user_input(client, message):
    """
    Single handler for all user text input collected via bot_data sessions.

    BUG FIXES:
    1. Each session type is checked independently using its OWN key so a stale
       caption_set entry never intercepts a block_words_set input.
    2. block_words / replace_words / prefix / suffix now APPEND to whatever
       is already saved (case-insensitive de-duplicated) instead of silently
       overwriting it. A "Delete Specific" flow (word_delete_set) lets the
       user remove just one or more existing items; "Delete All" still wipes
       everything.
    3. Only the session that the user is ACTIVELY in is consumed — all other
       session keys for this user are cleared when any session starts
       (done in CallbackQuery.py) so cross-bleed is impossible.
    """
    user_id = message.from_user.id

    # Build the set of users who have an active session
    active_users = set()
    for key in ("caption_set", "block_words_set", "replace_words_set",
                "prefix_set", "suffix_set", "url_set", "word_delete_set"):
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

        ok, warn, cleaned = validate_word_input("block", text.strip())
        if not ok:
            bot_data.setdefault("block_words_set", {})[user_id] = session
            await message.reply_text(warn)
            return

        old_raw = await get_block_words(channel_id)
        merged, added, dupes = merge_items(old_raw, cleaned)
        await set_block_words(channel_id, merged)

        try:
            await client.delete_messages(user_id, message.id)
        except Exception:
            pass

        lines = ["✅ <b>Blocked words updated!</b>"]
        if added:
            lines.append(f"➕ Added: {', '.join(added)}")
        if dupes:
            lines.append(f"ℹ️ Already set (skipped): {', '.join(dupes)}")
        if warn:
            lines.append(warn)
        await client.edit_message_text(
            chat_id=user_id,
            message_id=instr_msg_id,
            text="\n".join(lines),
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

        ok, warn, cleaned = validate_word_input("replace", text.strip())
        if not ok:
            bot_data.setdefault("replace_words_set", {})[user_id] = session
            await message.reply_text(warn)
            return

        old_raw = await get_replace_words(channel_id)
        merged, added, dupes = merge_items(old_raw, cleaned)
        await set_replace_words(channel_id, merged)

        try:
            await client.delete_messages(user_id, message.id)
        except Exception:
            pass

        lines = ["✅ <b>Replace words updated!</b>"]
        if added:
            lines.append(f"➕ Added: {', '.join(added)}")
        if dupes:
            lines.append(f"ℹ️ Already set (skipped): {', '.join(dupes)}")
        if warn:
            lines.append(warn)
        await client.edit_message_text(
            chat_id=user_id,
            message_id=instr_msg_id,
            text="\n".join(lines),
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

        ok, warn, cleaned = validate_word_input("prefix", text.strip())
        if not ok:
            bot_data.setdefault("prefix_set", {})[user_id] = session
            await message.reply_text(warn)
            return

        _, old_raw = await get_suffix_prefix(channel_id)
        merged, added, dupes = merge_items(old_raw, cleaned)
        await set_prefix(channel_id, merged)

        try:
            await client.delete_messages(user_id, message.id)
        except Exception:
            pass

        lines = ["✅ <b>Prefix updated!</b>"]
        if added:
            lines.append(f"➕ Added: {', '.join(added)}")
        if dupes:
            lines.append(f"ℹ️ Already set (skipped): {', '.join(dupes)}")
        await client.edit_message_text(
            chat_id=user_id,
            message_id=instr_msg_id,
            text="\n".join(lines),
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

        ok, warn, cleaned = validate_word_input("suffix", text.strip())
        if not ok:
            bot_data.setdefault("suffix_set", {})[user_id] = session
            await message.reply_text(warn)
            return

        old_raw, _ = await get_suffix_prefix(channel_id)
        merged, added, dupes = merge_items(old_raw, cleaned)
        await set_suffix(channel_id, merged)

        try:
            await client.delete_messages(user_id, message.id)
        except Exception:
            pass

        lines = ["✅ <b>Suffix updated!</b>"]
        if added:
            lines.append(f"➕ Added: {', '.join(added)}")
        if dupes:
            lines.append(f"ℹ️ Already set (skipped): {', '.join(dupes)}")
        await client.edit_message_text(
            chat_id=user_id,
            message_id=instr_msg_id,
            text="\n".join(lines),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("↩ Back", callback_data=f"back_to_suffixprefix_{channel_id}")]]
            ),
        )
        return

    # ---------- DELETE SPECIFIC (block / replace / prefix / suffix) ----------
    # Shared by all four settings: the user was shown their current items
    # (comma-separated, copy-pasteable) and sends back the exact word(s)/
    # phrase(s) they want removed — multiple at once, comma-separated.
    if user_id in bot_data.get("word_delete_set", {}):
        if not text.strip():
            return
        session      = bot_data["word_delete_set"].pop(user_id)
        channel_id   = session["channel_id"]
        kind         = session["kind"]
        instr_msg_id = session["instr_msg_id"]

        old_raw = await _get_kind_raw(channel_id, kind)
        new_raw, removed, not_found = remove_items(old_raw, text.strip())
        await _save_kind_raw(channel_id, kind, new_raw)

        try:
            await client.delete_messages(user_id, message.id)
        except Exception:
            pass

        label = WORD_KIND_LABELS.get(kind, "entries")
        lines = [f"🗑 <b>{label.title()} — delete specific</b>"]
        if removed:
            lines.append(f"✅ Removed: {', '.join(removed)}")
        if not_found:
            lines.append(f"⚠️ Not found (skipped): {', '.join(not_found)}")
        if not removed and not not_found:
            lines.append("⚠️ Nothing matched — no changes made.")

        back_cb = {
            "block":   f"back_to_blockwords_{channel_id}",
            "replace": f"back_to_replace_{channel_id}",
            "prefix":  f"back_to_suffixprefix_{channel_id}",
            "suffix":  f"back_to_suffixprefix_{channel_id}",
        }.get(kind, f"chinfo_{channel_id}")

        await client.edit_message_text(
            chat_id=user_id,
            message_id=instr_msg_id,
            text="\n".join(lines),
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("↩ Back", callback_data=back_cb)]]
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
