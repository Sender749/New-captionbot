import asyncio
import importlib
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

C = importlib.import_module("body.corrections_store")
Cap = importlib.import_module("body.Caption")


class FakeCol:
    def __init__(self):
        self.docs = {}
        self.fail = False

    async def update_one(self, flt, upd, upsert=False):
        if self.fail:
            raise RuntimeError("db down")
        self.docs[flt["sig"]] = upd["$set"]


class SigTests(unittest.TestCase):
    def test_stable_and_whitespace_insensitive(self):
        a = C.make_sig("Movie.2019.mkv", "Audio:  Hindi\nESub")
        b = C.make_sig("movie.2019.mkv", "audio: hindi esub")
        self.assertEqual(a, b)

    def test_differs_on_caption(self):
        self.assertNotEqual(C.make_sig("a", "x"), C.make_sig("a", "y"))


class ParseTests(unittest.TestCase):
    def test_name(self):
        self.assertEqual(C.parse_correction("  Movie  (2019)  Hindi  "), ("name", "Movie (2019) Hindi"))

    def test_fields(self):
        kind, val = C.parse_correction("audio=Hindi + English; sub=ESub\nyear=2019")
        self.assertEqual(kind, "fields")
        self.assertEqual(val, {"audio": "Hindi + English", "subtitle": "ESub", "year": "2019"})

    def test_sub_none(self):
        self.assertEqual(C.parse_correction("sub=none"), ("fields", {"subtitle": ""}))

    def test_bad_year(self):
        self.assertIsNone(C.parse_correction("year=abc")[0])

    def test_mixed_rejected(self):
        self.assertIsNone(C.parse_correction("audio=Hindi; random words")[0])

    def test_empty_value_rejected(self):
        self.assertIsNone(C.parse_correction("title=")[0])

    def test_empty(self):
        self.assertIsNone(C.parse_correction("   ")[0])

    def test_too_long(self):
        self.assertIsNone(C.parse_correction("x" * 700)[0])


class ApplyTests(unittest.TestCase):
    def test_audio_and_subtitle(self):
        info = {"audio_langs": ["Hindi"], "audio": "Hindi", "subtitle": "", "title": "T", "year": "2000"}
        C.apply_fields(info, {"audio": "Hindi + English", "subtitle": "ESub", "year": "2019", "title": "New"})
        self.assertEqual(info["audio_langs"], ["Hindi", "English"])
        self.assertEqual(info["audio"], "Hindi + English")
        self.assertEqual(info["subtitle"], "ESub")
        self.assertEqual((info["year"], info["title"]), ("2019", "New"))


class SaveTests(unittest.TestCase):
    def setUp(self):
        self.col = FakeCol()
        self.old = C.corrections_col
        C.corrections_col = self.col
        C._OVERRIDES.clear()

    def tearDown(self):
        C.corrections_col = self.old
        C._OVERRIDES.clear()

    def test_save_name_then_lookup(self):
        ok = asyncio.run(C.save_correction("s1", "name", "Fixed Name", via="t"))
        self.assertTrue(ok)
        self.assertEqual(C.get_correction("s1"), {"smart_name": "Fixed Name"})

    def test_save_fields_then_lookup(self):
        asyncio.run(C.save_correction("s2", "fields", {"audio": "Hindi"}, via="t"))
        self.assertEqual(C.get_correction("s2"), {"fields": {"audio": "Hindi"}})

    def test_db_failure_not_applied(self):
        self.col.fail = True
        self.assertFalse(asyncio.run(C.save_correction("s3", "name", "X")))
        self.assertIsNone(C.get_correction("s3"))

    def test_bad_input(self):
        self.assertFalse(asyncio.run(C.save_correction("", "name", "X")))
        self.assertFalse(asyncio.run(C.save_correction("s", "other", "X")))


class RenderTests(unittest.TestCase):
    JOB = {
        "chat_id": -1001, "message_id": 5, "file_name": "x.mkv",
        "original_file_name": "Some.Movie.2019.480p.WEBRip.x264.mkv",
        "default_caption_plain": "Some Movie 2019 Hindi",
        "cap_template": "{smart_file_name}", "file_size": 1,
    }

    def setUp(self):
        C._OVERRIDES.clear()

    def tearDown(self):
        C._OVERRIDES.clear()

    def test_unchanged_without_correction(self):
        meta = {}
        cap, _ = Cap.render_caption_for_job(dict(self.JOB), meta_out=meta)
        self.assertTrue(meta["sig"])
        self.assertFalse(meta["corrected"])
        self.assertEqual(cap, meta["smart_file_name"])

    def test_name_override(self):
        probe = {}
        Cap.render_caption_for_job(dict(self.JOB), meta_out=probe)
        C._OVERRIDES[probe["sig"]] = {"smart_name": "Custom Name"}
        meta = {}
        cap, dump = Cap.render_caption_for_job(dict(self.JOB), meta_out=meta)
        self.assertEqual(cap, "Custom Name")
        self.assertEqual(dump, "Custom Name")
        self.assertTrue(meta["corrected"])

    def test_field_override(self):
        probe = {}
        Cap.render_caption_for_job(dict(self.JOB), meta_out=probe)
        C._OVERRIDES[probe["sig"]] = {"fields": {"audio": "Hindi + English", "year": "2020"}}
        cap, _ = Cap.render_caption_for_job(dict(self.JOB), meta_out={})
        self.assertIn("(2020)", cap)
        self.assertIn("(Hindi + English)", cap)
        self.assertIn("Dual Audio", cap)


if __name__ == "__main__":
    unittest.main()
