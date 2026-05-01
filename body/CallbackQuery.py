import re
import asyncio
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.enums import ParseMode
from body.database import *
from info import *
from Script import script
from body.Caption import bot_data
from pyrogram.errors import RPCError, ChatAdminRequired, ChatWriteForbidden

FONT_TXT = script.FONT_TXT

# ─────────────────────────────────────────────
#  Tiny decorator: always answer the callback
#  immediately so Telegram stops the spinner,
#  then do any slow DB work in the edit.
# ─────────────────────────────────────────────

# ===================== CHANNEL SETTINGS =====================
@Client.on_callback_query(filters.regex(r'^chinfo_(-?\d+)$'))
async def channel_settings(client, query):
    await query.answer()                          # instant — no spinner
    channel_id = int(query.matches[0].group(1))

    # Fast cache lookup first
    cap_doc    = await get_channel_cached(channel_id)
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
    link_on     = bool(cap_doc.get("link_remover", False))
    emoji_on    = bool(cap_doc.get("emoji_remover", False))

    if not caption:
        cap_preview = "❌ No caption set."
    else:
        parts = []
        if prefix: parts.append(prefix)
        parts.append(caption)
        if suffix: parts.append(suffix)
        cap_preview = "\n".join(parts)

    text = (
        f"⚙️ <b>Manage:</b> {chat_title}\n\n"
        f"📝 <b>Caption:</b>\n<code>{cap_preview[:300]}</code>\n\n"
        "Choose what to configure 👇"
    )
    buttons = [
        [InlineKeyboardButton("📝 Set Caption",          callback_data=f"setcap_{channel_id}")],
        [InlineKeyboardButton("🧹 Words Remover",         callback_data=f"setwords_{channel_id}")],
        [InlineKeyboardButton("🔤 Prefix & Suffix",       callback_data=f"set_suffixprefix_{channel_id}")],
        [InlineKeyboardButton("🔄 Replace Words",         callback_data=f"setreplace_{channel_id}")],
        [InlineKeyboardButton("🔘 URL Buttons",           callback_data=f"seturl_{channel_id}")],
        [InlineKeyboardButton(f"🔗 Links {'ON ✅' if link_on else 'OFF ❌'}",
                              callback_data=f"togglelink_{channel_id}")],
        [InlineKeyboardButton(f"😀 Emoji {'ON ✅' if emoji_on else 'OFF ❌'}",
                              callback_data=f"toggleemoji_{channel_id}")],
        [InlineKeyboardButton("♻️ Reset Channel",         callback_data=f"reset_channel_{channel_id}")],
        [InlineKeyboardButton("↩ Back", callback_data="settings_cb"),
         InlineKeyboardButton("❌ Close", callback_data="close_msg")],
    ]
    try:
        await query.message.edit_text(text, parse_mode=ParseMode.HTML,
                                      reply_markup=InlineKeyboardMarkup(buttons),
                                      disable_web_page_preview=True)
    except Exception:
        await query.answer("⚠️ Caption too long to preview.", show_alert=True)


# ===================== CAPTION MENU =====================
@Client.on_callback_query(filters.regex(r'^setcap_(-?\d+)$'))
async def set_caption_menu(client, query):
    await query.answer()
    channel_id  = int(query.matches[0].group(1))
    cap_doc     = await get_channel_cached(channel_id)
    chat_title  = cap_doc.get("_title", str(channel_id))
    current_cap = cap_doc.get("caption")
    cap_display = f"📝 <b>Current:</b>\n<code>{current_cap[:200]}</code>" if current_cap else "📝 <b>Current:</b> None"

    await query.message.edit_text(
        f"⚙️ <b>Channel:</b> {chat_title}\n{cap_display}\n\nChoose an action 👇",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🆕 Set Caption",   callback_data=f"setcapmsg_{channel_id}"),
             InlineKeyboardButton("❌ Delete",         callback_data=f"delcap_{channel_id}")],
            [InlineKeyboardButton("🔤 Caption Font",  callback_data=f"capfont_{channel_id}")],
            [InlineKeyboardButton("↩ Back",           callback_data=f"chinfo_{channel_id}")],
        ]),
    )

