import os
import asyncio
import importlib
import logging
import pkgutil
import platform
import sys
from pyrogram import Client
from pyrogram.errors import FloodWait

from info import *
from body.database import (
    CAPTION_WORKERS,
    ensure_queue_indexes,
    ensure_forward_indexes,
    recover_stuck_jobs,
)
from body.Caption import start_caption_workers

PLUGIN_ROOT = "body"

# ── Global logging setup ──────────────────────────────────────────────────────
# Everything logs to stdout (Koyeb captures stdout via supervisord ->
# /app/logs/bot.log), so admins can `koyeb service logs` / tail the file to
# see exactly what the bot is doing (queue workers, /channels, FloodWaits,
# job failures, etc.) instead of the previous near-total silence.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    stream=sys.stdout,
)
# Pyrogram itself is fairly chatty at INFO; keep it at WARNING so our own
# [CAP_WORKER]/[CHANNELS]/[GFF] logs aren't drowned out.
logging.getLogger("pyrogram").setLevel(logging.WARNING)

logger = logging.getLogger("captionbot.bot")


class Bot(Client):
    def __init__(self):
        super().__init__(
            name="Auto Cap",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            # workers: Pyrogram's internal thread pool for update dispatch.
            # 10 is plenty on Koyeb free tier; all heavy lifting is asyncio tasks.
            workers=10,
            plugins={"root": PLUGIN_ROOT},
            sleep_threshold=60,   # auto-sleep up to 60 s on minor FloodWaits
        )

    async def start(self):
        # Connect with FloodWait guard
        try:
            await super().start()
        except FloodWait as e:
            logger.warning(f"🚨 Startup FloodWait: sleeping {e.value}s")
            await asyncio.sleep(e.value)
            await super().start()

        # One-time DB setup
        await ensure_queue_indexes()
        await ensure_forward_indexes()
        await recover_stuck_jobs()

        # Run per-plugin startup hooks (e.g. file_forward.on_bot_start,
        # admin_channels.on_bot_start)
        await self._run_plugin_startup_hooks()

        # Start the caption queue system: CAPTION_WORKERS (default 6)
        # parallel workers, each auto-restarted if it ever crashes, instead
        # of the previous single un-recovering task. This is the fix for
        # "bot only edits 10-15 files out of 1000+" — that happened because
        # the one and only worker died silently on the first transient
        # error and nothing ever replaced it.
        start_caption_workers(self, count=CAPTION_WORKERS)

        me = await self.get_me()
        self.force_channel = FORCE_SUB
        if FORCE_SUB:
            try:
                self.invitelink = await self.export_chat_invite_link(FORCE_SUB)
            except Exception:
                logger.warning("⚠️  Bot must be admin in force-sub channel")
                self.force_channel = None

        logger.info(
            f"{me.first_name} is started ✨ | "
            f"python={platform.python_version()} | "
            f"platform={platform.system()} {platform.release()} | "
            f"caption_workers={CAPTION_WORKERS}"
        )
        try:
            await self.send_message(ADMIN[0] if isinstance(ADMIN, list) else ADMIN,
                                    f"**{me.first_name} started ✨**")
        except Exception:
            pass

    async def _run_plugin_startup_hooks(self):
        package = importlib.import_module(PLUGIN_ROOT)
        for _, module_name, _ in pkgutil.iter_modules(package.__path__):
            module = importlib.import_module(f"{PLUGIN_ROOT}.{module_name}")
            hook = getattr(module, "on_bot_start", None)
            if callable(hook):
                logger.info(f"🔌 Running startup hook: {module_name}.on_bot_start()")
                hook(self)


Bot().run()
