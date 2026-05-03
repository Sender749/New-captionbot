import re
import asyncio
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.enums import ParseMode
from body.database import *
from info import *
from Script import script
from body.Caption import bot_data, _maintenance_block
from pyrogram.errors import RPCError, ChatAdminRequired, ChatWriteForbidden

FONT_TXT = script.FONT_TXT


# ======================== CHANNEL SETTINGS ========================
@Client.on_callback_query(filters.regex(r'^chinfo_(-?\d+)$'))
async def channel_settings(client, query):
    await query.answer()
    if await _maintenance_block(client, query):
        return
    channel_id = int(query.matches[0].group(1))
    cap_doc    = await get_channel_cached(channel_id)

    # Get title from cache first (fast), fall back to get_chat only if missing
    chat_title = cap_doc.get("_title")
    if not chat_title:
        try:
            chat       = await client.get_chat(channel_id)
            chat_title = getattr(chat, "title", str(channel_id))
            await set_channel_title_cache(channel_id, chat_title)
        except Exception:
            chat_title = str(channel_id)

    caption     = cap_doc.get("caption", "")
    prefix      = cap_doc.get("prefix", "")
    suffix      = cap_doc.get("suffix", "")
    link_status = bool(cap_doc.get("link_remover", False))
    link_text   = "Link & Usernames Remover (ON)" if link_status else "Link & Usernames Remover (OFF)"
    emoji_status= bool(cap_doc.get("emoji_remover", False))
    emoji_text  = "Emoji Remover (ON)" if emoji_status else "Emoji Remover (OFF)"

    if not caption:
        caption_preview = "❌ No caption set for this channel."
    else:
        if prefix and suffix:
            caption_preview = f"{prefix}\n{caption}\n{suffix}"
        elif prefix:
            caption_preview = f"{prefix}\n{caption}"
        elif suffix:
            caption_preview = f"{caption}\n{suffix}"
        else:
            caption_preview = caption

    text = (
        f"⚙️ **Manage Channel:** {chat_title}\n\n"
        f"📝 **Current Caption :**\n{caption_preview}\n\n"
        f"Choose what you want to configure 👇"
    )
    buttons = [
        [InlineKeyboardButton("📝 Set Caption",           callback_data=f"setcap_{channel_id}")],
        [InlineKeyboardButton("🧹 Set Words Remover",      callback_data=f"setwords_{channel_id}")],
        [InlineKeyboardButton("🔤 Set Prefix & Suffix",    callback_data=f"set_suffixprefix_{channel_id}")],
        [InlineKeyboardButton("🔄 Set Replace Words",      callback_data=f"setreplace_{channel_id}")],
        [InlineKeyboardButton("🔘 Button URL",             callback_data=f"seturl_{channel_id}")],
        [InlineKeyboardButton(f"🔗 {link_text}",           callback_data=f"togglelink_{channel_id}")],
        [InlineKeyboardButton(f"😀 {emoji_text}",          callback_data=f"toggleemoji_{channel_id}")],
        [InlineKeyboardButton("♻️ Reset Channel Settings", callback_data=f"reset_channel_{channel_id}")],
        [InlineKeyboardButton("↩ Back",  callback_data="settings_cb"),
         InlineKeyboardButton("❌ Close", callback_data="close_msg")],
    ]
    try:
        await query.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(buttons),
            disable_web_page_preview=True,
        )
    except Exception:
        await query.answer("⚠️ Caption too long to display fully.", show_alert=True)


# ======================== CAPTION MENU ========================
@Client.on_callback_query(filters.regex(r'^setcap_(-?\d+)$'))
async def set_caption_menu(client, query):
    await query.answer()
    channel_id      = int(query.matches[0].group(1))
    cap_doc         = await get_channel_cached(channel_id)
    chat_title      = cap_doc.get("_title", str(channel_id))
    current_caption = cap_doc.get("caption")
    caption_display = (
        f"📝 **Current Caption:**\n{current_caption}"
        if current_caption else
        "📝 **Current Caption:** None set yet."
    )
    text    = (
        f"⚙️ **Channel:** {chat_title}\n"
        f"{caption_display}\n\n"
        f"Choose what you want to do 👇"
    )
    buttons = [
        [InlineKeyboardButton("🆕 Set Caption",   callback_data=f"setcapmsg_{channel_id}"),
         InlineKeyboardButton("❌ Delete Caption", callback_data=f"delcap_{channel_id}")],
        [InlineKeyboardButton("🔤 Caption Font",   callback_data=f"capfont_{channel_id}")],
        [InlineKeyboardButton("↩ Back",            callback_data=f"chinfo_{channel_id}")],
    ]
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))


