from html import escape

ADMIN_ONLY = True
EVERYONE = False

SECTIONS = [
    ("info", "📊 INFO & MONITORING"),
    ("broadcast", "📢 BROADCAST"),
    ("dump", "🗃 DUMP CHANNEL CONTROL"),
    ("forward", "📤 FILE FORWARDING"),
    ("review", "🧠 CAPTION REVIEW"),
    ("control", "🔄 BOT CONTROL"),
    ("general", "⚙️ GENERAL"),
]

COMMANDS = [
    {"name": "start", "section": "general", "admin": EVERYONE,
     "menu": "Start the bot",
     "text": "Open the welcome screen.",
     "example": "/start"},
    {"name": "settings", "section": "general", "admin": EVERYONE,
     "menu": "Manage your channels and captions",
     "text": "Pick a channel and set its caption, replace words, block words, prefix/suffix and link remover.",
     "example": "/settings"},
    {"name": "file_forward", "section": "forward", "admin": EVERYONE,
     "menu": "Forward files between your channels",
     "text": "Start a forward session: pick source, then destination, then a range (0 = all).",
     "example": "/file_forward"},
    {"name": "admin", "section": "info", "admin": ADMIN_ONLY,
     "menu": "Show the admin command list",
     "text": "Show this command list.",
     "example": "/admin"},
    {"name": "stats", "section": "info", "admin": ADMIN_ONLY,
     "menu": "Edits, dump files and channel reports",
     "text": "Stats panel. Periods: today, yesterday, 7d, 30d, all.",
     "example": "/stats 7d"},
    {"name": "queue", "section": "info", "admin": ADMIN_ONLY,
     "menu": "Live caption and forward queue",
     "text": "Queue size, ETA and the busiest channels.",
     "example": "/queue"},
    {"name": "broadcast", "section": "broadcast", "admin": ADMIN_ONLY,
     "menu": "Broadcast a message to all users",
     "text": "Reply to any message to send a copy of it to every user.",
     "example": "Reply to a message with /broadcast"},
    {"name": "dump_skip", "section": "dump", "admin": ADMIN_ONLY,
     "menu": "Stop dump copies for a channel",
     "text": "Skip dump forwarding for one channel.",
     "example": "/dump_skip -1001234567890"},
    {"name": "remove_dump", "section": "dump", "admin": ADMIN_ONLY,
     "menu": "Resume dump copies for a channel",
     "text": "Remove a channel from the dump skip list.",
     "example": "/remove_dump -1001234567890"},
    {"name": "dump_skip_list", "section": "dump", "admin": ADMIN_ONLY,
     "menu": "List channels with dump skipped",
     "text": "Show every channel currently skipped, with buttons to change it.",
     "example": "/dump_skip_list"},
    {"name": "dump_change", "section": "dump", "admin": ADMIN_ONLY,
     "menu": "Redirect a channel's dump copies",
     "text": "Send the edited-file copies of a source channel to another channel.",
     "example": "/dump_change -1001234567890"},
    {"name": "id", "section": "dump", "admin": ADMIN_ONLY,
     "menu": "Find the origin of a dump file",
     "text": "Reply to a file in a dump channel to see which channel and message it came from.",
     "example": "Reply to a dump file with /id"},
    {"name": "channels", "section": "forward", "admin": ADMIN_ONLY,
     "menu": "All added channels and bulk forwarding",
     "text": "List user-added channels with file counts and start, continue or stop controls.",
     "example": "/channels"},
    {"name": "member_forward", "section": "forward", "admin": ADMIN_ONLY,
     "menu": "Forward from a channel the bot is not in",
     "text": "Uses the userbot session to pull files from a channel where only your account is a member.",
     "example": "/member_forward"},
    {"name": "gen_session", "section": "forward", "admin": ADMIN_ONLY,
     "menu": "Generate a userbot session string",
     "text": "Walks through phone, code and 2FA, then gives the SESSION_STRING for /member_forward.",
     "example": "/gen_session"},
    {"name": "history_stats", "section": "review", "admin": ADMIN_ONLY,
     "menu": "Caption history counts and flags",
     "text": "Records, reported files, flag counts, engine versions and writer health.",
     "example": "/history_stats"},
    {"name": "history_export", "section": "review", "admin": ADMIN_ONLY,
     "menu": "Export caption history files",
     "text": "Export history as .txt and .jsonl. Days 1-3650 (default 7); modes: all, unsure, changed.",
     "example": "/history_export 7 unsure"},
    {"name": "history_clear", "section": "review", "admin": ADMIN_ONLY,
     "menu": "Delete all caption history",
     "text": "Delete every history record after a confirmation button.",
     "example": "/history_clear"},
    {"name": "wrong", "section": "review", "admin": ADMIN_ONLY,
     "menu": "Correct a wrongly edited caption",
     "text": "Reply to a forwarded channel post with the correct smart file name, or fields: title, year, audio, sub. Saved permanently.",
     "example": "/wrong audio=Hindi + English; sub=ESub"},
    {"name": "original", "section": "review", "admin": ADMIN_ONLY,
     "menu": "Show a file's caption before the edit",
     "text": "Reply to a file to get its caption from before the bot edited it.",
     "example": "Reply to a file with /original"},
    {"name": "restart", "section": "control", "admin": ADMIN_ONLY,
     "menu": "Restart the bot safely",
     "text": "Requeues in-flight jobs, then restarts the process.",
     "example": "/restart"},
    {"name": "reset", "section": "control", "admin": ADMIN_ONLY,
     "menu": "Wipe users, channels and settings",
     "text": "⚠️ Deletes ALL users, channels and settings from the database. No confirmation.",
     "example": "/reset"},
]

PANEL_LIMIT = 3900


def user_menu():
    return [(c["name"], c["menu"]) for c in COMMANDS if not c["admin"]]


def admin_menu():
    return [(c["name"], c["menu"]) for c in COMMANDS]


def _entry(c):
    return "• /%s — %s\n   ↳ <i>Example:</i> <code>%s</code>" % (c["name"], escape(c["text"]), escape(c["example"]))


def build_admin_panel(username=""):
    head = "👑 <b>Admin Panel%s</b>\n━━━━━━━━━━━━━━━━━━━━━━━━" % (" — @%s" % escape(username) if username else "")
    blocks = [head]
    for key, title in SECTIONS:
        entries = [_entry(c) for c in COMMANDS if c["section"] == key]
        if entries:
            blocks.append("<b>%s</b>\n%s" % (title, "\n".join(entries)))
    blocks.append("🔐 Everything above is admin-only except /start, /settings and /file_forward.")
    chunks, cur = [], ""
    for block in blocks:
        if cur and len(cur) + len(block) + 2 > PANEL_LIMIT:
            chunks.append(cur)
            cur = block
        else:
            cur = block if not cur else cur + "\n\n" + block
    if cur:
        chunks.append(cur)
    return chunks
