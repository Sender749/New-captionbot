"""
Table tests for the smart-filename engine.

Run from the repo root:
    python -m unittest tests.test_smart_filename -v      (or: pytest tests -q)

Every row is (group, filename, caption, expected build_smart_filename()).
When you collect a real failure with /history_export + /wrong, add it here
as a new row, fix the engine, and run tools/baseline_diff.py to see what else
moved.
"""
import importlib
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

Cap = importlib.import_module("body.Caption")
ST = importlib.import_module("body.smart_text")

CASES = [
    ('spec', 'Nail Polish 2021 480p WEB-DL DS4K Hindi x264 AAC 2 0 ESub.mkv', '➥ Aᴜᴅɪᴏ Tʀᴀᴄᴋ:- 🔊 Hindi\n➥ sᴜʙᴛɪᴛʟᴇs:- 📝 English',
     'Nail Polish (2021) (Hindi AAC) x264 WEB-DL 480p ESub.mkv'),
    ('spec', '', 'ironman 2 2023 480p audio/language - Hindi Chinese subtitle - English',
     'Ironman 2 (2023) (Hindi + Chinese) Dual Audio 480p ESub'),
    ('label', '', 'Money Heist 2017 720p\nAudio: Hindi, English\nSubtitle: English',
     'Money Heist (2017) (Hindi + English) Dual Audio 720p ESub'),
    ('label', '', 'Money Heist 2017 720p\nAudio Track:- Hindi\nSubtitles:- English',
     'Money Heist (2017) (Hindi) 720p ESub'),
    ('label', '', 'Money Heist 2017 720p\nLanguage: Tamil + Telugu\nSubs: English',
     'Money Heist (2017) (Tamil + Telugu) Dual Audio 720p ESub'),
    ('label', '', 'Money Heist 2017 720p\nAudio/Language - Hindi Chinese\nSub - English',
     'Money Heist (2017) (Hindi + Chinese) Dual Audio 720p ESub'),
    ('label', '', 'Money Heist 2017 720p\nLanguages : Hindi / English\nSubtitle : None',
     'Money Heist (2017) (Hindi + English) Dual Audio 720p'),
    ('label', '', 'Money Heist 2017 720p\nDubbed: Hindi\nSubtitles: English, Hindi',
     'Money Heist (2017) (Hindi) 720p MSub'),
    ('fonts', '', 'Money Heist 2017 720p\n𝗔𝘂𝗱𝗶𝗼: Hindi\n𝗦𝘂𝗯𝘁𝗶𝘁𝗹𝗲: English',
     'Money Heist (2017) (Hindi) 720p ESub'),
    ('fonts', '', 'Money Heist 2017 720p\nＡｕｄｉｏ： Ｈｉｎｄｉ\nＳｕｂｔｉｔｌｅ： Ｅｎｇｌｉｓｈ',
     'Money Heist (2017) (Hindi) 720p ESub'),
    ('fonts', '', 'Money Heist 2017 720p\nAᴜᴅɪᴏ Tʀᴀᴄᴋ:- 🔊 Hindi, Thai\nsᴜʙᴛɪᴛʟᴇs:- 📝 English',
     'Money Heist (2017) (Hindi + Thai) Dual Audio 720p ESub'),
    ('order', '', 'Money Heist 2017 720p\nsᴜʙᴛɪᴛʟᴇs:- English\nAᴜᴅɪᴏ Tʀᴀᴄᴋ:- Hindi',
     'Money Heist (2017) (Hindi) 720p ESub'),
    ('both', '', 'Movie 2020 720p\nAudio: English\nSubtitle: English',
     'Movie (2020) (English) 720p ESub'),
    ('single', '', 'Movie 2020 720p Hindi Audio | English Sub',
     'Movie (2020) (Hindi) 720p ESub'),
    ('single', 'Movie.2020.720p.Hindi.mkv', 'Subtitles: English, Hindi',
     'Movie (2020) (Hindi) 720p MSub.mkv'),
    ('single', 'Some.Movie.2023.Hindi.Subtitle.English.720p.mkv', '',
     'Some Movie (2023) (Hindi) 720p ESub.mkv'),
    ('single', 'Movie.Name.2024.Hindi.Punjabi.English.Subtitle.WEB-DL.1080p.mkv', '',
     'Movie Name (2024) (Hindi + Punjabi) Dual Audio WEB-DL 1080p ESub.mkv'),
    ('single', 'Movie.Name.2024.Hindi.English+Punjabi.Subtitle.WEB-DL.1080p.mkv', '',
     'Movie Name (2024) (Hindi) WEB-DL 1080p MSub.mkv'),
    ('single', '', 'Movie 2024 1080p Hindi Dubbed English Subs',
     'Movie (2024) (Hindi) 1080p ESub'),
    ('file', 'Loki.S01E02.Hindi.WEB-DL.HEVC.480p.mkv', '',
     'Loki S01 E02 (Hindi) Web Series HEVC WEB-DL 480p.mkv'),
    ('file', 'Panchayat.S03.2024.Hindi.Netflix.WEB-DL.1080p.ESub.mkv', '',
     'Panchayat S03 (Complete Season) (2024) (Hindi) Web Series Netflix WEB-DL 1080p ESub.mkv'),
    ('file', 'Money.Heist.S01E01E02.Spanish.Dual.Audio.720p.Amazon.Prime.mkv', '',
     'Money Heist S01 Ep.01-02 (Combined Episodes) (Spanish) Dual Audio Web Series Amazon Prime 720p.mkv'),
    ('file', 'Random.Show.S15.E06-15.2022.Hindi.720p.mkv', '',
     'Random Show S15 Ep.06-15 (Combined Episodes) (2022) (Hindi) Web Series 720p.mkv'),
    ('file', 'My.Hero.Academia.S06E07.2023.Japanese.English.Anime.HEVC.1080p.ESub.mkv', '',
     'My Hero Academia S06 E07 (2023) (Japanese + English) Dual Audio Anime HEVC 1080p ESub.mkv'),
    ('file', 'Court.State.Vs.A.Nobody.2025.Hindi.DD5.1-224Kbps+Telugu.Dual.Audio.UnCut.South.Movie.HD.1080p.ESub.mkv', '',
     'Court State Vs A Nobody (2025) (Hindi DD5.1-224Kbps + Telugu) Dual Audio UnCut South Movie HD 1080p ESub.mkv'),
    ('file', 'Devara.Part.1.2024.Telugu.Hindi.Kannada.Malayalam.Tamil.UnCut.South.Remastered.HDR.HEVC.2160p.4K.mkv', '',
     'Devara Part 1 (2024) (Telugu + Hindi + Kannada + Malayalam + Tamil) Multi Audio UnCut Remastered South Movie HDR HEVC 2160p.mkv'),
    ('file', 'The.Boys.Season.4.Episode.5.2024.English.1080p.WEB-DL.mkv', '',
     'The Boys S04 E05 (2024) (English) Web Series WEB-DL 1080p.mkv'),
    ('file', 'Movie.Name.2024.Extended.Cut.Hindi.BluRay.1080p.mkv', '',
     'Movie Name (2024) (Hindi) Extended Cut BluRay 1080p.mkv'),
    ('idem', 'Man On A Ledge (2012) (Hindi + English) Dual Audio x264 HDRip 480p ESub.mkv', '',
     'Man On A Ledge (2012) (Hindi + English) Dual Audio x264 HDRip 480p ESub.mkv'),
    ('idem', 'Nail Polish (2021) (Hindi AAC) x264 WEB-DL 480p ESub.mkv', '',
     'Nail Polish (2021) (Hindi AAC) x264 WEB-DL 480p ESub.mkv'),
    ('idem', 'FILTER 2025 S01 E10 720p AMZN WEB-DL x264 [ Hindi Chinese ] ESub.mkv', 'Aᴜᴅɪᴏ Tʀᴀᴄᴋ:-  Hindi  sᴜʙᴛɪᴛʟᴇs:- English',
     'Filter S01 E10 (2025) (Hindi) Web Series Amazon Prime x264 WEB-DL 720p ESub.mkv'),
    ('title', 'Hindi Medium 2017 720p WEB-DL.mkv', '',
     'Hindi Medium (2017) WEB-DL 720p.mkv'),
    ('title', 'English Vinglish 2012 720p BluRay.mkv', '',
     'English Vinglish (2012) BluRay 720p.mkv'),
    ('title', 'Sub Zero 2017 720p WEB-DL.mkv', '',
     'Sub Zero (2017) WEB-DL 720p.mkv'),
    ('title', 'Dual Survival 2014 720p HDTV.mkv', '',
     'Dual Survival (2014) HDTV 720p.mkv'),
    ('title', 'Korean Drama 2021 720p WEB-DL.mkv', '',
     'Korean Drama (2021) WEB-DL 720p.mkv'),
    ('title', 'Tamil Rockers 2019 720p WEB-DL.mkv', '',
     'Tamil Rockers (2019) WEB-DL 720p.mkv'),
    ('title', 'Cam Girl 2018 720p WEB-DL.mkv', '',
     'Cam Girl (2018) WEB-DL 720p.mkv'),
    ('title', 'The Hindi Teacher 2020 720p WEB-DL.mkv', '',
     'The Hindi Teacher (2020) WEB-DL 720p.mkv'),
    ('title', 'Multi Bhai 2020 720p WEB-DL.mkv', '',
     'Multi Bhai (2020) WEB-DL 720p.mkv'),
    ('title', 'TS Madras 2020 720p WEB-DL.mkv', '',
     'TS Madras (2020) WEB-DL 720p.mkv'),
    ('title', 'Gone.in.60.Seconds.2000.720p.mkv', '',
     'Gone In 60 Seconds (2000) 720p.mkv'),
    ('baseline', '', 'Chup: Revenge Of The Artist (2022) Hindi AAC x264 HDRip 480p ESub.mp4',
     'Chup: Revenge Of The Artist (2022) (Hindi AAC) x264 HDRip 480p ESub.mp4'),
    ('baseline', '', 'Green Lantern: The Animated Series S01 (Complete Season) (Hindi + English) Dual Audio Web Series HEVC WEB-DL 720p ESub.mkv',
     'Green Lantern: The Animated Series S01 (Complete Season) (Hindi + English) Dual Audio Web Series HEVC WEB-DL 720p ESub.mkv'),
    ('baseline', '', 'Hindi | English | ESubs',
     '(Hindi + English) Dual Audio ESub'),
    ('baseline', '', 'Kaathal The Core (2023) Org [Hindi + Malyalam] Esub Mkv',
     'Kaathal The Core (2023) (Hindi) ESub.mkv'),
    ('baseline', '', 'Lokah Chapter-1 2025 720p HEVC HDTC Multi 2 0 HC-ESub x2 mkv',
     'Lokah Chapter-1 (2025) Multi Audio HEVC HDTC 720p ESub.mkv'),
    ('baseline', '', 'Lal Salaam (Extended Version) (2024) [Tamil] 720p HQ HDRip x264 ESub.mkv',
     'Lal Salaam (2024) (Tamil) Extended Version x264 HDRip 720p ESub.mkv'),
    ('baseline', '', 'Unicorn Academy Secrets Revealed Ep.03-00 (Combined Episodes) (Hindi DDP5.1 + English) Dual Audio Web Series Netflix H.264 WEB-DL 1080p.mkv',
     'Unicorn Academy Secrets Revealed E03 (Hindi DDP5.1 + English) Dual Audio Web Series Netflix H.264 WEB-DL 1080p.mkv'),
    ('baseline', '', 'Detektiv Conan Zeros Tea Time - S01E05 - Episode 5.mp4',
     'Detektiv Conan Zeros Tea Time S01 E05 Web Series.mp4'),
    ('baseline', '', 'Marham_2026_S01_COMBiNED_1080p_HEVC_AMZN_WEB_DL_x2.mkv',
     'Marham S01 (Complete Season) (2026) Web Series Amazon Prime HEVC WEB-DL 1080p.mkv'),
    ('baseline', '', 'Love Again 2019 WEBRip Hindi + Tam + Tel + Kor 720p x264 mkv',
     'Love Again (2019) (Hindi + Tamil + Telugu + Korean) Multi Audio x264 WEBRip 720p.mkv'),
    ('baseline', '', '<b>Spring.Fever.S01.E01-E06.720p.WEB-DL.Hindi-English.ESub.x264-HDHub4u.mkv</b>',
     'Spring Fever S01 Ep.01-06 (Combined Episodes) (Hindi + English) Dual Audio Web Series x264 WEB-DL 720p ESub.mkv'),
    ('baseline', '', '<a href="https://t.me/sspense"><b>💎</b></a><b>ㅤ<i>Kya Masti Kya Dhoom 2024 480p HDRip English AAC x264 .mkv</i></b>',
     'Kya Masti Kya Dhoom (2024) (English AAC) x264 HDRip 480p.mkv'),
    ('baseline', '', 'American Dad! - S16E06 - Lost Boys - [HDTV-1080p]h26.mkv',
     'American Dad! S16 E06 Web Series HDTV 1080p.mkv'),
    ('baseline', '', 'Laughter Chefs Unlimited S03E63 1080p WEB DL AAC2 0 H 264',
     'Laughter Chefs Unlimited S03 E63 Web Series WEB-DL 1080p'),
    ('baseline', '', 'Lupin.S01E05.Chapter.5.1080p.NF.WEBRip.x265.10bit.[Hindi DDP 5.1 + French DDP 5.1]-ESubs~(ARROW) DarQ.mkv',
     'Lupin S01 E05 (Hindi DDP + French) Dual Audio Web Series Netflix x265 WEBRip 1080p ESub.mkv'),
    ('baseline', '', 'Mission Impossible III (2006) 480p (Hindi + English) ESub',
     'Mission Impossible III (2006) (Hindi + English) Dual Audio 480p ESub'),
    ('baseline', '', "Disney's_Hercules_S01E06_Hercules_und_der_Fluss_der_Unterwelt.mp4",
     "Disney's Hercules S01 E06 Web Series.mp4"),
    ('baseline', '', 'Survival Island-Hindi-480p',
     'Survival Island (Hindi) 480p'),
    ('baseline', '', '2014 Malayalam Dubb HDRip x264 mkv',
     '(2014) (Malayalam) x264 HDRip.mkv'),
    ('baseline', '', 'Johnny English Reborn 2011 720P Hevc Bluray X264 Mkv',
     'Johnny English Reborn (2011) HEVC BluRay 720p.mkv'),
    ('baseline', '', 'Hindi Medium 2017 720P Brrip X264 Mkv',
     'Hindi Medium (2017) x264 BRRip 720p.mkv'),
    ('baseline', '', 'Raavan (Hindi OPUS + English) Dual Audio Movie YouTube H264 WEB-DL 720p.mkv',
     'Raavan (Hindi OPUS + English) Dual Audio Movie YouTube H264 WEB-DL 720p.mkv'),
    ('baseline', '', '(2025) (Hindi Opus + English) Dual Audio Netflix HEVC BluRay 1080p ESub.mkv',
     '(2025) (Hindi OPUS + English) Dual Audio Netflix HEVC BluRay 1080p ESub.mkv'),
    ('baseline', '', 'Reacher {2022} S01 Complete #Hindi & #English 480P Hevc Hdri Mkv',
     'Reacher S01 (Complete Season) (2022) (Hindi + English) Dual Audio Web Series HEVC 480p.mkv'),
    ('baseline', '', 'Peaky Blinders S02E01 1080p 10bit BluRay HEVC x265 [Hindi TV-DL DDP 2 0 224kbps + English DDP 2 0] ESub ~ DarQ mkv',
     'Peaky Blinders S02 E01 (Hindi DDP + English) Dual Audio Web Series HEVC BluRay 1080p ESub.mkv'),
    ('baseline', '', 'A Knight Of The Seven Kingdoms S01 E06 Mkv ➢ Aᴜᴅɪᴏ Tʀᴀᴄᴋ:- 🔊 ➪ Quality :- 🟰 (English + Hindi) Web Series WEB-DL 1080p.mkv',
     'A Knight Of The Seven Kingdoms S01 E06 (English + Hindi) Dual Audio Web Series WEB-DL 1080p.mkv'),
    ('round2', "The Devil's Plan S01 [E07-E12] COMBINED 720p 10bit N.mkv", "The Devil's Plan S01 [E07-E12] COMBINED 720p 10bit NF WEB-DL HEVC x265 MULTi [Hindi + Korean + English (AAC 5.1 80Kbps)] MSubs ~ NΛKϟU ~ MovieMod.mkv",
     "The Devil's Plan S01 Ep.07-12 (Combined Episodes) (Hindi AAC + Korean + English) Multi Audio Web Series Netflix HEVC WEB-DL 720p MSub.mkv"),
    ('round2', 'Udaala (2025) 1080p 10bit AMZN WEBRip x265 HEVC [Hindi D.mkv', 'Udaala (2025) 1080p 10bit AMZN WEBRip x265 HEVC [Hindi DDP 2.0 + Kannada DDP 2.0] ESub - Anup.mkv',
     'Udaala (2025) (Hindi DDP + Kannada) Dual Audio Amazon Prime HEVC WEBRip 1080p ESub.mkv'),
    ('round2', 'The_White_Olive_Tree_S01E37_Light_After_The_Ruins_720p_10bit_Hindi.mkv', 'The.White.Olive.Tree.S01E37.Light.After.The.Ruins.720p.10bit.Hindi.Chinese.Esub. farm [ ].mkv',
     'The White Olive Tree S01 E37 (Hindi + Chinese) Dual Audio Web Series 720p ESub.mkv'),
    ('round2', 'The_Punisher_S02E09_480p_DS4K_WEB_DL_Hindi_Studio_Dub_English_ESub.mkv', 'The.Punisher.S02E09.480p.DS4K.WEB-DL.Hindi.Studio-Dub-English.ESub.x264-HDHub4u.Ag.mkv',
     'The Punisher S02 E09 (Hindi + English) Dual Audio Web Series x264 WEB-DL 480p ESub.mkv'),
    ('round2', 'Hercai S01E35.1080p AMZN WEB DL Hindi DDP2.0 Turkish DDP.mkv', 'Hercai.S01E35.1080p.AMZN.WEB-DL.Hindi.DDP2.0-Turkish.DDP2.0.H.265-EonMovies.Com.mkv\n🔊 - Hindi, Turkish\n💬 - English',
     'Hercai S01 E35 (Hindi DDP2.0 + Turkish) Dual Audio Web Series Amazon Prime WEB-DL 1080p ESub.mkv'),
    ('round2', 'A Korean Odyssey (2017) S01 COMBINED 720p WEB-DL [HINDI + KO.mkv', 'A Korean Odyssey (2017) S01 [E06 - E10] COMBINED 720p WEB-DL [HINDI + KOREAN] HEVC x265 ~ AKSHAT - KatDrama.mkv',
     'A Korean Odyssey S01 Ep.06-10 (Combined Episodes) (2017) (Hindi + Korean) Dual Audio Web Series HEVC WEB-DL 720p.mkv'),
    ('round2', 'Tron_Uprising_S01_COMBiNED_1080p_JHS_WEB_DL_x265_HEVC_MULTi_Hindi.mkv', '📁Tron Uprising S01 COMBiNED 1080p JHS  DL x265 HEVC MULTi Hindi mkv\n\n🔈 #Hindi #English\n\n❗ Use VLC Player to avoid sound issues & switch languages',
     'Tron Uprising S01 (Complete Season) (Hindi + English) Dual Audio Web Series HEVC 1080p.mkv'),
    ('round2', 'Call Me Bae (2024) S01 COMBINED 720p WebRip Hindi x264.mkv', '[Call Me Bae 2024 S01 COMBINED 720p WebRip Hindi x264.mkv\n\nAudio(s) : Hindi\nSubtitle(s) : Arabic, English, Hindi, Indonesian, Japanese, Kannada, Korean, Malayalam, Tamil, Telugu, Thai, Turkish, Chinese](https://t.me/sspense)',
     'Call Me Bae S01 (Complete Season) (2024) (Hindi) Web Series x264 WEBRip 720p MSub.mkv'),
    ('round2', '[TurabSSH] BeyWarriors Cyborg - S01E08 - [21.04.23] 480p.mkv', '',
     'Beywarriors Cyborg S01 E08 Web Series 480p.mkv'),
    ('round2', 'One Piece - 53 720p', '',
     'One Piece E53 Web Series 720p'),
    ('round2', '[SubsPlease] Anime Name - 12 (1080p) [ABCD].mkv', '',
     'Anime Name E12 Anime 1080p.mkv'),
    ('round2', 'Die Goldbergs - S03E18 - 12 Kassetten für einen Penny.mp4', '',
     'Die Goldbergs S03 E18 Web Series.mp4'),
    ('round2', 'Mafia Game for Life 2025 720p WeB-DL Bengali 5 1x264-lol is mkv', '',
     'Mafia Game For Life (2025) (Bengali) WEB-DL 720p.mkv'),
    ('round2', 'Arcane S01 (Complete Season) English Web Series HEVC WEBRip 1080p MSub.mkv', '',
     'Arcane S01 (Complete Season) (English) Web Series HEVC WEBRip 1080p MSub.mkv'),
    ('round2', 'Hack Crimes Online S01E03 The Spoof 720P Amzn Web Dl Hindi In Aac2 Mkv', '',
     'Hack Crimes Online S01 E03 (Hindi) Web Series Amazon Prime WEB-DL 720p.mkv'),
    ('round2', 'Karkash 2005 Full Hindi Movie ｜ Suchitra Pillai, Anup Soni Mp4', '',
     'Karkash (2005) (Hindi) Movie.mp4'),
    ('round2', 'Whistle.(2025-2026).1080p.BLU-RAY.HIN-ENG.x264.ESub-VegaMovies.is.mkv', '',
     'Whistle (2025) (Hindi + English) Dual Audio x264 Blu-Ray 1080p ESub.mkv'),
    ('round2', 'Emily in Paris 2022 S03 {E01 10} COMBINED 720p NF WEBRip x265 HEVC Mkv', '',
     'Emily In Paris S03 Ep.01-10 (Combined Episodes) (2022) Web Series Netflix HEVC WEBRip 720p.mkv'),
    ('round2', 'The Traitors S01 [Ep06 10] Combined 720P Amzn Web Dl [Hindi Mkv', '',
     'The Traitors S01 Ep.06-10 (Combined Episodes) (Hindi) Web Series Amazon Prime WEB-DL 720p.mkv'),
    ('round2', 'Friends.S02E10.The.One.with.the.French.Lesson.720p.mkv', '',
     'Friends S02 E10 Web Series 720p.mkv'),
    ('round2', 'The.Boys.S04E05.Beware.the.Hindi.Ocean.1080p.mkv', '',
     'The Boys S04 E05 Web Series 1080p.mkv'),
    ('round2', 'Show.Name.2x05.720p.WEB-DL.mkv', '',
     'Show Name S02 E05 Web Series WEB-DL 720p.mkv'),
    ('round2', 'Show.Name.S01E05.S01E06.720p.mkv', '',
     'Show Name S01 Ep.05-06 (Combined Episodes) Web Series 720p.mkv'),
    ('round2', 'Show.Name.S01-S03.Complete.720p.mkv', '',
     'Show Name S01-S03 (Complete Season) Web Series 720p.mkv'),
    ('round2', 'Show Name Season 1-3 720p.mkv', '',
     'Show Name S01-S03 (Complete Season) Web Series 720p.mkv'),
    ('round2', 'Show Name Seasons 1 to 4 720p.mkv', '',
     'Show Name S01-S04 (Complete Season) Web Series 720p.mkv'),
    ('round2', 'Show Name S02 EP-12 720p.mkv', '',
     'Show Name S02 E12 Web Series 720p.mkv'),
    ('round2', 'Show.Name.S01E01-03.720p.mkv', '',
     'Show Name S01 Ep.01-03 (Combined Episodes) Web Series 720p.mkv'),
    ('round2', 'Show Name Ep.1 to 8 720p.mkv', '',
     'Show Name Ep.01-08 (Combined Episodes) Web Series 720p.mkv'),
    ('round2', 'Show Name E1234 720p.mkv', '',
     'Show Name E1234 Web Series 720p.mkv'),
    ('round2', 'WALL-E.2008.720p.mkv', '',
     'Wall-E (2008) 720p.mkv'),
    ('round2', 'The.Visit.2015.720p.BluRay.mkv', '',
     'The Visit (2015) BluRay 720p.mkv'),
    ('round2', 'Join.The.Club.2020.720p.mkv', '',
     'Join The Club (2020) 720p.mkv'),
    ('round2', 'Blade.Runner.2049.2017.1080p.BluRay.mkv', '',
     'Blade Runner 2049 (2017) BluRay 1080p.mkv'),
    ('round2', '@Mc_Hereditary_2018_English_HDRip.mkv', '',
     'Hereditary (2018) (English) HDRip.mkv'),
    ('round2', '@MovieChannel_Dune_Part_Two_2024_720p_HDRip.mkv', '',
     'Dune Part Two (2024) HDRip 720p.mkv'),
    ('round2', 'Movie 1920x1080 2020 720p.mkv', '',
     'Movie (2020) 720p.mkv'),
    ('junk', 'video_2024-01-05_12-30-00.mp4', 'Pushpa 2 The Rule (2024) Hindi 720p',
     'Pushpa 2 The Rule (2024) (Hindi) 720p.mp4'),
    ('junk', 'a.mkv', 'Pushpa 2 The Rule (2024) Hindi 720p',
     'Pushpa 2 The Rule (2024) (Hindi) 720p.mkv'),
    ('junk', 'video_2024-01-05_12-30-00.mp4', '',
     'Video 2024-01-05 12-30-00.mp4'),
    ('free', '', 'Pushpa 2 The Rule Hindi',
     'Pushpa 2 The Rule (Hindi)'),
]


