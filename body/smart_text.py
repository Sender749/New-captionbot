"""
smart_text.py -- pure text analysis for the {smart_file_name} engine.

No Telegram / MongoDB / network imports, so it is fast, unit-testable on its
own, and safe to call from the caption workers.

Pipeline (see analyze()):
  A. normalize_text(): fonts (small-caps, math-bold, fullwidth ...) -> plain
     letters, emoji/decorative symbols stripped, separators unified, line
     breaks kept.
  B. Labeled lines ("Audio Track: Hindi", "Subtitles: English", ...) are read
     first.  They are HIGH confidence.
  C. The filename (and any un-labeled caption text) is read next: only the
     "tag zone" (after the year / season / resolution / quality anchor) and
     explicit role words ("Hindi Audio", "English Sub", "[Hindi + English]")
     are used.  They are MEDIUM confidence.
  D. Languages found only in free text are LOW confidence and are OMITTED
     (never guessed) -- they only raise the `omitted_language` flag.

Every field comes back as value + confidence + source.
"""
import difflib
import html as _html
import re
import unicodedata

ENGINE_VERSION = "2.1"

HIGH, MEDIUM, LOW = "high", "medium", "low"

# ───────────────────────────── A. normalisation ─────────────────────────────

_SC_SRC = "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡʏᴢ"          # small-cap letters (no 'x' exists)
_SC_DST = "abcdefghijklmnopqrstuvwyz"
_TRANS = {ord(s): d for s, d in zip(_SC_SRC, _SC_DST)}
_TRANS.update({ord(c): "-" for c in "–—―−‐‑‒"})
_TRANS.update({ord(c): ":" for c in "→⇒➜➔➡»≫="})
_TRANS.update({ord(c): "|" for c in "│┃║┆┇┊┋•·●"})
_TRANS[ord("~")] = " "
_TRANS.update({ord(c): "|" for c in "‣▪▫◦‧∙"})
# invisible "blank" letters used as spacers in channel captions
_TRANS.update({ord(c): " " for c in "\u3164\u115f\u1160\uffa0\u2800\u200b\u200c\u200d\u2060\ufeff"})

_NATIVE = {
    "हिन्दी": "Hindi", "हिंदी": "Hindi", "தமிழ்": "Tamil", "తెలుగు": "Telugu",
    "മലയാളം": "Malayalam", "ಕನ್ನಡ": "Kannada", "বাংলা": "Bengali", "ਪੰਜਾਬੀ": "Punjabi",
    "मराठी": "Marathi", "ગુજરાતી": "Gujarati", "اردو": "Urdu", "ภาษาไทย": "Thai",
    "한국어": "Korean", "日本語": "Japanese", "中文": "Chinese",
}
_ICON_AUDIO = re.compile(r"(?m)^[ \t]*(?:[➥➢➤➪▶►•|]\s*)*(?:🔊|🔈|🔉|🎧)\s*[:\-]*\s*")
_ICON_SUB = re.compile(r"(?m)^[ \t]*(?:[➥➢➤➪▶►•|]\s*)*(?:💬|🗨️?|🗨)\s*[:\-]*\s*")
_PLURAL_S = re.compile(r"(?i)\b(audio|subtitle|language|lang)\(s\)")
_PROMO_ANCHOR = re.compile(
    r"(?is)<a\b[^>]*>(?:\s|<[^>]+>)*[^<]{0,12}?\b(?:join|subscribe|follow|visit|click)\b[^<]{0,25}?(?:<[^>]+>|\s)*</a>")
_HTML_BR = re.compile(r"<\s*br\s*/?\s*>", re.I)
_HTML_TAG = re.compile(
    r"</?(?:b|i|u|s|em|strong|code|pre|a|span|blockquote|tg-spoiler)\b[^>]*>", re.I
)
_ESC_U = re.compile(r"\\u[0-9a-fA-F]{4}")
_COLON_DASH = re.compile(r":\s*-+\s*")
_HASHTAG = re.compile(r"#(?=[^\W\d_])")
_MULTI_COLON = re.compile(r":{2,}")
_HSPACE = re.compile(r"[ \t]+")
_LEAD_JUNK = re.compile(r"^(?:[|:\-]\s*)+")


def _strip_symbols(s):
    out = []
    for ch in s:
        if ch < "\x80":
            out.append(ch)
            continue
        cat = unicodedata.category(ch)
        if cat in ("Mn", "Me", "Cf"):
            continue
        if cat in ("So", "Sk"):          # emoji, ➥ ➢ 🔊 📝 📂 ... decorative symbols
            out.append(" ")
            continue
        out.append(ch)
    return "".join(out)


def normalize_text(s):
    """Plain, lower-noise text with line breaks preserved (pure function)."""
    if not s:
        return ""
    if "&" in s and ";" in s:
        s = _html.unescape(s)
    if not s.isascii():
        for k, v in _NATIVE.items():
            if k in s:
                s = s.replace(k, " %s " % v)
        s = _ICON_AUDIO.sub("Audio: ", s)
        s = _ICON_SUB.sub("Subtitle: ", s)
        s = s.replace("™", "").replace("®", "").replace("©", "")
        # NFKC per character, but leave "other numbers" (1/2, 2) alone
        s = "".join(
            ch if (ch < "\x80" or unicodedata.category(ch) == "No") else unicodedata.normalize("NFKC", ch)
            for ch in s
        )
    s = s.translate(_TRANS)
    if not s.isascii():
        s = _strip_symbols(s)
    if "<" in s:
        s = _PROMO_ANCHOR.sub(" ", s)
        s = _HTML_BR.sub("\n", s)
        s = _HTML_TAG.sub("", s)
    if "\\u" in s:
        s = _ESC_U.sub("", s)
    s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\u2028", "\n")
    if "(s)" in s.lower():
        s = _PLURAL_S.sub(r"\1s", s)               # "Audio(s) : Hindi"
    if "#" in s:
        s = _HASHTAG.sub("", s)                 # "#Hindi #English" -> "Hindi English"
    s = _COLON_DASH.sub(": ", s)
    s = _MULTI_COLON.sub(":", s)
    lines = []
    for ln in s.split("\n"):
        ln = _HSPACE.sub(" ", ln).strip()
        ln = _LEAD_JUNK.sub("", ln).strip()
        if ln:
            lines.append(ln)
    return "\n".join(lines)


