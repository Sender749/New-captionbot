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
from body.smart_text import analyze, normalize_text, ENGINE_VERSION, FALLBACK_NAMES
from body.caption_history import record_history
from body.corrections_store import make_sig, get_correction, apply_fields
from body.review_flow import enqueue_review
from body.name_fixups import strip_lead_noise, site_marked, prefer_caption_title, add_dub_tags, part_label
from body.stats_tracker import record_edit, record_dump, record_failure, record_floodwait
from body.member_forward import MF_SESSIONS, handle_member_forward_text
from body.gen_session import GS_SESSIONS, handle_gen_session_text
from body.dump_direction import render_dump_skip_list
from collections import deque, defaultdict
from imdb import IMDb
from body.database import _CHANNEL_CACHE as CHANNEL_CACHE, CHANNEL_ACTIVE, CHANNEL_COOLDOWN, DEFAULT_MAX_WORKERS
from body.database import get_channel_delay, note_edit_success, note_edit_floodwait, get_channel_cap

logger = logging.getLogger("captionbot.caption")

_ia = None

def _get_ia():
    global _ia
    if _ia is None:
        _ia = IMDb("http")
    return _ia

MESSAGE_LINK_RE = re.compile(r"(?:https?://)?t\.me/(?:c/\d+|[A-Za-z0-9_]+)/(\d+)")
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

_PENDING_AUTO_DELETE: dict = {}

async def auto_delete_message(msg, delay: int):
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
    await message.reply_text("✅ <b>Dump skip enabled</b>", parse_mode=ParseMode.HTML)
    await render_dump_skip_list(client, message, page=0)

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
    await message.reply_text("🗑 <b>Dump skip removed</b>", parse_mode=ParseMode.HTML)
    await render_dump_skip_list(client, message, page=0)

@Client.on_message(filters.private & filters.user(ADMIN) & filters.command("id"))
async def dump_origin_id_cmd(client, message):
    reply = message.reply_to_message
    if not reply:
        return await message.reply_text(
            "❌ <b>Usage:</b> Forward a file from a dump channel "
            "(or open it inside the dump channel) and reply to it with /id.",
            parse_mode=ParseMode.HTML,
        )

    dest_chat_id = None
    dest_msg_id = None
    if reply.forward_from_chat:
        dest_chat_id = reply.forward_from_chat.id
        dest_msg_id = reply.forward_from_message_id
    elif reply.chat:
        dest_chat_id = reply.chat.id
        dest_msg_id = reply.id

    if not dest_chat_id or not dest_msg_id:
        return await message.reply_text(
            "❌ That message isn't a file forwarded from a dump channel."
        )

    origin = await get_dump_origin(dest_chat_id, dest_msg_id)
    if not origin:
        return await message.reply_text(
            "❌ No origin info found for this file "
            "(it may predate this tracking feature, or came from a chat "
            "that isn't a registered dump destination)."
        )

    origin_channel_id = origin["origin_channel_id"]
    title = await get_channel_title_cached(origin_channel_id)
    skipped = await is_dump_skip(origin_channel_id)
    custom_dest = await get_dump_destination(origin_channel_id)
    dest_text = (
        f"{await get_channel_title_cached(custom_dest)} (<code>{custom_dest}</code>)"
        if custom_dest else "Default (CP_CH)"
    )

    await message.reply_text(
        "📡 <b>Origin Channel Found</b>\n\n"
        f"📢 <b>Channel:</b> {title}\n"
        f"🆔 <b>Channel ID:</b> <code>{origin_channel_id}</code>\n"
        f"🗂 <b>Dump skip:</b> {'✅ Enabled' if skipped else '❌ Not enabled'}\n"
        f"📥 <b>Dump destination:</b> {dest_text}\n\n"
        f"Use <code>/dump_skip {origin_channel_id}</code> to stop dumping files from this channel.\n"
        f"Use <code>/dump_change {origin_channel_id}</code> to redirect its dump destination.",
        parse_mode=ParseMode.HTML,
    )


@Client.on_message(filters.private & filters.command("f"))
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
        "├ /stats — Stats panel: edits, dump, channels, owners, daily reports\n"
        "└ /queue — Live caption &amp; forward queue status\n\n"

        "📢 <b>BROADCAST</b>\n"
        "┌ /broadcast — Broadcast a message to all users\n"
        "└   <i>(reply to any message to broadcast it)</i>\n\n"

        "🗃 <b>DUMP CHANNEL CONTROL</b>\n"
        "┌ /dump_skip <code>-100xxx</code> — Skip dump for a channel\n"
        "├ /remove_dump <code>-100xxx</code> — Remove dump skip for a channel\n"
        "└ /dump_change <code>-100xxx</code> — Redirect a channel's dump to another channel\n\n"

        "🗄 <b>DATABASE</b>\n"
        "└ /reset — ⚠️ Wipe ALL users, channels &amp; settings from DB\n\n"

        "🔄 <b>BOT CONTROL</b>\n"
        "└ /restart — Restart the bot process\n\n"

        "📤 <b>FILE FORWARDING</b>\n"
        "├ /file_forward — Start a user file forward session\n"
        "│   <i>→ Pick source → destination → range (or 0 for all)</i>\n"
        "├ /channels — View all user-added channels &amp; bulk-forward files\n"
        "│   <i>→ Shows channel info, who added it, file count,</i>\n"
        "│   <i>   forwarding progress, start/continue/stop controls</i>\n"
        "└ /member_forward — Forward from a channel the BOT isn't in\n"
        "    <i>→ Uses the userbot (SESSION_STRING) to pull files from a</i>\n"
        "    <i>   channel where only your personal account is a member</i>\n\n"
        "🔑 <b>USERBOT SETUP</b>\n"
        "└ /gen_session — Generate a SESSION_STRING for /member_forward\n"
        "    <i>→ Walks you through phone number → code → (2FA if any)</i>\n"
        "    <i>   and gives you the string to set as an env var</i>\n\n"

        "⚙️ <b>CHANNEL SETTINGS</b>\n"
        "└ /settings — Manage your added channels\n\n"

        "━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔐 All commands above are admin-only.\n"
        "📋 /queue is available to all users (shows their own tasks)."
    )
    await message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

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
    uid      = message.from_user.id
    is_admin = True

    loading = await message.reply_text("🔄 Fetching queue…")
    try:
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

