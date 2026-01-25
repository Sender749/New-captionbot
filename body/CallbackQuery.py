import re
from pyrogram import Client, filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from pyrogram.enums import ParseMode
from body.database import *
from info import *
from Script import script
from body.Caption import * 
from pyrogram.errors import RPCError, ChatAdminRequired, ChatWriteForbidden

FONT_TXT = script.FONT_TXT

@Client.on_callback_query(filters.regex(r'^chinfo_(-?\d+)$'))
async def channel_settings(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    try:
        chat = await client.get_chat(channel_id)
        chat_title = getattr(chat, "title", str(channel_id))
    except Exception:
        chat_title = str(channel_id)
    cap_doc = await get_channel_cached(channel_id)
    caption = cap_doc.get("caption", "")
    prefix = cap_doc.get("prefix", "")
    suffix = cap_doc.get("suffix", "")
    link_status = await get_link_remover_status(channel_id)
    link_text = "Link & Usernames Remover (ON)" if link_status else "Link & Usernames Remover (OFF)"
    emoji_status = await get_emoji_remover_status(channel_id)
    emoji_text = "Emoji Remover (ON)" if emoji_status else "Emoji Remover (OFF)"
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
        [InlineKeyboardButton("📝 Set Caption", callback_data=f"setcap_{channel_id}")],
        [InlineKeyboardButton("🧹 Set Words Remover", callback_data=f"setwords_{channel_id}")],
        [InlineKeyboardButton("🔤 Set Prefix & Suffix", callback_data=f"set_suffixprefix_{channel_id}")],
        [InlineKeyboardButton("🔄 Set Replace Words", callback_data=f"setreplace_{channel_id}")],
        [InlineKeyboardButton("🔘 Button URL", callback_data=f"seturl_{channel_id}")],
        [InlineKeyboardButton(f"🔗 {link_text}", callback_data=f"togglelink_{channel_id}")],
        [InlineKeyboardButton(f"😀 {emoji_text}", callback_data=f"toggleemoji_{channel_id}")],
        [InlineKeyboardButton("♻️ Reset Channel Settings", callback_data=f"reset_channel_{channel_id}")],
        [InlineKeyboardButton("↩ Back", callback_data="settings_cb"), InlineKeyboardButton("❌ Close", callback_data="close_msg")]
    ]
    try:
        await query.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(buttons),
            disable_web_page_preview=True
        )
    except Exception:
        await query.answer("⚠️ Caption too long to display fully.", show_alert=True)

# ===================== CAPTION MENU =====================
@Client.on_callback_query(filters.regex(r'^setcap_(-?\d+)$'))
async def set_caption_menu(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    chat = await client.get_chat(channel_id)
    chat_title = getattr(chat, "title", str(channel_id))

    caption_data = await get_channel_caption(channel_id)
    current_caption = caption_data.get("caption") if caption_data else None
    caption_display = f"📝 **Current Caption:**\n{current_caption}" if current_caption else "📝 **Current Caption:** None set yet."

    buttons = [
        [InlineKeyboardButton("🆕 Set Caption", callback_data=f"setcapmsg_{channel_id}"),
         InlineKeyboardButton("❌ Delete Caption", callback_data=f"delcap_{channel_id}")],
        [InlineKeyboardButton("🔤 Caption Font", callback_data=f"capfont_{channel_id}")],
        [InlineKeyboardButton("↩ Back", callback_data=f"chinfo_{channel_id}")]
    ]

    text = (
        f"⚙️ **Channel:** {chat_title}\n"
        f"{caption_display}\n\n"
        f"Choose what you want to do 👇"
    )

    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))

