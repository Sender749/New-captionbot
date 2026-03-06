import os
import asyncio
import importlib
import pkgutil
from pyrogram import Client, errors
from pyrogram.errors import FloodWait
from info import *
from body.database import *

EXECUTORS   = 30
PLUGIN_ROOT = "body"


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
        try:
            await super().start()
        except FloodWait as e:
            print(f"🚨 Startup FloodWait: sleeping {e.value}s")
            await asyncio.sleep(e.value)
            await super().start()
        await self._run_plugin_startup_hooks()
        await ensure_queue_indexes()
        await recover_stuck_jobs()
        from body.Caption import caption_worker
        for _ in range(EXECUTORS):
            asyncio.create_task(caption_worker(self))
        me = await self.get_me()
        self.force_channel = FORCE_SUB
        if FORCE_SUB:
            try:
                self.invitelink = await self.export_chat_invite_link(FORCE_SUB)
            except Exception:
                print("⚠️ Bot must be admin in force-sub channel")
                self.force_channel = None
        print(f"{me.first_name} started ✨")
        try:
            await self.send_message(ADMIN, f"<b>{me.first_name} started ✨</b>")
        except:
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