@Client.on_callback_query(filters.regex(r'^setcapmsg_(-?\d+)$'))
async def set_caption_message(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    user_id    = query.from_user.id
    bot_data.get("caption_set", {}).pop(user_id, None)

    instr = await query.message.edit_text(
        "📌 <b>Send your caption template</b>\n\n"
        "<blockquote expandable>"
        "📦 <b>Placeholders</b>\n\n"
        "<b>File Info</b>\n"
        "<code>{file_name}</code> — file name\n"
        "<code>{file_size}</code> — size\n"
        "<code>{extension}</code> — ext\n"
        "<code>{default_caption}</code> — original caption\n"
        "<code>{empty}</code> — blank line\n\n"
        "<b>Smart Fields</b>\n"
        "<code>{smart_file_name}</code> — auto-built full name\n"
        "<code>{title}</code>  <code>{year}</code>  <code>{season}</code>  <code>{episode}</code>\n"
        "<code>{audio}</code>  <code>{subtitle}</code>  <code>{quality}</code>\n"
        "<code>{resolution}</code>  <code>{source}</code>  <code>{vcodec}</code>  <code>{acodec}</code>"
        "</blockquote>\n\n"
        "<blockquote expandable>"
        "🖋 <b>HTML Styles</b>\n"
        "<code>&lt;b&gt;</code> <code>&lt;i&gt;</code> <code>&lt;u&gt;</code> "
        "<code>&lt;s&gt;</code> <code>&lt;code&gt;</code> <code>&lt;pre&gt;</code> "
        "<code>&lt;spoiler&gt;</code> <code>&lt;blockquote&gt;</code>"
        "</blockquote>\n\n"
        "✍️ <b>Example:</b>\n"
        "<code>&lt;b&gt;{title}&lt;/b&gt; {season}{episode} ({year})\n"
        "{audio} | {quality} | {subtitle}\n"
        "💾 {file_size}</code>",
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
    await query.answer("✅ Caption deleted")
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
    cap_txt    = cap_doc.get("caption") or "No caption set."
    await query.message.edit_text(
        f"📝 Current: {cap_txt[:100]}\n\n🖋️ Available Fonts:\n\n{FONT_TXT}",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩ Back", callback_data=f"setcap_{channel_id}")]]
        ),
    )


# ===================== WORDS REMOVER =====================
@Client.on_callback_query(filters.regex(r"^setwords_(-?\d+)$"))
async def set_words_menu(client, query):
    await query.answer()
    channel_id   = int(query.matches[0].group(1))
    cap_doc      = await get_channel_cached(channel_id)
    chat_title   = cap_doc.get("_title", str(channel_id))
    blocked_raw  = cap_doc.get("block_words", "")
    words_text   = (
        "\n".join(f"• {w.strip()}" for w in re.split(r"[,\n]+", blocked_raw) if w.strip())
        if blocked_raw else "None set yet."
    )
    await query.message.edit_text(
        f"📛 <b>Channel:</b> {chat_title}\n\n🚫 <b>Blocked Words:</b>\n{words_text}\n\nChoose an action 👇",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 Set Block Words", callback_data=f"addwords_{channel_id}"),
             InlineKeyboardButton("🗑️ Delete All",      callback_data=f"delwords_{channel_id}")],
            [InlineKeyboardButton("↩ Back",             callback_data=f"chinfo_{channel_id}")],
        ]),
    )

@Client.on_callback_query(filters.regex(r"^addwords_(-?\d+)$"))
async def set_block_words_message(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    user_id    = query.from_user.id
    bot_data.get("block_words_set", {}).pop(user_id, None)
    instr = await query.message.edit_text(
        "🚫 <b>Send blocked words</b>\nSeparate with commas.\n\n"
        "Example: <code>spam, fake, scam</code>",
        parse_mode=ParseMode.HTML,
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
    await query.answer("✅ Deleted")
    channel_id = int(query.matches[0].group(1))
    await delete_block_words(channel_id)
    await query.message.edit_text(
        "✅ <b>All blocked words deleted.</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩ Back", callback_data=f"setwords_{channel_id}")]]
        ),
    )


# ===================== SUFFIX & PREFIX =====================
@Client.on_callback_query(filters.regex(r'^set_suffixprefix_(-?\d+)$'))
async def suffix_prefix_menu(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    cap_doc    = await get_channel_cached(channel_id)
    chat_title = cap_doc.get("_title", str(channel_id))
    suffix     = cap_doc.get("suffix", "") or "None"
    prefix     = cap_doc.get("prefix", "") or "None"
    await query.message.edit_text(
        f"📌 <b>Channel:</b> {chat_title}\n\n"
        f"Prefix: <code>{prefix}</code>\n"
        f"Suffix: <code>{suffix}</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Set Prefix",  callback_data=f"set_pre_{channel_id}"),
             InlineKeyboardButton("Del Prefix",  callback_data=f"del_pre_{channel_id}")],
            [InlineKeyboardButton("Set Suffix",  callback_data=f"set_suf_{channel_id}"),
             InlineKeyboardButton("Del Suffix",  callback_data=f"del_suf_{channel_id}")],
            [InlineKeyboardButton("↩ Back",      callback_data=f"chinfo_{channel_id}")],
        ]),
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
        "🖋️ <b>Send suffix text</b>\nThis is appended after every caption.",
        parse_mode=ParseMode.HTML,
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
        "✍️ <b>Send prefix text</b>\nThis is added before every caption.",
        parse_mode=ParseMode.HTML,
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
    await query.answer("✅ Deleted")
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
    await query.answer("✅ Deleted")
    channel_id = int(query.matches[0].group(1))
    await delete_prefix(channel_id)
    await query.message.edit_text(
        "✅ Prefix deleted.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩ Back", callback_data=f"set_suffixprefix_{channel_id}")]]
        ),
    )


