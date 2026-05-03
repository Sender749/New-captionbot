"""
Shared in-memory state — imported by Caption.py and file_forward.py
to avoid circular imports.
"""
import asyncio

# per-user caption tasks:  user_id -> asyncio.Task
_USER_CAPTION_TASKS: dict[int, asyncio.Task] = {}

# per-user forward tasks:  user_id -> asyncio.Task
_USER_FORWARD_TASKS: dict[int, asyncio.Task] = {}

# file-forward session state:  user_id -> session dict
FF_SESSIONS: dict[int, dict] = {}

# cancelled session IDs (session_id strings)
CANCELLED_SESSIONS: set[str] = set()
