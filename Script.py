import os

class script(object):

    START_TXT = """<b>Hᴇʟʟᴏ {mention}\n
ɪ ᴀᴍ ᴛʜᴇ ᴍᴏꜱᴛ ᴘᴏᴡᴇʀꜰᴜʟ ᴀᴜᴛᴏ ᴄᴀᴘᴛɪᴏɴ ʙᴏᴛ ᴡɪᴛʜ ᴘʀᴇᴍɪᴜᴍ ꜰᴇᴀᴛᴜʀᴇꜱ, ᴊᴜsᴛ ᴀᴅᴅ ᴍᴇ ᴛᴏ ʏᴏᴜʀ cʜᴀɴɴᴇʟ ᴀɴᴅ ᴇɴᴊᴏʏ
</b>
"""

    NEW_USER_TXT = (
    "👤 <b>New User Started the Bot</b>\n\n"
    "🙋‍♂️ <b>User:</b> {user}\n"
    "🆔 <b>User ID:</b> <code>{user_id}</code>"
)

    NEW_CHANNEL_TXT = (
    "📥 <b>Bot Added to Channel</b>\n\n"
    "👤 <b>By User:</b> {owner_name} (<code>{owner_id}</code>)\n"
    "📢 <b>Channel:</b> {channel_name}\n"
    "🆔 <b>Channel ID:</b> <code>{channel_id}</code>"
)

    HELP_TEXT = """
✨ **How to Use This Bot**

1️⃣ **Add Bot to Channel**  
→ Add this bot to your channel as **Admin** with all permissions.  

2️⃣ **Open Settings**  
→ After adding, go to `/settings`  
→ Select your channel from the list.

3️⃣ **Customize Channel Caption**  
🖊️ Set a default caption for all uploaded media.

4️⃣ **Replace Words**  
✏️ Automatically replace specific words in captions.  
Example: `old new, hello hi`

5️⃣ **Block Words**  
🚫 Remove unwanted or bad words from captions.

6️⃣ **Prefix & Suffix**  
🔠 Add text before (prefix) or after (suffix) the caption.

7️⃣ **Link Remover**  
🔗 Turn **ON/OFF** automatic link removal from captions.

✅ That’s it!  
Your channel captions will now be fully automatic ✨
"""

    ABOUT_TXT = """<b>╔════❰ 🤖 ᴀᴜᴛᴏ ᴄᴀᴘᴛɪᴏɴ ʙᴏᴛ ❱═❍⊱❁
║╭━━━━━━━━━━━━━━━━━━━━➣
║┣⪼ 📃 <b>Bot Name :</b> <a href='https://t.me/{bot_username}'>{bot_name}</a>
║┣⪼ 👦 <b>Movie Group :</b> <a href='https://t.me/Navex_Movies'>Mᴏᴠɪᴇ Zᴏɴᴇ🍿</a>
║┣⪼ 🤖 <b>Main Channel :</b> <a href='https://t.me/+j47Zv1sA9WViODk1'>Nᴀᴠᴇx™</a>
║╰━━━━━━━━━━━━━━━━━━━━➣
╚══════════════════════❍⊱❁</b>

<b>✨ Key Features</b>
• 🚀 Handles <b>large number of files</b> smoothly  
• 📂 Supports <b>multiple users & multiple channels</b>  
• 📝 Automatically <b>edits captions</b> of media files  
• 🔁 Can <b>forward files</b> from one channel to another  
• 🧠 Smart caption system with placeholders  
• 🧹 Remove links, words & unwanted text  
• 🔤 Prefix, suffix & replace words support  
• 🌐 Language, year, quality & metadata detection  

<b>⚙️ Advanced System</b>
• 📥 Persistent queue (no file loss)
• 🔄 Auto recovery after restart
• ⏳ FloodWait handled automatically
• 🧮 Fair processing for all channels
• ♾️ Unlimited file backlog supported

<b>⏳ Important Notice</b>
• If you send <b>many files</b>, editing may take time  
• Please be <b>patient</b> — every file will be processed  
• Speed depends on Telegram limits (not bot issue)  
• Do NOT resend the same files again

<b>📌 Things You Should Know</b>
• Bot must be <b>admin</b> in your channel
• Caption editing is <b>safe & reliable</b>
• Files are never skipped or dropped
• Works 24×7 without stopping

<b>❤️ Thank You for Using Auto Caption Bot</b>
<b>⚡ Fast • Stable • Powerful</b>
"""

    FONT_TXT = """🔰 About Caption Font

➢ Bold Text
☞ <code>&lt;b&gt;{file_name}&lt;/b&gt;</code>

➢ Spoiler Text
☞ <code>&lt;spoiler&gt;{file_name}&lt;/spoiler&gt;</code>

➢ Preformatted Text
☞ <code>&lt;pre&gt;{file_name}&lt;/pre&gt;</code>

➢ Block Quote Text
☞ <code>&lt;blockquote&gt;{file_name}&lt;/blockquote&gt;</code>
☞ <code>&lt;blockquote expandable&gt;{file_name}&lt;/blockquote&gt;</code>

➢ Italic Text
☞ <code>&lt;i&gt;{file_name}&lt;/i&gt;</code>

➢ Underline Text
☞ <code>&lt;u&gt;{file_name}&lt;/u&gt;</code>

➢ Strike Text
☞ <code>&lt;s&gt;{file_name}&lt;/s&gt;</code>

➢ Mono Text
☞ <code>&lt;code&gt;{file_name}&lt;/code&gt;</code>

➢ Hyperlink Text
☞ <code>&lt;a href="https://t.me/Navex_Movies"&gt;{file_name}&lt;/a&gt;</code>
"""

    ADMIN_HELP_TEXT = """👑 <b>ADMIN CONTROL PANEL</b>

<b>Bot Status</b>
• /queue – View queue stats, ETA, busy channels
• /restart – Restart bot safely
• Reply + /broadcast – Send message to all users
• /reset – ⚠️ Reset all DB data (users, channels, settings)
• /stats – Shows bot statistics:Total users, Pending caption jobs, Processing jobs, Worker count, Edit delay, Queue mode
• /dump_skip – Set channel to skip forwarding.
• /remove_dump – Set channel to remove from skip forwarding.

<b>File Forwarding</b>
• /file_forward – Start interactive file forwarding wizard
  → Select source channel → destination channel → skip/range
  → Supports: 0 (all), msg_id/link (from there to end), start-end range
  → Automatically resumes after bot restart
• /ff_status – View active and pending forwarding tasks

<b>System Info</b>
• Workers: {workers}
• Edit Delay: {delay}s
• Queue Mode: Persistent (MongoDB)
• FloodWait Handling: Enabled
• Crash Recovery: Enabled
• FF Dump Channel: Set via FF_CH env variable
"""
