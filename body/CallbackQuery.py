import re
import asyncio
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.enums import ParseMode
from body.database import *
from info import *
from Script import script
from body.Caption import bot_data, _is_admin_member, user_settings, get_bot_me

FONT_TXT = script.FONT_TXT

# ─── Placeholder reference shown in collapsed blockquote ───────────────────
PLACEHOLDER_HELP = (
    "🔹 <b>Available Placeholders</b>\n\n"
    "<blockquote expandable>"
    "  <code>{file_name}</code> — Raw file name\n"
    "  <code>{smart_file_name}</code> — Auto-built smart name\n"
    "  <code>{file_size}</code> — File size (e.g. 1.23 GB)\n"
    "  <code>{default_caption}</code> — Original caption as-is\n"
    "  <code>{resolution}</code> — Video resolution (e.g. 1920x1080)\n"
    "  <code>{duration}</code> — Video duration (e.g. 02:15:30)\n"
    "  <code>{empty}</code> — Empty string (use to clear a line)\n\n"
    "  <code>{language}</code> — Detected audio languages\n"
    "  <code>{year}</code> — Release year (e.g. 2024)\n\n"
    "</blockquote>"

    "🔹 <b>Text Styles</b>\n"
    "<blockquote expandable>"
    "  <code>&lt;b&gt;Text&lt;/b&gt;</code> — Bold\n"
    "  <code>&lt;i&gt;Text&lt;/i&gt;</code> — Italic\n"
    "  <code>&lt;u&gt;Text&lt;/u&gt;</code> — Underline\n"
    "  <code>&lt;s&gt;Text&lt;/s&gt;</code> — Strikethrough\n"
    "  <code>&lt;code&gt;Text&lt;/code&gt;</code> — Mono\n"
    "  <code>&lt;spoiler&gt;Text&lt;/spoiler&gt;</code> — Spoiler\n"
    "  <code>&lt;pre&gt;Text&lt;/pre&gt;</code> — Preformatted\n"
    "  <code>&lt;blockquote&gt;Text&lt;/blockquote&gt;</code> — Quote\n"
    "  <code>&lt;blockquote expandable&gt;…&lt;/blockquote&gt;</code> — Collapsible\n"
    "  <code>&lt;a href=\"url\"&gt;Text&lt;/a&gt;</code> — Hyperlink\n\n"
    "</blockquote>"
    "✍️ <b>Example Caption</b>\n"
    "  <code>&lt;b&gt;{smart_file_name}&lt;/b&gt;</code>\n"
    "  <code>📦 {file_size} | 🗓 {year} | 🌐 {language}</code>"
)


async def _get_chat_title(client, channel_id: int) -> str:
    cached = get_cached_chat_title(channel_id)
    if cached:
        return cached
    try:
        chat = await client.get_chat(channel_id)
        title = getattr(chat, "title", str(channel_id))
        set_cached_chat_title(channel_id, title)
        return title
    except Exception:
        return str(channel_id)