# ===================== REPLACE WORDS =====================
@Client.on_callback_query(filters.regex(r"^setreplace_(-?\d+)$"))
async def set_replace_menu(client, query):
    await query.answer()
    channel_id  = int(query.matches[0].group(1))
    cap_doc     = await get_channel_cached(channel_id)
    chat_title  = cap_doc.get("_title", str(channel_id))
    replace_raw = cap_doc.get("replace_words", "")
    replace_txt = (
        "\n".join(l.strip() for l in replace_raw.splitlines() if l.strip())
        if replace_raw else "None set yet."
    )
    await query.message.edit_text(
        f"📛 <b>Channel:</b> {chat_title}\n\n🔤 <b>Replace Words:</b>\n{replace_txt}\n\nChoose an action 👇",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("📝 Set Replace", callback_data=f"addreplace_{channel_id}"),
             InlineKeyboardButton("🗑️ Delete All",  callback_data=f"delreplace_{channel_id}")],
            [InlineKeyboardButton("↩ Back",         callback_data=f"chinfo_{channel_id}")],
        ]),
    )

@Client.on_callback_query(filters.regex(r"^addreplace_(-?\d+)$"))
async def set_replace_words_message(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    user_id    = query.from_user.id
    bot_data.get("replace_words_set", {}).pop(user_id, None)
    instr = await query.message.edit_text(
        "🔤 <b>Send replace pairs</b>\n"
        "Format: <code>old new, another_old another_new</code>\n\n"
        "Example: <code>spam scam, fake real</code>",
        parse_mode=ParseMode.HTML,
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
    await query.answer("✅ Deleted")
    channel_id = int(query.matches[0].group(1))
    await delete_replace_words_db(channel_id)
    await query.message.edit_text(
        "✅ <b>All replace words deleted.</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩ Back", callback_data=f"setreplace_{channel_id}")]]
        ),
    )


# ===================== URL BUTTONS =====================
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
    await query.message.edit_text(
        f"🔘 <b>Channel:</b> {chat_title}\n\n🔗 <b>URL Buttons:</b>\n{preview}\n\nChoose an option 👇",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Set URL",  callback_data=f"seturlmsg_{channel_id}"),
             InlineKeyboardButton("🗑 Delete",   callback_data=f"delurl_{channel_id}")],
            [InlineKeyboardButton("↩ Back",      callback_data=f"chinfo_{channel_id}")],
        ]),
        disable_web_page_preview=True,
    )

@Client.on_callback_query(filters.regex(r"^seturlmsg_(-?\d+)$"))
async def set_url_message(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    user_id    = query.from_user.id
    bot_data.setdefault("url_set", {}).pop(user_id, None)
    instr = await query.message.edit_text(
        '🔗 <b>Send URL buttons in this format:</b>\n\n'
        '<code>"Button 1" "url1" | "Button 2" "url2"</code>\n'
        '<code>"Button 3" "url3"</code>\n\n'
        '• Use <b>|</b> for same row  •  New line = new row',
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
    await query.answer("✅ Deleted")
    channel_id = int(query.matches[0].group(1))
    await delete_url_buttons(channel_id)
    await query.message.edit_text(
        "✅ <b>URL buttons deleted.</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩ Back", callback_data=f"seturl_{channel_id}")]]
        ),
    )


# ===================== TOGGLE LINK / EMOJI =====================
@Client.on_callback_query(filters.regex(r'^togglelink_(-?\d+)$'))
async def toggle_link_remover(client, query):
    await query.answer()
    channel_id  = int(query.matches[0].group(1))
    current     = await get_link_remover_status(channel_id)
    await set_link_remover_status(channel_id, not current)
    await channel_settings(client, query)

@Client.on_callback_query(filters.regex(r'^toggleemoji_(-?\d+)$'))
async def toggle_emoji_remover(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    current    = await get_emoji_remover_status(channel_id)
    await set_emoji_remover_status(channel_id, not current)
    await channel_settings(client, query)


# ===================== RESET CHANNEL =====================
@Client.on_callback_query(filters.regex(r"^reset_channel_(-?\d+)$"))
async def reset_channel_settings(client, query):
    await query.answer("♻️ Resetting…")
    channel_id = int(query.matches[0].group(1))
    await asyncio.gather(
        delete_channel_caption(channel_id),
        delete_block_words(channel_id),
        delete_replace_words_db(channel_id),
        delete_prefix(channel_id),
        delete_suffix(channel_id),
        set_link_remover_status(channel_id, False),
        set_emoji_remover_status(channel_id, False),
    )
    _CHANNEL_CACHE.pop(channel_id, None)
    await query.message.edit_text(
        "♻️ <b>Channel settings reset.</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩ Back", callback_data=f"chinfo_{channel_id}")]]
        ),
    )
