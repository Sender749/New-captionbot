import os
import asyncio
import importlib
import pkgutil
from pyrogram import Client
from pyrogram.errors import FloodWait

from info import *
from body.database import (
    CAPTION_WORKERS,
    ensure_queue_indexes,
    ensure_forward_indexes,
    ensure_global_ff_indexes,   # ← NEW
    recover_stuck_jobs,
)
from body.Caption import caption_worker

PLUGIN_ROOT = "body"


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
            print(f"🚨 Startup FloodWait: sleeping {e.value}s")
            await asyncio.sleep(e.value)
            await super().start()

        # One-time DB setup
        await ensure_queue_indexes()
        await ensure_forward_indexes()
        await ensure_global_ff_indexes()   # ← NEW: indexes for admin forwarding
        await recover_stuck_jobs()

        # Run per-plugin startup hooks (e.g. file_forward.on_bot_start,
        # admin_channels.on_bot_start)
        await self._run_plugin_startup_hooks()

        # Launch caption worker pool  (CAPTION_WORKERS = 6)
        for i in range(CAPTION_WORKERS):
            asyncio.create_task(caption_worker(self), name=f"cap_worker_{i}")
        print(f"[BOT] {CAPTION_WORKERS} caption workers started")

        me = await self.get_me()
        self.force_channel = FORCE_SUB
        if FORCE_SUB:
            try:
                self.invitelink = await self.export_chat_invite_link(FORCE_SUB)
            except Exception:
                print("⚠️  Bot must be admin in force-sub channel")
                self.force_channel = None

        print(f"{me.first_name} is started ✨")
        try:
            await self.send_message(
                ADMIN[0] if isinstance(ADMIN, list) else ADMIN,
                f"**{me.first_name} started ✨**",
            )
        except Exception:
            pass

    async def _run_plugin_startup_hooks(self):
        package = importlib.import_module(PLUGIN_ROOT)
        for _, module_name, _ in pkgutil.iter_modules(package.__path__):
            module = importlib.import_module(f"{PLUGIN_ROOT}.{module_name}")
            hook = getattr(module, "on_bot_start", None)
            if callable(hook):
                print(f"🔌 Running startup hook: {module_name}.on_bot_start()")
                hook(self)


Bot().run()
