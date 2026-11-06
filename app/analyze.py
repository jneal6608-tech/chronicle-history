"""Finds the best standalone moments in a long video.

Two content modes:
  * documentary - narrated explainers: 3-5 complete stories of 60-90 s.
  * stream      - streams, podcasts, influencer videos: many 20-60 s moments,
                  guided by loudness spikes (reactions, laughter) as well as speech.

Two engines share the same validation step:
  * Claude (recommended) - reads the whole timestamped transcript (in chunks for
    multi-hour VODs), ranks candidates and writes titles/captions/hashtags,
    following a campaign brief when one is given.
  * Offline - free, deterministic scorers that run with no API key.

Both engines may only choose ranges of *existing sentences*; the clip
boundaries always land on real sentence edges in the narration, so nothing
is fabricated or cut mid-thought.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Callable, Optional

from . import brief, config, credits, post, results
from .media import fmt_ts, peak_energy

# Source-time limits for a segment (output is shorter after speed-up + pause removal).
SRC_MIN = 28.0
SRC_IDEAL_MIN = 62.0
SRC_IDEAL_MAX = 100.0
SRC_MAX = 104.0

# --------------------------------------------------------------------------- sentences

_END_RE = re.compile(r"[.!?…][\"')\]]*$")


def build_sentences(words: list[dict]) -> list[dict]:
    sents, cur = [], []
    for k, w in enumerate(words):
        cur.append(k)
        nxt = words[k + 1] if k + 1 < len(words) else None
        gap = (nxt["s"] - w["e"]) if nxt else 99
        ends = bool(_END_RE.search(w["w"]))
        too_long = len(cur) >= 45 and (w["w"].endswith(",") or len(cur) >= 60)
        if ends or gap > 1.6 or too_long or nxt is None:
            sents.append(cur)
            cur = []
    out = []
    for i, idxs in enumerate(sents):
        ws = [words[k] for k in idxs]
        out.append({"i": i, "s": ws[0]["s"], "e": ws[-1]["e"], "w0": idxs[0], "w1": idxs[-1],
                    "text": " ".join(x["w"] for x in ws).strip()})
    return out


# --------------------------------------------------------------------------- lexicons

DRAMA = set("""war wars battle battles empire empires emperor king kings queen pharaoh sultan tsar czar
death died dead dying killed kill killing murdered murder assassinated assassination executed execution
betrayed betrayal traitor collapse collapsed fell fall fallen ruin ruins destroyed destruction burned burning
revolution revolt rebellion rebels uprising conquered conquest invaded invasion siege besieged massacre
plague famine disaster catastrophe secret secrets mystery mysterious hidden lost discovered discovery
treasure gold fortune legend legendary curse cursed scandal coup throne crown dynasty army armies soldiers
navy fleet weapon weapons bomb bombs nuclear victory defeat defeated surrender escape escaped prison
poison poisoned spy spies conspiracy enemy enemies blood bloody brutal tragedy tragic terror fear
shocking strange bizarre unbelievable incredible extraordinary forbidden vanished disappeared""".split())

SUPERLATIVE = set("""first last largest biggest greatest deadliest richest oldest most least only never nobody
worst best longest fastest strongest tallest smallest youngest""".split())

HOOK_PHRASES = ["imagine", "what if", "few people know", "nobody knows", "no one knows", "the truth",
                "but here's", "here's the thing", "believe it or not", "in the year", "it was the",
                "have you ever", "why did", "how did", "what happened", "this is the story", "the story of",
                "one of the", "the most", "was the first", "was the last"]

ENDING_PHRASES = ["to this day", "ever since", "forever", "would never", "never again", "and that is why",
                  "that's why", "and so", "the end of", "finally", "in the end", "legacy", "changed history",
                  "changed the world", "remains", "still", "would go on", "became"]

DANGLING = {"he", "she", "they", "it", "this", "that", "these", "those", "his", "her", "their", "its",
            "and", "but", "so", "then", "also", "which", "however", "meanwhile", "him", "them", "there"}

PROMO = ["subscribe", "patreon", "sponsor", "sponsored", "thanks for watching", "thank you for watching",
         "like and", "comment below", "next video", "this video", "link in the description", "welcome back",
         "welcome to", "notification bell", "members of the channel", "see you next", "in today's video"]

STOP = set("""The A An And But So Then He She They It This That These Those His Her Their In On At Of For
To With By From As When While After Before During If Or Nor Yet Its We You I Our Your One Two Three Four
Five Six Seven Eight Nine Ten Yes No Today Now Here There What Why How Who Where Which Even Still Just
Mr Mrs Sir Lord Lady Saint St Not Over Under Into Some Many Most Every Each All""".split())

_NUM = re.compile(r"\b(\d{2,4}|\d+(?:[.,]\d+)?\s?(?:%|percent|million|billion|thousand)|hundreds|thousands|millions)\b", re.I)


def _tokens(text: str) -> list[str]:
    return re.findall(r"[A-Za-z']+", text.lower())


def proper_nouns(sents: list[dict]) -> list[str]:
    names = []
    for s in sents:
        toks = re.findall(r"[A-Za-z][A-Za-z'\-]+", s["text"])
        for k, t in enumerate(toks):
            if k == 0 or not t[0].isupper() or t in STOP or len(t) < 3:
                continue
            names.append(t.strip("'"))
    return names


# --------------------------------------------------------------------------- features

def _hook_score(text: str) -> tuple[float, list[str]]:
    low = text.lower()
    toks = _tokens(text)
    score, why = 0.0, []
    if not toks:
        return -1.0, ["empty"]
    if toks[0] in DANGLING:
        score -= 0.9 if toks[0] in {"he", "she", "they", "it", "him", "them", "its", "his", "her", "their"} else 0.5
        why.append(f"opens with '{toks[0]}' (needs context)")
    if text.rstrip().endswith("?"):
        score += 0.7
        why.append("opens with a question")
    hp = [p for p in HOOK_PHRASES if p in low]
    if hp:
        score += 0.5
        why.append(f"hook phrase \"{hp[0]}\"")
    if _NUM.search(text):
        score += 0.35
        why.append("opens on a concrete date/number")
    sup = [t for t in toks if t in SUPERLATIVE]
    if sup:
        score += 0.3
        why.append(f"superlative \"{sup[0]}\"")
    dr = [t for t in toks if t in DRAMA]
    if dr:
        score += min(0.5, 0.2 * len(dr))
        why.append("dramatic opening (" + ", ".join(dict.fromkeys(dr[:3])) + ")")
    if len(toks) < 4:
        score -= 0.3
    return score, why


def _end_score(text: str) -> tuple[float, list[str]]:
    low = text.lower()
    score, why = 0.0, []
    t = text.rstrip()
    if t.endswith("?"):
        score -= 0.6
        why.append("ends on an open question")
    elif t.endswith((".", "!", "…")):
        score += 0.3
    if t.endswith((":", ",")):
        score -= 0.8
    ep = [p for p in ENDING_PHRASES if p in low]
    if ep:
        score += 0.6
        why.append(f"conclusive ending (\"{ep[0]}\")")
    if len(_tokens(text)) <= 3:
        score -= 0.3
    return score, why


def _dur_fit(d: float) -> float:
    if SRC_IDEAL_MIN <= d <= SRC_IDEAL_MAX:
        return 1.0
    if d < SRC_IDEAL_MIN:
        return max(0.0, (d - SRC_MIN) / (SRC_IDEAL_MIN - SRC_MIN)) * 0.75
    return max(0.0, 1 - (d - SRC_IDEAL_MAX) / (SRC_MAX - SRC_IDEAL_MAX))


def _scene_rate(scenes: list[float], s: float, e: float) -> float:
    n = sum(1 for t in scenes if s <= t <= e)
    return n / max((e - s) / 60, 0.1)


_CONTENT_STOP = set("""about after again against also because been before being between both could does doing
down during each from further have having here into itself just more most much must only other over same
should some such than that their them then there these they this those through under until very were what
when where which while will with would your said like even ever still every many first""".split())


def _content_words(text: str) -> Counter:
    return Counter(t for t in _tokens(text) if len(t) > 3 and t not in _CONTENT_STOP)


def _cos(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    num = sum(a[k] * b[k] for k in a.keys() & b.keys())
    return num / (math.sqrt(sum(v * v for v in a.values())) * math.sqrt(sum(v * v for v in b.values())))


def boundary_strengths(sents: list[dict], scenes: list[float]) -> list[float]:
    """b[k] = how strongly a new story/section starts at sentence k (0..1).

    Combines a pause in the narration, a visual cut at the same moment, and a
    shift in vocabulary between the sentences before and after (TextTiling-style).
    b[0] and b[n] are 1 (start and end of the film)."""
    n = len(sents)
    bag = [_content_words(s["text"]) for s in sents]
    b = [1.0] * (n + 1)
    for k in range(1, n):
        left = sum((bag[x] for x in range(max(0, k - 3), k)), Counter())
        right = sum((bag[x] for x in range(k, min(n, k + 3))), Counter())
        shift = 1.0 - _cos(left, right)
        gap = sents[k]["s"] - sents[k - 1]["e"]
        g = min(1.0, max(0.0, (gap - 0.3) / 1.5))
        cut = 1.0 if any(sents[k - 1]["e"] - 0.6 <= t <= sents[k]["s"] + 0.6 for t in scenes) else 0.0
        b[k] = round(0.45 * g + 0.25 * cut + 0.30 * shift, 3)
    return b


def score_window(sents, i, j, scenes, avg_scene_rate, total_dur, bounds=None, L=None) -> dict:
    s, e = sents[i]["s"], sents[j]["e"]
    text = " ".join(x["text"] for x in sents[i:j + 1])
    toks = _tokens(text)
    nw = max(len(toks), 1)
    hook, hook_why = _hook_score(sents[i]["text"])
    # Strongest hook in the first two sentences counts (titles often come second).
    if j > i:
        h2, w2 = _hook_score(sents[i + 1]["text"])
        if h2 * 0.6 > hook and hook > -0.4:
            hook, hook_why = h2 * 0.6, w2
    end, end_why = _end_score(sents[j]["text"])
    drama_hits = [t for t in toks if t in DRAMA]
    drama = min(1.0, len(drama_hits) / nw * 100 / 6)
    facts = min(1.0, len(_NUM.findall(text)) / nw * 100 / 3)
    names = proper_nouns(sents[i:j + 1])
    name_d = min(1.0, len(names) / nw * 100 / 6)
    pacing = min(1.0, _scene_rate(scenes, s, e) / max(avg_scene_rate, 0.5)) if scenes else 0.5
    low = text.lower()
    promo = sum(1 for p in PROMO if p in low)
    if bounds is None:
        bounds = [1.0] * (len(sents) + 1)
    edge_in, edge_out = bounds[i], bounds[j + 1]
    boundary = (edge_in + edge_out) / 2
    # A window must not swallow a section break stronger than its own edges:
    # that would glue two stories together or cut one in half.
    internal = max(bounds[i + 1:j + 1], default=0.0)
    crossing = max(0.0, internal - min(edge_in, edge_out))
    durfit = _dur_fit(e - s) if L is None else _dur_fit_l(e - s, L)
    score = (1.2 * hook + 1.0 * end + 1.0 * drama + 0.5 * facts + 0.35 * name_d +
             0.3 * pacing + 1.1 * durfit + 1.2 * boundary - 2.5 * crossing - 1.5 * promo)
    if s < 15:
        score -= 0.4  # cold opens usually tease the whole video, not one story
    return {"i": i, "j": j, "start": s, "end": e, "score": round(score, 3), "names": names,
            "drama_words": list(dict.fromkeys(drama_hits))[:6],
            "parts": {"hook": round(hook, 2), "ending": round(end, 2), "drama": round(drama, 2),
                      "facts": round(facts, 2), "names": round(name_d, 2), "pacing": round(pacing, 2),
                      "duration_fit": round(durfit, 2), "boundary": round(boundary, 2),
                      "crosses_section": round(crossing, 2), "promo": promo},
            "hook_why": hook_why, "end_why": end_why}


def target_clip_count(duration: float) -> int:
    if duration < 8 * 60:
        return 3
    if duration < 20 * 60:
        return 4
    return 5


def _overlap(a, b) -> float:
    inter = max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))
    return inter / max(1e-6, min(a["end"] - a["start"], b["end"] - b["start"]))


def _same_story(a_names, b_names) -> bool:
    A = {n for n, c in Counter(a_names).most_common(4)}
    B = {n for n, c in Counter(b_names).most_common(4)}
    if len(A) < 2 or len(B) < 2:
        return False
    return len(A & B) / len(A | B) >= 0.6


# --------------------------------------------------------------------------- offline engine

def _title_from(sents, i, j) -> str:
    best, best_s = sents[i]["text"], -9.0
    for k in range(i, min(j + 1, i + 6)):
        sc, _ = _hook_score(sents[k]["text"])
        sc -= 0.1 * (k - i)
        if sc > best_s:
            best, best_s = sents[k]["text"], sc
    t = best.strip()
    if len(t) > 70:
        cut = max(t.rfind(",", 0, 70), t.rfind(" - ", 0, 70), t.rfind(";", 0, 70))
        t = t[:cut] if cut > 25 else t[:67].rsplit(" ", 1)[0] + "…"
    return t.rstrip(",;:- ")


_GENERIC_NAMES = set("""western eastern northern southern major minor christian christians catholic catholics
protestant protestants empire roman romans egyptian egyptians greek greeks army navy king queen emperor
general captain lord great old new first second third last day days year years century bc ad""".split())


def name_phrases(sents: list[dict]) -> Counter:
    """Capitalised name phrases ("Golden Horn", "Constantinople"), skipping sentence-initial words."""
    found: Counter = Counter()
    for s in sents:
        toks = re.findall(r"[A-Za-z][A-Za-z'\-]*|[.,;:!?]", s["text"])
        cur: list[str] = []
        for k, t in enumerate(toks + ["."]):
            cap = t[:1].isupper() and t not in STOP and (k > 0 or (k + 1 < len(toks) and toks[k + 1][:1].isupper()))
            if cap:
                cur.append(t)
                continue
            if cur:
                words = cur if len(cur) > 1 else [w for w in cur if w.lower() not in _GENERIC_NAMES]
                if words and not (len(words) == 1 and len(words[0]) < 4):
                    found[" ".join(words)] += 1
                cur = []
    return found


def _hashtags(names: list[str], drama: list[str], phrases: Counter | None = None, text: str = "") -> list[str]:
    """#shorts, any hashtags you set in .env, then the clip's own names and topics."""
    tags = ["#shorts"] + config.EXTRA_HASHTAGS
    ranked = sorted((phrases or Counter(names)).items(), key=lambda kv: (-kv[1], -len(kv[0])))
    for n, _ in ranked[:4]:
        tag = "#" + re.sub(r"[^A-Za-z0-9]", "", n)
        if len(tag) > 3 and tag.lower() not in {t.lower() for t in tags}:
            tags.append(tag)
    for t in ("#explained", "#didyouknow"):
        if t not in tags:
            tags.append(t)
    return tags[:9]


