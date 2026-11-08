import asyncio
import html
import logging
import os
from collections import deque

from pyrogram.errors import FloodWait

from info import LOG_CH

LEVEL_NAME = os.environ.get("LOG_CH_LEVEL", "WARNING").upper()
LEVEL = getattr(logging, LEVEL_NAME, logging.WARNING)
MAX_BUFFER = 500
SEND_INTERVAL = 5
CHUNK_LIMIT = 3500
MAX_CHUNKS_PER_CYCLE = 4
MAX_LINE = 400
EXCLUDED_PREFIXES = ("pyrogram", "captionbot.log_channel")


class ChannelLogHandler(logging.Handler):
    def __init__(self):
        super().__init__(LEVEL)
        self.buffer = deque(maxlen=MAX_BUFFER)
        self.dropped = 0
        self.setFormatter(logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s", "%H:%M:%S"))

    def emit(self, record):
        if record.name.startswith(EXCLUDED_PREFIXES):
            return
        try:
            line = self.format(record).replace("\n", " ⏎ ")
            key = f"{record.levelname}|{record.name}|{record.getMessage()}"
            if len(line) > MAX_LINE:
                line = line[:MAX_LINE] + "…"
            if len(self.buffer) == self.buffer.maxlen:
                self.dropped += 1
            self.buffer.append((key, line))
        except Exception:
            pass


_handler = None


def _collapse(items):
    order, counts = [], {}
    for key, line in items:
        if key not in counts:
            counts[key] = 0
            order.append((key, line))
        counts[key] += 1
    return [line if counts[key] == 1 else f"{line}  (x{counts[key]})" for key, line in order]


def _chunks(lines):
    current, size = [], 0
    for line in lines:
        if size + len(line) + 1 > CHUNK_LIMIT and current:
            yield "\n".join(current)
            current, size = [], 0
        current.append(line)
        size += len(line) + 1
    if current:
        yield "\n".join(current)


async def _send(client, chat_id, text):
    for _ in range(2):
        try:
            await client.send_message(chat_id, f"<pre>{html.escape(text)}</pre>", disable_web_page_preview=True)
            return
        except FloodWait as e:
            await asyncio.sleep(e.value + 1)
        except Exception:
            return


async def _sender(client, chat_id):
    while True:
        await asyncio.sleep(SEND_INTERVAL)
        if not _handler.buffer:
            continue
        items = []
        while _handler.buffer:
            items.append(_handler.buffer.popleft())
        dropped, _handler.dropped = _handler.dropped, 0
        lines = _collapse(items)
        if dropped:
            lines.append(f"[{dropped} older log line(s) dropped: buffer full]")
        chunks = list(_chunks(lines))
        for chunk in chunks[:MAX_CHUNKS_PER_CYCLE]:
            await _send(client, chat_id, chunk)
        if len(chunks) > MAX_CHUNKS_PER_CYCLE:
            await _send(client, chat_id, f"[{len(chunks) - MAX_CHUNKS_PER_CYCLE} more log chunk(s) omitted]")


async def _guarded(client, chat_id):
    while True:
        try:
            await _sender(client, chat_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(5)


def on_bot_start(client):
    global _handler
    if _handler is not None:
        return
    try:
        chat_id = int(LOG_CH)
    except (TypeError, ValueError):
        return
    _handler = ChannelLogHandler()
    logging.getLogger().addHandler(_handler)
    asyncio.create_task(_guarded(client, chat_id), name="log_channel_sender")
