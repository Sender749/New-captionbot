import os
import asyncio
import importlib
import logging
import pkgutil
from pyrogram import Client
from pyrogram.errors import FloodWait

from info import *
from body.database import (
    CAPTION_WORKERS,
    ensure_queue_indexes,
    ensure_forward_indexes,
    recover_stuck_jobs,
)
from body.Caption import caption_worker

PLUGIN_ROOT = "body"

# ── Logging — output to stdout so Koyeb log stream captures everything ────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler()],
)
log = logging.getLogger("BOT")


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
        log.info("[BOOT] Bot starting up…")

        # Connect with FloodWait guard
        try:
            await super().start()
        except FloodWait as e:
            log.warning("[BOOT] Startup FloodWait: sleeping %ds", e.value)
            await asyncio.sleep(e.value)
            await super().start()

        # One-time DB setup
        log.info("[BOOT] Setting up DB indexes…")
        await ensure_queue_indexes()
        await ensure_forward_indexes()

        stuck = await recover_stuck_jobs()
        log.info("[BOOT] DB indexes ready")

        # Run per-plugin startup hooks (e.g. file_forward.on_bot_start)
        await self._run_plugin_startup_hooks()

        # Start per-channel caption queue workers
        for i in range(CAPTION_WORKERS):
            asyncio.create_task(caption_worker(self), name=f"cap_worker_{i}")
        log.info("[BOOT] %d caption worker(s) started", CAPTION_WORKERS)

        me = await self.get_me()
        self.force_channel = FORCE_SUB
        if FORCE_SUB:
            try:
                self.invitelink = await self.export_chat_invite_link(FORCE_SUB)
            except Exception:
                log.warning("[BOOT] Bot must be admin in force-sub channel")
                self.force_channel = None

        log.info("[BOOT] %s is online ✨", me.first_name)
        try:
            await self.send_message(
                ADMIN[0] if isinstance(ADMIN, list) else ADMIN,
                f"**{me.first_name} started ✨**"
            )
        except Exception:
            pass

    async def _run_plugin_startup_hooks(self):
        package = importlib.import_module(PLUGIN_ROOT)
        for _, module_name, _ in pkgutil.iter_modules(package.__path__):
            module = importlib.import_module(f"{PLUGIN_ROOT}.{module_name}")
            hook = getattr(module, "on_bot_start", None)
            if callable(hook):
                log.info("[BOOT] Running startup hook: %s.on_bot_start()", module_name)
                hook(self)


Bot().run()
