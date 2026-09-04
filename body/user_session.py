"""
user_session.py — Optional Pyrogram *user* client ("userbot"), authenticated
with a Pyrogram session string belonging to a personal Telegram account.

Why this exists
────────────────────────────────────────────────────────────────────────────
The main `Bot` client in bot.py is a bot account. Telegram bots can only see
messages in chats they've been explicitly added to (as a member or admin) —
there's no way around this via the Bot API/MTProto as a bot. If someone is a
regular member of a channel but never added this bot to it, the bot has
ZERO access to that channel, full stop.

A personal account, on the other hand, already has access to every chat it's
a member of. body/member_forward.py uses this second client to read (and,
where possible, directly copy) messages from those channels, so files can
still be pulled out and forwarded into any channel the bot administers.

Setup
────────────────────────────────────────────────────────────────────────────
Generate a session string once, locally — never on the server / in a repo:

    from pyrogram import Client
    from info import API_ID, API_HASH
    with Client("gen", api_id=API_ID, api_hash=API_HASH, in_memory=True) as app:
        print(app.export_session_string())

Set the printed string as the SESSION_STRING environment variable. If it's
left unset, member_forward.py's command simply replies that the feature is
disabled (checked via USER_SESSION_ENABLED) — nothing else in the bot is
affected.

Security note: a session string grants FULL access to that Telegram account
(read every chat, send messages, even delete the account) — treat it like a
password. Never share it, log it, or commit it to source control.
"""

import logging
from typing import Optional

from pyrogram import Client

from info import API_ID, API_HASH, SESSION_STRING

logger = logging.getLogger("captionbot.user_session")

USER_SESSION_ENABLED = bool(SESSION_STRING.strip())

# `plugins` is intentionally omitted (defaults to None) here — this client
# must NEVER auto-load body/*.py as plugins the way the main Bot does.
# Those handlers are written for the bot account (filters.channel, "client
# is always the bot" assumptions, etc.) and must only ever be attached
# once, to the Bot client in bot.py.
user_client: Optional[Client] = (
    Client(
        name="MemberSession",
        api_id=API_ID,
        api_hash=API_HASH,
        session_string=SESSION_STRING,
        no_updates=True,     # we only ever make outgoing API calls with this client
        sleep_threshold=60,  # FloodWaits ≤60s are auto-retried by Pyrogram itself;
                              # anything longer raises FloodWait for our own code to
                              # handle (see member_forward.py's explicit FloodWait
                              # handling in the scan loop and worker pool).
    )
    if USER_SESSION_ENABLED
    else None
)


async def start_user_session() -> bool:
    """
    Connects the userbot client. Safe to call even when disabled (returns
    False immediately). Never raises — a missing/bad/expired session string
    just disables the feature for this run instead of crashing the whole bot.
    """
    if not USER_SESSION_ENABLED:
        logger.info("[USER_SESSION] SESSION_STRING not set — member-channel forwarding disabled")
        return False
    try:
        await user_client.start()
        me = await user_client.get_me()
        logger.info(
            f"[USER_SESSION] 👤 connected as {me.first_name} "
            f"(@{me.username or me.id}) — member-channel forwarding enabled"
        )
        return True
    except Exception as e:
        logger.warning(
            f"[USER_SESSION] ⚠️  Could not start userbot session "
            f"(member-channel forwarding disabled this run): {e}"
        )
        return False


async def stop_user_session():
    if USER_SESSION_ENABLED and user_client is not None and user_client.is_connected:
        try:
            await user_client.stop()
        except Exception as e:
            logger.warning(f"[USER_SESSION] ⚠️  error stopping userbot session: {e}")
