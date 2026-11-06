"""Builds burned-in, word-synchronised ASS captions for a vertical short."""
from __future__ import annotations

import re

from . import config

GOLD = "&H003DC8FF&"     # #FFC83D in ASS BGR
WHITE = "&H00FFFFFF&"
YELLOW = "&H0029E6FF&"   # #FFE629 - the active word in "pop" captions
GREEN = "&H0066F27A&"    # #7AF266 - names/numbers in "pop" captions

# Caption looks. "clean" suits narrated explainers; "pop" is the bold one-or-two-words-at-a-time
# style that clip channels use for streams and podcasts.
STYLES = {
    "clean": {"size": 80, "outline": 6, "shadow": 3, "max_words": 3, "max_chars": 20, "title": 52,
              "active": None, "important": GOLD, "grow": 114, "pop_from": 92},
    "pop": {"size": 98, "outline": 9, "shadow": 0, "max_words": 2, "max_chars": 14, "title": 66,
            "active": YELLOW, "important": GREEN, "grow": 118, "pop_from": 70},
}

_HEADER = """[Script Info]
ScriptType: v4.00+
PlayResX: {w}
PlayResY: {h}
WrapStyle: 2
ScaledBorderAndShadow: yes
YCbCr Matrix: TV.709

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Cap,{font},{cap_size},&H00FFFFFF,&H00FFFFFF,&H00101010,&H96000000,0,0,0,0,100,100,1,0,1,{cap_outline},{cap_shadow},5,60,60,0,1
Style: Title,{font},{title_size},&H00FFFFFF,&H00FFFFFF,&H00101010,&H96000000,0,0,0,0,100,100,0.5,0,1,4,2,2,90,90,0,1
Style: Tag,{font},30,&H003DC8FF,&H003DC8FF,&H00101010,&H96000000,0,0,0,0,100,100,6,0,1,2,0,8,60,60,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def _ts(t: float) -> str:
    t = max(0.0, t)
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _clean(word: str) -> str:
    w = word.replace("{", "").replace("}", "").replace("\\", "")
    w = re.sub(r"^[\"'“‘(\[]+|[\"'”’)\],.;:]+$", "", w)
    return w.upper()


_NUMERIC = re.compile(r"\d")


def is_important(word: str, prev: str | None, keywords: set[str]) -> bool:
    core = re.sub(r"[^A-Za-z0-9']", "", word).lower()
    if not core:
        return False
    if core in keywords:
        return True
    if _NUMERIC.search(core):
        return True
    sentence_start = prev is None or prev.rstrip().endswith((".", "!", "?"))
    raw = re.sub(r"^[^A-Za-z]+", "", word)
    if raw[:1].isupper() and not sentence_start and core not in {"i", "i'm", "i've"} and len(core) > 2:
        return True
    return False


def group_words(words: list[dict], max_words: int = 3, max_chars: int = 20) -> list[list[dict]]:
    groups, cur = [], []
    for k, w in enumerate(words):
        txt = _clean(w["w"])
        if not txt:
            continue
        cand = cur + [w]
        chars = sum(len(_clean(x["w"])) for x in cand) + len(cand) - 1
        if cur and (len(cand) > max_words or chars > max_chars or w["s"] - cur[-1]["e"] > 0.45):
            groups.append(cur)
            cand = [w]
        cur = cand
        if re.search(r"[.!?;:,]$", w["w"]) and cur:
            groups.append(cur)
            cur = []
    if cur:
        groups.append(cur)
    return groups


def _line_text(group: list[dict], active: int, important: list[bool], st: dict | None = None) -> str:
    st = st or STYLES["clean"]
    tokens = [_clean(w["w"]) for w in group]
    # Two balanced lines when the group is long.
    total = sum(len(t) for t in tokens) + len(tokens) - 1
    split_at = None
    if total > (13 if st["max_chars"] > 14 else 11) and len(tokens) >= 2:
        best = None
        for k in range(1, len(tokens)):
            a = sum(len(t) for t in tokens[:k]) + k - 1
            b = total - a - 1
            diff = abs(a - b)
            if best is None or diff < best:
                best, split_at = diff, k
    parts = []
    for k, t in enumerate(tokens):
        col = st["important"] if important[k] else WHITE
        if k == active:
            col = st["active"] or col
            g = st["grow"]
            seg = f"{{\\c{col}\\fscx{g}\\fscy{g}}}{t}{{\\fscx100\\fscy100}}"
        else:
            seg = f"{{\\c{col}}}{t}"
        if split_at is not None and k == split_at:
            parts.append("\\N")
        elif k:
            parts.append(" ")
        parts.append(seg)
    return "".join(parts)


def wrap_title(title: str, max_chars: int = 26, max_lines: int = 3) -> str:
    """Balanced manual line breaks (libass auto-wrap is off so captions never reflow)."""
    words = title.split()
    total = len(title)
    lines_needed = min(max_lines, max(1, -(-total // max_chars)))
    target = total / lines_needed
    lines, cur = [], ""
    for w in words:
        if cur and (len(cur) + 1 + len(w) > max_chars or
                    (len(lines) < lines_needed - 1 and len(cur) >= target)):
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(".,;:") + "…"
    return "\\N".join(lines)


def build_ass(words: list[dict], clip_len: float, cap_y: int, title: str, title_y: int,
              tag_y: int, keywords: list[str], style: str = "clean", show_tag: bool | None = None,
              title_dur: float | None = None) -> str:
    """`words` must already be in output time (seconds from the start of the short).
    `title_dur` shows the title only for the first N seconds (None = whole clip)."""
    st = STYLES.get(style, STYLES["clean"])
    show_tag = bool(config.WATERMARK) and (True if show_tag is None else show_tag)
    kw = set()
    for k in keywords:
        for part in re.findall(r"[A-Za-z0-9']+", k.lower()):
            if len(part) > 2:
                kw.add(part)
    out = [_HEADER.format(w=config.OUT_W, h=config.OUT_H, font=config.CAPTION_FONT, cap_size=st["size"],
                          cap_outline=st["outline"], cap_shadow=st["shadow"], title_size=st["title"])]
    if show_tag:
        out.append(f"Dialogue: 1,{_ts(0)},{_ts(clip_len)},Tag,,0,0,0,,{{\\an8\\pos(540,{tag_y})}}"
                   f"{config.WATERMARK.upper().replace('{', '(').replace('}', ')')}")
    if title:
        safe = wrap_title(title.replace("{", "(").replace("}", ")").replace("\\", "/"))
        t_end = clip_len if title_dur is None else min(clip_len, title_dur)
        fade_out = 0 if title_dur is None else 250
        out.append(f"Dialogue: 1,{_ts(0)},{_ts(t_end)},Title,,0,0,0,,{{\\an2\\pos(540,{title_y})"
                   f"\\fad(250,{fade_out})}}{safe}")
    groups = group_words(words, st["max_words"], st["max_chars"])
    flat_prev: dict[int, str | None] = {}
    prev = None
    for w in words:
        flat_prev[id(w)] = prev
        prev = w["w"]
    for gi, g in enumerate(groups):
        nxt_start = groups[gi + 1][0]["s"] if gi + 1 < len(groups) else None
        g_end = g[-1]["e"] + 0.30
        if nxt_start is not None:
            g_end = nxt_start if nxt_start - g[-1]["e"] < 0.7 else min(g_end, nxt_start)
        g_end = min(g_end, clip_len)
        important = [is_important(w["w"], flat_prev[id(w)], kw) for w in g]
        for k, w in enumerate(g):
            s = g[0]["s"] if k == 0 else w["s"]
            e = g[k + 1]["s"] if k + 1 < len(g) else g_end
            if e - s < 0.02:
                continue
            pf = st["pop_from"]
            pop = f"\\fscx{pf}\\fscy{pf}\\t(0,90,\\fscx100\\fscy100)" if k == 0 else ""
            out.append(f"Dialogue: 0,{_ts(s)},{_ts(e)},Cap,,0,0,0,,{{\\an5\\pos(540,{cap_y}){pop}}}"
                       + _line_text(g, k, important, st))
    return "\n".join(out) + "\n"