@Client.on_callback_query(filters.regex(r'^setcapmsg_(-?\d+)$'))
async def set_caption_message(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    user_id = query.from_user.id
    if "caption_set" in bot_data and user_id in bot_data["caption_set"]:
        bot_data["caption_set"].pop(user_id, None)

    instr = await query.message.edit_text(
        text=(
            "📌 Send caption for this channel\n\n"

            "🔹 Placeholders:\n"
            "<b>File name</b> ⇛ <code>{file_name}</code> \n"
            "<b>Smart file name</b> ⇛ <code>{smart_file_name}</code> \n"
            "<b>File size</b> ⇛ <code>{file_size}</code>  \n"
            "<b>Original caption</b> ⇛ <code>{default_caption}</code>  \n"
            "<b>Language</b> ⇛ <code>{language}</code>  \n"
            "<b>Year</b> ⇛ <code>{year}</code> \n\n"
 
            "🔹 Text Styles:\n"
            "<b>Bold</b> ⇛ <code>&lt;b&gt;Text&lt;/b&gt;</code> \n "
            "<b>Italic</b> ⇛ <code>&lt;i&gt;Text&lt;/i&gt;</code> \n "
            "<b>Underline</b> ⇛ <code>&lt;u&gt;Text&lt;/u&gt;</code> \n"
            "<b>Strike</b> ⇛ <code>&lt;s&gt;Text&lt;/s&gt;</code> \n"
            "<b>Mono</b> ⇛ <code>&lt;code&gt;Text&lt;/code&gt;</code> \n"
            "<b>Spoiler</b> ⇛ <code>&lt;spoiler&gt;Text&lt;/spoiler&gt;</code> \n"
            "<b>Preformatted</b> ⇛ <code>&lt;pre&gt;Text&lt;/pre&gt;</code> \n"
            "<b>Block Quote</b> ⇛ <code>&lt;blockquote&gt;Text&lt;/blockquote&gt;</code> \n"
            "<b>Link</b> ⇛  <code>&lt;a href=\"url\"&gt;Text&lt;/a&gt;</code> \n\n"
            "✍️ Example:\n"
            "<code>&lt;b&gt;{file_name}&lt;/b&gt;</code>\n <code>&lt;i&gt;{file_size}&lt;/i&gt;</code>"
         ),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩ Back", callback_data=f"setcap_{channel_id}")]]
        )
    )
    bot_data.setdefault("caption_set", {})[user_id] = {
        "channel_id": channel_id,
        "instr_msg_id": instr.id
    }

@Client.on_callback_query(filters.regex(r"^back_to_captionmenu_(-?\d+)$"))
async def back_to_caption_menu(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    await set_caption_menu(client, query)

@Client.on_callback_query(filters.regex(r'^delcap_(-?\d+)$'))
async def delete_caption(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    await delete_channel_caption(channel_id)
    buttons = [[InlineKeyboardButton("↩ Back", callback_data=f"setcap_{channel_id}")]]
    await query.message.edit_text(f"✅ Caption deleted.\n❌ No caption set currently.", reply_markup=InlineKeyboardMarkup(buttons))

@Client.on_callback_query(filters.regex(r'^capfont_(-?\d+)$'))
async def caption_font(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    current_cap = await get_channel_caption(channel_id)
    cap_txt = current_cap.get("caption") if current_cap else "No custom caption set."
    disable_web_page_preview=True
    buttons = [[InlineKeyboardButton("↩ Back", callback_data=f"setcap_{channel_id}")]]
    text = f"📝 Current Caption: {cap_txt}\n\n🖋️ Available Fonts:\n\n{FONT_TXT}"
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))


# ========== SET WORDS REMOVER MENU ==========================================
@Client.on_callback_query(filters.regex(r"^setwords_(-?\d+)$"))
async def set_words_menu(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    chat = await client.get_chat(channel_id)
    chat_title = getattr(chat, "title", str(channel_id))

    blocked_words = await get_block_words(channel_id)
    if blocked_words:
        words_text = "\n".join(
            f"• {w.strip()}"
            for w in re.split(r"[,\n]+", blocked_words)
            if w.strip()
        )
    else:
        words_text = "None set yet."


    text = (
        f"📛 **Channel:** {chat_title}\n\n"
        f"🚫 **Blocked Words:**\n{words_text}\n\n"
        f"Choose what you want to do 👇"
    )

    buttons = [
        [InlineKeyboardButton("📝 Set Block Words", callback_data=f"addwords_{channel_id}"),
         InlineKeyboardButton("🗑️ Delete Block Words", callback_data=f"delwords_{channel_id}")],
        [InlineKeyboardButton("↩ Back", callback_data=f"chinfo_{channel_id}")]
    ]
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))

@Client.on_callback_query(filters.regex(r"^addwords_(-?\d+)$"))
async def set_block_words_message(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    user_id = query.from_user.id
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
        )
    )
    bot_data.setdefault("block_words_set", {})[user_id] = {
        "channel_id": channel_id,
        "instr_msg_id": instr.id
    }

