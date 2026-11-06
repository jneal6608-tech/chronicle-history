"""Whop / Content Rewards campaign briefs.

A brief is the text a campaign posts ("must tag @brand, use #ad, 15-60 seconds,
no gambling..."). This module pulls the checkable rules out of it, makes every
clip's caption obey them, and checks each finished clip before you submit it.

Claude also reads the brief (when a key is set) and returns the same rule
structure; both sets are merged so nothing is missed.
"""
from __future__ import annotations

import re

EMPTY = {"required_hashtags": [], "required_mentions": [], "required_phrases": [],
         "min_seconds": 0, "max_seconds": 0, "other_rules": []}

_TAG = re.compile(r"(?<![\w#])#([A-Za-z0-9_]{2,60})")
_MENTION = re.compile(r"(?<![\w@])@([A-Za-z0-9_.]{2,40})")
_RANGE = re.compile(r"\b(\d{1,3})\s*(?:-|–|to)\s*(\d{1,3})\s*(?:s\b|sec|secs|second|seconds)", re.I)
_MAX = re.compile(r"\b(?:max(?:imum)?|under|no longer than|less than|up to|at most)\s*(?:of\s*)?(\d{1,3})\s*"
                  r"(?:s\b|sec|secs|second|seconds)", re.I)
_MIN = re.compile(r"\b(?:min(?:imum)?|at least|longer than|over|more than)\s*(?:of\s*)?(\d{1,3})\s*"
                  r"(?:s\b|sec|secs|second|seconds)", re.I)
_QUOTED = re.compile(r"(?:caption|description|text|say|include|mention)[^\"“\n]{0,40}[\"“]([^\"”\n]{3,80})[\"”]", re.I)
_RULE_LINE = re.compile(r"\b(must|don't|do not|never|no |only|required|requirement|avoid|banned|not allowed|"
                        r"make sure|need to|needs to|has to|have to)\b", re.I)


def parse(text: str) -> dict:
    """Rules that can be found in the brief text without AI."""
    text = text or ""
    rules = {k: (list(v) if isinstance(v, list) else v) for k, v in EMPTY.items()}
    if not text.strip():
        return rules
    rules["required_hashtags"] = _dedupe("#" + t for t in _TAG.findall(text))
    rules["required_mentions"] = _dedupe("@" + m.rstrip(".") for m in _MENTION.findall(text))
    rules["required_phrases"] = _dedupe(q.strip() for q in _QUOTED.findall(text))
    r = _RANGE.search(text)
    if r:
        a, b = sorted((int(r.group(1)), int(r.group(2))))
        rules["min_seconds"], rules["max_seconds"] = a, b
    m = _MAX.search(text)
    if m and not rules["max_seconds"]:
        rules["max_seconds"] = int(m.group(1))
    m = _MIN.search(text)
    if m and not rules["min_seconds"]:
        rules["min_seconds"] = int(m.group(1))
    lines = [ln.strip(" -•*\t") for ln in re.split(r"[\n\r]+|(?<=[.!])\s+", text)]
    auto = (_TAG, _MENTION, _RANGE, _MAX, _MIN, _QUOTED)  # already checked automatically
    rules["other_rules"] = _dedupe(ln for ln in lines if 6 <= len(ln) <= 200 and _RULE_LINE.search(ln)
                                   and not any(rx.search(ln) for rx in auto))[:12]
    return rules


def merge(*sets: dict | None) -> dict:
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in EMPTY.items()}
    for s in sets:
        if not s:
            continue
        for k in ("required_hashtags", "required_mentions", "required_phrases", "other_rules"):
            out[k] = _dedupe(out[k] + [str(x).strip() for x in s.get(k, []) if str(x).strip()])
        for k in ("min_seconds", "max_seconds"):
            try:
                v = int(s.get(k) or 0)
            except (TypeError, ValueError):
                v = 0
            if v > 0:
                # The stricter limit wins.
                out[k] = v if not out[k] else (max(out[k], v) if k == "min_seconds" else min(out[k], v))
    out["required_hashtags"] = _dedupe("#" + re.sub(r"[^A-Za-z0-9_]", "", h.lstrip("#"))
                                       for h in out["required_hashtags"] if h.lstrip("#"))
    out["required_mentions"] = _dedupe("@" + h.lstrip("@") for h in out["required_mentions"] if h.lstrip("@"))
    if out["min_seconds"] and out["max_seconds"] and out["min_seconds"] > out["max_seconds"]:
        out["min_seconds"] = 0
    return out


def is_empty(rules: dict | None) -> bool:
    return not rules or not any(rules.get(k) for k in EMPTY)


def apply(clip: dict, rules: dict) -> dict:
    """Make the caption and hashtags contain everything the campaign requires."""
    if is_empty(rules):
        return clip
    tags = list(clip.get("hashtags", []))
    low = {t.lower() for t in tags}
    for t in rules["required_hashtags"]:
        if t.lower() not in low:
            tags.insert(0, t)
            low.add(t.lower())
    desc = clip.get("description", "").strip()
    extra = []
    for m in rules["required_mentions"]:
        if m.lower() not in desc.lower():
            extra.append(m)
    for p in rules["required_phrases"]:
        if p.lower() not in desc.lower():
            extra.append(p)
    if extra:
        desc = (desc + "\n\n" + " ".join(extra)).strip()
    return {**clip, "hashtags": tags[:15], "description": desc}


def check(clip: dict, duration: float | None, rules: dict) -> dict:
    """Pass/fail checklist for one finished clip, plus rules a person has to check by eye."""
    if is_empty(rules):
        return {"ok": True, "checks": [], "manual": []}
    checks = []
    text = (clip.get("description", "") + " " + " ".join(clip.get("hashtags", []))).lower()
    for t in rules["required_hashtags"]:
        checks.append({"label": f"Hashtag {t}", "ok": t.lower() in text})
    for m in rules["required_mentions"]:
        checks.append({"label": f"Mentions {m}", "ok": m.lower() in text})
    for p in rules["required_phrases"]:
        checks.append({"label": f'Caption includes "{p}"', "ok": p.lower() in text})
    if duration is not None:
        if rules["min_seconds"]:
            checks.append({"label": f"At least {rules['min_seconds']}s (is {duration:.0f}s)",
                           "ok": duration >= rules["min_seconds"] - 0.5})
        if rules["max_seconds"]:
            checks.append({"label": f"At most {rules['max_seconds']}s (is {duration:.0f}s)",
                           "ok": duration <= rules["max_seconds"] + 0.5})
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "manual": list(rules["other_rules"])}


def _dedupe(items) -> list[str]:
    seen, out = set(), []
    for x in items:
        k = x.lower()
        if x and k not in seen:
            seen.add(k)
            out.append(x)
    return out
