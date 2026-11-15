import asyncio
import logging

from pyrogram.errors import FloodWait
from pyrogram.types import (
    BotCommand,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeChat,
    BotCommandScopeDefault,
)

from info import ADMIN, AUTO_SET_COMMANDS
from body.command_registry import admin_menu, user_menu

logger = logging.getLogger("captionbot.commands")

ADMIN_GAP_S = 0.4


def _commands(pairs):
    return [BotCommand(name, desc[:256]) for name, desc in pairs]


async def _call(coro_factory):
    try:
        return await coro_factory()
    except FloodWait as e:
        await asyncio.sleep(e.value + 1)
        return await coro_factory()


async def apply_commands(client):
    users = _commands(user_menu())
    admins = _commands(admin_menu())
    await _call(lambda: client.set_bot_commands(users, scope=BotCommandScopeDefault()))
    await _call(lambda: client.set_bot_commands(users, scope=BotCommandScopeAllPrivateChats()))
    done = 0
    for admin_id in {int(a) for a in ADMIN}:
        try:
            await _call(lambda: client.set_bot_commands(admins, scope=BotCommandScopeChat(chat_id=admin_id)))
            done += 1
        except Exception as e:
            logger.warning("admin menu not set for %s (they must press /start once): %s", admin_id, e)
        await asyncio.sleep(ADMIN_GAP_S)
    logger.info("bot commands set: %d user, %d admin (%d admin chat(s))", len(users), len(admins), done)


async def _safe_apply(client):
    try:
        await apply_commands(client)
    except Exception as e:
        logger.warning("bot commands setup failed: %s", e)


def on_bot_start(client):
    if not AUTO_SET_COMMANDS:
        logger.info("bot commands: auto setup disabled (AUTO_SET_COMMANDS=0)")
        return
    asyncio.create_task(_safe_apply(client), name="bot_commands_setup")