def _explain(c) -> str:
    p = c["parts"]
    bits = []
    if c["hook_why"]:
        bits.append("Strong opening: " + "; ".join(c["hook_why"][:2]))
    if p["drama"] >= 0.4 and c["drama_words"]:
        bits.append("dense with dramatic events (" + ", ".join(c["drama_words"][:4]) + ")")
    if p["facts"] >= 0.4:
        bits.append("rich in concrete dates and numbers")
    if c["end_why"]:
        bits.append("ends cleanly: " + c["end_why"][0])
    elif p["ending"] > 0:
        bits.append("ends on a complete sentence")
    if p["boundary"] >= 0.5:
        bits.append("starts and ends exactly where this story begins and ends in the documentary")
    if p["pacing"] >= 0.8:
        bits.append("visually busy section with frequent scene changes")
    bits.append(f"{c['end']-c['start']:.0f}s self-contained segment")
    s = "; ".join(bits)
    return s[0].upper() + s[1:] + "."


def _pct(score: float, ranked: list[dict]) -> int:
    """Show offline scores on a 0-100 scale relative to the best story found."""
    hi = max(c["score"] for c in ranked)
    lo = min(0.0, min(c["score"] for c in ranked))
    return int(round(40 + 58 * (score - lo) / max(1e-6, hi - lo)))


