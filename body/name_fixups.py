import re

JOIN_LEAD_RE = re.compile(r"(?i)^[\s\[\(]*join[\s._-]+(?=@)")
DOMAIN_LEAD_RE = re.compile(
    r"(?i)^[\s\[\(]*(?:www\.)?[a-z0-9][a-z0-9-]*\."
    r"(?:com|net|org|in|top|xyz|lol|vip|pro|site|club|ws|cc|me|co|tv|io|cam|fun|life|buzz|run)"
    r"\s+[-–—|:]+\s*"
)
SITE_MARK_RE = re.compile(
    r"(?i)(?<![a-z])www(?![a-z])|[a-z0-9]\.(?:com|net|org|top|xyz|lol|vip|pro|site|club|ws|cc|cam)(?![a-z])"
)
DUB_BODY = r"(fan|unofficial)([\s._-]*)(dub(?:bed)?)(?![a-z])"
PART_RE = re.compile(r"(?i)(?<![a-z0-9])part0*(\d{1,3})\.(?:mkv|mp4|avi|ts|webm)(?![a-z0-9])")
WORD_RE = re.compile(r"[^\W_]+")


def strip_lead_noise(text, prefixes=()):
    text = JOIN_LEAD_RE.sub("", text or "", count=1)
    text = DOMAIN_LEAD_RE.sub("", text, count=1)
    for prefix in prefixes:
        text = re.sub(r"(?i)^[\s\[\(]*%s[\s._-]+(?=[A-Za-z0-9])" % re.escape(prefix), "", text, count=1)
    return text


def site_marked(text):
    return bool(SITE_MARK_RE.search(text or ""))


def titles_disjoint(a, b):
    wa = {w.casefold() for w in WORD_RE.findall(a or "")}
    wb = {w.casefold() for w in WORD_RE.findall(b or "")}
    return bool(wa) and bool(wb) and not (wa & wb)


def prefer_caption_title(file_result, caption_result):
    ft, ct = file_result.get("title") or {}, caption_result.get("title") or {}
    fy, cy = file_result.get("year") or {}, caption_result.get("year") or {}
    if ft.get("source") != "filename" or not ct.get("value") or ct.get("conf") == "low":
        return False
    if not fy.get("value") or fy.get("value") != cy.get("value"):
        return False
    return titles_disjoint(ft.get("value"), ct.get("value"))


def add_dub_tags(label, langs, raw):
    if not label or not langs or not raw:
        return label
    for lang in langs:
        m = re.search(
            r"(?i)(?<![a-z])%s(?![a-z])[\s({\[._,-]{0,4}%s" % (re.escape(lang), DUB_BODY), raw
        )
        if not m:
            continue
        sep = "-" if "-" in m.group(2) else " "
        tag = "%s%s%s" % (m.group(1).title(), sep, m.group(3).title())
        label = re.sub(r"(?i)(?<![a-z])%s(?![a-z])" % re.escape(lang), lambda x: "%s {%s}" % (x.group(0), tag), label, count=1)
    return label


def part_label(raw):
    m = PART_RE.search(raw or "")
    return "(Part %d)" % int(m.group(1)) if m else ""