@Client.on_callback_query(filters.regex(r"^back_to_blockwords_(-?\d+)$"))
async def back_to_blockwords_menu(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    user_id = query.from_user.id
    bot_data.get("block_words_set", {}).pop(user_id, None)
    await set_words_menu(client, query)


@Client.on_callback_query(filters.regex(r"^delwords_(-?\d+)$"))
async def delete_blocked_words(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    await delete_block_words(channel_id)
    chat = await client.get_chat(channel_id)
    chat_title = getattr(chat, "title", str(channel_id))
    buttons = [[InlineKeyboardButton("↩ Back", callback_data=f"setwords_{channel_id}")]]
    await query.message.edit_text(
        f"✅ **All blocked words deleted successfully.**\n\n📛 **Channel:** {chat_title}",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# ======================== Suffix & Prefix Menu ==================================
@Client.on_callback_query(filters.regex(r'^set_suffixprefix_(-?\d+)$'))
async def suffix_prefix_menu(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    chat = await client.get_chat(channel_id)
    chat_title = getattr(chat, "title", str(channel_id))
    suffix, prefix = await get_suffix_prefix(channel_id)

    buttons = [
        [InlineKeyboardButton("Set Suffix", callback_data=f"set_suf_{channel_id}"),
         InlineKeyboardButton("Del Suffix", callback_data=f"del_suf_{channel_id}")],
        [InlineKeyboardButton("Set Prefix", callback_data=f"set_pre_{channel_id}"),
         InlineKeyboardButton("Del Prefix", callback_data=f"del_pre_{channel_id}")],
        [InlineKeyboardButton("↩ Back", callback_data=f"chinfo_{channel_id}")]
    ]

    text = f"📌 Channel: {chat_title}\n\nCurrent Suffix: {suffix or 'None'}\nCurrent Prefix: {prefix or 'None'}"
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))

@Client.on_callback_query(filters.regex(r"^back_to_suffixprefix_(-?\d+)$"))
async def back_to_suffixprefix_menu(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    await suffix_prefix_menu(client, query)

@Client.on_callback_query(filters.regex(r'^set_suf_(-?\d+)$'))
async def set_suffix_message(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    user_id = query.from_user.id
    instr = await query.message.edit_text(
        text="🖋️ Send the suffix text you want to add to your captions.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩ Back", callback_data=f"set_suffixprefix_{channel_id}")]]
        )
    )
    bot_data.setdefault("suffix_set", {})[user_id] = {
        "channel_id": channel_id,
        "instr_msg_id": instr.id
    }
@Client.on_callback_query(filters.regex(r'^set_pre_(-?\d+)$'))
async def set_prefix_message(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    user_id = query.from_user.id
    instr = await query.message.edit_text(
        text="✍️ Send the prefix text you want to add to your captions.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("↩ Back", callback_data=f"set_suffixprefix_{channel_id}")]]
        )
    )
    bot_data.setdefault("prefix_set", {})[user_id] = {
        "channel_id": channel_id,
        "instr_msg_id": instr.id
    }
@Client.on_callback_query(filters.regex(r'^del_suf_(-?\d+)$'))
async def delete_suffix_cb(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    await delete_suffix(channel_id)
    buttons = [[InlineKeyboardButton("↩ Back", callback_data=f"set_suffixprefix_{channel_id}")]]
    await query.message.edit_text(f"✅ Suffix deleted.", reply_markup=InlineKeyboardMarkup(buttons))

@Client.on_callback_query(filters.regex(r'^del_pre_(-?\d+)$'))
async def delete_prefix_cb(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    await delete_prefix(channel_id)
    buttons = [[InlineKeyboardButton("↩ Back", callback_data=f"set_suffixprefix_{channel_id}")]]
    await query.message.edit_text(f"✅ Prefix deleted.", reply_markup=InlineKeyboardMarkup(buttons))
    
# ======================== Replace Words ==================================
@Client.on_callback_query(filters.regex(r"^setreplace_(-?\d+)$"))
async def set_replace_menu(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    chat = await client.get_chat(channel_id)
    chat_title = getattr(chat, "title", str(channel_id))
    replace_raw = await get_replace_words(channel_id)
    if replace_raw:
        replace_text = "\n".join(
            line.strip()
            for line in replace_raw.splitlines()
            if line.strip()
        )
    else:
        replace_text = "None set yet."
    text = (
        f"📛 **Channel:** {chat_title}\n\n"
        f"🔤 **Replace Words:**\n{replace_text}\n\n"
        f"Choose what you want to do 👇"
    )
    buttons = [
        [InlineKeyboardButton("📝 Set Replace Words", callback_data=f"addreplace_{channel_id}"),
         InlineKeyboardButton("🗑️ Delete Replace Words", callback_data=f"delreplace_{channel_id}")],
        [InlineKeyboardButton("↩ Back", callback_data=f"chinfo_{channel_id}")]
    ]
    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))

@Client.on_callback_query(filters.regex(r"^addreplace_(-?\d+)$"))
async def set_replace_words_message(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    user_id = query.from_user.id
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
        )
    )
    bot_data.setdefault("replace_words_set", {})[user_id] = {
        "channel_id": channel_id,
        "instr_msg_id": instr.id
    }