class TableTests(unittest.TestCase):
    def test_table(self):
        for group, fn, cap, expected in CASES:
            with self.subTest(group=group, filename=fn, caption=cap):
                self.assertEqual(Cap.build_smart_filename(fn, cap), expected)

    def test_idempotent(self):
        """Feeding the engine its own output must not change it."""
        for group, fn, cap, expected in CASES:
            if group in ("idem",):
                continue
            with self.subTest(expected=expected):
                again = Cap.build_smart_filename(expected, "")
                self.assertEqual(again, expected)


class NormalizeTests(unittest.TestCase):
    def test_small_caps(self):
        self.assertEqual(ST.normalize_text("Aᴜᴅɪᴏ Tʀᴀᴄᴋ:- 🔊 Hindi"), "Audio Track: Hindi")
        self.assertEqual(ST.normalize_text("sᴜʙᴛɪᴛʟᴇs:- 📝 English"), "subtitles: English")

    def test_math_fonts_and_fullwidth(self):
        self.assertEqual(ST.normalize_text("𝗦𝘂𝗯𝘁𝗶𝘁𝗹𝗲: English"), "Subtitle: English")
        self.assertEqual(ST.normalize_text("Ｈｉｎｄｉ"), "Hindi")

    def test_separators_and_lines_kept(self):
        self.assertEqual(ST.normalize_text("Audio = Hindi\n➥ Subs » English"), "Audio : Hindi\nSubs : English")
        self.assertEqual(ST.normalize_text("a\r\n\r\nb"), "a\nb")

    def test_html_and_escapes(self):
        self.assertEqual(ST.normalize_text("<b><i>Audio: Hindi</i></b>"), "Audio: Hindi")


