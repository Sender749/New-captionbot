import importlib
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

F = importlib.import_module("body.name_fixups")
Cap = importlib.import_module("body.Caption")


def smart(filename, caption=""):
    return Cap.build_smart_filename(filename, caption)


class StripLeadNoiseTests(unittest.TestCase):
    def test_join_before_handle(self):
        self.assertEqual(F.strip_lead_noise("Join_@Desiflix1_Love_Guru_2023"), "@Desiflix1_Love_Guru_2023")

    def test_domain_prefix_needs_dash(self):
        self.assertEqual(F.strip_lead_noise("CINEFREAK.TOP - Romeo and Juliet"), "Romeo and Juliet")
        self.assertEqual(F.strip_lead_noise("How.to.Train.Your.Dragon.2010"), "How.to.Train.Your.Dragon.2010")

    def test_configured_prefix_only_exact(self):
        self.assertEqual(F.strip_lead_noise("KC_Kotthu_2022", ["KC"]), "Kotthu_2022")
        self.assertEqual(F.strip_lead_noise("KCR_Movie", ["KC"]), "KCR_Movie")
        self.assertEqual(F.strip_lead_noise("KC_Kotthu_2022"), "KC_Kotthu_2022")


class DubTagTests(unittest.TestCase):
    def test_fan_dub(self):
        self.assertEqual(F.add_dub_tags("Hindi", ["Hindi"], "x 480p HINDI FAN DUB Bluray"), "Hindi {Fan Dub}")

    def test_second_language_untouched(self):
        raw = "Hindi Unofficial Dubbed + English ORG"
        self.assertEqual(F.add_dub_tags("Hindi + English", ["Hindi", "English"], raw), "Hindi {Unofficial Dubbed} + English")

    def test_codec_stays_after_tag(self):
        self.assertEqual(F.add_dub_tags("Hindi AAC + English", ["Hindi", "English"], "Hindi (Unofficial-Dubbed) + English"), "Hindi {Unofficial-Dubbed} AAC + English")

    def test_studio_dub_unchanged(self):
        self.assertEqual(F.add_dub_tags("Hindi + English", ["Hindi", "English"], "Hindi Studio-Dub-English"), "Hindi + English")

    def test_plain_dubbed_unchanged(self):
        self.assertEqual(F.add_dub_tags("Hindi", ["Hindi"], "Hindi Dubbed 720p"), "Hindi")


class PartTests(unittest.TestCase):
    def test_split_marker(self):
        self.assertEqual(F.part_label("Movie.1080p.part002.mkv"), "(Part 2)")

    def test_text_part_ignored(self):
        self.assertEqual(F.part_label("Show S03 Part 2 1080p.mkv"), "")
        self.assertEqual(F.part_label("Show_S03_Part.mkv"), "")


class SiteTitleTests(unittest.TestCase):
    def test_marker(self):
        self.assertTrue(F.site_marked("Official_AFSKMHD_2014_Www_SkymoviesHD_Team_480p"))
        self.assertFalse(F.site_marked("Team America World Police 2004 720p"))

    def test_prefers_caption_only_when_disjoint_same_year(self):
        f = {"title": {"value": "Official Afskmhd", "source": "filename"}, "year": {"value": "2014"}}
        c = {"title": {"value": "Ardennes Fury", "conf": "medium"}, "year": {"value": "2014"}}
        self.assertTrue(F.prefer_caption_title(f, c))
        c["year"]["value"] = "2015"
        self.assertFalse(F.prefer_caption_title(f, c))
        c["year"]["value"] = "2014"
        c["title"]["value"] = "Official Fury"
        self.assertFalse(F.prefer_caption_title(f, c))


class EngineCasesTests(unittest.TestCase):
    def test_kc_prefix_removed(self):
        out = smart("KC_Kotthu_2022_Malayalam_1080p_10bit_AMZN_WEB_DL_AAC_5_1_x265_HEVC.mkv")
        self.assertTrue(out.startswith("Kotthu (2022)"))

    def test_domain_prefix_removed(self):
        out = smart("CINEFREAK.TOP - Romeo and Juliet (1968) BluRay [English].mkv", "Romeo and Juliet (1968) 480p Bluray WEB-DL English Ripx264 ESub.MKV")
        self.assertTrue(out.startswith("Romeo And Juliet (1968)"))

    def test_join_handle_prefix_removed(self):
        out = smart("Join_@Desiflix1_Love_Guru_2023_UNRATED_1080p_HEVC_HDRip_S03_Part.mkv")
        self.assertTrue(out.startswith("Love Guru"))

    def test_junk_site_filename_uses_caption_title(self):
        out = smart("@Eliteflix_Official_AFSKMHD_2014_Www_SkymoviesHD_Team_480p_BluRay.mkv",
                    "Ardennes Fury (2014) 480p BluRay WEB-DL Dual Audio Hindi+English Ripx264 ESub.MKV")
        self.assertTrue(out.startswith("Ardennes Fury (2014)"))

    def test_fan_dub_label(self):
        out = smart("[@ATRO_FiLES] -Malena (2000) 480p HINDI FAN DUB Bluray")
        self.assertIn("(Hindi {Fan Dub})", out)

    def test_unofficial_dub_with_second_language(self):
        out = smart("18+_Malena_2000_Hindi_Unofficial_Dubbed_+_English_ORG_Dual_Audio.mkv")
        self.assertIn("(Hindi {Unofficial Dubbed} + English)", out)

    def test_part_suffix_before_extension(self):
        out = smart("Heartland.2026.1080p.WEB-DL.part002.mkv")
        self.assertTrue(out.endswith("(Part 2).mkv"))

    def test_normal_movie_unchanged(self):
        out = smart("Man.On.A.Ledge.2012.480p.HDRip.x264.Hindi.English.mkv")
        self.assertNotIn("{", out)
        self.assertNotIn("Part", out)
        self.assertTrue(out.startswith("Man On A Ledge (2012)"))


if __name__ == "__main__":
    unittest.main()