# ═══════════════════════════════ CHANNEL INFO ══════════════════════════════
@Client.on_callback_query(filters.regex(r'^chinfo_(-?\d+)$'))
async def channel_settings(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    chat_title = await _get_chat_title(client, channel_id)
    cap_doc    = await get_channel_cached(channel_id)

    caption      = cap_doc.get("caption", "")
    prefix       = cap_doc.get("prefix", "")
    suffix       = cap_doc.get("suffix", "")
    link_status  = bool(cap_doc.get("link_remover", False))
    emoji_status = bool(cap_doc.get("emoji_remover", False))

    link_text  = "Link & Usernames Remover (ON ✅)" if link_status  else "Link & Usernames Remover (OFF ❌)"
    emoji_text = "Emoji Remover (ON ✅)"             if emoji_status else "Emoji Remover (OFF ❌)"

    if not caption:
        caption_preview = "❌ No caption set."
    else:
        parts = []
        if prefix: parts.append(prefix)
        parts.append(caption)
        if suffix: parts.append(suffix)
        caption_preview = "\n".join(parts)

    text = (
        f"⚙️ <b>Manage Channel:</b> {chat_title}\n\n"
        f"📝 <b>Current Caption:</b>\n{caption_preview}\n\n"
        "Choose what you want to configure 👇"
    )
    buttons = [
        [InlineKeyboardButton("📝 Set Caption",           callback_data=f"setcap_{channel_id}")],
        [InlineKeyboardButton("🧹 Set Words Remover",     callback_data=f"setwords_{channel_id}")],
        [InlineKeyboardButton("🔤 Set Prefix & Suffix",   callback_data=f"set_suffixprefix_{channel_id}")],
        [InlineKeyboardButton("🔄 Set Replace Words",     callback_data=f"setreplace_{channel_id}")],
        [InlineKeyboardButton("🔘 Button URL",            callback_data=f"seturl_{channel_id}")],
        [InlineKeyboardButton(f"🔗 {link_text}",          callback_data=f"togglelink_{channel_id}")],
        [InlineKeyboardButton(f"😀 {emoji_text}",         callback_data=f"toggleemoji_{channel_id}")],
        [InlineKeyboardButton("♻️ Reset Channel Settings", callback_data=f"reset_channel_{channel_id}")],
        [InlineKeyboardButton("🗑️ Delete Channel",        callback_data=f"del_ch_confirm_{channel_id}")],
        [InlineKeyboardButton("↩ Back", callback_data="settings_cb"),
         InlineKeyboardButton("❌ Close", callback_data="close_msg")],
    ]
    try:
        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons),
                                      disable_web_page_preview=True)
    except Exception:
        await query.answer("⚠️ Caption too long to display.", show_alert=True)


# ═══════════════════════════════ CAPTION MENU ══════════════════════════════
@Client.on_callback_query(filters.regex(r'^setcap_(-?\d+)$'))
async def set_caption_menu(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    chat_title = await _get_chat_title(client, channel_id)
    cap_data   = await get_channel_caption(channel_id)
    current    = cap_data.get("caption") if cap_data else None
    cap_display = f"📝 <b>Current Caption:</b>\n{current}" if current else "📝 <b>Current Caption:</b> None set."
    buttons = [
        [InlineKeyboardButton("🆕 Set Caption",  callback_data=f"setcapmsg_{channel_id}"),
         InlineKeyboardButton("❌ Delete",        callback_data=f"delcap_{channel_id}")],
        [InlineKeyboardButton("🔤 Caption Font",  callback_data=f"capfont_{channel_id}")],
        [InlineKeyboardButton("↩ Back",           callback_data=f"chinfo_{channel_id}")],
    ]
    await query.message.edit_text(
        f"⚙️ <b>Channel:</b> {chat_title}\n{cap_display}\n\nChoose what to do 👇",
        reply_markup=InlineKeyboardMarkup(buttons)
    )


@Client.on_callback_query(filters.regex(r'^setcapmsg_(-?\d+)$'))
async def set_caption_message(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    user_id = query.from_user.id
    bot_data.get("caption_set", {}).pop(user_id, None)

    text = (
        "📌 <b>Send your caption for this channel</b>\n\n"
        + PLACEHOLDER_HELP
    )
    instr = await query.message.edit_text(
        text,
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back", callback_data=f"setcap_{channel_id}")]])
    )
    bot_data.setdefault("caption_set", {})[user_id] = {
        "channel_id": channel_id,
        "instr_msg_id": instr.id
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
    invalidate_channel_cache(channel_id)
    await query.message.edit_text(
        "✅ Caption deleted.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back", callback_data=f"setcap_{channel_id}")]]))


@Client.on_callback_query(filters.regex(r'^capfont_(-?\d+)$'))
async def caption_font(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    current_cap = await get_channel_caption(channel_id)
    cap_txt = current_cap.get("caption") if current_cap else "No custom caption set."
    await query.message.edit_text(
        f"📝 Current Caption: {cap_txt}\n\n🖋️ Available Fonts:\n\n{FONT_TXT}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back", callback_data=f"setcap_{channel_id}")]]))