def sanitize_caption_html(text: str) -> str:
    if not text:
        return ""
    allowed_tags = {"b", "i", "u", "s", "code", "pre", "a", "spoiler", "blockquote"}
    def repl(match):
        tag = match.group(1).casefold()
        return match.group(0) if tag in allowed_tags else ""
    return re.sub(r"</?\s*([a-zA-Z0-9]+)(?:\s[^>]*)?>", repl, text)

MAX_JOB_RETRIES = 8

PERMANENT_EDIT_ERRORS = (
    errors.MessageIdInvalid,
    errors.ChannelInvalid,
    errors.ChannelPrivate,
    errors.ChatAdminRequired,
    errors.ChatWriteForbidden,
    errors.MediaCaptionTooLong,
    errors.UserBannedInChannel,
)

PEER_ERRORS = (ValueError, KeyError, errors.PeerIdInvalid, errors.ChatIdInvalid)


async def _retry_db(fn, *args, attempts: int = 5):
    last = None
    for attempt in range(attempts):
        try:
            return await fn(*args)
        except Exception as e:
            last = e
            await asyncio.sleep(min(5, 0.5 * 2 ** attempt))
    raise last


async def caption_worker(client: Client, worker_id: int = 0):
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
            hist_meta = {}
            new_caption, dump_smart_caption = render_caption_for_job(job, meta_out=hist_meta)
            try:
                await client.edit_message_caption(
                    chat_id=ch,
                    message_id=job["message_id"],
                    caption=new_caption,
                    parse_mode=ParseMode.HTML,
                    reply_markup=(InlineKeyboardMarkup([[InlineKeyboardButton(btn["text"], url=btn["url"]) for btn in row] for row in job.get("url_buttons", [])])
                                  if job.get("url_buttons") else None
                                 )
                )
            except errors.MessageNotModified:
                pass
            if not await is_dump_skip(ch):
                try:
                    dump_dest = await get_dump_destination(ch)
                    dump_caption = dump_smart_caption or ""
                    if not dump_caption:
                        dump_caption = sanitize_dump_caption(job.get("default_caption_html") or "")
                    dest = dump_dest or CP_CH
                    dump_sent = await client.copy_message(
                        chat_id=dest,
                        from_chat_id=ch,
                        message_id=job["message_id"],
                        caption=dump_caption,
                    )
                    await save_dump_origin(dest, dump_sent.id, ch, job["message_id"])
                    record_dump(ch)
                except Exception as e:
                    logger.warning(f"[CAP_WORKER_{worker_id}] dump-copy failed ch={ch} msg={job['message_id']}: {type(e).__name__}: {e}")
            await mark_done(job["_id"])
            logger.info(f"[CAP_WORKER_{worker_id}] edited ch={ch} msg={job['message_id']}")
            record_history(job, hist_meta, new_caption, dump_smart_caption)
            enqueue_review(job, hist_meta)
            record_edit(ch)
            note_edit_success(ch)
            await asyncio.sleep(get_channel_delay(ch))
        except FloodWait as e:
            wait = e.value + 2
            CHANNEL_COOLDOWN[ch] = time.time() + wait
            note_edit_floodwait(ch)
            record_floodwait(ch)
            try:
                still_pending = await queue_col.count_documents({"chat_id": ch, "status": "pending"})
            except Exception:
                still_pending = "?"
            logger.warning(
                f"[CAP_WORKER_{worker_id}] FloodWait {wait}s ch={ch}, "
                f"new pace={get_channel_delay(ch):.2f}s, cap now={get_channel_cap(ch)}, "
                f"{still_pending} file(s) still pending for this channel"
            )
            await reschedule_floodwait(job["_id"])
        except Exception as e:
            retries = job.get("retries", 0)
            if isinstance(e, PERMANENT_EDIT_ERRORS) or retries >= MAX_JOB_RETRIES:
                await mark_done(job["_id"])
                record_failure(ch)
                logger.error(
                    f"[CAP_WORKER_{worker_id}] dropped ch={ch} msg={job.get('message_id')} "
                    f"after {retries} retries: {type(e).__name__}: {e}"
                )
            else:
                if isinstance(e, PEER_ERRORS):
                    try:
                        await client.get_chat(ch)
                    except Exception:
                        pass
                delay = min(300, 10 * 2 ** retries)
                logger.warning(
                    f"[CAP_WORKER_{worker_id}] job failed ch={ch} msg={job.get('message_id')} "
                    f"(retry {retries + 1}/{MAX_JOB_RETRIES} in {delay}s): {type(e).__name__}: {e}"
                )
                await reschedule(job["_id"], delay=delay)
        finally:
            if not released:
                CHANNEL_ACTIVE[ch] = max(0, CHANNEL_ACTIVE[ch] - 1)
                released = True


def start_caption_workers(client: Client, count: int = None):
    n = count or CAPTION_WORKERS

    async def _guarded(i):
        while True:
            try:
                await caption_worker(client, worker_id=i)
            except Exception as e:
                logger.exception(f"[CAP_WORKER_{i}] crashed unexpectedly, restarting in 3s: {e}")
                await asyncio.sleep(3)
            else:
                logger.warning(f"[CAP_WORKER_{i}] exited unexpectedly, restarting in 3s")
                await asyncio.sleep(3)

    for i in range(n):
        asyncio.create_task(_guarded(i), name=f"cap_worker_{i}")
    logger.info(f"[CAP] {n} caption workers started")

