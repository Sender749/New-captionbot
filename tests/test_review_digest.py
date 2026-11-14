import importlib
import os
import sys
import unittest
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

D = importlib.import_module("body.review_digest")
R = importlib.import_module("body.review_flow")


def doc(name, title, flags=("low_confidence",), fixes=None, reported=False):
    return {
        "original_filename": name, "original_caption": "", "flags": list(flags),
        "parsed": {"title": {"value": title}}, "field_fixes": fixes or {}, "reported": reported,
    }


class PatternTests(unittest.TestCase):
    def test_same_shape_groups(self):
        a = doc("Alpha.Movie.2019.720p.WEBRip.x264.mkv", "Alpha Movie")
        b = doc("Beta.Film.2021.1080p.WEBRip.x264.mkv", "Beta Film")
        self.assertEqual(D.pattern_of(a), D.pattern_of(b))
        groups = D.group_patterns([a, b, doc("Other.Thing.S01E02.mkv", "Other Thing")])
        self.assertEqual(groups[0]["count"], 2)

    def test_flags_split_groups(self):
        a = doc("Alpha.2019.720p.mkv", "Alpha", flags=("conflict",))
        b = doc("Beta.2019.720p.mkv", "Beta", flags=("low_confidence",))
        self.assertEqual(len(D.group_patterns([a, b])), 2)


class TextTests(unittest.TestCase):
    def test_digest_contains_counts(self):
        docs = [doc("A.2019.720p.mkv", "A"), doc("B.2019.720p.mkv", "B", fixes={"audio": "x"}, reported=True)]
        text = D.build_digest_text(docs, 40, 3, 7, now=datetime(2026, 11, 13, 4, 0, tzinfo=timezone.utc))
        self.assertIn("Edited (last 24h): <b>40</b>", text)
        self.assertIn("reported wrong: <b>1</b>", text)
        self.assertIn("Fields fixed: audio 1", text)
        self.assertIn("Awaiting review: <b>3</b>", text)
        self.assertIn("Top patterns", text)

    def test_chunks_respect_size(self):
        text = "\n".join("line %d" % i for i in range(2000))
        chunks = D.split_chunks(text, 500)
        self.assertTrue(all(len(c) <= 500 for c in chunks))
        self.assertEqual("\n".join(chunks), text)


class TimeTests(unittest.TestCase):
    def test_parse_time(self):
        self.assertEqual(D.parse_time("04:05"), (4, 5))
        self.assertEqual(D.parse_time("bad"), (3, 30))
        self.assertEqual(D.parse_time("25:00"), (3, 30))

    def test_seconds_until_rolls_over(self):
        now = datetime(2026, 11, 13, 5, 0, tzinfo=timezone.utc)
        self.assertEqual(D.seconds_until(3, 30, now), 22.5 * 3600)
        self.assertEqual(D.seconds_until(6, 0, now), 3600)


class EnqueueTests(unittest.TestCase):
    def setUp(self):
        import asyncio
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.old = (R._queue, R.REVIEW_CH)
        R._queue = __import__("asyncio").Queue(maxsize=10)
        R.REVIEW_CH = -100123
        R._recent.clear()
        self.job = {"chat_id": -1001, "message_id": 9, "original_file_name": "a.mkv", "default_caption_plain": "c"}
        self.meta = {"sig": "s", "flags": ["conflict"], "title": {"value": "A"}, "smart_file_name": "out"}

    def tearDown(self):
        R._queue, R.REVIEW_CH = self.old
        self.loop.close()

    def test_flagged_is_queued(self):
        R.enqueue_review(self.job, self.meta)
        self.assertEqual(R._queue.qsize(), 1)

    def test_unflagged_skipped(self):
        R.enqueue_review(self.job, {**self.meta, "flags": []})
        self.assertEqual(R._queue.qsize(), 0)

    def test_corrected_skipped(self):
        R.enqueue_review(self.job, {**self.meta, "corrected": True})
        self.assertEqual(R._queue.qsize(), 0)

    def test_dedupe_same_title(self):
        R.enqueue_review(self.job, self.meta)
        R.enqueue_review({**self.job, "message_id": 10}, self.meta)
        self.assertEqual(R._queue.qsize(), 1)

    def test_disabled_without_channel(self):
        R.REVIEW_CH = 0
        R.enqueue_review(self.job, self.meta)
        self.assertEqual(R._queue.qsize(), 0)

    def test_card_text_escaped(self):
        item = {"cid": -1001234, "mid": 5, "filename": "<b>x</b>", "caption": "c", "bot_output": "o", "flags": ["conflict"]}
        text = R.build_card_text(item)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", text)
        self.assertIn("https://t.me/c/1234/5", text)


if __name__ == "__main__":
    unittest.main()