class FieldTests(unittest.TestCase):
    def test_confidence_and_source(self):
        a = ST.analyze("Nail Polish 2021 480p WEB-DL Hindi x264 ESub.mkv",
                       "Audio Track:- Hindi\nSubtitles:- English")
        self.assertEqual(a["audio"]["langs"], ["Hindi"])
        self.assertEqual((a["audio"]["conf"], a["audio"]["source"]), ("high", "label"))
        self.assertEqual(a["subtitle"]["langs"], ["English"])
        self.assertEqual(a["title"]["value"], "Nail Polish")
        self.assertEqual(a["title"]["source"], "filename")
        self.assertEqual(a["flags"], [])

    def test_conflict_is_flagged_but_label_wins(self):
        a = ST.analyze("Movie 2020 720p Hindi English.mkv", "Audio: Tamil")
        self.assertEqual(a["audio"]["langs"], ["Tamil"])
        self.assertIn("conflict", a["flags"])

    def test_trailing_language_in_free_text_counts(self):
        # the owner's rule: title, then year/season/episode, then language, then quality
        a = ST.analyze("", "Pushpa 2 The Rule Hindi")
        self.assertEqual(a["audio"]["langs"], ["Hindi"])
        self.assertEqual(a["title"]["value"], "Pushpa 2 The Rule")

    def test_title_words_that_are_languages_stay_in_the_title(self):
        a = ST.analyze("Hindi Medium 2017 720p.mkv", "")
        self.assertEqual(a["title"]["value"], "Hindi Medium")
        self.assertEqual(a["audio"]["langs"], [])

    def test_truncated_filename_is_repaired_from_the_caption(self):
        fn = "Udaala (2025) 1080p WEB-DL x264 [Hindi DDP 2.0 + Kannada DDP 2.mkv"
        cap = "Udaala (2025) 1080p WEB-DL x264 [Hindi DDP 2.0 + Kannada DDP 2.0] ESub.mkv"
        a = ST.analyze(fn, cap)
        self.assertEqual(a["audio"]["langs"], ["Hindi", "Kannada"])
        self.assertEqual(a["subtitle"]["tag"], "ESub")
        self.assertNotIn("conflict", a["flags"])

    def test_junk_filename_uses_caption_only_if_there_is_one(self):
        self.assertTrue(ST.looks_junk_filename("video_2024-01-05_12-30-00.mp4"))
        self.assertTrue(ST.looks_junk_filename("a.mkv"))
        self.assertFalse(ST.looks_junk_filename("It.2017.720p.mkv"))
        self.assertFalse(ST.looks_junk_filename("2012.2009.720p.mkv"))

    def test_same_language_audio_and_subtitle(self):
        a = ST.analyze("", "Movie 2020 720p\nAudio: English\nSubtitle: English")
        self.assertEqual(a["audio"]["langs"], ["English"])
        self.assertEqual(a["subtitle"]["langs"], ["English"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