def offline_select(sents, scenes, total_dur, progress=None, L=None, want=None) -> dict:
    L = L or DOC_LIMITS
    avg_rate = len(scenes) / max(total_dur / 60, 1) if scenes else 0
    bounds = boundary_strengths(sents, scenes)
    cands = []
    for i in range(len(sents)):
        for j in range(i, len(sents)):
            d = sents[j]["e"] - sents[i]["s"]
            if d > L.src_max:
                break
            if d < L.src_min:
                continue
            cands.append(score_window(sents, i, j, scenes, avg_rate, total_dur, bounds, L))
        if progress and i % 25 == 0:
            progress(i / max(len(sents), 1))
    cands.sort(key=lambda c: -c["score"])
    want = want or target_clip_count(total_dur)
    chosen: list[dict] = []
    ranked: list[dict] = []
    for c in cands:
        if any(_overlap(c, x) > 0 for x in ranked):
            continue
        if any(_same_story(c["names"], x["names"]) for x in ranked):
            continue
        ranked.append(c)
        if len(ranked) >= max(10, want * 2):
            break
    if not ranked:
        return {"candidates": [], "clips": []}
    best = ranked[0]["score"]
    for c in ranked:
        if len(chosen) >= want:
            break
        if len(chosen) >= config.MIN_CLIPS and c["score"] < 0.55 * best:
            break
        chosen.append(c)
    clips, cand_out = [], []
    for rank, c in enumerate(ranked):
        sel = c in chosen
        title = _title_from(sents, c["i"], c["j"])
        entry = {"rank": rank + 1, "start_sentence": c["i"], "end_sentence": c["j"],
                 "start": c["start"], "end": c["end"], "title": title,
                 "score": _pct(c["score"], ranked),
                 "selected": sel, "summary": _explain(c), "parts": c["parts"]}
        cand_out.append(entry)
        if sel:
            first = sents[c["i"]]["text"]
            second = sents[c["i"] + 1]["text"] if c["j"] > c["i"] else ""
            desc = (first + " " + second).strip()
            if len(desc) > 220:
                desc = desc[:217].rsplit(" ", 1)[0] + "…"
            tags = _hashtags(c["names"], c["drama_words"], name_phrases(sents[c["i"]:c["j"] + 1]),
                             " ".join(x["text"] for x in sents[c["i"]:c["j"] + 1]))
            kw = [n for n, _ in Counter(c["names"]).most_common(6)] + c["drama_words"][:4]
            clips.append({"start_sentence": c["i"], "end_sentence": c["j"], "title": title,
                          "description": desc,
                          "hashtags": tags, "why_selected": _explain(c), "keywords": kw,
                          "scores": c["parts"], "rank": rank + 1})
    clips.sort(key=lambda x: x["start_sentence"])
    return {"candidates": cand_out, "clips": clips}


# --------------------------------------------------------------------------- limits per mode

@dataclass(frozen=True)
class Limits:
    """Source-time limits for one clip (seconds of the original video)."""
    src_min: float
    ideal_min: float
    ideal_max: float
    src_max: float


DOC_LIMITS = Limits(SRC_MIN, SRC_IDEAL_MIN, SRC_IDEAL_MAX, SRC_MAX)
STREAM_LIMITS = Limits(15.0, 22.0, 55.0, 75.0)


