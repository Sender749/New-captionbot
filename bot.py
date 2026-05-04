import os
import asyncio
import importlib
import pkgutil
from pyrogram import Client, errors
from pyrogram.errors import FloodWait
from info import *
from body.database import *
from body.Caption import *
from body.file_forward import *

# Caption workers: enough to handle many channels simultaneously.
# Each worker handles ONE caption job at a time; more workers = more parallel channel edits.
EXECUTORS = 50

# Forward workers: each handles ONE file forward job at a time.
# Set high so many users can forward simultaneously without waiting for a free worker.
FORWARD_EXECUTORS = 30

PLUGIN_ROOT = "body"


class Bot(Client):
    def __init__(self):
        super().__init__(
            name="Auto Cap",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            workers=100,          # Pyrogram internal handler threads — keep high so UI never blocks
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
        await ensure_forward_indexes()
        await recover_stuck_jobs()
        await recover_stuck_forward_jobs()

        # Start caption workers
        for _ in range(EXECUTORS):
            asyncio.create_task(caption_worker(self))

        # Start forward workers
        for _ in range(FORWARD_EXECUTORS):
            asyncio.create_task(forward_worker(self))

        me = await self.get_me()
        self.force_channel = FORCE_SUB
        if FORCE_SUB:
            try:
                self.invitelink = await self.export_chat_invite_link(FORCE_SUB)
            except Exception:
                print("⚠️ Bot must be admin in force-sub channel")
                self.force_channel = None

        print("========== DUMP CHANNEL DEBUG ==========")
        print(f"FF_CH = {FF_CH} | type = {type(FF_CH)}")
        print(f"CP_CH = {CP_CH} | type = {type(CP_CH)}")
        print("========================================")
        print(f"{me.first_name} is started ✨")
        try:
            await self.send_message(ADMIN, f"**{me.first_name} started ✨**")
        except:
            pass

    async def _run_plugin_startup_hooks(self):
        package = importlib.import_module(PLUGIN_ROOT)
        for _, module_name, _ in pkgutil.iter_modules(package.__path__):
            module = importlib.import_module(f"{PLUGIN_ROOT}.{module_name}")
            hook = getattr(module, "on_bot_start", None)
            if callable(hook):
                # Skip file_forward startup hook — we launch workers above with our own count
                if module_name == "file_forward":
                    continue
                print(f"🔌 Running startup hook: {module_name}.on_bot_start()")
                hook(self)


Bot().run()
