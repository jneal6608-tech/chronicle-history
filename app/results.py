"""Results log: how each posted clip actually performed.

You type in the views (and optionally earnings and the post link) for clips you
posted. The log is kept in data/results.json and the best and worst performers
are shown to Claude as examples the next time it picks moments, so its picks
learn what goes viral for you.
"""
from __future__ import annotations

import json
import threading
import time

from . import config

_lock = threading.Lock()


def load() -> list[dict]:
    p = config.RESULTS_FILE
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save(rows: list[dict]) -> None:
    p = config.RESULTS_FILE
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    tmp.replace(p)


def record(job: dict, clip: dict, views: int, earnings: float | None, url: str) -> dict:
    """Add or update the result for one clip (one row per job + clip)."""
    key = f"{job['id']}:{clip['index']}"
    settings = job.get("settings") or {}
    row = {"key": key, "job_id": job["id"], "clip_index": clip["index"], "source": job.get("filename"),
           "mode": settings.get("mode", "documentary"), "title": clip.get("title", ""),
           "hook": (clip.get("transcript") or "")[:220], "duration": (clip.get("render") or {}).get("duration"),
           "why_selected": clip.get("why_selected", ""), "views": int(views),
           "earnings": earnings, "url": url[:500], "updated": time.time()}
    with _lock:
        rows = [r for r in load() if r.get("key") != key]
        rows.append(row)
        _save(rows)
    return row


def examples(mode: str, n_best: int = 5, n_worst: int = 3) -> dict:
    """Best and worst logged clips for this mode (needs at least 3 logged to be useful)."""
    rows = [r for r in load() if r.get("mode") == mode and isinstance(r.get("views"), int)]
    if len(rows) < 3:
        return {"best": [], "worst": []}
    rows.sort(key=lambda r: -r["views"])
    best = rows[:n_best]
    worst = [r for r in rows[::-1][:n_worst] if r not in best]
    return {"best": best, "worst": worst}


def prompt_block(mode: str) -> str:
    ex = examples(mode)
    if not ex["best"]:
        return ""

    def fmt(r):
        return f'- {r["views"]:,} views: "{r["title"]}" - opened with: "{r["hook"][:160]}"'

    parts = ["Past performance of clips this editor posted (learn what this audience responds to; "
             "do not copy topics, copy what made the openings work):", "Best:"]
    parts += [fmt(r) for r in ex["best"]]
    if ex["worst"]:
        parts += ["Weakest:"] + [fmt(r) for r in ex["worst"]]
    return "\n".join(parts)