def limits_for(mode: str, rules: dict | None = None) -> Limits:
    """Mode defaults, tightened by a campaign brief's minimum/maximum length.
    Streams keep their speed; documentaries are sped up ~1.1x, so their limits are in source time."""
    base = STREAM_LIMITS if mode == "stream" else DOC_LIMITS
    if not rules:
        return base
    speed = 1.0 if mode == "stream" else 1.1
    lo, ideal_lo, ideal_hi, hi = base.src_min, base.ideal_min, base.ideal_max, base.src_max
    if rules.get("max_seconds"):
        cap = rules["max_seconds"] * speed - 1.0      # leave a second of headroom
        hi = max(8.0, min(hi, cap))
        ideal_hi = min(ideal_hi, hi - 2)
    if rules.get("min_seconds"):
        floor = rules["min_seconds"] * speed + 1.0
        lo = max(lo, min(floor, hi - 2))
        ideal_lo = max(ideal_lo, lo)
    ideal_lo = min(ideal_lo, ideal_hi)
    return Limits(round(lo, 1), round(ideal_lo, 1), round(max(ideal_hi, ideal_lo), 1), round(hi, 1))


def _dur_fit_l(d: float, L: Limits) -> float:
    if L.ideal_min <= d <= L.ideal_max:
        return 1.0
    if d < L.ideal_min:
        return max(0.0, (d - L.src_min) / max(1e-6, L.ideal_min - L.src_min)) * 0.75
    return max(0.0, 1 - (d - L.ideal_max) / max(1e-6, L.src_max - L.ideal_max))


def clip_count(duration: float, mode: str, requested: int = 0) -> int:
    """How many clips to make. Streams: about one per 8 minutes of video, 3 to 30."""
    if requested:
        return max(1, min(requested, config.STREAM_MAX_CLIPS if mode == "stream" else 10))
    if mode == "stream":
        return int(max(config.STREAM_MIN_CLIPS, min(config.STREAM_MAX_CLIPS, round(duration / 60 / 8))))
    return target_clip_count(duration)


# --------------------------------------------------------------------------- offline engine: streams

HYPE_PHRASES = ["no way", "oh my god", "oh my gosh", "what the", "let's go", "lets go", "are you serious",
                "are you kidding", "bro", "dude", "insane", "crazy", "holy", "wait wait", "wait what", "yo ",
                "i can't believe", "i cannot believe", "shut up", "stop it", "no no no", "oh no", "what?!",
                "chat", "clip that", "clip it", "that's crazy", "that's insane", "unbelievable", "hilarious",
                "i'm dead", "i'm crying", "dying", "screaming", "goat", "w ", " l ", "ratio", "real talk",
                "hot take", "honestly", "i swear", "fight me", "admit it", "tell me", "the truth is"]
LAUGH = re.compile(r"\b(ha(ha)+|he(he)+|lol|lmao|lmfao|laugh(s|ing)?|giggl\w*)\b", re.I)
STREAM_PROMO = ["subscribe", "follow the channel", "turn on notifications", "thanks for the sub", "thank you for the sub",
                "gifted", "bits", "donation", "donated", "prime", "sponsor", "sponsored", "use code", "link in",
                "be right back", "brb", "starting soon", "technical difficulties", "can you hear me",
                "is my mic", "my audio", "stream is lagging", "let me check chat"]


def _sentence_features(sents, energy) -> list[dict]:
    feats = []
    for s in sents:
        low = " " + s["text"].lower() + " "
        toks = _tokens(s["text"])
        hype = sum(1 for p in HYPE_PHRASES if p in low) + len(LAUGH.findall(low)) + s["text"].count("!")
        caps = sum(1 for w in re.findall(r"[A-Za-z]{3,}", s["text"]) if w.isupper())
        feats.append({
            "hype": hype + 0.5 * caps,
            "laugh": bool(LAUGH.search(low)),
            "q": s["text"].rstrip().endswith("?"),
            "z": peak_energy(energy, s["s"], s["e"]),
            "dangling": bool(toks) and toks[0] in DANGLING,
            "promo": sum(1 for p in STREAM_PROMO if p in low),
            "nw": max(1, len(toks)),
        })
    return feats


def offline_stream_select(sents, energy, total_dur, L: Limits, want: int, progress=None) -> dict:
    """Free scorer for streams/podcasts: loud reactions, laughter, hype language,
    a clean opening and a payoff near the end. Uses prefix sums so a 4-hour VOD
    (thousands of sentences) still scores in seconds."""
    n = len(sents)
    if n == 0:
        return {"candidates": [], "clips": []}
    f = _sentence_features(sents, energy)
    gaps_before = [9.0] + [max(0.0, sents[k]["s"] - sents[k - 1]["e"]) for k in range(1, n)]
    gaps_after = gaps_before[1:] + [9.0]
    cands = []
    for i in range(n):
        if f[i]["promo"]:
            continue
        hook = (0.0 if not f[i]["dangling"] else -0.8) + (0.5 if f[i]["q"] else 0.0) \
            + min(0.8, 0.3 * f[i]["hype"]) + min(0.6, max(0.0, f[i]["z"]) * 0.15) + (0.3 if f[i]["laugh"] else 0)
        if i + 1 < n:
            hook = max(hook, 0.7 * (min(0.8, 0.3 * f[i + 1]["hype"]) + min(0.6, max(0.0, f[i + 1]["z"]) * 0.15)) - 0.2)
        hype = words = promo = laughs = 0.0
        peak, peak_at = -9.0, i
        for j in range(i, n):
            d = sents[j]["e"] - sents[i]["s"]
            if d > L.src_max:
                break
            hype += f[j]["hype"]
            words += f[j]["nw"]
            promo += f[j]["promo"]
            laughs += f[j]["laugh"]
            if f[j]["z"] > peak:
                peak, peak_at = f[j]["z"], j
            if d < L.src_min:
                continue
            # Payoff: the loudest moment should land in the back half, with the clip ending soon after.
            pos = (sents[peak_at]["s"] - sents[i]["s"]) / max(d, 1)
            tail = sents[j]["e"] - sents[peak_at]["e"]
            payoff = (0.6 if pos >= 0.35 else 0.1) + (0.4 if tail <= 8 else 0.0) if peak >= 2.0 else 0.0
            ending = (0.3 if sents[j]["text"].rstrip().endswith((".", "!", "?")) else -0.2) \
                + min(0.4, gaps_after[j] / 2.5)
            start_gap = min(0.4, gaps_before[i] / 2.5)
            density = min(1.2, hype / words * 100 / 4)
            score = (1.1 * hook + 1.2 * min(1.6, max(0.0, peak) / 3.0) + 1.0 * density + 0.4 * min(2, laughs)
                     + 0.9 * payoff + 0.6 * ending + 0.5 * start_gap + 1.0 * _dur_fit_l(d, L) - 1.6 * promo)
            cands.append({"i": i, "j": j, "start": sents[i]["s"], "end": sents[j]["e"], "score": round(score, 3),
                          "peak": round(peak, 2), "peak_t": sents[peak_at]["s"], "hype": round(hype, 1),
                          "laughs": int(laughs), "q": f[i]["q"],
                          "parts": {"hook": round(hook, 2), "energy_peak": round(peak, 2),
                                    "hype_density": round(density, 2), "payoff": round(payoff, 2),
                                    "ending": round(ending, 2), "duration_fit": round(_dur_fit_l(d, L), 2)}})
        if progress and i % 200 == 0:
            progress(i / n)
    cands.sort(key=lambda c: -c["score"])
    ranked: list[dict] = []
    limit = max(want * 2, 10)
    for c in cands:
        if any(_overlap(c, x) > 0 for x in ranked):
            continue
        ranked.append(c)
        if len(ranked) >= limit:
            break
    if not ranked:
        return {"candidates": [], "clips": []}
    chosen = ranked[:want]
    clips, cand_out = [], []
    for rank, c in enumerate(ranked):
        sel = c in chosen
        title = _stream_title(sents, c["i"], c["j"], f)
        why = _explain_stream(c)
        cand_out.append({"rank": rank + 1, "start_sentence": c["i"], "end_sentence": c["j"], "start": c["start"],
                         "end": c["end"], "title": title, "score": _pct(c["score"], ranked), "selected": sel,
                         "summary": why, "parts": c["parts"]})
        if sel:
            seg = sents[c["i"]:c["j"] + 1]
            desc = _stream_caption(seg, f, c, len(clips))
            names = [n for n, _ in sorted(name_phrases(seg).items(), key=lambda kv: -kv[1])[:3]]
            tags = ["#shorts", "#clips"] + config.EXTRA_HASHTAGS + ["#" + re.sub(r"[^A-Za-z0-9]", "", nm) for nm in names
                                            if len(re.sub(r"[^A-Za-z0-9]", "", nm)) > 2] + ["#streamerclips"]
            clips.append({"start_sentence": c["i"], "end_sentence": c["j"], "title": title, "description": desc,
                          "hashtags": list(dict.fromkeys(tags))[:8], "why_selected": why,
                          "keywords": names, "scores": c["parts"], "rank": rank + 1})
    clips.sort(key=lambda x: x["start_sentence"])
    return {"candidates": cand_out, "clips": clips}