def render_caption_for_job(job: dict, meta_out: dict = None):
    original_file_name = job.get("original_file_name") or ""
    file_name = job.get("file_name") or ""
    default_caption_plain = job.get("default_caption_plain") or ""
    default_caption_html = job.get("default_caption_html") or default_caption_plain
    default_caption = default_caption_html
    file_size = job.get("file_size")
    cap_template = job["cap_template"]

    language = ""
    year = ""
    smart_file_name = ""
    try:
        raw_file_name = normalize_series_name(file_name)
        sig_name = original_file_name or raw_file_name
        sig = make_sig(sig_name, default_caption_plain)
        fix = get_correction(sig)
        file_info = parse_file_info(sig_name, default_caption_plain)
        if fix and fix.get("fields"):
            apply_fields(file_info, fix["fields"])
        language = " + ".join(file_info.get("audio_langs") or [])
        year = file_info.get("year") or extract_year(default_caption_plain) or extract_year(original_file_name) or ""
        if meta_out is not None:
            meta_out.update(file_info.get("_meta") or {})
            meta_out["sig"] = sig
        smart_file_name = build_smart_filename(
            sig_name, default_caption_plain,
            precomputed_info=file_info,
        )
        if fix and fix.get("smart_name"):
            smart_file_name = fix["smart_name"]
        if meta_out is not None:
            meta_out["smart_file_name"] = smart_file_name
            meta_out["corrected"] = bool(fix)
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
        logger.warning(
            "render_caption_for_job: template formatting failed for chat=%s msg=%s: %s",
            job.get("chat_id"), job.get("message_id"), e,
        )
        new_caption = cap_template

    if cap_template.strip() == "{default_caption}":
        new_caption = default_caption
        if job.get("blocked_words_raw"):
            new_caption = remove_blocked_words_keep_layout(new_caption, job["blocked_words_raw"])
        if job.get("replace_raw"):
            replace_pairs = parse_replace_pairs(job["replace_raw"])
            if replace_pairs:
                new_caption = apply_replacements(new_caption, replace_pairs)
        if job.get("link_remover_on"):
            new_caption = strip_links_keep_layout(new_caption)
        if job.get("prefix"):
            new_caption = f"{job['prefix']}\n{new_caption}" if new_caption else job["prefix"]
        if job.get("suffix"):
            new_caption = f"{new_caption}\n{job['suffix']}" if new_caption else job["suffix"]
        if job.get("emoji_remover_on"):
            new_caption = remove_emojis_keep_layout(new_caption)
        if "<" in new_caption and ">" in new_caption:
            new_caption = sanitize_caption_html(new_caption)
    else:
        if job.get("blocked_words_raw"):
            new_caption = apply_block_words(new_caption, job["blocked_words_raw"])
        if job.get("replace_raw"):
            replace_pairs = parse_replace_pairs(job["replace_raw"])
            if replace_pairs:
                new_caption = apply_replacements(new_caption, replace_pairs)
        if job.get("link_remover_on"):
            new_caption = strip_links_only(new_caption)
        if job.get("prefix"):
            new_caption = f"{job['prefix']}\n{new_caption}".strip()
        if job.get("suffix"):
            new_caption = f"{new_caption}\n{job['suffix']}".strip()
        if job.get("emoji_remover_on"):
            new_caption = remove_emojis(new_caption)
        new_caption = new_caption.strip()
        if "<" in new_caption and ">" in new_caption:
            new_caption = sanitize_caption_html(new_caption)

    if smart_file_name:
        dump_smart_caption = strip_links_only(smart_file_name)
        dump_smart_caption = remove_emojis(dump_smart_caption)
        dump_smart_caption = DUMP_EMOJI_RE.sub("", dump_smart_caption).strip()
    else:
        dump_smart_caption = ""

    return new_caption, dump_smart_caption


@Client.on_message(filters.channel & filters.media)
async def reCap(client, msg):
    if msg.edit_date or not msg.media:
        return
    chnl_id = msg.chat.id
    logger.info(f"[RECAP] media received ch={chnl_id} msg={msg.id}")
    try:
        default_caption_plain = str(msg.caption) if msg.caption else ""
        default_caption_html = msg.caption.html if msg.caption else default_caption_plain
        file_name = None
        file_size = None
        file_unique_id = None
        for file_type in ("video", "audio", "document", "voice"):
            obj = getattr(msg, file_type, None)
            if obj:
                file_unique_id = getattr(obj, "file_unique_id", None)
                file_name = getattr(obj, "file_name", None)
                if not file_name and file_type == "voice":
                    file_name = "Voice Message"
                elif not file_name:
                    file_name = "File"
                file_name = file_name.replace("_", " ").replace(".", " ")
                file_size = get_size(getattr(obj, "file_size", 0))
                break
        if not file_name:
            logger.warning(
                f"[RECAP] ch={chnl_id} msg={msg.id}: no video/audio/document/voice "
                f"object found on this media message -- skipped"
            )
            return
        cap_doc = await _retry_db(get_channel_cached, chnl_id)
        cap_template = cap_doc.get("caption")
        if not cap_template:
            logger.warning(
                f"[RECAP] ch={chnl_id} msg={msg.id}: no caption template saved for "
                f"this channel (cap_doc keys={list(cap_doc.keys())}) -- skipped. "
                f"Set one via /settings in this channel first."
            )
            return
        link_remover_on = bool(cap_doc.get("link_remover", False))
        emoji_remover_on = bool(cap_doc.get("emoji_remover", False))
        blocked_words_raw = cap_doc.get("block_words", "")
        suffix = cap_doc.get("suffix", "") or ""
        prefix = cap_doc.get("prefix", "") or ""
        replace_raw = cap_doc.get("replace_words", None)
        url_buttons = cap_doc.get("url_buttons", [])
        original_file_name = ""
        for file_type in ("video", "audio", "document", "voice"):
            obj = getattr(msg, file_type, None)
            if obj:
                original_file_name = getattr(obj, "file_name", None) or ""
                break

        job = {
            "chat_id": msg.chat.id,
            "message_id": msg.id,
            "file_name": file_name,
            "original_file_name": original_file_name,
            "file_unique_id": file_unique_id,
            "default_caption_plain": default_caption_plain,
            "default_caption_html": default_caption_html,
            "file_size": file_size,
            "cap_template": cap_template,
            "link_remover_on": link_remover_on,
            "emoji_remover_on": emoji_remover_on,
            "blocked_words_raw": blocked_words_raw,
            "suffix": suffix,
            "prefix": prefix,
            "replace_raw": replace_raw,
            "url_buttons": url_buttons or [],
            "user_id": msg.from_user.id if msg.from_user else None
        }

        last_err = None
        try:
            await _retry_db(enqueue_caption, job)
        except Exception as e:
            last_err = e
        if last_err is not None:
            logger.error(
                "reCap: FAILED to enqueue caption job for chat=%s msg=%s after retries: %s",
                chnl_id, msg.id, last_err
            )
        else:
            logger.info(f"[RECAP] ch={chnl_id} msg={msg.id}: enqueued for editing")
    except Exception as e:
        logger.error("reCap: unhandled error for chat=%s msg=%s: %s", chnl_id, msg.id, e)


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

RESOLUTION_LIST = [
    "2160p", "1440p", "1080p", "900p", "720p",
    "576p", "540p", "480p", "360p", "240p", "216p", "144p",
    "8K", "4K UHD", "4K", "UHD",
]