# ───────────────────────────── language tables ──────────────────────────────

LANGUAGES = [
    "Hindi", "English", "Tamil", "Telugu", "Malayalam", "Kannada", "Marathi",
    "Gujarati", "Bengali", "Punjabi", "Bhojpuri", "Rajasthani", "Haryanvi",
    "Odia", "Assamese", "Maithili", "Santali", "Kashmiri", "Sindhi", "Konkani",
    "Manipuri", "Dogri", "Tulu", "Urdu", "Japanese", "Korean", "Mandarin",
    "Chinese", "Cantonese", "Spanish", "French", "German", "Italian", "Russian",
    "Arabic", "Dutch", "Portuguese", "Turkish", "Thai", "Vietnamese",
    "Indonesian", "Malay", "Tagalog", "Filipino", "Sinhala", "Nepali", "Burmese",
    "Persian", "Hebrew", "Ukrainian",
]   # NB: "Polish", "Greek", "Danish" ... are deliberately NOT here -- they are
    #     ordinary title words ("Nail Polish", "My Big Fat Greek Wedding").
_LANG = {n.lower(): n for n in LANGUAGES}

# aliases that are safe even inside a filename's tag zone
_ALIAS_SAFE = {
    "eng": "English", "hin": "Hindi", "tam": "Tamil", "jpn": "Japanese",
    "jap": "Japanese", "kor": "Korean", "chi": "Chinese", "chn": "Chinese",
    "rus": "Russian", "ara": "Arabic", "tha": "Thai", "spa": "Spanish",
    "fre": "French", "fra": "French", "ger": "German", "deu": "German",
}
# aliases only trusted right next to a label / role word / inside a bracket
_ALIAS_LABEL = {
    "tel": "Telugu", "mal": "Malayalam", "kan": "Kannada", "ben": "Bengali",
    "mar": "Marathi", "guj": "Gujarati", "pun": "Punjabi", "urd": "Urdu",
    "ita": "Italian", "por": "Portuguese", "tur": "Turkish", "vie": "Vietnamese",
    "hindu": "Hindi",
}


def _lang_of(tok, level):
    """level 0: full names | 1: + safe aliases | 2: + label-only aliases."""
    v = _LANG.get(tok)
    if v:
        return v
    if level >= 1:
        return _ALIAS_SAFE.get(tok) or _ALIAS_LABEL.get(tok)
    return None


# ───────────────────────────── token classes ────────────────────────────────

_RES_NUMBERS = {"144", "240", "360", "480", "540", "576", "720", "900", "1080", "1440", "2160"}
_YEAR_RE = re.compile(r"^(?:19|20)\d\d$")
_RES_RE = re.compile(r"^(?:\d{3,4}p|[248]k|uhd|\d{3,4}x\d{3,4})$")
_SE_RE = re.compile(
    r"^(?:s\d{1,3}(?:e\d{1,3})*(?:-?e?\d{1,3})?|e\d{1,4}(?:-e?\d{1,3})?|ep\.?\d{1,4}(?:-\d{1,3})?"
    r"|\d{1,2}x\d{2,3})$"
)
_EXTS = {"mkv", "mp4", "avi", "webm", "mov", "m4v", "wmv", "flv"}
# unambiguous quality / codec / platform tokens that start the "tag zone"
_STRONG = {
    "webrip", "web-dl", "webdl", "bluray", "blu-ray", "brrip", "bdrip", "hdrip",
    "dvdrip", "dvdscr", "hdtv", "hdcam", "hdts", "hdtc", "predvd", "pre-dvd",
    "tsrip", "camrip", "hdcamrip", "hdscr", "web-rip", "web-dlrip", "dlrip",
    "pdvd", "remux", "bdremux", "x264", "x265", "h264", "h265", "h.264", "h.265",
    "h\x01264", "h\x01265", "x\x01264", "x\x01265",
    "hevc", "avc", "av1", "xvid", "divx", "amzn", "dsnp", "hmax", "atvp", "pcok",
    "sonyliv", "zee5", "jiocinema", "voot", "altbalaji", "mxplayer", "erosnow",
}
_AUDIO_KW = {"audio", "audios", "dub", "dubb", "dubbed", "dubbing"}
_SUB_KW = {"sub", "subs", "subtitle", "subtitles", "subbed"}
_SUB_LIT = {"esub": "English", "esubs": "English", "hsub": "Hindi", "hsubs": "Hindi"}
_MSUB_LIT = {"msub", "msubs"}
_SOFT_WORDS = {
    "dual", "multi", "org", "original", "uncut", "remastered", "extended",
    "hdr", "hd", "fhd", "south", "bollywood", "hollywood",
    "hdr10", "dolby", "vision", "complete", "completed",
    "netflix", "hotstar", "youtube", "amazon", "disney+", "disneyplus", "crunchyroll",
    "itunes", "peacock", "sonyliv", "zee5", "voot", "jiocinema", "mxplayer",
    "erosnow", "altbalaji",
}
_SERIES_PFX = {"web", "tv", "mini", "ott", "limited"}
_TAG_FILLER = {"movie", "movies", "film", "full", "version", "cut", "edition"}   # "Hindi Dubbed Movie 2015"
_SEP_TOKS = {",", "+", "&", "/", "|", ":", "-"}
_EXPLICIT_SEP = {",", "+", "&", "/"}
_FILLER_VALUE = {
    "or", "org", "original", "dubbed", "dub", "hq", "clean", "cleaned", "audio",
    "track", "tracks", "only", "in", "available", "lang", "language",
    "languages", "dual", "multi", "line", "and", "also", "of",
}
_RUN_FILLER = {":", "-", "in", "available", "only", "track", "tracks",
               "language", "languages", "lang"}
