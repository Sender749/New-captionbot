import os
import asyncio
import importlib
import pkgutil
import platform
import sys
from datetime import datetime, timezone

from pyrogram import Client
from pyrogram.errors import FloodWait

from info import ADMIN
from body.database import (
    CAPTION_WORKERS,
    ensure_queue_indexes,
    ensure_forward_indexes,
    ensure_global_ff_indexes,
    recover_stuck_jobs,
)
from body.Caption import caption_worker

PLUGIN_ROOT = "body"


class Bot(Client):
    def __init__(self):
        super().__init__(
            name="Auto Cap",
            api_id=os.environ.get("API_ID"),
            api_hash=os.environ.get("API_HASH"),
            bot_token=os.environ.get("BOT_TOKEN"),
            workers=10,
            plugins={"root": PLUGIN_ROOT},
            sleep_threshold=60,
        )

    async def start(self):
        # ── Connect ────────────────────────────────────────────────────────
        try:
            await super().start()
        except FloodWait as e:
            print(f"🚨 Startup FloodWait: sleeping {e.value}s")
            await asyncio.sleep(e.value)
            await super().start()

        # ── DB setup ───────────────────────────────────────────────────────
        await ensure_queue_indexes()
        await ensure_forward_indexes()
        await ensure_global_ff_indexes()
        await recover_stuck_jobs()

        # ── Plugin startup hooks (e.g. admin_channels.on_bot_start) ───────
        await self._run_plugin_startup_hooks()

        # ── Caption workers ────────────────────────────────────────────────
        for i in range(CAPTION_WORKERS):
            asyncio.create_task(caption_worker(self), name=f"cap_worker_{i}")
        print(f"[BOT] {CAPTION_WORKERS} caption workers started")

        # ── Force-sub channel ──────────────────────────────────────────────
        from info import FORCE_SUB
        self.force_channel = FORCE_SUB or None
        if self.force_channel:
            try:
                self.invitelink = await self.export_chat_invite_link(self.force_channel)
            except Exception:
                print("⚠️  Bot must be admin in force-sub channel")
                self.force_channel = None

        # ── Startup / restart notification → ALL admins ────────────────────
        me = await self.get_me()
        now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        admin_ids = ADMIN if isinstance(ADMIN, (list, tuple)) else [ADMIN]

        startup_text = (
            f"🤖 <b>{me.first_name} — Started Successfully</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🕐 <b>Time:</b> <code>{now_utc}</code>\n"
            f"🐍 <b>Python:</b> <code>{sys.version.split()[0]}</code>\n"
            f"🖥 <b>Platform:</b> <code>{platform.system()} {platform.release()}</code>\n\n"
            f"⚙️ <b>Workers:</b> {CAPTION_WORKERS} caption | 4 forward | 2 global-FF\n"
            f"📌 <b>Username:</b> @{me.username or 'N/A'}\n\n"
            "✅ All systems online. Bot is ready."
        )

        for admin_id in admin_ids:
            try:
                await self.send_message(
                    admin_id,
                    startup_text,
                    parse_mode="html",
                    disable_web_page_preview=True,
                )
            except Exception as e:
                print(f"[STARTUP_MSG] Could not notify admin {admin_id}: {e}")

        print(f"✨ {me.first_name} started — {now_utc}")

    async def _run_plugin_startup_hooks(self):
        package = importlib.import_module(PLUGIN_ROOT)
        for _, module_name, _ in pkgutil.iter_modules(package.__path__):
            try:
                module = importlib.import_module(f"{PLUGIN_ROOT}.{module_name}")
                hook   = getattr(module, "on_bot_start", None)
                if callable(hook):
                    print(f"🔌 Running startup hook: {module_name}.on_bot_start()")
                    hook(self)
            except Exception as e:
                print(f"[HOOK_ERR] {module_name}: {e}")


Bot().run()