# ═══════════════════════════════ WORDS REMOVER ═════════════════════════════
@Client.on_callback_query(filters.regex(r"^setwords_(-?\d+)$"))
async def set_words_menu(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    chat_title = await _get_chat_title(client, channel_id)
    blocked = await get_block_words(channel_id)
    words_text = "\n".join(f"• {w.strip()}" for w in re.split(r"[,\n]+", blocked) if w.strip()) if blocked else "None set yet."
    buttons = [
        [InlineKeyboardButton("📝 Set Block Words",  callback_data=f"addwords_{channel_id}"),
         InlineKeyboardButton("🗑️ Delete",          callback_data=f"delwords_{channel_id}")],
        [InlineKeyboardButton("↩ Back",              callback_data=f"chinfo_{channel_id}")],
    ]
    await query.message.edit_text(
        f"📛 <b>Channel:</b> {chat_title}\n\n🚫 <b>Blocked Words:</b>\n{words_text}\n\nChoose 👇",
        reply_markup=InlineKeyboardMarkup(buttons))


@Client.on_callback_query(filters.regex(r"^addwords_(-?\d+)$"))
async def set_block_words_message(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    user_id = query.from_user.id
    bot_data.get("block_words_set", {}).pop(user_id, None)
    instr = await query.message.edit_text(
        "🚫 Send the <b>blocked words</b> for this channel.\n"
        "Separate with commas.\n\n"
        "Example: <code>spam, fake, scam</code>",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back", callback_data=f"setwords_{channel_id}")]]))
    bot_data.setdefault("block_words_set", {})[user_id] = {"channel_id": channel_id, "instr_msg_id": instr.id}


@Client.on_callback_query(filters.regex(r"^back_to_blockwords_(-?\d+)$"))
async def back_to_blockwords_menu(client, query):
    await query.answer()
    bot_data.get("block_words_set", {}).pop(query.from_user.id, None)
    await set_words_menu(client, query)


@Client.on_callback_query(filters.regex(r"^delwords_(-?\d+)$"))
async def delete_blocked_words(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    chat_title = await _get_chat_title(client, channel_id)
    await delete_block_words(channel_id)
    await query.message.edit_text(
        f"✅ All blocked words deleted.\n\n📛 <b>Channel:</b> {chat_title}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back", callback_data=f"setwords_{channel_id}")]]))


# ═══════════════════════════ SUFFIX & PREFIX ═══════════════════════════════
@Client.on_callback_query(filters.regex(r'^set_suffixprefix_(-?\d+)$'))
async def suffix_prefix_menu(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    chat_title = await _get_chat_title(client, channel_id)
    suffix, prefix = await get_suffix_prefix(channel_id)
    buttons = [
        [InlineKeyboardButton("Set Suffix", callback_data=f"set_suf_{channel_id}"),
         InlineKeyboardButton("Del Suffix", callback_data=f"del_suf_{channel_id}")],
        [InlineKeyboardButton("Set Prefix", callback_data=f"set_pre_{channel_id}"),
         InlineKeyboardButton("Del Prefix", callback_data=f"del_pre_{channel_id}")],
        [InlineKeyboardButton("↩ Back",     callback_data=f"chinfo_{channel_id}")],
    ]
    await query.message.edit_text(
        f"📌 <b>Channel:</b> {chat_title}\n\n"
        f"Current Suffix: {suffix or 'None'}\nCurrent Prefix: {prefix or 'None'}",
        reply_markup=InlineKeyboardMarkup(buttons))


@Client.on_callback_query(filters.regex(r"^back_to_suffixprefix_(-?\d+)$"))
async def back_to_suffixprefix_menu(client, query):
    await query.answer()
    await suffix_prefix_menu(client, query)


@Client.on_callback_query(filters.regex(r'^set_suf_(-?\d+)$'))
async def set_suffix_message(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    user_id = query.from_user.id
    instr = await query.message.edit_text(
        "🖋️ Send the <b>suffix</b> text to add after captions.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back", callback_data=f"set_suffixprefix_{channel_id}")]]))
    bot_data.setdefault("suffix_set", {})[user_id] = {"channel_id": channel_id, "instr_msg_id": instr.id}