QUALITY_LIST = [
    "UHD BluRay", "BluRay REMUX", "BDRemux", "Remux",
    "WEB-DL", "WEBRip", "WEB",
    "BluRay", "Blu-Ray", "BRRip", "BDRip",
    "HDRip", "DVDRip", "DVDScr", "DVD",
    "HDCAMRip", "CAMRip", "TSRip", "HDScr",
    "HDTS", "HDTC", "HDCAM", "HDTV",
    "Theater Print", "Theatrical Print", "TC Print",
    "PreDVD", "Pre-DVD", "PDVD",
    "R5", "TS", "TC", "SCR", "CAM",
]

SOURCE_LIST = [
    ("Disney+ Hotstar", "Disney+ Hotstar"),
    ("Amazon Prime", "Amazon Prime"), ("AmazonPrime", "Amazon Prime"),
    ("Amazon", "Amazon Prime"), ("AMZN", "Amazon Prime"),
    ("Netflix", "Netflix"), ("NF", "Netflix"),
    ("DisneyPlus", "Disney+"), ("Disney+", "Disney+"), ("DSNP", "Disney+"),
    ("Hotstar", "Hotstar"),
    ("HBO Max", "HBO Max"), ("HBOMax", "HBO Max"), ("HMAX", "HBO Max"),
    ("Apple TV+", "Apple TV+"), ("AppleTV+", "Apple TV+"), ("ATVP", "Apple TV+"),
    ("Peacock", "Peacock"), ("PCOK", "Peacock"),
    ("SonyLIV", "SonyLIV"),
    ("ZEE5", "ZEE5"),
    ("JioCinema", "JioCinema"),
    ("Voot", "Voot"),
    ("ALTBalaji", "ALTBalaji"), ("ALT Balaji", "ALTBalaji"),
    ("MX Player", "MX Player"), ("MXPlayer", "MX Player"),
    ("Eros Now", "Eros Now"), ("ErosNow", "Eros Now"),
    ("Crunchyroll", "Crunchyroll"),
    ("Paramount+", "Paramount+"), ("PMTP", "Paramount+"),
    ("iTunes", "iTunes"),
    ("Google Play", "Google Play"),
    ("YouTube", "YouTube"),
]

VIDEO_CODEC_LIST = [
    "HEVC", "x265", "H.265", "H265",
    "x264", "H.264", "H264", "AVC",
    "AV1", "VP9", "MPEG-4", "MPEG-2", "XviD", "DivX",
]

AUDIO_CODEC_LIST = [
    "TrueHD Atmos", "Dolby Atmos", "DTS-HD MA", "DTS-HD", "DTS-X", "DTS",
    "DD+5.1", "DDP5.1", "DD5.1", "DD+2.0", "DDP2.0", "DD2.0", "DD+", "DDP",
    "Atmos", "TrueHD",
    "AAC5.1", "AAC2.0", "AAC", "AC3", "EAC3",
    "MP3", "FLAC", "OPUS", "PCM",
]

EXT_LIST = ["mkv", "mp4", "avi", "webm", "mov", "m4v", "ts", "wmv", "flv"]

ESUB_RE = re.compile(r'\bE\.?Subs?\b', re.I)
HSUB_RE = re.compile(r'\bH\.?Subs?\b', re.I)
MSUB_RE = re.compile(r'\bM\.?Subs?\b', re.I)
SUB_RE  = re.compile(r'\b(?<!E\.)(?<!H\.)(?<!M\.)Subs?(?:titles?)?\b', re.I)

_LANG_ALT = '|'.join(re.escape(l) for l in LANG_LIST)
_SUB_KEYWORD = r'(?:subtitles?|subs?)'

_SEP = r'(?:\s*(?:,|\+|&|/|\band\b)\s*)'

_SUB_KEYWORD_RE = re.compile(rf'\b{_SUB_KEYWORD}\b', re.I)

_LANG_CHAIN_AFTER_RE = re.compile(
    rf'\s*[:\-]?\s*((?:{_LANG_ALT})(?:{_SEP}(?:{_LANG_ALT})){{0,3}})',
    re.I
)
_LANG_CHAIN_BEFORE_RE = re.compile(
    rf'((?:{_LANG_ALT})(?:{_SEP}(?:{_LANG_ALT})){{0,3}})\s*\Z',
    re.I
)

def _scan_subtitle_context(text: str):
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
_MULTI_RE     = re.compile(r'\bMulti(?![\s.\-]*(?:sub|subs|subtitle|subtitles)\b)', re.I)
_COMPLETED_RE = re.compile(r'\bCompleted\b', re.I)
_HD_RE        = re.compile(r'\b(?:HD|FHD|Full[\s.]*HD)\b', re.I)
_HDR_RE       = re.compile(r'\b(?:HDR10\+|HDR10|HDR|Dolby[\s.]*Vision|DV)\b', re.I)
_REMASTER_RE  = re.compile(r'\bRe[\s.\-]?master(?:ed)?\b', re.I)
_EXTENDED_RE  = re.compile(r'\bExtended(?:[\s.\-]?(?:Cut|Edition|Version))?\b', re.I)
_BITRATE_RE   = re.compile(r'\b(\d{2,4})[\s\-]?[Kk]bps\b')

_URL_RE       = re.compile(r'(?:https?://|www\.)\S+', re.I)
_TME_RE       = re.compile(r't\.me/\S+', re.I)
_MENTION_RE   = re.compile(r'@[A-Za-z0-9_]{3,}')
_DOMAIN_RE    = re.compile(
    r'\b[A-Za-z0-9-]{2,30}(?:\.(?:com|net|org|info|xyz|live|link|pw|icu|site)\b'
    r'|\.(?:in|co|me|to|cc)(?![.\w]))',
    re.I
)
_AD_PHRASE_RE = re.compile(
    r'\b(?:join[\s.]+(?:us|our|now|my)\b|subscribe[\s.]+(?:now|to|our|us)\b|follow[\s.]+us|'
    r'click[\s.]+here|visit[\s.]+(?:us|now|our)\b|for[\s.]+more[\s.]+(?:movies|videos|updates)\b|'
    r'download[\s.]+from|powered[\s.]+by|uploaded[\s.]+by|encoded[\s.]+by|'
    r'telegram[\s.]+channel|our[\s.]+channel|official[\s.]+channel)\b',
    re.I
)