@Client.on_callback_query(filters.regex(r'^setcapmsg_(-?\d+)$'))
async def set_caption_message(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    user_id    = query.from_user.id
    bot_data.get("caption_set", {}).pop(user_id, None)

    instr = await query.message.edit_text(
        text=(
            "📌 <b>Send caption for this channel</b>\n\n"
            "<blockquote expandable>"
            "📦 <b>Placeholders</b>\n\n"
            "<b>🗂 File Info</b>\n"
            "File name ⇛ <code>{file_name}</code>\n"
            "File size ⇛ <code>{file_size}</code>\n"
            "Extension ⇛ <code>{extension}</code>\n"
            "Duration ⇛ <code>{duration}</code>\n"
            "Original caption ⇛ <code>{default_caption}</code>\n"
            "Empty line ⇛ <code>{empty}</code>\n\n"
            "<b>🎬 Smart File Name</b>\n"
            "Full smart name ⇛ <code>{smart_file_name}</code>\n"
            "  ↳ Auto-builds clean caption from filename\n"
            "  ↳ Detects title, year, season, episode,\n"
            "     audio, subtitle, quality, codec, ext\n\n"
            "<b>🏷 Individual Smart Fields</b>\n"
            "Title ⇛ <code>{title}</code>\n"
            "Year ⇛ <code>{year}</code>\n"
            "Season ⇛ <code>{season}</code>\n"
            "Episode ⇛ <code>{episode}</code>\n"
            "Audio / Language ⇛ <code>{audio}</code>\n"
            "Language (alt) ⇛ <code>{language}</code>\n"
            "Subtitle tag ⇛ <code>{subtitle}</code>\n"
            "Quality ⇛ <code>{quality}</code>\n"
            "Resolution ⇛ <code>{resolution}</code>\n"
            "Video codec ⇛ <code>{vcodec}</code>\n"
            "Audio codec ⇛ <code>{acodec}</code>\n"
            "Source ⇛ <code>{source}</code>"
            "</blockquote>\n\n"
            "<blockquote expandable>"
            "🖋 <b>Text Styles</b>\n\n"
            "Bold ⇛ <code>&lt;b&gt;Text&lt;/b&gt;</code>\n"
            "Italic ⇛ <code>&lt;i&gt;Text&lt;/i&gt;</code>\n"
            "Underline ⇛ <code>&lt;u&gt;Text&lt;/u&gt;</code>\n"
            "Strike ⇛ <code>&lt;s&gt;Text&lt;/s&gt;</code>\n"
            "Mono ⇛ <code>&lt;code&gt;Text&lt;/code&gt;</code>\n"
            "Spoiler ⇛ <code>&lt;spoiler&gt;Text&lt;/spoiler&gt;</code>\n"
            "Pre ⇛ <code>&lt;pre&gt;Text&lt;/pre&gt;</code>\n"
            "Block Quote ⇛ <code>&lt;blockquote&gt;Text&lt;/blockquote&gt;</code>\n"
            "Link ⇛ <code>&lt;a href=\"url\"&gt;Text&lt;/a&gt;</code>"
            "</blockquote>\n\n"
            "✍️ <b>Example:</b>\n"
            "<code>&lt;b&gt;{title}&lt;/b&gt; {season}{episode} ({year})\n"
            "{audio} | {quality} | {subtitle}\n"
            "💾 {file_size}</code>"
        ),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩ Back", callback_data=f"setcap_{channel_id}")]]
        ),
    )
    bot_data.setdefault("caption_set", {})[user_id] = {
        "channel_id":   channel_id,
        "instr_msg_id": instr.id,
    }


@Client.on_callback_query(filters.regex(r"^back_to_captionmenu_(-?\d+)$"))
async def back_to_caption_menu(client, query):
    await query.answer()
    await set_caption_menu(client, query)


@Client.on_callback_query(filters.regex(r'^delcap_(-?\d+)$'))
async def delete_caption(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    await delete_channel_caption(channel_id)
    await query.message.edit_text(
        "✅ Caption deleted.\n❌ No caption set currently.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩ Back", callback_data=f"setcap_{channel_id}")]]
        ),
    )