@Client.on_callback_query(filters.regex(r"^back_to_replace_(-?\d+)$"))
async def back_to_replace_menu(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    user_id = query.from_user.id
    bot_data.get("replace_words_set", {}).pop(user_id, None)
    await set_replace_menu(client, query)

@Client.on_callback_query(filters.regex(r"^delreplace_(-?\d+)$"))
async def delete_replace_words(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    await delete_replace_words_db(channel_id)
    chat = await client.get_chat(channel_id)
    chat_title = getattr(chat, "title", str(channel_id))
    buttons = [[InlineKeyboardButton("↩ Back", callback_data=f"setreplace_{channel_id}")]]
    await query.message.edit_text(
        f"✅ **All replace words deleted successfully.**\n\n📛 **Channel:** {chat_title}",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# ======================== URL Button ==================================
@Client.on_callback_query(filters.regex(r"^seturl_(-?\d+)$"))
async def url_button_menu(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    chat = await client.get_chat(channel_id)
    chat_title = getattr(chat, "title", str(channel_id))
    buttons = await get_url_buttons(channel_id)
    if buttons:
        preview = "\n".join(
            f"• [{b['text']}]({b['url']})" for b in buttons
        )
    else:
        preview = "❌ No URL buttons set."
    text = (
        f"🔘 **Channel:** {chat_title}\n\n"
        f"🔗 **Current URL Buttons:**\n{preview}\n\n"
        "Choose an option 👇"
    )
    keyboard = [
        [InlineKeyboardButton("➕ Set URL", callback_data=f"seturlmsg_{channel_id}"),
         InlineKeyboardButton("🗑 Delete URL", callback_data=f"delurl_{channel_id}")],
        [InlineKeyboardButton("↩ Back", callback_data=f"chinfo_{channel_id}")]
    ]
    await query.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True
    )

@Client.on_callback_query(filters.regex(r"^seturlmsg_(-?\d+)$"))
async def set_url_message(client, query):
    await query.answer()
    channel_id = int(query.matches[0].group(1))
    user_id = query.from_user.id
    bot_data.setdefault("url_set", {}).pop(user_id, None)
    instr = await query.message.edit_text(
        text=(
            "🔗 **Send URL buttons in this format:**\n\n"
            "<code>\"Button Name\" \"https://example.com\"</code>\n\n"
            "You can send up to **3 buttons** (one per line).\n\n"
            "Example:\n"
            "<code>\"Join Channel\" \"https://t.me/example\"</code>"
        ),
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("❌ Cancel", callback_data=f"url_cancel_{channel_id}")]
        ]),
        disable_web_page_preview=True
    )
    bot_data["url_set"][user_id] = {
        "channel_id": channel_id,
        "instr_msg_id": instr.id
    }

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
        "✅ **All URL buttons deleted successfully.**",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("↩ Back", callback_data=f"seturl_{channel_id}")]
        ])
    )


# ======================== Link Remover ==================================
@Client.on_callback_query(filters.regex(r'^togglelink_(-?\d+)$'))
async def toggle_link_remover(client, query):
    channel_id = int(query.matches[0].group(1))
    current_status = await get_link_remover_status(channel_id)
    new_status = not current_status
    await set_link_remover_status(channel_id, new_status)
    await channel_settings(client, query)
    
# ======================== Emoji Remover ==================================
@Client.on_callback_query(filters.regex(r'^toggleemoji_(-?\d+)$'))
async def toggle_emoji_remover(client, query):
    channel_id = int(query.matches[0].group(1))
    current = await get_emoji_remover_status(channel_id)
    await set_emoji_remover_status(channel_id, not current)
    await channel_settings(client, query)

# ======================== Reset Button ==================================
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
    except:
        pass
    try:
        await query.message.edit_text("♻️ Channel settings reset successfully.")
        await asyncio.sleep(1)
        await channel_settings(client, query)
    except:
        pass