def strip_promo_noise(text: str) -> str:
    if not text:
        return text
    t = _URL_RE.sub(' ', text)
    t = _TME_RE.sub(' ', t)
    t = _MENTION_RE.sub(' ', t)
    t = _DOMAIN_RE.sub(' ', t)
    t = _AD_PHRASE_RE.sub(' ', t)
    return t

def _build_clean_raw(filename: str, caption: str) -> str:
    return strip_promo_noise(f"{filename or ''} {caption or ''}")

def _build_noise_pattern() -> str:
    tokens = []
    tokens += [re.escape(r) for r in RESOLUTION_LIST]
    tokens += [re.escape(q).replace(r'\ ', r'[\s.\-]*') for q in QUALITY_LIST]
    tokens += [re.escape(pattern).replace(r'\ ', r'[\s.\-]*') for pattern, _ in SOURCE_LIST]
    tokens += [re.escape(c) for c in VIDEO_CODEC_LIST]
    tokens += [re.escape(c) for c in AUDIO_CODEC_LIST]
    tokens += [re.escape(l.lower()) for l in LANG_LIST]
    tokens += [re.escape(e) for e in EXT_LIST]
    tokens += [
        r'e\.?subs?', r'h\.?subs?', r'm\.?subs?', r'subs?', r'subtitles?',
        r'dual[\s]?audio', r'multi[\s]?audio', r'multi',
        r'uncut', r're[\s\-]?master(?:ed)?', r'extended(?:[\s.\-]?(?:cut|edition|version))?',
        r'south', r'bollywood', r'hollywood',
        r'hdr10\+', r'hdr10', r'hdr', r'dolby[\s]?vision',
        r'\d{2,4}[\s\-]?kbps',
        r'complete(?:d)?', r'full\s*hd', r'\bhd\b', r'\bfhd\b',
        r'voice[\s.\-]?message', r'\bfile\b', r'\bdocument\b', r'\bvideo\b', r'\baudio\b',
    ]
    return '|'.join(tokens)

_NOISE = _build_noise_pattern()

def _norm(text: str) -> str:
    return re.sub(r'\s+', ' ', text.lower()).strip()

def _clean_raw(text: str) -> str:
    return re.sub(r'[._]', ' ', text)

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

def extract_title_year(raw: str):
    a = analyze("", raw)
    return a["title"]["value"], a["year"]["value"]

_SE_BRACKETS = re.compile(r"[\[\]\(\)\{\}]")
_EPW = r"E(?:p(?:isode)?)?[\s.\-]*"


def _ep_range(a: int, b: int):
    if a == b or b < a or b == 0:
        return f"E{a:02d}", ""
    return f"Ep.{a:02d}-{b:02d}", "Combined Episodes"


def extract_season_episode(text: str):
    orig = re.sub(r"[._]", " ", text)
    t = re.sub(r"\s+", " ", _SE_BRACKETS.sub(" ", orig))
    SEP = r"[\s.\-]*"

    RNG = rf"(?:\s*(?:-+|–|to)\s*{_EPW}|-+|–|\s+to\s+)"

    m = re.search(
        r"\bS(?:eason)?\s*0*(\d{1,3})\s*(?:E(?:p(?:isode)?)?[\s.\-]*)?[\(\[\{]\s*(?:E(?:p(?:isode)?)?[\s.\-]*)?0*(\d{1,3})"
        r"\s*(?:-+|–|to|\s)\s*(?:E(?:p(?:isode)?)?[\s.\-]*)?0*(\d{1,3})\s*[\)\]\}]", orig, re.I)
    if m and int(m.group(3)) > int(m.group(2)):
        ep, tag = _ep_range(int(m.group(2)), int(m.group(3)))
        return f"S{int(m.group(1)):02d}", ep, tag

    m = re.search(
        rf"\bS(?:eason)?\s*0*(\d{{1,3}}){SEP}{_EPW}0*(\d{{1,4}}){RNG}0*(\d{{1,4}})\b",
        t, re.I)
    if m and not re.match(r"\s+\d{1,2}\s+\d{2}\b", t[m.end(3):]):
        ep, tag = _ep_range(int(m.group(2)), int(m.group(3)))
        return f"S{int(m.group(1)):02d}", ep, tag

    m = re.search(rf"\bS(?:eason)?\s*0*(\d{{1,3}}){SEP}E(\d{{1,4}})E(\d{{1,4}})\b", t, re.I)
    if m:
        ep, tag = _ep_range(int(m.group(2)), int(m.group(3)))
        return f"S{int(m.group(1)):02d}", ep, tag

    tags = re.findall(rf"\bS(?:eason)?\s*0*(\d{{1,3}}){SEP}{_EPW}0*(\d{{1,4}})\b", t, re.I)
    if len({e for _, e in tags}) >= 2 and len({s for s, _ in tags}) == 1:
        eps = sorted({int(e) for _, e in tags})
        ep, tag = _ep_range(eps[0], eps[-1])
        return f"S{int(tags[0][0]):02d}", ep, tag

    m = re.search(rf"\bS(?:eason)?\s*0*(\d{{1,3}}){SEP}{_EPW}0*(\d{{1,4}})\b", t, re.I)
    if m:
        return f"S{int(m.group(1)):02d}", f"E{int(m.group(2)):02d}", ""

    m = re.search(r"(?<![\w])(\d{1,2})x(?!26[45]\b)(\d{2,3})\b", t, re.I)
    if m and int(m.group(1)) > 0:
        return f"S{int(m.group(1)):02d}", f"E{int(m.group(2)):02d}", ""

    m = re.search(rf"(?<![\w-]){_EPW}0*(\d{{1,4}}){RNG}0*(\d{{1,4}})\b", t, re.I)
    if m and not re.match(r"\s+\d{1,2}\s+\d{2}\b", t[m.end(2):]):
        ep, tag = _ep_range(int(m.group(1)), int(m.group(2)))
        return "", ep, tag

    m = re.search(rf"(?<![\w-]){_EPW}0*(\d{{1,3}})\b", t, re.I) or re.search(r"(?<![\w-])E(\d{4})\b", t, re.I)
    if m:
        return "", f"E{int(m.group(1)):02d}", ""

    m = re.search(r"\s-\s0*(\d{2,4})(?:v\d)?(?=\s*(?:[\[\(]|\d{3,4}p|\.\w{2,4}$|$))", text)
    if m and not re.match(r"(?:19|20)\d\d$", m.group(1)) \
            and m.group(1).lstrip("0") not in ("144", "240", "360", "480", "540", "576", "720", "900", "1080", "1440", "2160"):
        return "", f"E{int(m.group(1)):02d}", ""

    m = re.search(r"\bS(?:eason)?s?\s*0*(\d{1,2})\s*(?:-+|–|to|&|and)\s*S?(?:eason)?s?\s*0*(\d{1,2})\b", t, re.I)
    if m and int(m.group(1)) < int(m.group(2)):
        return f"S{int(m.group(1)):02d}-S{int(m.group(2)):02d}", "", "Complete Season"

    m = re.search(r"\bS(?:eason)?\s*0*(\d{1,3})\b", t, re.I)
    if m:
        return f"S{int(m.group(1)):02d}", "", "Complete Season"

    return "", "", ""