@Client.on_callback_query(filters.regex(r'^capfont_(-?\d+)$'))
async def caption_font(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    cap_doc    = await get_channel_cached(channel_id)
    cap_txt    = cap_doc.get("caption") or "No custom caption set."
    await query.message.edit_text(
        f"📝 Current Caption: {cap_txt}\n\n🖋️ Available Fonts:\n\n{FONT_TXT}",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩ Back", callback_data=f"setcap_{channel_id}")]]
        ),
    )


# ======================== WORDS REMOVER ========================
@Client.on_callback_query(filters.regex(r"^setwords_(-?\d+)$"))
async def set_words_menu(client, query):
    await query.answer()
    channel_id    = int(query.matches[0].group(1))
    cap_doc       = await get_channel_cached(channel_id)
    chat_title    = cap_doc.get("_title", str(channel_id))
    blocked_words = cap_doc.get("block_words", "")
    words_text    = (
        "\n".join(
            f"• {w.strip()}"
            for w in re.split(r"[,\n]+", blocked_words)
            if w.strip()
        )
        if blocked_words else "None set yet."
    )
    text    = (
        f"📛 **Channel:** {chat_title}\n\n"
        f"🚫 **Blocked Words:**\n{words_text}\n\n"
        f"Choose what you want to do 👇"
    )
    buttons = [
        [InlineKeyboardButton("📝 Set Block Words",   callback_data=f"addwords_{channel_id}"),
         InlineKeyboardButton("🗑️ Delete Block Words", callback_data=f"delwords_{channel_id}")],
        [InlineKeyboardButton("↩ Back",               callback_data=f"chinfo_{channel_id}")],
    ]
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))


@Client.on_callback_query(filters.regex(r"^addwords_(-?\d+)$"))
async def set_block_words_message(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    user_id    = query.from_user.id
    bot_data.get("block_words_set", {}).pop(user_id, None)
    instr = await query.message.edit_text(
        text=(
            "🚫 Send me the **blocked words** for this channel.\n"
            "Separate words using commas.\n\n"
            "Example:\n"
            "<code>spam, fake, scam</code>\n\n"
        ),
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩ Back", callback_data=f"setwords_{channel_id}")]]
        ),
    )
    bot_data.setdefault("block_words_set", {})[user_id] = {
        "channel_id":   channel_id,
        "instr_msg_id": instr.id,
    }


@Client.on_callback_query(filters.regex(r"^back_to_blockwords_(-?\d+)$"))
async def back_to_blockwords_menu(client, query):
    await query.answer()
    bot_data.get("block_words_set", {}).pop(query.from_user.id, None)
    await set_words_menu(client, query)


@Client.on_callback_query(filters.regex(r"^delwords_(-?\d+)$"))
async def delete_blocked_words(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    await delete_block_words(channel_id)
    cap_doc    = await get_channel_cached(channel_id)
    chat_title = cap_doc.get("_title", str(channel_id))
    await query.message.edit_text(
        f"✅ **All blocked words deleted successfully.**\n\n📛 **Channel:** {chat_title}",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩ Back", callback_data=f"setwords_{channel_id}")]]
        ),
    )


# ======================== SUFFIX & PREFIX ========================
@Client.on_callback_query(filters.regex(r'^set_suffixprefix_(-?\d+)$'))
async def suffix_prefix_menu(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    cap_doc    = await get_channel_cached(channel_id)
    chat_title = cap_doc.get("_title", str(channel_id))
    suffix     = cap_doc.get("suffix", "")
    prefix     = cap_doc.get("prefix", "")
    buttons    = [
        [InlineKeyboardButton("Set Suffix", callback_data=f"set_suf_{channel_id}"),
         InlineKeyboardButton("Del Suffix", callback_data=f"del_suf_{channel_id}")],
        [InlineKeyboardButton("Set Prefix", callback_data=f"set_pre_{channel_id}"),
         InlineKeyboardButton("Del Prefix", callback_data=f"del_pre_{channel_id}")],
        [InlineKeyboardButton("↩ Back",     callback_data=f"chinfo_{channel_id}")],
    ]
    await query.message.edit_text(
        f"📌 Channel: {chat_title}\n\nCurrent Suffix: {suffix or 'None'}\nCurrent Prefix: {prefix or 'None'}",
        reply_markup=InlineKeyboardMarkup(buttons),
    )


@Client.on_callback_query(filters.regex(r"^back_to_suffixprefix_(-?\d+)$"))
async def back_to_suffixprefix_menu(client, query):
    await query.answer()
    await suffix_prefix_menu(client, query)


@Client.on_callback_query(filters.regex(r'^set_suf_(-?\d+)$'))
async def set_suffix_message(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    user_id    = query.from_user.id
    instr = await query.message.edit_text(
        "🖋️ Send the suffix text you want to add to your captions.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩ Back", callback_data=f"set_suffixprefix_{channel_id}")]]
        ),
    )
    bot_data.setdefault("suffix_set", {})[user_id] = {
        "channel_id":   channel_id,
        "instr_msg_id": instr.id,
    }