_CTA_LAUGH = ["I can't stop replaying this 😭", "the timing on this 😭", "how is this real 💀"]
_CTA_Q = ["What would you have said?", "Who's right here?", "Be honest, would you do it?"]
_CTA_HYPE = ["Thoughts? 👀", "This one went crazy", "Wait for it…"]


def _stream_caption(seg, f, c, k: int) -> str:
    """Free-scorer caption: the clip's strongest line in quotes, then a comment-driving line."""
    i0 = c["i"]
    best = max(range(len(seg)), key=lambda n: f[i0 + n]["hype"] + max(0, f[i0 + n]["z"]) * 0.4
               - (2.0 if f[i0 + n]["nw"] < 4 else 0) - 0.03 * n)
    line = seg[best]["text"].strip().strip("\"'")
    if len(line) > 120:
        line = line[:117].rsplit(" ", 1)[0] + "…"
    pool = _CTA_LAUGH if c["laughs"] else _CTA_Q if c["q"] or any(s["text"].endswith("?") for s in seg) else _CTA_HYPE
    return f'"{line}" {pool[k % len(pool)]}'


def _stream_title(sents, i, j, f) -> str:
    best = max(range(i, min(j + 1, i + 8)), key=lambda k: f[k]["hype"] + max(0, f[k]["z"]) * 0.4 - 0.05 * (k - i)
               - (1.0 if f[k]["dangling"] else 0) - (2.0 if f[k]["nw"] < 4 else 0))
    t = sents[best]["text"].strip().strip("\"'")
    if len(t) > 60:
        cut = max(t.rfind(",", 0, 60), t.rfind(" - ", 0, 60), t.rfind(";", 0, 60))
        t = t[:cut] if cut > 20 else t[:57].rsplit(" ", 1)[0] + "…"
    t = t.rstrip(",;:- ")
    return t[:1].upper() + t[1:]


def _explain_stream(c) -> str:
    bits = []
    if c["peak"] >= 2.0:
        bits.append(f"big audio spike (+{c['peak']:.1f}) at {fmt_ts(c['peak_t'])}, likely a reaction or punchline")
    if c["laughs"]:
        bits.append("laughter in the clip")
    if c["hype"] >= 3:
        bits.append("lots of hype language")
    if c["q"]:
        bits.append("opens on a question")
    if c["parts"]["payoff"] >= 0.6:
        bits.append("builds up to the peak and ends soon after it")
    bits.append(f"{c['end'] - c['start']:.0f}s self-contained moment")
    s = "; ".join(bits)
    return s[0].upper() + s[1:] + "."


# --------------------------------------------------------------------------- Claude engine

def _brief_schema() -> dict:
    lst = {"type": "array", "items": {"type": "string"}}
    return {"type": "object",
            "properties": {"required_hashtags": lst, "required_mentions": lst, "required_phrases": lst,
                           "min_seconds": {"type": "integer"}, "max_seconds": {"type": "integer"},
                           "other_rules": lst},
            "required": ["required_hashtags", "required_mentions", "required_phrases", "min_seconds",
                         "max_seconds", "other_rules"],
            "additionalProperties": False}


DOC_SCORES = ("storytelling", "significance", "curiosity", "pacing", "standalone_value")
STREAM_SCORES = ("hook", "payoff", "entertainment", "energy", "standalone_value")


def _schema(mode: str) -> dict:
    keys = STREAM_SCORES if mode == "stream" else DOC_SCORES
    cand_props = {"start_sentence": {"type": "integer"}, "end_sentence": {"type": "integer"},
                  "working_title": {"type": "string"}}
    cand_props.update({k: {"type": "integer"} for k in keys})
    cand_props.update({"overall": {"type": "integer"}, "assessment": {"type": "string"}})
    return {
        "type": "object",
        "properties": {
            "documentary_summary": {"type": "string"},
            "candidates": {"type": "array", "items": {
                "type": "object", "properties": cand_props, "required": list(cand_props),
                "additionalProperties": False}},
            "selected": {"type": "array", "items": {
                "type": "object",
                "properties": {"candidate_index": {"type": "integer"}, "title": {"type": "string"},
                               "post_title": {"type": "string"},
                               "description": {"type": "string"},
                               "search_tags": {"type": "array", "items": {"type": "string"}},
                               "hashtags": {"type": "array", "items": {"type": "string"}},
                               "why_selected": {"type": "string"},
                               "highlight_words": {"type": "array", "items": {"type": "string"}}},
                "required": ["candidate_index", "title", "post_title", "description", "hashtags",
                             "search_tags", "why_selected", "highlight_words"],
                "additionalProperties": False}},
            "brief_rules": _brief_schema(),
        },
        "required": ["documentary_summary", "candidates", "selected", "brief_rules"],
        "additionalProperties": False,
    }


