import os
import asyncio
import importlib
import pkgutil
from pyrogram import Client, errors
from pyrogram.errors import FloodWait
from info import *
from body.database import *
from body.Caption import global_caption_worker
from body.file_forward import on_bot_start

PLUGIN_ROOT          = "body"
GLOBAL_CAP_WORKERS   = 8    # fallback caption workers (orphan/unowned jobs)


class Bot(Client):
    def __init__(self):
        super().__init__(
            name="Auto Cap",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            workers=50,
            plugins={"root": PLUGIN_ROOT},
            sleep_threshold=15,
        )

    async def start(self):
        # ── Connect ──────────────────────────────────────────────────────
        try:
            await super().start()
        except FloodWait as e:
            print(f"🚨 Startup FloodWait — sleeping {e.value}s")
            await asyncio.sleep(e.value)
            await super().start()

        # ── DB indexes ───────────────────────────────────────────────────
        await ensure_queue_indexes()
        await ensure_forward_indexes()
        await recover_stuck_jobs()

        # ── Plugin startup hooks ─────────────────────────────────────────
        await self._run_plugin_startup_hooks()

        # ── Global fallback caption workers ──────────────────────────────
        for _ in range(GLOBAL_CAP_WORKERS):
            asyncio.create_task(global_caption_worker(self))

        # ── Force-sub channel ────────────────────────────────────────────
        self.force_channel = FORCE_SUB or None
        if self.force_channel:
            try:
                self.invitelink = await self.export_chat_invite_link(self.force_channel)
            except Exception:
                print("⚠️  Bot must be admin in force-sub channel")
                self.force_channel = None

        me = await self.get_me()
        print(f"✅ {me.first_name} started")

        try:
            admin_ids = ADMIN if isinstance(ADMIN, (list, tuple)) else [ADMIN]
            await self.send_message(admin_ids[0], f"**{me.first_name} started ✨**")
        except Exception:
            pass

    async def _run_plugin_startup_hooks(self):
        pkg = importlib.import_module(PLUGIN_ROOT)
        for _, mod_name, _ in pkgutil.iter_modules(pkg.__path__):
            mod  = importlib.import_module(f"{PLUGIN_ROOT}.{mod_name}")
            hook = getattr(mod, "on_bot_start", None)
            if callable(hook):
                print(f"🔌 {mod_name}.on_bot_start()")
                hook(self)


Bot().run()