_CODEC_FILLER = {
    "aac", "dd", "ddp", "ac3", "eac3", "dts", "atmos", "truehd", "mp3", "flac",
    "opus", "pcm", "hq", "kbps",
}
_NONE_VALUES = {"no", "none", "nil", "na", "n/a", "nahi"}
_MSUB_VALUES = {"multi", "multiple", "various", "all", "many"}

_PROT_CH = re.compile(r"(?<![0-9])([1-9])\.([0-2])(?![0-9])")
_PROT_H = re.compile(r"\b([hHxX])\.(26[45])\b")   # H.264 keeps its dot
_DOTS = re.compile(r"[._]+")
_EP_DOT = re.compile(r"(?<![\w-])([Ee][Pp]?)\.(?=\d{1,3}(?!\d))")
_EP_SPACE = re.compile(r"(?<![\w-])([Ee][Pp]?)[ ]+(?=\d{1,3}\b)")
_TOKEN_RE = re.compile(r"[\[\](){}]|[,+&/|:]|[^\s\[\](){},+&/|:]+")
_BRACKET_RE = re.compile(r"[\[\(\{]([^\[\]\(\)\{\}]{1,80})[\]\)\}]")
_BR_TOK = "\x02"        # placeholder token for a language bracket group
_JUNK_TOK = "\x04"      # placeholder token for a junk bracket group [ProjectX]


_CODEC_NUM = re.compile(r"^(?:he-)?(?:dd|ddp|aac|ac3|eac3|dts|truehd|flac|mp3)?\+?[\d\x01]+(?:-?\d+kbps)?$")
_KBPS = re.compile(r"^\d{2,4}-?k(?:bps|pbs)$|^\d(?:ch)?$")


def _is_codecish(tok):
    return tok in _CODEC_FILLER or bool(_CODEC_NUM.match(tok)) or bool(_KBPS.match(tok))


def _tokenize(text, protect=True):
    if protect:
        text = _PROT_CH.sub(lambda m: m.group(1) + "\x01" + m.group(2), text)
        text = _PROT_H.sub(lambda m: m.group(1) + "\x01" + m.group(2), text)
    text = _DOTS.sub(" ", text)
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def _tokenize_pairs(text):
    """-> [(lower, original-case)] so a title keeps TS / DJ / II casing."""
    text = _PROT_CH.sub(lambda m: m.group(1) + "\x01" + m.group(2), text)
    text = _PROT_H.sub(lambda m: m.group(1) + "\x01" + m.group(2), text)
    text = _EP_DOT.sub(r"\1", text)
    text = _EP_SPACE.sub(r"\1", text)
    text = _DOTS.sub(" ", text)
    out = []
    for t in _TOKEN_RE.findall(text):
        if len(t) > 1:
            t = t.strip("-")
        if not t:
            continue
        low = t.lower()
        if "-" in low and low not in _STRONG:
            parts = low.split("-")
            hits = sum(1 for x in parts if _YEAR_RE.match(x) or _RES_RE.match(x)
                       or _SE_RE.match(x) or x in _STRONG)
            langs_in = sum(1 for x in parts if _lang_of(x, 1))
            if len(parts) == 2 and all(_YEAR_RE.match(x) for x in parts):
                out.append((parts[0], t.split("-")[0]))                 # "2025-2026" -> 2025
                continue
            if hits >= 2 or (hits >= 1 and hits + langs_in >= 2):   # hyphen is the separator here
                for lo, o in zip(parts, t.split("-")):
                    if lo:
                        out.append((lo, o))
                continue
        out.append((low, t))
    return out


def _restore(tok):
    return tok.replace("\x01", ".")


_STOPWORDS = {"the", "a", "an", "of", "and", "in", "on", "to", "my", "is", "for", "at", "by", "with"}


def _lang_group(content):
    """
    Languages of a bracket group, or None when it is not a language group.

    A language group holds languages, codec words / channel layouts
    ("DDP 5.1", "AAC 2.0", "6CH", "224kbps"), separators, and short release
    shorthand ("NF", "MX", "JC", "TV-DL").  One misspelt long word is
    tolerated ("[Hindi + Malyalam]") but never together with short unknown
    words, and an English stop-word ("The Hindi Teacher") disqualifies it.
    """
    toks = _tokenize(content)
    langs = []
    short_unknown = long_unknown = 0
    for t in toks:
        if t in _SEP_TOKS or t in _FILLER_VALUE or _is_codecish(t):
            continue
        if t in _STOPWORDS:
            return None
        if "-" in t:
            parts = [x for x in t.split("-") if x]
            got = [_lang_of(x, 2) for x in parts]
            if any(got):
                langs += [g for g in got if g]
                continue
            if all(_is_codecish(x) or len(x) <= 4 for x in parts):
                short_unknown += 1
                continue
            return None
        l = _lang_of(t, 2)
        if l:
            langs.append(l)
        elif t.isalpha() and len(t) >= 5:
            long_unknown += 1
        elif len(t) <= 4:
            short_unknown += 1
        else:
            return None
    if not langs or long_unknown > 1 or (long_unknown and short_unknown):
        return None
    return _dedup(langs)