def extract_audio_languages(text: str) -> list:
    return analyze("", text)["audio"]["langs"]


def extract_subtitle_tag(text: str) -> str:
    return analyze("", text)["subtitle"]["tag"]


def extract_resolution(text: str) -> str:
    for r in RESOLUTION_LIST:
        if re.search(rf'\b{re.escape(r)}\b', text, re.I):
            return r
    return ""

_WEB_SERIES_RE = re.compile(r'\bweb[\s.\-]*series\b', re.I)
_WEB_RIP_RE = re.compile(r'\bweb[\s.\-]rip\b', re.I)


def extract_quality(text: str) -> str:
    text = _WEB_SERIES_RE.sub(" ", text)
    text = _WEB_RIP_RE.sub("WEBRip", text)
    for q in QUALITY_LIST:
        pattern = re.escape(q).replace(r'\ ', r'[\s.\-]*').replace(r'\-', r'[\s.\-_]?')
        if re.search(rf'\b{pattern}\b', text, re.I):
            return q
    return ""

def extract_source(text: str) -> str:
    for pattern, display in SOURCE_LIST:
        esc = re.escape(pattern).replace(r'\ ', r'[\s.\-]*')
        if re.search(rf'\b{esc}\b', text, re.I):
            return display
    return ""

def extract_video_codec(text: str) -> str:
    for c in VIDEO_CODEC_LIST:
        if re.search(rf'\b{re.escape(c)}\b', text, re.I):
            return c
    return ""

def extract_audio_codec(text: str) -> str:
    for codec in AUDIO_CODEC_LIST:
        pattern = re.escape(codec)
        m = re.search(
            rf'\b{pattern}(?:[- ]\d+[Kk]bps)?\b',
            text, re.I
        )
        if m:
            return codec
    return ""

def extract_bitrate(text: str) -> str:
    m = _BITRATE_RE.search(text)
    return f"{m.group(1)}Kbps" if m else ""

def extract_extension(text: str) -> str:
    m = re.search(r'\.(' + '|'.join(EXT_LIST) + r')\b', text, re.I)
    if m:
        return m.group(1).lower()
    for e in EXT_LIST:
        if re.search(rf'(?<![.\w]){re.escape(e)}(?![.\w])', text, re.I):
            return e.lower()
    return ""

_CODEC_CASE = {
    "truehd atmos": "TrueHD Atmos", "dolby atmos": "Dolby Atmos",
    "atmos": "Atmos", "truehd": "TrueHD",
}


def _format_audio_label(langs: list, text: str) -> str:
    if not langs:
        return ""

    m = re.search(
        r'\b(TrueHD\s+Atmos|Dolby\s+Atmos|DTS[\s\-]HD(?:\s+MA)?|DTS[\s\-]X|DTS|'
        r'DD\+?5\.1|DDP5\.1|DD\+?2\.0|DDP2\.0|DD\+|DDP|Atmos|TrueHD|'
        r'AAC5\.1|AAC2\.0|AAC|AC3|EAC3|MP3|FLAC|OPUS)'
        r'(?:[- ](\d{2,4}[Kk]bps))?\b',
        text, re.I
    )
    acodec_str = ""
    if m:
        codec_part   = _CODEC_CASE.get(m.group(1).lower().replace("  ", " "), m.group(1).upper())
        bitrate_part = re.sub(r"(?i)kbps$", "Kbps", m.group(2)) if m.group(2) else None
        acodec_str   = f" {codec_part}-{bitrate_part}" if bitrate_part else f" {codec_part}"
    else:
        br_m = _BITRATE_RE.search(text)
        if br_m:
            acodec_str = f" {br_m.group(1)}Kbps"

    if len(langs) == 1:
        return f"{langs[0]}{acodec_str}"

    parts = [f"{langs[0]}{acodec_str}"] + langs[1:]
    return " + ".join(parts)

def detect_media_type(text: str) -> str:
    if _ANIME_RE.search(text):
        return "anime"
    if _SERIES_RE.search(text):
        return "series"
    season, episode, _season_tag = extract_season_episode(text)
    if season or episode:
        return "series"
    return "movie"

_LEAD_HANDLE = re.compile(r"(?m)^[\s\[\(]*@[A-Za-z0-9]+_(?=[A-Za-z0-9]+_)")


def parse_file_info(filename: str, caption: str) -> dict:
    fn_in = strip_lead_noise(normalize_text(filename or ""), TITLE_STRIP_PREFIXES)
    fn_n = strip_promo_noise(strip_lead_noise(_LEAD_HANDLE.sub("", fn_in), TITLE_STRIP_PREFIXES))
    cap_n = strip_promo_noise(_LEAD_HANDLE.sub("", normalize_text(caption or "")))
    a = analyze(fn_n, cap_n, normalized=True)
    if fn_n and cap_n and site_marked(fn_n):
        alt = analyze("", cap_n, normalized=True)
        if prefer_caption_title(a, alt):
            a = alt
    eff_fn = a.get("filename", fn_n)
    ext_src = fn_n if fn_n else eff_fn
    raw = f"{eff_fn} {cap_n}".replace("\n", " ")
    tag = a["tag_text"] or raw

    title, year = a["title"]["value"], a["year"]["value"]
    title, year = imdb_enrich_title(title, year)
    season, episode, stag = extract_season_episode(raw)
    audio_langs = a["audio"]["langs"]
    resolution = extract_resolution(tag)
    quality = extract_quality(tag)
    source = extract_source(tag)
    vcodec = extract_video_codec(tag)
    acodec = extract_audio_codec(tag)
    bitrate = extract_bitrate(tag)
    ext = extract_extension(f"{ext_src} {raw}")
    audio_str = _format_audio_label(audio_langs, tag) if audio_langs else ""

    return {
        "title":       title,
        "year":        year,
        "season":      season,
        "episode":     episode,
        "season_tag":  stag,
        "audio":       audio_str,
        "audio_langs": audio_langs,
        "subtitle":    a["subtitle"]["tag"],
        "quality":     quality,
        "resolution":  resolution,
        "source":      source,
        "vcodec":      vcodec,
        "acodec":      acodec,
        "bitrate":     bitrate,
        "extension":   ext,
        "_raw":        raw,
        "_tag_text":   tag,
        "_meta": {
            "engine_version": ENGINE_VERSION,
            "title":    a["title"],
            "year":     a["year"],
            "audio":    {k: a["audio"][k] for k in ("langs", "conf", "source")},
            "subtitle": {k: a["subtitle"][k] for k in ("langs", "tag", "conf", "source")},
            "flags":    a["flags"],
            "dual_kw":  a["audio"]["dual"],
        },
    }


