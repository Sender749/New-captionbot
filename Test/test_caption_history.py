"""
Unit tests for body/caption_history.py (no MongoDB needed: the collection is faked).

    python -m unittest tests.test_caption_history -v
"""
import asyncio
import importlib
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

H = importlib.import_module("body.caption_history")
Cap = importlib.import_module("body.Caption")


class FakeCol:
    def __init__(self):
        self.ops = []
        self.fail = False

    async def bulk_write(self, ops, ordered=False):
        if self.fail:
            raise RuntimeError("mongo down")
        self.ops.extend(ops)


JOB = {
    "chat_id": -1001, "message_id": 7, "file_unique_id": "AgADxyz",
    "original_file_name": "Nail.Polish.2021.480p.WEB-DL.Hindi.ESub.mkv",
    "default_caption_plain": "Audio: Hindi\nSubtitle: English",
    "user_id": 12345,                      # must NEVER be stored
}


class DocTests(unittest.TestCase):
    def _meta(self):
        meta = {}
        Cap.render_caption_for_job(
            {**JOB, "cap_template": "{smart_file_name}", "file_name": "Nail Polish"}, meta_out=meta)
        return meta

    def test_doc_shape_and_no_user_id(self):
        meta = {"engine_version": "2.0", "flags": ["conflict", "junk"],
                "title": {"value": "Nail Polish", "conf": "high", "source": "filename"}}
        d = H.build_history_doc(JOB, meta, "NEW CAPTION", "SMART")
        self.assertEqual(d["channel_id"], -1001)
        self.assertEqual(d["file_unique_id"], "AgADxyz")
        self.assertEqual(d["flags"], ["conflict"])                # unknown flags dropped
        self.assertEqual(d["parsed"]["title"]["value"], "Nail Polish")
        self.assertNotIn("user_id", d)
        self.assertNotIn("12345", repr(d))
        self.assertEqual(d["engine_version"], "2.0")

    def test_text_is_capped(self):
        d = H.build_history_doc({**JOB, "default_caption_plain": "x" * 9000}, {}, "y" * 9000)
        self.assertEqual(len(d["original_caption"]), H.MAX_TEXT)
        self.assertEqual(len(d["new_caption"]), H.MAX_TEXT)

    def test_fallback_unique_id(self):
        d = H.build_history_doc({**JOB, "file_unique_id": None}, {}, "n")
        self.assertEqual(d["file_unique_id"], "msg7")

    def test_render_fills_meta(self):
        meta = self._meta()
        self.assertEqual(meta["engine_version"], H.ENGINE_VERSION)
        self.assertEqual(meta["audio"]["langs"], ["Hindi"])
        self.assertEqual(meta["audio"]["source"], "label")
        self.assertIn("smart_file_name", meta)


class WriterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.col = FakeCol()
        self._old = H.history_col
        H.history_col = self.col
        H._queue = asyncio.Queue(maxsize=3)

    async def asyncTearDown(self):
        H.history_col = self._old
        H._queue = None

    async def test_record_is_nonblocking_and_drops_when_full(self):
        for i in range(5):
            H.record_history({**JOB, "message_id": i}, {}, "n")
        self.assertEqual(H._queue.qsize(), 3)                    # 2 dropped, never raised

    async def test_bulk_write_is_upsert_on_unique_key(self):
        docs = [H.build_history_doc({**JOB, "message_id": i}, {}, "n") for i in range(3)]
        await H._write_batch(docs)
        self.assertEqual(len(self.col.ops), 3)
        op = self.col.ops[0]
        self.assertTrue(op._upsert)
        self.assertEqual(set(op._filter), {"channel_id", "file_unique_id", "engine_version"})

    async def test_db_errors_are_swallowed(self):
        self.col.fail = True
        await H._write_batch([H.build_history_doc(JOB, {}, "n")])   # must not raise

    async def test_without_queue_record_is_a_noop(self):
        H._queue = None
        H.record_history(JOB, {}, "n")                           # must not raise


class ExportTests(unittest.TestCase):
    def test_args(self):
        self.assertEqual(H.parse_export_args([]), (7, "all"))
        self.assertEqual(H.parse_export_args(["30", "unsure"]), (30, "unsure"))
        self.assertEqual(H.parse_export_args(["changed", "2"]), (2, "changed"))

    def test_files(self):
        d = H.build_history_doc(JOB, {"flags": ["low_confidence"]}, "OUT")
        d["reported"] = True
        d["expected_output"] = "RIGHT"
        jl, txt = H.export_files([d])
        self.assertIn(b'"expected_output": "RIGHT"', jl)
        t = txt.decode()
        for key in ("FILE:", "CAPTION:", "OUTPUT:", "FLAGS:", "EXPECTED: RIGHT", "[REPORTED]"):
            self.assertIn(key, t)


if __name__ == "__main__":
    unittest.main(verbosity=2)