def _dedup(seq):
    seen, out = set(), []
    for x in seq:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# ───────────────────────────── segment parsing ──────────────────────────────

class _Seg:
    __slots__ = ("toks", "orig", "groups", "texts", "title_toks", "title_orig",
                 "tag_toks", "anchored", "has_year", "year")

    def __init__(self):
        self.toks = []
        self.orig = []        # same tokens, original casing
        self.groups = []      # bracket language groups, referenced by index
        self.texts = []       # raw text of each bracket group
        self.title_toks = []  # tokens that belong to the title
        self.title_orig = []
        self.tag_toks = []    # leading tags + everything after the title
        self.anchored = False
        self.has_year = False
        self.year = ""


def _prep_brackets(text, groups, texts):
    """Replace language brackets with placeholder tokens; junk brackets too."""
    lead = len(text) - len(text.lstrip())

    def repl(m):
        content = m.group(1)
        langs = _lang_group(content)
        if langs:
            groups.append(langs)
            texts.append(content)
            return " %s%d%s " % (_BR_TOK, len(groups) - 1, _BR_TOK)
        toks = _tokenize(content)
        if m.start() <= lead and not any(
                _YEAR_RE.match(x) or _RES_RE.match(x) or _SE_RE.match(x)
                for t in toks for x in ([t] + t.split("-"))):
            return " %s " % _JUNK_TOK
        keep = any(
            _YEAR_RE.match(x) or _RES_RE.match(x) or _SE_RE.match(x)
            or x in _STRONG or x in _SUB_KW or x in _AUDIO_KW or x in _SUB_LIT
            or x in _MSUB_LIT or x in _SOFT_WORDS or x in _EXTS
            for t in toks for x in ([t] + t.split("-"))
        )
        return " %s " % content if keep else " %s " % _JUNK_TOK
    return _BRACKET_RE.sub(repl, text)


_NOT_PLAIN = {"web", "dl", "rip", "org", "line", "clean", "hq", "season", "seasons", "series",
              "complete", "episode", "episodes", "movie", "movies", "full", "in", "ripx"}


def _is_plain(tok):
    """An ordinary word (not a tag, language, codec, anchor or separator)."""
    return (tok.isalpha() and len(tok) > 1 and not _is_soft(tok) and tok not in _NOT_PLAIN
            and tok not in _STRONG and tok not in _CODEC_FILLER and tok not in _EXTS)


def _has_episode_before(toks, i):
    return any(re.match(r"^s\d{1,3}e\d|^e\d|^ep\.?\d|^\d{1,2}x\d", t) for t in toks[:i])


def _is_br(tok):
    return tok.startswith(_BR_TOK)


def _meaningful(p):
    return bool(_lang_of(p, 1) or p in _SUB_LIT or p in _MSUB_LIT or p in _SUB_KW or p in _AUDIO_KW)


def _is_soft(tok, level=1):
    if tok in _SEP_TOKS or tok in _AUDIO_KW or tok in _SUB_KW or tok in _SOFT_WORDS:
        return True
    if tok in _SUB_LIT or tok in _MSUB_LIT or tok == _JUNK_TOK or _is_br(tok):
        return True
    if "-" in tok and tok not in _STRONG and any(_meaningful(p) for p in tok.split("-")):
        return True
    return bool(_lang_of(tok, level))


def _expand_hyphen_langs(pairs):
    """Split "Hin-Eng", "LiNE-Tamil", "HC-ESub", "5.1-English" in the TAG zone."""
    out = []
    for t, o in pairs:
        if "-" in t and len(t) > 2 and t not in _STRONG:
            parts = t.split("-")
            if len(parts) > 1 and any(_meaningful(x) for x in parts):
                oparts = o.split("-")
                if len(oparts) != len(parts):
                    oparts = parts
                for i, x in enumerate(parts):
                    if not x:
                        continue
                    if i:
                        prev_lang = bool(out) and bool(_lang_of(out[-1][0], 1))
                        out.append((",", ",") if (prev_lang and _lang_of(x, 1)) else ("-", "-"))
                    out.append((x, oparts[i]))
                continue
        out.append((t, o))
    return out


_EMPTY_BR = re.compile(r"[\[\(\{]\s*[\]\)\}]")


def _anchor_kind(toks, i, n):
    t = toks[i]
    return bool(
        _RES_RE.match(t) or _SE_RE.match(t) or t in _STRONG or t in _EXTS
        or (t in ("season", "seasons", "episode") and i + 1 < n
            and re.match(r"^\d{1,4}(?:-\d{1,3})?$", toks[i + 1]))
    )