_SCHEMA = _schema("documentary")  # kept for anything importing the old name


def _brief_section(brief_text: str) -> str:
    if not brief_text.strip():
        return ("\nNo campaign brief was given: return brief_rules with empty lists and 0 for "
                "min_seconds/max_seconds.")
    return f"""

CAMPAIGN BRIEF. These clips are submitted to a paid campaign; a clip that breaks its rules is rejected
and earns nothing. Follow every rule. Skip moments the brief forbids, write captions and hashtags the
brief asks for, and respect any length limits. Also copy the brief's checkable rules into brief_rules:
required hashtags (with #), required @mentions, exact phrases the caption must contain, min/max clip
length in seconds (0 if not stated), and every other rule as a short sentence in other_rules.
<brief>
{brief_text.strip()[:6000]}
</brief>"""


def _system(mode: str, L: Limits, want: int, brief_text: str, history: str) -> str:
    n_cands = min(40, max(6, int(want * 1.6) + 2))
    if mode == "stream":
        base = f"""You are the short-form clip editor for a clipper who posts clips of streamers, podcasts and
influencers to TikTok, Instagram Reels and YouTube Shorts and is paid per view.
You receive the timestamped transcript of a long video (a stream VOD, podcast or vlog), split into numbered
sentences. A line may end with [LOUD +z]: that moment is much louder than the audio around it
(shouting, laughter, hype, a crowd or chat going wild). Treat it as a strong hint, not a rule.

Each clip is a contiguous range of sentences [start_sentence, end_sentence] (inclusive), used exactly as
spoken - you cannot reorder or rewrite speech.

A clip that goes viral:
- hooks in the first two seconds: a bold claim, a question, a strong reaction, conflict or a funny line.
  Never start mid-thought or on filler ("so yeah", "anyway", "um");
- is one moment a stranger understands with no context;
- builds to a payoff - the punchline, the reaction, the reveal, the argument's peak - and ends right after it
  instead of trailing off;
- runs {L.ideal_min:.0f}-{L.ideal_max:.0f} seconds. Down to {L.src_min:.0f}s is fine for a tight moment; never exceed {L.src_max:.0f}s.
Prefer genuinely funny moments, real reactions, hot takes, arguments, stories with a punchline,
surprising facts, wholesome or emotional moments.
Avoid: reading donations or alerts, sponsor reads, "be right back", tech problems, setup with no payoff,
moments that only work if you can see the screen, and anything hateful, harassing or sexual.

Process: read the whole transcript first. List {n_cands} candidates from across the entire video, each scored
0-10 on hook, payoff, entertainment, energy and standalone_value, with an overall 0-100 and a one-sentence
assessment. Then select the best {want} (by candidate_index) - fewer only if the rest would be weak.
Selected clips must not overlap and must not repeat the same moment.

For each selected clip write everything needed to post it, in the voice of a top clip account:
- title: max 60 characters. It is burned onto the video as the hook text, so make it punchy and
  curiosity-driven, no hashtags, no emoji, and nothing the clip does not deliver;
- post_title: the title shown in the feed and on YouTube, max 90 characters. Specific and curious
  ("Streamer finds out chat was lying the whole time"), not generic ("Funny moment"). Name the creator
  when the transcript or brief makes clear who it is - names are what people search. At most one emoji,
  no hashtags, no ALL-CAPS sentences, nothing the clip does not deliver;
- description: the post caption, 1-2 short sentences in a casual, native social-media voice that makes
  people comment - end with a question or a bold take. 0-2 emoji. No hashtags here;
- hashtags: 3-6 hashtags, most important first: the creator's name if known, the game/show/topic,
  then broader ones, including #shorts (plus any the brief requires);
- search_tags: 5-12 plain YouTube search tags without # (creator, game/show, topic, what happens);
- why_selected: 1-2 sentences on why this clip will hold viewers;
- highlight_words: 3-8 key words that appear verbatim in that clip's speech (names, numbers, punchline words).
Accuracy: describe only what is actually said in the clip. Never invent claims about real people.
Use documentary_summary for a 1-2 sentence summary of the whole video."""
    else:
        base = f"""You are the short-form editor for a narrated, animated explainer YouTube channel that covers any topic.
You receive the complete timestamped narration of one finished long-form video, split into
numbered sentences, plus a per-minute visual-pacing signal (scene changes per minute).

Your job: find the stories in this video that would make the best standalone YouTube Shorts /
TikToks / Reels, then pick the final set.

Each short is a contiguous range of sentences [start_sentence, end_sentence] (inclusive), used exactly as
narrated - you cannot reorder, rewrite or add narration.

A great short:
- opens with a compelling first sentence that makes sense to a viewer who has seen nothing else
  (no dangling "he"/"this"/"but" that refers to something earlier, unless it is obvious from the sentence);
- tells one complete idea or story: a revelation, dramatic event, unexpected fact,
  turning point or curiosity-driven story;
- ends on a satisfying, conclusive sentence (a payoff or resolution, not a cliffhanger into the next chapter);
- runs {L.ideal_min:.0f}-{L.ideal_max:.0f} seconds of source narration (it will be sped up ~1.1x).
  Shorter (down to {L.src_min:.0f}s) is acceptable only when the story is genuinely complete and strong. Never exceed {L.src_max:.0f}s.
Avoid: channel intros/outros, sponsor reads, calls to subscribe, recaps, and sections that only make sense
in context. Selected shorts must not overlap and must not tell the same story twice.

Process: read the whole transcript first. Then list {n_cands} candidate segments drawn from across the entire
video (not just the beginning), each scored 0-10 on storytelling, significance (how important or surprising the
idea is), curiosity, pacing and standalone_value, with an overall
0-100 and a one-sentence assessment. Then select the best {want} candidates (by candidate_index into your
list) - choose fewer only if the extra ones would be weak.

For each selected short write everything needed to post it:
- title: a scroll-stopping title, max 70 characters, no clickbait that the clip does not pay off.
  It is burned onto the video;
- post_title: the upload title, max 90 characters: specific and curiosity-driven, can differ from title;
- description: the post caption, 1-3 sentences that tease the payoff and end with a question that
  invites comments. 0-2 emoji, no hashtags here;
- search_tags: 5-12 plain YouTube search tags without # (people, places, the topic, key terms);
- hashtags: 5-9 hashtags including #shorts{(" " + " ".join(config.EXTRA_HASHTAGS)) if config.EXTRA_HASHTAGS else ""} and topic tags;
- why_selected: 1-2 sentences explaining why this works as a standalone short;
- highlight_words: 4-12 important words that appear verbatim in that segment's narration
  (names, places, dates, numbers, key nouns) to colour in the captions.

Strict accuracy rule: titles, descriptions and hashtags must only state facts that the narration in that
segment actually says. Do not add outside facts, dates, or claims. If unsure, stay general."""
    return base + _brief_section(brief_text) + (f"\n\n{history}" if history else "")


