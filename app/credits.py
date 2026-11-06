"""Claude connection and credit tracking.

Anthropic has no API that reports your remaining prepaid balance, so the studio
keeps its own running estimate: you tell it your balance (or what you just added),
and every run subtracts the cost Anthropic reports for that run (from its token
counts). The Console's Billing page always has the exact number.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

from . import config

BILLING_URL = "https://platform.claude.com/settings/billing"
KEYS_URL = "https://platform.claude.com/settings/keys"
_lock = threading.Lock()
_KEY_RX = re.compile(r"^sk-ant-[A-Za-z0-9_\-]{20,}$")


# ------------------------------------------------------------------ ledger

def _load() -> dict:
    try:
        d = json.loads(_path().read_text(encoding="utf-8"))
        if isinstance(d, dict):
            d.setdefault("baseline", None)
            d.setdefault("baseline_t", 0.0)
            d.setdefault("runs", [])
            return d
    except Exception:
        pass
    return {"baseline": None, "baseline_t": 0.0, "runs": []}


def _path():
    return config.DATA_DIR / "credits.json"


def _save(d: dict) -> None:
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    tmp.replace(p)


def record_run(job_id: str, model: str, cost: float | None, minutes: float, clips: int, mode: str) -> None:
    """Called after every Claude analysis with the cost Anthropic's token counts imply."""
    if cost is None:
        return
    with _lock:
        d = _load()
        d["runs"] = [r for r in d["runs"] if r.get("job_id") != job_id]
        d["runs"].append({"job_id": job_id, "model": model, "cost": round(float(cost), 4),
                          "minutes": round(minutes, 2), "clips": clips, "mode": mode, "t": time.time()})
        _save(d)


def set_balance(amount: float) -> dict:
    """The balance shown on the Console's Billing page, right now."""
    with _lock:
        d = _load()
        d["baseline"], d["baseline_t"] = round(max(0.0, amount), 2), time.time()
        _save(d)
    return summary()


def add_credits(amount: float) -> dict:
    """You just bought `amount` more credits."""
    with _lock:
        d = _load()
        if d["baseline"] is None:
            d["baseline"], d["baseline_t"] = round(amount, 2), time.time()
        else:
            d["baseline"] = round(d["baseline"] + amount, 2)
        _save(d)
    return summary()


def summary() -> dict:
    d = _load()
    runs = d["runs"]
    since = [r for r in runs if r["t"] >= d["baseline_t"]]
    spent_since = round(sum(r["cost"] for r in since), 4)
    remaining = None if d["baseline"] is None else round(d["baseline"] - spent_since, 2)
    total = round(sum(r["cost"] for r in runs), 4)
    clips = sum(r.get("clips", 0) for r in runs)
    return {"connected": bool(config.ANTHROPIC_API_KEY), "key_hint": key_hint(),
            "balance_known": d["baseline"] is not None, "remaining": remaining,
            "spent_total": total, "runs": len(runs), "clips": clips,
            "avg_per_video": round(total / len(runs), 4) if runs else None,
            "avg_per_clip": round(total / clips, 4) if clips else None,
            "billing_url": BILLING_URL, "keys_url": KEYS_URL,
            "recent": sorted(runs, key=lambda r: -r["t"])[:8]}


# ------------------------------------------------------------------ estimate

def estimate(minutes: float, model: str, mode: str, clips: int) -> dict:
    """Rough cost of one run before it happens.

    Speech runs ~150 words a minute; each transcript line adds timestamps, so input is
    about 2.4 tokens per spoken word plus the instructions. Output (Claude's reasoning
    plus the candidate list) is the uncertain part, hence a range. Once a few runs are
    logged for this model, the estimate uses your real cost per minute instead."""
    m = next((x for x in config.CLAUDE_MODELS if x["id"] == model), config.CLAUDE_MODELS[0])
    chunk = max(1.0, config.CLAUDE_CHUNK_MINUTES)
    parts = 1 if minutes <= chunk * 1.25 else int(-(-minutes // chunk))
    words = minutes * (165 if mode == "stream" else 150)
    tokens_in = int(words * 2.4 + 2500 * parts)
    tokens_out = int(parts * 2500 + clips * 450)
    # Priced per request (one per part), since long-context pricing tiers apply per request.
    low = parts * (config.estimate_cost(model, int(tokens_in * 0.8 / parts), int(tokens_out * 0.6 / parts)) or 0.0)
    high = parts * (config.estimate_cost(model, int(tokens_in * 1.2 / parts), int(tokens_out * 2.2 / parts)) or 0.0)
    basis = "rough estimate from video length"
    runs = [r for r in _load()["runs"] if r.get("model") == model and r.get("minutes", 0) > 0]
    if len(runs) >= 3:
        per_min = sum(r["cost"] for r in runs) / sum(r["minutes"] for r in runs)
        mid = per_min * minutes
        low, high = mid * 0.7, mid * 1.4
        basis = f"based on your last {len(runs)} runs with this model"
    s = summary()
    return {"low": round(low, 3), "high": round(high, 3), "parts": parts, "basis": basis,
            "model_label": m["label"].split(" ·")[0], "remaining": s["remaining"],
            "enough": None if s["remaining"] is None else s["remaining"] >= high}


# ------------------------------------------------------------------ API key

def key_hint() -> str:
    k = config.ANTHROPIC_API_KEY
    return f"…{k[-4:]}" if len(k) >= 8 else ""


def check_key(key: str) -> str | None:
    """None if Anthropic accepts the key, otherwise a plain-English problem.
    Listing models is free, so checking a key costs nothing."""
    if not _KEY_RX.match(key):
        return "That doesn't look like an Anthropic API key. Keys start with sk-ant-."
    try:
        import anthropic
    except Exception:
        return None  # can't check offline; the format looks right
    try:
        anthropic.Anthropic(api_key=key, max_retries=1, timeout=20).models.list(limit=1)
    except anthropic.AuthenticationError:
        return "Anthropic didn't accept that key. Copy it again from the API keys page."
    except anthropic.PermissionDeniedError:
        return "That key doesn't have permission to use the API."
    except anthropic.APIConnectionError:
        return "Couldn't reach Anthropic to check the key. Check your internet connection and try again."
    except Exception as e:  # anything else: don't block saving
        print(f"[credits] key check skipped: {type(e).__name__}: {e}")
    return None


def save_key(key: str | None) -> None:
    """Write (or remove) ANTHROPIC_API_KEY in .env and use it right away, no restart needed."""
    env = config.ROOT / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    out, done = [], False
    for ln in lines:
        if re.match(r"\s*ANTHROPIC_API_KEY\s*=", ln):
            if not done:
                out.append(f"ANTHROPIC_API_KEY={key or ''}")
                done = True
            continue
        out.append(ln)
    if not done:
        out.append(f"ANTHROPIC_API_KEY={key or ''}")
    tmp = env.with_suffix(".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(env)
    config.ANTHROPIC_API_KEY = key or ""
    if key:
        os.environ["ANTHROPIC_API_KEY"] = key
    else:
        os.environ.pop("ANTHROPIC_API_KEY", None)


def is_credit_error(e: Exception) -> bool:
    s = str(e).lower()
    return "credit balance" in s or "billing" in s or "insufficient" in s