def _segment(text):
    seg = _Seg()
    text = _EMPTY_BR.sub(" ", text)
    text = _prep_brackets(text, seg.groups, seg.texts)
    pairs = [p for p in _tokenize_pairs(text) if p[0] not in ("[", "]", "(", ")", "{", "}")]
    while pairs and pairs[0][0] in _SEP_TOKS:
        pairs.pop(0)
    while pairs and pairs[-1][0] in _SEP_TOKS:
        pairs.pop()
    toks = [p[0] for p in pairs]
    n = len(toks)
    seg.toks = toks
    seg.orig = [p[1] for p in pairs]

    # leading tags ("480p | Oye Bhole Oye 2024 ...", "[Hindi] Movie 2020") are tags;
    # the title starts after them.  A leading language WORD is never skipped
    # (that is the first word of "Hindi Medium").
    t0 = 0
    while t0 < n:
        if (_anchor_kind(toks, t0, n) or toks[t0] in _SEP_TOKS
                or toks[t0] == _JUNK_TOK or _is_br(toks[t0])):
            t0 += 1
        elif toks[t0] in _SERIES_PFX and t0 + 1 < n and toks[t0 + 1] == "series":
            t0 += 2
        elif (_YEAR_RE.match(toks[t0]) and t0 + 1 < n
              and (_is_br(toks[t0 + 1]) or _anchor_kind(toks, t0 + 1, n) or _is_soft(toks[t0 + 1]))):
            t0 += 1
        else:
            break
    anchor = None
    for i in range(t0 + 1, n):
        t = toks[i]
        if _YEAR_RE.match(t) and i + 1 < n and _YEAR_RE.match(toks[i + 1]):
            continue                                  # "2049 2017": 2049 is part of the title
        if t == "-" and i + 1 < n and toks[i + 1].isdigit() and 2 <= len(toks[i + 1]) <= 4 \
                and not _YEAR_RE.match(toks[i + 1]) and toks[i + 1] not in _RES_NUMBERS \
                and (i + 2 >= n or _anchor_kind(toks, i + 2, n) or toks[i + 2] in _SEP_TOKS):
            anchor = i                                  # anime absolute episode number
            break
        if _YEAR_RE.match(t) or _anchor_kind(toks, i, n):
            anchor = i
            break
    seg.anchored = anchor is not None
    if n and all(_is_soft(t) or _anchor_kind(toks, i, n) or t in _CODEC_FILLER or _is_codecish(t) or t == "web"
                 for i, t in enumerate(toks)):
        seg.anchored = True                     # tag-only line: no title, languages are real
        t0, anchor = n, None
    end = anchor if anchor is not None else n
    s = end
    while s > t0 + 1:
        tk = toks[s - 1]
        if _is_soft(tk):
            s -= 1
        elif tk == "series" and s - 2 >= t0 and toks[s - 2] in _SERIES_PFX:
            s -= 2
        elif tk == "prime" and s - 2 >= t0 and toks[s - 2] == "amazon":
            s -= 2
        elif tk in _TAG_FILLER and s - 2 > t0 and _is_soft(toks[s - 2]) and toks[s - 2] not in _SEP_TOKS:
            s -= 1
        else:
            break
    if t0 >= n:
        s = n
    seg.title_toks = toks[t0:s]
    seg.title_orig = seg.orig[t0:s]
    tag_pairs = _expand_hyphen_langs(pairs[:t0] + pairs[s:])
    seg.tag_toks = [p[0] for p in tag_pairs]
    for i in range(n):                          # year (a title-initial token is never the year)
        if (i != t0 and _YEAR_RE.match(toks[i]) and not (i + 1 < n and _YEAR_RE.match(toks[i + 1]))
                and not (i and toks[i - 1] in ("episode", "ep", "e"))):
            seg.has_year = True
            seg.year = toks[i]
            break
    return seg


# role resolution inside a token run ------------------------------------------

def _run_after(toks, j, claimed, groups):
    n = len(toks)
    while j < n and toks[j] in _RUN_FILLER:
        j += 1
    if j >= n or j in claimed:
        return []
    if _is_br(toks[j]):
        return [j]
    if not _lang_of(toks[j], 2):
        return []
    run = [j]
    k = j + 1
    while k + 1 < n and toks[k] in _EXPLICIT_SEP and _lang_of(toks[k + 1], 2) and (k + 1) not in claimed:
        run.append(k + 1)
        k += 2
    return run


def _run_before(toks, j, claimed):
    while j >= 0 and toks[j] in _RUN_FILLER:
        j -= 1
    if j < 0 or j in claimed:
        return []
    if _is_br(toks[j]):
        return [j]
    if not _lang_of(toks[j], 2):
        return []
    run = [j]
    k = j - 1
    while k - 1 >= 0 and toks[k] in _EXPLICIT_SEP and _lang_of(toks[k - 1], 2) and (k - 1) not in claimed:
        run.append(k - 1)
        k -= 2
    return run[::-1]


def _langs_at(toks, idxs, groups):
    out = []
    for i in idxs:
        t = toks[i]
        if _is_br(t):
            out += groups[int(t.strip(_BR_TOK))]
        else:
            l = _lang_of(t, 2)
            if l:
                out.append(l)
    return out


class _Roles:
    __slots__ = ("audio_role", "audio_free", "audio_all", "sub_role", "msub", "dual", "multi_kw")

    def __init__(self):
        self.audio_all = []    # role + free languages in text order
        self.audio_role = []   # explicit "Hindi Audio" / "[Hindi + English]"
        self.audio_free = []   # un-claimed language words in the tag zone
        self.sub_role = []
        self.msub = False
        self.dual = False
        self.multi_kw = False