@Client.on_callback_query(filters.regex(r'^set_pre_(-?\d+)$'))
async def set_prefix_message(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    user_id    = query.from_user.id
    instr = await query.message.edit_text(
        "✍️ Send the prefix text you want to add to your captions.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩ Back", callback_data=f"set_suffixprefix_{channel_id}")]]
        ),
    )
    bot_data.setdefault("prefix_set", {})[user_id] = {
        "channel_id":   channel_id,
        "instr_msg_id": instr.id,
    }


@Client.on_callback_query(filters.regex(r'^del_suf_(-?\d+)$'))
async def delete_suffix_cb(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    await delete_suffix(channel_id)
    await query.message.edit_text(
        "✅ Suffix deleted.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩ Back", callback_data=f"set_suffixprefix_{channel_id}")]]
        ),
    )


@Client.on_callback_query(filters.regex(r'^del_pre_(-?\d+)$'))
async def delete_prefix_cb(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    await delete_prefix(channel_id)
    await query.message.edit_text(
        "✅ Prefix deleted.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩ Back", callback_data=f"set_suffixprefix_{channel_id}")]]
        ),
    )


# ======================== REPLACE WORDS ========================
@Client.on_callback_query(filters.regex(r"^setreplace_(-?\d+)$"))
async def set_replace_menu(client, query):
    await query.answer()
    channel_id  = int(query.matches[0].group(1))
    cap_doc     = await get_channel_cached(channel_id)
    chat_title  = cap_doc.get("_title", str(channel_id))
    replace_raw = cap_doc.get("replace_words", "")
    replace_text= (
        "\n".join(l.strip() for l in replace_raw.splitlines() if l.strip())
        if replace_raw else "None set yet."
    )
    text    = (
        f"📛 **Channel:** {chat_title}\n\n"
        f"🔤 **Replace Words:**\n{replace_text}\n\n"
        f"Choose what you want to do 👇"
    )
    buttons = [
        [InlineKeyboardButton("📝 Set Replace Words",   callback_data=f"addreplace_{channel_id}"),
         InlineKeyboardButton("🗑️ Delete Replace Words", callback_data=f"delreplace_{channel_id}")],
        [InlineKeyboardButton("↩ Back",                 callback_data=f"chinfo_{channel_id}")],
    ]
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))


@Client.on_callback_query(filters.regex(r"^addreplace_(-?\d+)$"))
async def set_replace_words_message(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    user_id    = query.from_user.id
    bot_data.get("replace_words_set", {}).pop(user_id, None)
    instr = await query.message.edit_text(
        text=(
            "🔤 Send me the **replace words** for this channel.\n"
            "Use format: `old new, another_old another_new`\n\n"
            "Example:\n"
            "<code>spam scam, fake real</code>\n\n"
        ),
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩ Back", callback_data=f"setreplace_{channel_id}")]]
        ),
    )
    bot_data.setdefault("replace_words_set", {})[user_id] = {
        "channel_id":   channel_id,
        "instr_msg_id": instr.id,
    }


@Client.on_callback_query(filters.regex(r"^back_to_replace_(-?\d+)$"))
async def back_to_replace_menu(client, query):
    await query.answer()
    bot_data.get("replace_words_set", {}).pop(query.from_user.id, None)
    await set_replace_menu(client, query)