def _chunks(sents, total_dur: float, minutes: float, overlap: float = 90.0) -> list[tuple[int, int]]:
    """Sentence index ranges [a, b) covering the video in overlapping time windows."""
    if total_dur <= minutes * 60 * 1.25 or not sents:
        return [(0, len(sents))]
    out, t = [], 0.0
    step = minutes * 60
    while t < total_dur:
        lo, hi = t - (overlap if t else 0), t + step + overlap
        a = next((k for k, s in enumerate(sents) if s["e"] >= lo), len(sents))
        b = next((k for k, s in enumerate(sents) if s["s"] > hi), len(sents))
        if b > a:
            out.append((a, b))
        t += step
    return out


def _call_claude(client, model: str, system: str, user: str, schema: dict) -> dict:
    with client.beta.messages.stream(
        model=model,
        max_tokens=32000,
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": config.CLAUDE_EFFORT,
                       "format": {"type": "json_schema", "schema": schema}},
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
    ) as stream:
        resp = stream.get_final_message()
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined to analyse this transcript.")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Claude's answer was cut off (max_tokens).")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    data = json.loads(text)
    usage = getattr(resp, "usage", None)
    data["_usage"] = {"input_tokens": getattr(usage, "input_tokens", None) or 0,
                      "output_tokens": getattr(usage, "output_tokens", None) or 0}
    return data