def _parse_tag_tokens(toks, start, groups):
    """Role-resolve toks[start:] -> _Roles."""
    r = _Roles()
    n = len(toks)
    claimed = set()
    audio_idx, sub_idx = [], []

    for i in range(start, n):                        # 1) audio keywords
        t = toks[i]
        if t in _AUDIO_KW:
            pv = toks[i - 1] if i > start else ""
            if pv in _SEP_TOKS and i - 2 >= start:
                pv = toks[i - 2]
            if pv in ("dual", "multi"):
                r.dual = r.dual or pv == "dual"
                r.multi_kw = r.multi_kw or pv == "multi"
            run = _run_before(toks, i - 1, claimed)
            if run and run[0] < start:
                run = []
            if not run:
                run = _run_after(toks, i + 1, claimed, groups)
            for k in run:
                claimed.add(k)
            audio_idx += run
    for i in range(start, n):                        # 2) subtitle keywords
        t = toks[i]
        if t in _SUB_KW:
            if i > start and toks[i - 1] in ("multi", "multiple"):
                r.msub = True
                continue
            run = _run_after(toks, i + 1, claimed, groups)
            if not run:
                run = _run_before(toks, i - 1, claimed)
                if run and run[0] < start:
                    run = []
            for k in run:
                claimed.add(k)
            sub_idx += run
            if not run and t != "sub" and t != "subbed":
                r.msub = True                         # bare "Subs"/"Subtitles" tag
        elif t in _SUB_LIT:
            r.sub_role.append(_SUB_LIT[t])
        elif t in _MSUB_LIT:
            r.msub = True
    ordered = [(i, _langs_at(toks, [i], groups)) for i in audio_idx]
    r.audio_role = _dedup(_langs_at(toks, audio_idx, groups))
    r.sub_role += _langs_at(toks, sub_idx, groups)
    r.sub_role = _dedup(r.sub_role)
    for i in range(start, n):                        # 3) everything else
        if i in claimed:
            continue
        t = toks[i]
        if _is_br(t):
            r.audio_free += groups[int(t.strip(_BR_TOK))]
            ordered.append((i, groups[int(t.strip(_BR_TOK))]))
        else:
            l = _lang_of(t, 1)
            if l:
                if (i > start and i + 1 < n and _is_plain(toks[i - 1]) and _is_plain(toks[i + 1])
                        and _has_episode_before(toks, i)):
                    continue                         # inside an EPISODE TITLE ("Beware the Hindi Ocean")
                r.audio_free.append(l)
                ordered.append((i, [l]))
    r.audio_free = _dedup(r.audio_free)
    r.audio_all = _dedup([l for _, ls in sorted(ordered, key=lambda x: x[0]) for l in ls])
    if "dual" in toks[start:] and "audio" in toks[start:]:
        r.dual = True
    return r


# ───────────────────────────── labeled lines ────────────────────────────────

_LABEL_RE = re.compile(
    r"(?<![a-z0-9-])(?:"
    r"(?P<a>audio\s*/\s*lang(?:uage)?s?|audio\s*lang(?:uage)?s?|audio\s*tracks?|audios?"
    r"|original\s*lang(?:uage)?s?|languages?|lang|dubbed|dubbing|dub|voice"
    r"|dual\s*audio|multi\s*audio)"
    r"|(?P<s>subtitle\s*lang(?:uage)?s?|sub\s*lang(?:uage)?s?|subtitles?|subs?)"
    r")\s*(?P<sep>:|-+)\s*"
)
_AUDIO_SUFFIX_KW = {"audio", "audios", "dub", "dubbed", "dubbing", "voice"}
_VALUE_WORD = re.compile(r"[a-z]+(?:-[a-z]+)*|[,+&/]")


def _label_value(value):
    """-> (langs, none_flag, msub_flag, used)  `used` = chars of `value` consumed."""
    cut = value.find("|")
    if cut >= 0:
        value = value[:cut]
    first = re.match(r"\s*([a-z/]+)", value)
    if first and first.group(1) in _NONE_VALUES:
        return [], True, False, first.end()
    langs = []
    msub = False
    used = 0
    for wm in _VALUE_WORD.finditer(value):
        w = wm.group(0)
        if w in _EXPLICIT_SEP:
            used = wm.end()
            continue
        if "-" in w:
            parts = w.split("-")
            got = [_lang_of(p, 2) for p in parts]
            if all(got):
                langs += got
                used = wm.end()
                continue
            if all((p in _FILLER_VALUE) or _lang_of(p, 2) for p in parts):
                langs += [g for g in got if g]
                used = wm.end()
                continue
            break
        l = _lang_of(w, 2)
        if l:
            langs.append(l)
            used = wm.end()
        elif w in _FILLER_VALUE:
            used = wm.end()
            continue
        elif w in _MSUB_VALUES and not langs:
            msub = True
            used = wm.end()
        else:
            break
    return _dedup(langs), False, msub, used


def _split_labels(line):
    """-> (pre_text, [(kind, langs, none, msub), ...]).  kind: 'a' | 's'."""
    low = line.lower()
    found = []
    for m in _LABEL_RE.finditer(low):
        kind = "a" if m.group("a") else "s"
        if kind == "a" and ":" not in m.group("sep") and m.group("a").split()[0] in _AUDIO_SUFFIX_KW:
            prev = re.search(r"([a-z]+)\W*$", low[: m.start()])
            if prev and _lang_of(prev.group(1), 2):
                continue                      # "Hindi Dubbed - ..." is a suffix role
        found.append((m.start(), m.end(), kind))
    if not found:
        return line, []
    labels = []
    spans = []
    for idx, (st, en, kind) in enumerate(found):
        nxt = found[idx + 1][0] if idx + 1 < len(found) else len(low)
        langs, none, msub, used = _label_value(low[en:nxt])
        if langs or none or msub:
            labels.append((kind, langs, none, msub))
            spans.append((st, en + used))
    if not spans:
        return line, []
    # everything NOT consumed by an accepted label stays available as normal text
    pieces, pos = [], 0
    for st, en in spans:
        if st > pos:
            pieces.append(line[pos:st])
        pos = max(pos, en)
    if pos < len(line):
        pieces.append(line[pos:])
    return " | ".join(x.strip() for x in pieces if x.strip()), labels


# ───────────────────────────── title helpers ────────────────────────────────