_FLAG_CASE = {
    "hd": "HD", "fhd": "FHD", "full hd": "Full HD", "hdr": "HDR", "hdr10": "HDR10",
    "hdr10+": "HDR10+", "dolby vision": "Dolby Vision", "dv": "DV",
}


def build_smart_filename(filename: str, caption: str, precomputed_info: dict = None) -> str:
    info        = precomputed_info if precomputed_info is not None else parse_file_info(filename, caption)
    raw_full    = info.get("_raw") or _build_clean_raw(filename, caption)
    raw         = info.get("_tag_text") or raw_full
    media_type  = detect_media_type(raw_full)
    audio_langs = info.get("audio_langs") or []
    parts: list = []

    if info["title"]:
        parts.append(info["title"])

    if info["season"] or info["episode"]:
        se = f"{info['season']} {info['episode']}".strip()
        parts.append(se)

    if info["season_tag"] and media_type != "movie":
        parts.append(f"({info['season_tag']})")

    if info["year"]:
        parts.append(f"({info['year']})")

    if audio_langs:
        audio_label = add_dub_tags(_format_audio_label(audio_langs, raw), audio_langs, raw_full)
        parts.append(f"({audio_label})")

    explicit_dual  = bool(_DUAL_RE.search(raw))
    explicit_multi = bool(_MULTI_RE.search(raw))
    if ((info.get("_meta") or {}).get("audio") or {}).get("source") == "label":
        explicit_dual = explicit_multi = False
    if len(audio_langs) >= 3 or explicit_multi:
        parts.append("Multi Audio")
    elif len(audio_langs) == 2 or explicit_dual:
        parts.append("Dual Audio")

    if _UNCUT_RE.search(raw):
        parts.append("UnCut")

    if _REMASTER_RE.search(raw):
        parts.append("Remastered")

    ext_m = _EXTENDED_RE.search(raw)
    if ext_m:
        parts.append(re.sub(r'[\s.\-]+', ' ', ext_m.group(0).strip()).title())

    if media_type == "movie":
        if _SOUTH_RE.search(raw):
            parts.append("South")
        elif _BOLLYWOOD_RE.search(raw):
            parts.append("Bollywood")
        elif _HOLLYWOOD_RE.search(raw):
            parts.append("Hollywood")

    completed = bool(_COMPLETED_RE.search(raw))

    if media_type == "series":
        series_label = "Web Series"
        s_m = _SERIES_RE.search(raw)
        if s_m:
            label = re.sub(r'[\s.]+', ' ', s_m.group(0).strip())
            label = re.sub(
                r'\b(Web|TV|Mini|OTT|Limited|Drama|Short)(Series)\b',
                r'\1 \2', label, flags=re.I
            )
            series_label = label.title()
        if completed:
            parts.append(f"Completed {series_label}")
        else:
            parts.append(series_label)
    elif media_type == "anime":
        parts.append("Anime")
    else:
        if _SOUTH_RE.search(raw) or _BOLLYWOOD_RE.search(raw) or _HOLLYWOOD_RE.search(raw):
            parts.append("Movie")
        elif re.search(r'\bMovie\b', raw, re.I):
            parts.append("Movie")

    hd_m = _HD_RE.search(raw)
    if hd_m:
        parts.append(_FLAG_CASE.get(re.sub(r'[\s.]+', ' ', hd_m.group(0).strip()).lower(), hd_m.group(0).strip()))

    hdr_labels: list = []
    for hdr_m in _HDR_RE.finditer(raw):
        label = _FLAG_CASE.get(re.sub(r'[\s.]+', ' ', hdr_m.group(0).strip()).lower(), hdr_m.group(0).strip())
        if label.upper() not in [l.upper() for l in hdr_labels]:
            hdr_labels.append(label)
    if hdr_labels:
        parts.append(" ".join(hdr_labels))

    if info["source"]:
        parts.append(info["source"])

    if info["vcodec"]:
        parts.append(info["vcodec"])

    if info["quality"]:
        parts.append(info["quality"])

    if info["resolution"]:
        parts.append(info["resolution"])

    if info["subtitle"]:
        parts.append(info["subtitle"])

    part = part_label(raw_full)
    if part:
        parts.append(part)

    if info["extension"]:
        parts.append(f".{info['extension']}")

    result = ""
    for p in parts:
        if p.startswith("."):
            result = result.rstrip() + p
        else:
            result = f"{result} {p}" if result else p

    return result.strip()

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
    text = re.sub(r'\(\s*\)', '', text)
    text = re.sub(r'\[\s*\]', '', text)
    text = re.sub(r'\{\s*\}', '', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    text = re.sub(r'[ \t]+\n', '\n', text)
    text = re.sub(r'\n[ \t]+', '\n', text)
    return text.strip()

DUMP_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U00002190-\U000021FF"
    "\U00002B00-\U00002BFF"
    "\U0001F1E6-\U0001F1FF"
    "\U0000FE0F"
    "\U0000200D"
    "]+",
    flags=re.UNICODE,
)

def strip_links_keep_layout(text: str) -> str:
    if not text:
        return text
    text = MD_LINK_RE.sub(r'\1', text)
    text = TG_USER_LINK_RE.sub(r'\1', text)
    text = HTML_A_RE.sub(r'\1', text)
    text = URL_RE.sub("", text)
    text = MENTION_RE.sub("", text)
    return text