def claude_select(sents, scenes, total_dur, title_hint: str, progress=None, *, mode: str = "documentary",
                  model: str | None = None, L: Limits = DOC_LIMITS, want: int = 5, energy: dict | None = None,
                  brief_text: str = "", log: Callable[[str], None] = print, client=None) -> dict:
    """Ask Claude for candidates and picks. Long videos are sent in overlapping chunks;
    a failed chunk is logged and skipped (the offline scorer fills the gap later)."""
    if client is None:
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY or None, max_retries=3)
    model = model or config.CLAUDE_MODEL
    history = results.prompt_block(mode)
    schema = _schema(mode)

    def line(s):
        z = peak_energy(energy, s["s"], s["e"]) if mode == "stream" else 0.0
        loud = f" [LOUD +{z:.1f}]" if z >= 2.0 else ""
        return f"[{s['i']}] ({fmt_ts(s['s'])}-{fmt_ts(s['e'])}, {s['e']-s['s']:.1f}s) {s['text']}{loud}"

    pieces = _chunks(sents, total_dur, config.CLAUDE_CHUNK_MINUTES)
    merged = {"documentary_summary": "", "candidates": [], "selected": [], "brief_rules": None,
              "_usage": {"input_tokens": 0, "output_tokens": 0, "cost": 0.0}, "_failed_chunks": 0,
              "_chunks": len(pieces)}
    for k, (a, b) in enumerate(pieces):
        part = sents[a:b]
        span = part[-1]["e"] - part[0]["s"]
        want_k = want if len(pieces) == 1 else max(2, math.ceil(want * span / max(total_dur, 1)) + 1)
        system = _system(mode, L, want_k, brief_text, history)
        head = f"Video file: {title_hint}\nDuration: {fmt_ts(total_dur)}\n"
        if len(pieces) > 1:
            head += (f"This is part {k + 1} of {len(pieces)} of the transcript, covering "
                     f"{fmt_ts(part[0]['s'])}-{fmt_ts(part[-1]['e'])}. Pick only from this part.\n")
        if mode != "stream" and scenes:
            mins = int(math.ceil(total_dur / 60))
            rate = [0] * mins
            for t in scenes:
                rate[min(mins - 1, int(t // 60))] += 1
            head += "Scene changes per minute (minute:count): " + ", ".join(f"{m}:{rate[m]}" for m in range(mins)) + "\n"
        user = head + f"\nTranscript ({len(part)} sentences):\n" + "\n".join(line(s) for s in part)
        if progress:
            progress(0.05 + 0.85 * k / len(pieces),
                     f"Claude ({model}) is reading" + (f" part {k + 1} of {len(pieces)}" if len(pieces) > 1 else
                                                      " the full transcript") + "…")
        try:
            data = _call_claude(client, model, system, user, schema)
        except Exception as e:
            if len(pieces) == 1:
                raise
            merged["_failed_chunks"] += 1
            log(f"Claude failed on part {k + 1} of {len(pieces)} ({type(e).__name__}: {e}); skipping it.")
            continue
        off = len(merged["candidates"])
        merged["candidates"] += data.get("candidates", [])
        merged["selected"] += [{**s, "candidate_index": int(s["candidate_index"]) + off}
                               for s in data.get("selected", [])]
        merged["documentary_summary"] = (merged["documentary_summary"] + " " + data.get("documentary_summary", "")).strip()
        merged["brief_rules"] = brief.merge(merged["brief_rules"], data.get("brief_rules"))
        u = data["_usage"]
        merged["_usage"]["input_tokens"] += u["input_tokens"]
        merged["_usage"]["output_tokens"] += u["output_tokens"]
        merged["_usage"]["cost"] += config.estimate_cost(model, u["input_tokens"], u["output_tokens"]) or 0.0
    if len(merged["candidates"]) == 0:
        raise RuntimeError("Claude returned no candidates for any part of the video.")
    # Best picks first, so overlapping picks from neighbouring chunks keep the stronger one.
    score = {k: c.get("overall", 0) for k, c in enumerate(merged["candidates"])}
    merged["selected"].sort(key=lambda s: -score.get(s["candidate_index"], 0))
    merged["_usage"]["cost"] = round(merged["_usage"]["cost"], 4)
    return merged


def _snap_claude(sents, data, L: Limits = DOC_LIMITS, max_clips: int | None = None) -> dict:
    """Validate Claude's answer against the real transcript. Never trust indices blindly."""
    max_clips = max_clips or config.MAX_CLIPS
    n = len(sents)
    cands = data.get("candidates", [])
    cand_out = []
    for k, c in enumerate(cands):
        try:
            i, j = int(c["start_sentence"]), int(c["end_sentence"])
        except (KeyError, TypeError, ValueError):
            continue
        if not (0 <= i < n and 0 <= j < n) or j < i:
            continue
        # Trim from the end until within the hard maximum.
        while j > i and sents[j]["e"] - sents[i]["s"] > L.src_max:
            j -= 1
        parts = {key: v for key, v in c.items()
                 if isinstance(v, int) and key not in ("start_sentence", "end_sentence", "overall")}
        cand_out.append({"_k": k, "start_sentence": i, "end_sentence": j, "start": sents[i]["s"],
                         "end": sents[j]["e"], "title": c.get("working_title", ""),
                         "score": max(0, min(100, int(c.get("overall", 0)))), "selected": False,
                         "summary": c.get("assessment", ""), "parts": parts})
    by_k = {c["_k"]: c for c in cand_out}
    clips = []
    for sel in data.get("selected", []):
        c = by_k.get(int(sel["candidate_index"]))
        if c is None or c["selected"] or c["end"] - c["start"] < L.src_min - 3:
            continue
        clash = any(_overlap(c, x) > 0.15 for x in clips)
        if clash or len(clips) >= max_clips:
            continue
        c["selected"] = True
        seg_text = " ".join(s["text"] for s in sents[c["start_sentence"]:c["end_sentence"] + 1]).lower()
        hl = [w for w in sel.get("highlight_words", []) if w and w.lower() in seg_text]
        tags = []
        for t in sel.get("hashtags", []):
            t = "#" + re.sub(r"[^A-Za-z0-9_]", "", t.lstrip("#"))
            if len(t) > 1 and t.lower() not in {x.lower() for x in tags}:
                tags.append(t)
        clips.append({"start_sentence": c["start_sentence"], "end_sentence": c["end_sentence"],
                      "start": c["start"], "end": c["end"], "title": sel["title"].strip()[:100],
                      "post_title": (sel.get("post_title") or sel["title"]).strip()[:100],
                      "search_tags": [t.strip().lstrip("#") for t in sel.get("search_tags", []) if t.strip()][:15],
                      "description": sel["description"].strip(), "hashtags": tags[:10],
                      "why_selected": sel["why_selected"].strip(), "keywords": hl,
                      "scores": c["parts"], "rank": None})
    # Drop duplicate candidates from overlapping chunks (same moment listed twice).
    cand_out.sort(key=lambda c: (not c["selected"], -c["score"]))
    uniq: list[dict] = []
    for c in cand_out:
        if any(_overlap(c, x) > 0.6 for x in uniq):
            continue
        uniq.append(c)
    cand_out = sorted(uniq, key=lambda c: -c["score"])
    for r, c in enumerate(cand_out):
        c["rank"] = r + 1
        c.pop("_k", None)
    for cl in clips:
        cl["rank"] = next((c["rank"] for c in cand_out if c["start_sentence"] == cl["start_sentence"]
                           and c["end_sentence"] == cl["end_sentence"]), None)
    clips.sort(key=lambda x: x["start_sentence"])
    return {"candidates": cand_out, "clips": clips, "summary": data.get("documentary_summary", "")}


# --------------------------------------------------------------------------- entry point

def select_segments(transcript: dict, scenes: list[float], total_dur: float, title_hint: str,
                    progress: Optional[Callable[[float, str], None]] = None,
                    log: Callable[[str], None] = print, settings: dict | None = None,
                    energy: dict | None = None, client=None) -> dict:
    settings = settings or config.default_settings()
    mode = settings.get("mode", "documentary")
    model = settings.get("model") or config.CLAUDE_MODEL
    brief_text = settings.get("brief", "")
    rules = brief.parse(brief_text)
    L = limits_for(mode, rules)
    want = clip_count(total_dur, mode, settings.get("max_clips", 0))
    min_clips = min(want, config.STREAM_MIN_CLIPS if mode == "stream" else config.MIN_CLIPS)
    sents = build_sentences(transcript["words"])
    use_claude = config.ANALYSIS_MODE == "claude" or (config.ANALYSIS_MODE == "auto" and bool(config.ANTHROPIC_API_KEY))
    result = None
    engine = "offline"
    usage = None
    claude_error = None
    top_up_to = min_clips
    if use_claude:
        try:
            raw = claude_select(sents, scenes, total_dur, title_hint, progress, mode=mode, model=model, L=L,
                                want=want, energy=energy, brief_text=brief_text, log=log, client=client)
            rules = brief.merge(rules, raw.get("brief_rules"))
            L = limits_for(mode, rules)
            result = _snap_claude(sents, raw, L, want)
            usage = {**raw["_usage"], "model": model, "chunks": raw["_chunks"]}
            engine = "claude"
            if raw["_failed_chunks"]:
                top_up_to = want
            if len(result["clips"]) < top_up_to:
                log(f"Claude returned {len(result['clips'])} usable clips; topping up with offline analysis.")
        except Exception as e:  # network, quota, bad key... fall back, but tell the user
            if credits.is_credit_error(e):
                claude_error = "credits"
                log("Your Claude credits have run out, so the free offline scorer picked these moments. "
                    "Buy more credits to use Claude again.")
            else:
                claude_error = "other"
                log(f"Claude analysis failed ({type(e).__name__}: {e}). Falling back to offline analysis.")
            result = None
    if result is None or len(result["clips"]) < top_up_to:
        if progress:
            progress(0.3, "Scoring every possible clip window…")
        sub = (lambda f: progress(0.3 + 0.6 * f, "Scoring clip windows…")) if progress else None
        if mode == "stream":
            off = offline_stream_select(sents, energy, total_dur, L, want, progress=sub)
        else:
            off = offline_select(sents, scenes, total_dur, progress=sub, L=L, want=want)
        if result is None:
            result = off
        else:  # top up Claude's picks with non-overlapping offline picks
            for c in off["clips"]:
                if len(result["clips"]) >= top_up_to:
                    break
                cc = {**c, "start": sents[c["start_sentence"]]["s"], "end": sents[c["end_sentence"]]["e"]}
                if not any(_overlap(cc, x) > 0 for x in result["clips"]):
                    result["clips"].append(cc)
            result["clips"].sort(key=lambda x: x["start_sentence"])
            engine = "claude+offline"
    for c in result["clips"]:
        c["start"] = sents[c["start_sentence"]]["s"]
        c["end"] = sents[c["end_sentence"]]["e"]
        c["transcript"] = " ".join(s["text"] for s in sents[c["start_sentence"]:c["end_sentence"] + 1])
        if mode == "stream":
            c["peak_energy"] = round(peak_energy(energy, c["start"], c["end"]), 2)
    result["clips"] = [brief.apply(c, rules) for c in result["clips"]]
    for c in result["clips"]:
        c.setdefault("post_title", c["title"])
        c.setdefault("search_tags", [k for k in c.get("keywords", []) if k][:10])
        c["post_kit"] = post.kit(c)
    result["engine"] = engine
    result["mode"] = mode
    result["model"] = model if engine.startswith("claude") else (
        "offline-streamscore-v1" if mode == "stream" else "offline-storyscore-v1")
    result["usage"] = usage
    result["claude_error"] = claude_error
    result["brief_rules"] = rules
    result["limits"] = L.__dict__
    result["target_clips"] = want
    result["sentence_count"] = len(sents)
    return result