_ROMAN = {"ii", "iii", "iv", "vi", "vii", "viii", "ix", "xi", "xii", "xiii"}
_WORD_RE = re.compile(r"[^\W\d_]+(?:['’][^\W\d_]+)*")
FALLBACK_NAMES = {"file", "voice message", "document", "video", "audio", "photo"}
_KEYVAL_RE = re.compile(
    r"^(?:quality|size|file ?size|file ?name|genre|genres|language|languages|audio|subtitles?|"
    r"format|runtime|duration|release|year|rating|imdb|director|cast|plot|story|type|source|"
    r"resolution|codec|info|note|notes|download|channel|join|share|tags?|category)\s*:"
)


def _title_case(title):
    has_lower = any(c.islower() for c in title)

    def fix(m):
        w = m.group(0)
        if has_lower and w.isupper() and len(w) <= 3:
            return w                              # TS Madras, DC, MIB ...
        if w.isupper() and w.lower() in _ROMAN and has_lower:
            return w
        return w[:1].upper() + w[1:].lower()
    return _WORD_RE.sub(fix, title)


def _title_from(seg):
    toks = [o for t, o in zip(seg.title_toks, seg.title_orig)
            if t not in (_JUNK_TOK,) and not _is_br(t)]
    while toks and toks[-1] in _SEP_TOKS:
        toks.pop()
    while toks and toks[0] in _SEP_TOKS:
        toks.pop(0)
    title = " ".join(_restore(t) for t in toks)
    title = re.sub(r"\s*([,:])\s*", r"\1 ", title)
    title = re.sub(r"\s*\|\s*", " | ", title)
    title = re.sub(r"\s{2,}", " ", title).strip(" -:|,")
    return _title_case(title) if title else ""


# ─────────────────── junk / truncated filename handling ─────────────────────

_JUNK_FN = re.compile(
    r"^(?:video|vid|img|image|file|document|doc|audio|aud|voice|clip|screen[\s_-]*recording|"
    r"telegram|tg|download|untitled|new[\s_-]*file)?[\s_-]*"
    r"(?:\d{6,}|(?:19|20)\d{2}[-_.]?(?:0[1-9]|1[0-2])[-_.]?(?:0[1-9]|[12]\d|3[01]))"
    r"(?:[\s_-][A-Za-z0-9]{1,8})*$", re.I)
_HASH_FN = re.compile(r"^[0-9a-f]{16,}$", re.I)
_EXT_TAIL = re.compile(r"\.\w{2,4}$")


def looks_junk_filename(filename):
    """Camera / app / timestamp / hash names that say nothing about the content."""
    stem = _EXT_TAIL.sub("", (filename or "").strip())
    if not stem:
        return True
    if _JUNK_FN.match(stem) or _HASH_FN.match(stem):
        return True
    letters = re.sub(r"[^A-Za-z\u0080-\uffff]", "", stem)
    if len(letters) <= 2:                               # "a.mkv" -- but "It 2017" keeps its year
        toks = _tokenize(stem)
        return not any(_YEAR_RE.match(t) or _RES_RE.match(t) or _SE_RE.match(t) for t in toks)
    return False