def remove_emojis_keep_layout(text: str) -> str:
    if not text:
        return text
    for emo in EMOJI_LIST:
        text = text.replace(emo, "")
    return text

def remove_blocked_words_keep_layout(text: str, raw_blocked: str) -> str:
    if not text or not raw_blocked:
        return text
    items = sorted(
        {i.strip() for i in re.split(r"[,\n]+", raw_blocked) if i.strip()},
        key=len,
        reverse=True,
    )
    for item in items:
        text = re.sub(re.escape(item), "", text, flags=re.IGNORECASE)
    return text

def sanitize_dump_caption(text: str) -> str:
    if not text:
        return ""
    text = strip_links_only(text)
    text = remove_emojis(text)
    text = DUMP_EMOJI_RE.sub("", text)
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
        esc = re.escape(item)
        plain = re.sub(esc + r'[ \t]', '', plain, flags=re.IGNORECASE)
        plain = re.sub(r'[ \t]' + esc, '', plain, flags=re.IGNORECASE)
        plain = re.sub(esc, '', plain, flags=re.IGNORECASE)
    return plain

def parse_replace_pairs(raw):
    if not raw:
        return []
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
            new_text = pattern.sub(lambda m, _new=new: _new, new_text)
        except re.error:
            new_text = new_text.replace(old, new)
    return new_text


WORD_KIND_LABELS = {
    "block":   "blocked word(s)",
    "replace": "replace-word pair(s)",
}

def _split_items(raw) -> List[str]:
    if not raw:
        return []
    if not isinstance(raw, str):
        raw = str(raw)
    return [item.strip() for item in re.split(r'[,\n]+', raw) if item.strip()]

def format_items_preview(raw) -> str:
    return ", ".join(_split_items(raw))

def merge_items(old_raw: str, new_raw: str) -> Tuple[str, List[str], List[str]]:
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

    return True, None, ", ".join(items)

async def _get_kind_raw(channel_id: int, kind: str) -> str:
    if kind == "block":
        return await get_block_words(channel_id)
    if kind == "replace":
        return await get_replace_words(channel_id) or ""
    return ""

async def _save_kind_raw(channel_id: int, kind: str, raw: str):
    if kind == "block":
        await set_block_words(channel_id, raw)
    elif kind == "replace":
        await set_replace_words(channel_id, raw)


_NOT_A_COMMAND = filters.create(
    lambda _, __, m: not (m.text or m.caption or "").startswith("/")
)


@Client.on_message(filters.private & _NOT_A_COMMAND)
async def capture_user_input(client, message):
    user_id = message.from_user.id

    active_users = set()
    for key in ("caption_set", "block_words_set", "replace_words_set",
                "prefix_set", "suffix_set", "url_set", "word_delete_set"):
        active_users.update(bot_data.get(key, {}).keys())
    active_users.update(FF_SESSIONS.keys())
    active_users.update(MF_SESSIONS.keys())
    active_users.update(GS_SESSIONS.keys())
    if user_id not in active_users:
        return

    text = (
        message.text.html if message.text else
        message.caption.html if message.caption else
        ""
    )

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

    if user_id in bot_data.get("prefix_set", {}):
        if not text.strip():
            return
        session      = bot_data["prefix_set"].pop(user_id)
        channel_id   = session["channel_id"]
        instr_msg_id = session["instr_msg_id"]

        await set_prefix(channel_id, text.strip())

        try:
            await client.delete_messages(user_id, message.id)
        except Exception:
            pass

        await client.edit_message_text(
            chat_id=user_id,
            message_id=instr_msg_id,
            text="✅ <b>Prefix updated!</b>",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("↩ Back", callback_data=f"back_to_suffixprefix_{channel_id}")]]
            ),
        )
        return

    if user_id in bot_data.get("suffix_set", {}):
        if not text.strip():
            return
        session      = bot_data["suffix_set"].pop(user_id)
        channel_id   = session["channel_id"]
        instr_msg_id = session["instr_msg_id"]

        await set_suffix(channel_id, text.strip())

        try:
            await client.delete_messages(user_id, message.id)
        except Exception:
            pass

        await client.edit_message_text(
            chat_id=user_id,
            message_id=instr_msg_id,
            text="✅ <b>Suffix updated!</b>",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("↩ Back", callback_data=f"back_to_suffixprefix_{channel_id}")]]
            ),
        )
        return

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
        lines = [f"🗑 <b>{label[0].upper()}{label[1:]} — delete specific</b>"]
        if removed:
            lines.append(f"✅ Removed: {', '.join(removed)}")
        if not_found:
            lines.append(f"⚠️ Not found (skipped): {', '.join(not_found)}")
        if not removed and not not_found:
            lines.append("⚠️ Nothing matched — no changes made.")

        back_cb = {
            "block":   f"back_to_blockwords_{channel_id}",
            "replace": f"back_to_replace_{channel_id}",
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

    if user_id in bot_data.get("url_set", {}):
        if not text.strip():
            return
        session      = bot_data["url_set"].pop(user_id)
        channel_id   = session["channel_id"]
        instr_msg_id = session["instr_msg_id"]
        rows  = []
        normalized = (
            text.strip()
            .replace("\u201c", '"').replace("\u201d", '"')
            .replace("\u2018", "'").replace("\u2019", "'")
        )
        lines = normalized.splitlines()
        for line in lines:
            row   = []
            parts = [p.strip() for p in line.split("|") if p.strip()]
            for part in parts:
                matched = re.findall(r'"([^"]+)"', part)
                if len(matched) == 2:
                    btn_text, btn_url = matched[0].strip(), matched[1].strip()
                    if btn_text and btn_url:
                        if not re.match(r'^[a-zA-Z][a-zA-Z0-9+.\-]*://', btn_url) and not btn_url.startswith("tg://") and not btn_url.startswith("mailto:"):
                            btn_url = f"https://{btn_url}"
                        row.append({"text": btn_text, "url": btn_url})
            if row:
                rows.append(row)
        if not rows:
            bot_data.setdefault("url_set", {})[user_id] = session
            await message.reply_text(
                "❌ Invalid format. Each button needs a text and a URL in quotes:\n"
                '<code>"Button Text" "https://example.com"</code>'
            )
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

    if user_id in GS_SESSIONS:
        if await handle_gen_session_text(client, message):
            return

    if user_id in MF_SESSIONS:
        if await handle_member_forward_text(client, message):
            return

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