@Client.on_callback_query(filters.regex(r'^set_pre_(-?\d+)$'))
async def set_prefix_message(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    user_id = query.from_user.id
    instr = await query.message.edit_text(
        "✍️ Send the <b>prefix</b> text to add before captions.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back", callback_data=f"set_suffixprefix_{channel_id}")]]))
    bot_data.setdefault("prefix_set", {})[user_id] = {"channel_id": channel_id, "instr_msg_id": instr.id}


@Client.on_callback_query(filters.regex(r'^del_suf_(-?\d+)$'))
async def delete_suffix_cb(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    await delete_suffix(channel_id)
    await query.message.edit_text("✅ Suffix deleted.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back", callback_data=f"set_suffixprefix_{channel_id}")]]))


@Client.on_callback_query(filters.regex(r'^del_pre_(-?\d+)$'))
async def delete_prefix_cb(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    await delete_prefix(channel_id)
    await query.message.edit_text("✅ Prefix deleted.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back", callback_data=f"set_suffixprefix_{channel_id}")]]))


# ═════════════════════════════ REPLACE WORDS ══════════════════════════════
@Client.on_callback_query(filters.regex(r"^setreplace_(-?\d+)$"))
async def set_replace_menu(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    chat_title = await _get_chat_title(client, channel_id)
    raw = await get_replace_words(channel_id)
    replace_text = "\n".join(l.strip() for l in raw.splitlines() if l.strip()) if raw else "None set yet."
    buttons = [
        [InlineKeyboardButton("📝 Set Replace Words", callback_data=f"addreplace_{channel_id}"),
         InlineKeyboardButton("🗑️ Delete",           callback_data=f"delreplace_{channel_id}")],
        [InlineKeyboardButton("↩ Back",               callback_data=f"chinfo_{channel_id}")],
    ]
    await query.message.edit_text(
        f"📛 <b>Channel:</b> {chat_title}\n\n🔤 <b>Replace Words:</b>\n{replace_text}\n\nChoose 👇",
        reply_markup=InlineKeyboardMarkup(buttons))


@Client.on_callback_query(filters.regex(r"^addreplace_(-?\d+)$"))
async def set_replace_words_message(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    user_id = query.from_user.id
    bot_data.get("replace_words_set", {}).pop(user_id, None)
    instr = await query.message.edit_text(
        "🔤 Send <b>replace words</b> in format: <code>old new, another_old another_new</code>\n\n"
        "Example: <code>spam scam, fake real</code>",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back", callback_data=f"setreplace_{channel_id}")]]))
    bot_data.setdefault("replace_words_set", {})[user_id] = {"channel_id": channel_id, "instr_msg_id": instr.id}


@Client.on_callback_query(filters.regex(r"^back_to_replace_(-?\d+)$"))
async def back_to_replace_menu(client, query):
    await query.answer()
    bot_data.get("replace_words_set", {}).pop(query.from_user.id, None)
    await set_replace_menu(client, query)


@Client.on_callback_query(filters.regex(r"^delreplace_(-?\d+)$"))
async def delete_replace_words(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    chat_title = await _get_chat_title(client, channel_id)
    await delete_replace_words_db(channel_id)
    await query.message.edit_text(
        f"✅ All replace words deleted.\n\n📛 <b>Channel:</b> {chat_title}",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back", callback_data=f"setreplace_{channel_id}")]]))


# ══════════════════════════════ URL BUTTONS ════════════════════════════════
@Client.on_callback_query(filters.regex(r"^seturl_(-?\d+)$"))
async def url_button_menu(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    chat_title = await _get_chat_title(client, channel_id)
    buttons_data = await get_url_buttons(channel_id)
    if buttons_data:
        lines = [" | ".join(f"[{b['text']}]({b['url']})" for b in row) for row in buttons_data]
        preview = "\n".join(f"• {l}" for l in lines)
    else:
        preview = "❌ No URL buttons set."
    keyboard = [
        [InlineKeyboardButton("➕ Set URL",  callback_data=f"seturlmsg_{channel_id}"),
         InlineKeyboardButton("🗑 Delete URL", callback_data=f"delurl_{channel_id}")],
        [InlineKeyboardButton("↩ Back",       callback_data=f"chinfo_{channel_id}")],
    ]
    await query.message.edit_text(
        f"🔘 <b>Channel:</b> {chat_title}\n\n🔗 <b>Current URL Buttons:</b>\n{preview}\n\nChoose 👇",
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True)


@Client.on_callback_query(filters.regex(r"^seturlmsg_(-?\d+)$"))
async def set_url_message(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    user_id = query.from_user.id
    bot_data.setdefault("url_set", {}).pop(user_id, None)
    instr = await query.message.edit_text(
        "🔗 <b>Send URL buttons in this format:</b>\n\n"
        '<code>"Button 1" "url1" | "Button 2" "url2"</code>\n'
        '<code>"Button 3" "url3"</code>\n\n'
        "• Use <b>|</b> to put buttons in the same row\n"
        "• Use new line for next row",
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data=f"url_cancel_{channel_id}")]]))
    bot_data["url_set"][user_id] = {"channel_id": channel_id, "instr_msg_id": instr.id}


@Client.on_callback_query(filters.regex(r"^url_cancel_(-?\d+)$"))
async def cancel_url_set(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    bot_data.get("url_set", {}).pop(query.from_user.id, None)
    await url_button_menu(client, query)


@Client.on_callback_query(filters.regex(r"^delurl_(-?\d+)$"))
async def delete_url_buttons_cb(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    await delete_url_buttons(channel_id)
    await query.message.edit_text(
        "✅ All URL buttons deleted.",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("↩ Back", callback_data=f"seturl_{channel_id}")]]))


# ═══════════════════════════ LINK / EMOJI TOGGLE ══════════════════════════
@Client.on_callback_query(filters.regex(r'^togglelink_(-?\d+)$'))
async def toggle_link_remover(client, query):
    channel_id = int(query.matches[0].group(1))
    current = await get_link_remover_status(channel_id)
    await set_link_remover_status(channel_id, not current)
    await channel_settings(client, query)


@Client.on_callback_query(filters.regex(r'^toggleemoji_(-?\d+)$'))
async def toggle_emoji_remover(client, query):
    channel_id = int(query.matches[0].group(1))
    current = await get_emoji_remover_status(channel_id)
    await set_emoji_remover_status(channel_id, not current)
    await channel_settings(client, query)


# ═══════════════════════════════ RESET ════════════════════════════════════
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
    await delete_url_buttons(channel_id)
    invalidate_channel_cache(channel_id)
    try:
        await query.message.edit_text("♻️ Channel settings reset successfully.")
        await asyncio.sleep(1)
        await channel_settings(client, query)
    except:
        pass


# ═══════════════════════════ DELETE CHANNEL ═══════════════════════════════
@Client.on_callback_query(filters.regex(r"^del_ch_confirm_(-?\d+)$"))
async def del_channel_confirm(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    chat_title = await _get_chat_title(client, channel_id)
    await query.message.edit_text(
        f"⚠️ <b>Delete Channel?</b>\n\n"
        f"📛 <b>Channel:</b> {chat_title}\n\n"
        "This will remove the channel from your list and <b>delete ALL its settings</b> "
        "(caption, prefix, suffix, blocked words, replace words, URL buttons, etc.).\n\n"
        "<b>This cannot be undone.</b>",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("✅ Yes, Delete",  callback_data=f"del_ch_do_{channel_id}"),
             InlineKeyboardButton("❌ No, Go Back",  callback_data=f"chinfo_{channel_id}")],
        ])
    )


@Client.on_callback_query(filters.regex(r"^del_ch_do_(-?\d+)$"))
async def del_channel_execute(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    user_id = query.from_user.id
    # Wipe channel data
    await delete_all_channel_data(user_id, channel_id)
    # Show updated channel list immediately
    await user_settings(client, user=query.from_user, send_func=query.message.edit_text)