def _alnum(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def repair_truncated(filename, lines):
    """
    Telegram cuts long file names (~60 chars) -- "…[Hindi D.mkv" -- while the
    caption often holds the COMPLETE release name.  When a caption line is a
    longer copy of the filename, use that line instead, so languages,
    subtitles and codecs that were cut off are not lost.
    """
    stem = _EXT_TAIL.sub("", filename)
    key = _alnum(stem)
    if len(key) < 30:
        return filename
    prefix = key[: max(25, len(key) - 12)]            # the last chars may be a cut-off word
    best, best_len = None, len(key) - 1                 # same length or longer (typo-fixed copies too)
    for ln in lines:
        lk = _alnum(ln)
        if len(lk) <= best_len:
            continue
        if lk.startswith(prefix):
            best, best_len = ln, len(lk)
        elif lk[:12] == prefix[:12]:                      # same start -> allow inserted/changed text
            sm = difflib.SequenceMatcher(None, prefix, lk[: len(prefix) + 40], autojunk=False)
            if sum(b.size for b in sm.get_matching_blocks()) >= 0.88 * len(prefix):
                best, best_len = ln, len(lk)
    if best is None:
        return filename
    m = _EXT_TAIL.search(filename)
    return best if _EXT_TAIL.search(best) or not m else best + m.group(0)


# ───────────────────────────── main entry point ─────────────────────────────

def _field(value="", conf="", source=""):
    return {"value": value, "conf": conf, "source": source}


def analyze(filename, caption, normalized=False):
    """
    Returns a dict:
      title / year          {value, conf, source}
      audio                 {langs, conf, source, dual}
      subtitle              {langs, tag, conf, source}
      flags                 sorted list: low_confidence / conflict / omitted_language
      tag_text              text with the title words removed (for quality/codec/...)
    """
    if not normalized:
        filename = normalize_text(filename or "")
        caption = normalize_text(caption or "")
    flags = set()
    filename = filename.replace("\n", " ").strip()
    cap_lines = [ln for ln in caption.split("\n") if ln.strip()]
    if filename.lower() in FALLBACK_NAMES:
        filename = ""
    elif filename and cap_lines:
        if looks_junk_filename(filename):
            filename = ""                         # junk name: the caption is the source
        else:
            filename = repair_truncated(filename, cap_lines)

    audio_label, sub_label = [], []
    sub_none = msub_label = False
    segs = []                       # (kind, text, seg)
    if filename:
        segs.append(("file", filename, _segment(filename)))
    for ln in caption.split("\n"):
        pre, labels = _split_labels(ln)
        for kind, langs, none, msub in labels:
            if kind == "a":
                audio_label += langs
            else:
                sub_label += langs
                sub_none = sub_none or none
                msub_label = msub_label or msub
        if pre.strip() and not (labels and not re.search(r"[a-z0-9]", pre.lower())):
            segs.append(("cap", pre.strip(), _segment(pre)))

    # ---- title / year ----------------------------------------------------
    title = _field()
    year = _field()
    title_seg = None
    if segs and segs[0][0] == "file":
        t = _title_from(segs[0][2])
        if t:
            title = _field(t, HIGH if segs[0][2].anchored else MEDIUM, "filename")
            title_seg = segs[0][2]
    if not title["value"]:
        cands = [s for s in segs if s[0] == "cap" and not _KEYVAL_RE.match(s[1].lower())]
        pick = next((s for s in cands if s[2].anchored), None) or (cands[0] if cands else None)
        if pick:
            t = _title_from(pick[2])
            if t:
                title = _field(t, MEDIUM if pick[2].anchored else LOW, "caption")
                title_seg = pick[2]
    for kind, _, seg in segs:
        if seg.has_year:
            year = _field(seg.year, HIGH if kind == "file" else MEDIUM, "filename" if kind == "file" else "caption")
            break

    # ---- roles per segment -----------------------------------------------
    file_audio, cap_audio, file_sub, cap_sub = [], [], [], []
    msub_tag = False
    dual_kw = False
    audio_src = {"file": "filename", "cap": "caption-tags"}
    cap_audio_src = ""
    omitted = False
    for kind, text, seg in segs:
        roles = _parse_tag_tokens(seg.tag_toks, 0, seg.groups)
        found = list(roles.audio_all)
        if seg.anchored or roles.audio_role or found:
            aud = found
            if kind == "file":
                file_audio += aud
            else:
                cap_audio += aud
                if roles.audio_role and not cap_audio_src:
                    cap_audio_src = "role-keyword"
        elif found:
            omitted = True
        if seg.title_toks:
            tr = _parse_tag_tokens(seg.title_toks, 0, seg.groups)
            if tr.audio_role:
                roles.audio_role = _dedup(roles.audio_role + tr.audio_role)
                if kind == "file":
                    file_audio += tr.audio_role
                else:
                    cap_audio += tr.audio_role
                    cap_audio_src = cap_audio_src or "role-keyword"
                omitted = omitted and False
            roles.sub_role = _dedup(roles.sub_role + [x for x in tr.sub_role if x not in roles.sub_role])
        if kind == "file":
            file_sub += roles.sub_role
        else:
            cap_sub += roles.sub_role
        msub_tag = msub_tag or roles.msub
        dual_kw = dual_kw or roles.dual
    file_audio, cap_audio = _dedup(file_audio), _dedup(cap_audio)
    file_sub, cap_sub = _dedup(file_sub), _dedup(cap_sub)

    # ---- audio decision ----------------------------------------------------
    audio = {"langs": [], "conf": "", "source": "", "dual": dual_kw}
    if audio_label:
        audio.update(langs=_dedup(audio_label), conf=HIGH, source="label")
        for other in (file_audio, cap_audio):
            if other and set(other) != set(audio["langs"]):
                flags.add("conflict")
    elif file_audio:
        audio.update(langs=file_audio, conf=MEDIUM, source="filename")
        if cap_audio and set(cap_audio) != set(file_audio):
            flags.add("conflict")
    elif cap_audio:
        audio.update(langs=cap_audio, conf=MEDIUM, source=cap_audio_src or "caption-tags")
    if omitted and not audio["langs"]:
        flags.add("omitted_language")

    # ---- subtitle decision -------------------------------------------------
    sub = {"langs": [], "tag": "", "conf": "", "source": ""}
    if sub_label or sub_none or msub_label:
        sub.update(langs=_dedup(sub_label), conf=HIGH, source="label")
        found = file_sub or cap_sub
        if found and set(found) != set(sub["langs"]):
            flags.add("conflict")
        if msub_label and not sub["langs"]:
            sub["tag"] = "MSub"
    elif file_sub:
        sub.update(langs=file_sub, conf=MEDIUM, source="filename")
        if cap_sub and set(cap_sub) != set(file_sub):
            flags.add("conflict")
    elif cap_sub:
        sub.update(langs=cap_sub, conf=MEDIUM, source="caption-tags")
    if not sub["tag"] and not (sub_none and sub["source"] == "label" and not sub["langs"]):
        langs = sub["langs"]
        if len(langs) > 1:
            sub["tag"] = "MSub"
        elif langs == ["English"]:
            sub["tag"] = "ESub"
        elif langs == ["Hindi"]:
            sub["tag"] = "HSub"
        elif langs:
            sub["tag"] = "MSub"
        elif msub_tag and not sub_none:
            sub["tag"] = "MSub"
            sub["conf"], sub["source"] = MEDIUM, "filename"

    # ---- tag text (everything except the title words) ---------------------
    parts = []
    tt = (title["value"] or "").lower()
    for kind, text, seg in segs:
        if seg is title_seg:
            parts.append(" ".join(
                (seg.texts[int(t.strip(_BR_TOK))].lower() if _is_br(t) else _restore(t))
                for t in seg.tag_toks
            ))
        else:
            low = text.lower()
            parts.append(low.replace(tt, " ") if len(tt) >= 3 else low)
    tag_text = " ".join(parts)

    if not title["value"] or (title["source"] == "caption" and title["conf"] == LOW):
        flags.add("low_confidence")
    if "omitted_language" in flags:
        flags.add("low_confidence")

    return {
        "title": title, "year": year, "audio": audio, "subtitle": sub,
        "flags": sorted(flags), "tag_text": tag_text, "filename": filename,
    }