@Client.on_callback_query(filters.regex(r"^delreplace_(-?\d+)$"))
async def delete_replace_words(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    await delete_replace_words_db(channel_id)
    cap_doc    = await get_channel_cached(channel_id)
    chat_title = cap_doc.get("_title", str(channel_id))
    await query.message.edit_text(
        f"✅ **All replace words deleted successfully.**\n\n📛 **Channel:** {chat_title}",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩ Back", callback_data=f"setreplace_{channel_id}")]]
        ),
    )


# ======================== URL BUTTONS ========================
@Client.on_callback_query(filters.regex(r"^seturl_(-?\d+)$"))
async def url_button_menu(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    cap_doc    = await get_channel_cached(channel_id)
    chat_title = cap_doc.get("_title", str(channel_id))
    url_btns   = cap_doc.get("url_buttons", [])
    if url_btns:
        lines   = [" | ".join(f"[{b['text']}]({b['url']})" for b in row) for row in url_btns]
        preview = "\n".join(f"• {l}" for l in lines)
    else:
        preview = "❌ No URL buttons set."
    text = (
        f"🔘 **Channel:** {chat_title}\n\n"
        f"🔗 **Current URL Buttons:**\n{preview}\n\n"
        "Choose an option 👇"
    )
    keyboard = [
        [InlineKeyboardButton("➕ Set URL",  callback_data=f"seturlmsg_{channel_id}"),
         InlineKeyboardButton("🗑 Delete URL", callback_data=f"delurl_{channel_id}")],
        [InlineKeyboardButton("↩ Back",       callback_data=f"chinfo_{channel_id}")],
    ]
    await query.message.edit_text(
        text, reply_markup=InlineKeyboardMarkup(keyboard), disable_web_page_preview=True
    )


@Client.on_callback_query(filters.regex(r"^seturlmsg_(-?\d+)$"))
async def set_url_message(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    user_id    = query.from_user.id
    bot_data.setdefault("url_set", {}).pop(user_id, None)
    instr = await query.message.edit_text(
        text=(
            '🔗 <b>Send URL buttons in this format:</b>\n\n'
            '<code>"Button 1" "url1" | "Button 2" "url2"</code>\n'
            '<code>"Button 3" "url3"</code>\n\n'
            '• Use <b>|</b> to put buttons in the same row\n'
            '• Use new line for next row'
        ),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data=f"url_cancel_{channel_id}")]]
        ),
    )
    bot_data["url_set"][user_id] = {"channel_id": channel_id, "instr_msg_id": instr.id}


@Client.on_callback_query(filters.regex(r"^url_cancel_(-?\d+)$"))
async def cancel_url_set(client, query):
    await query.answer()
    bot_data.get("url_set", {}).pop(query.from_user.id, None)
    await url_button_menu(client, query)


@Client.on_callback_query(filters.regex(r"^delurl_(-?\d+)$"))
async def delete_url_buttons_cb(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    await delete_url_buttons(channel_id)
    await query.message.edit_text(
        "✅ **All URL buttons deleted successfully.**",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩ Back", callback_data=f"seturl_{channel_id}")]]
        ),
    )


# ======================== LINK REMOVER ========================
@Client.on_callback_query(filters.regex(r'^togglelink_(-?\d+)$'))
async def toggle_link_remover(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    current    = await get_link_remover_status(channel_id)
    await set_link_remover_status(channel_id, not current)
    await channel_settings(client, query)


# ======================== EMOJI REMOVER ========================
@Client.on_callback_query(filters.regex(r'^toggleemoji_(-?\d+)$'))
async def toggle_emoji_remover(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    current    = await get_emoji_remover_status(channel_id)
    await set_emoji_remover_status(channel_id, not current)
    await channel_settings(client, query)


# ======================== RESET CHANNEL ========================
@Client.on_callback_query(filters.regex(r"^reset_channel_(-?\d+)$"))
async def reset_channel_settings(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    await delete_channel_caption(channel_id)
    await delete_block_words(channel_id)
    await delete_replace_words_db(channel_id)
    await delete_prefix(channel_id)
    await delete_suffix(channel_id)
    await set_link_remover_status(channel_id, False)
    await set_emoji_remover_status(channel_id, False)
    try:
        from body.database import _CHANNEL_CACHE
        _CHANNEL_CACHE.pop(channel_id, None)
    except Exception:
        pass
    try:
        await query.message.edit_text("♻️ Channel settings reset successfully.")
        await asyncio.sleep(1)
        await channel_settings(client, query)
    except Exception:
        pass
