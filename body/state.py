"""
Shared in-memory state module.
Imported by both Caption.py and file_forward.py to avoid circular imports.
"""
import asyncio

# ── Per-user caption tasks ────────────────────────────────────────────────────
# user_id -> asyncio.Task
_USER_CAPTION_TASKS: dict[int, asyncio.Task] = {}

# ── Per-user forward tasks ────────────────────────────────────────────────────
# user_id -> asyncio.Task
_USER_FORWARD_TASKS: dict[int, asyncio.Task] = {}

# ── File-forward session state ────────────────────────────────────────────────
# user_id -> session dict
FF_SESSIONS: dict[int, dict] = {}

# ── Cancelled session IDs (session_id strings) ───────────────────────────────
CANCELLED_SESSIONS: set[str] = set()
