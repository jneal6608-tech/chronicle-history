"""Central configuration. Every value has a sensible default; override any of
them in the `.env` file next to this folder (copy `.env.example`)."""
from __future__ import annotations

import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
APP_DIR = ROOT / "app"


def _load_env_file() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_env_file()


def _get(name: str, default: str) -> str:
    return os.environ.get(name, default).strip() or default


def _float(name: str, default: float) -> float:
    try:
        return float(_get(name, str(default)))
    except ValueError:
        return default


# --- Platform ---------------------------------------------------------------
# ChromeOS runs Linux apps in a container ("penguin"). Chrome reaches it at
# http://penguin.linux.test:<port> (or http://localhost:<port> via ChromeOS port
# forwarding). The container is only reachable from this Chromebook itself.
IS_CHROMEOS = (Path("/opt/google/cros-containers").exists()
               or Path("/dev/.cros_milestone").exists()
               or os.environ.get("CH_FORCE_CHROMEOS") == "1")

# --- Server -----------------------------------------------------------------
HOST = _get("CH_HOST", "0.0.0.0" if IS_CHROMEOS else "127.0.0.1")
PORT = int(_get("CH_PORT", "8765"))
DATA_DIR = Path(_get("CH_DATA_DIR", str(ROOT / "data"))).resolve()
JOBS_DIR = DATA_DIR / "jobs"
MAX_UPLOAD_GB = _float("CH_MAX_UPLOAD_GB", 20.0)
MAX_SOURCE_MINUTES = _float("CH_MAX_SOURCE_MINUTES", 480.0)  # 8 h covers long stream VODs
RESULTS_FILE = DATA_DIR / "results.json"

# --- Branding ---------------------------------------------------------------
# Optional branding. Both are empty by default, so clips carry no channel name at all.
# (The old CH_CHANNEL_NAME / CH_CHANNEL_HASHTAG / CH_SHOW_CHANNEL_TAG settings are ignored.)
WATERMARK = _get("CH_WATERMARK", "").strip()          # small text at the top of documentary-mode clips
EXTRA_HASHTAGS = ["#" + t.lstrip("#") for t in re.split(r"[\s,]+", _get("CH_EXTRA_HASHTAGS", "")) if t.lstrip("#")]

# --- Speech to text (faster-whisper, free, runs locally) ---------------------
# tiny / base / small / medium / large-v3, or a path to a local model folder.
WHISPER_MODEL = _get("CH_WHISPER_MODEL", "base")  # Chromebook-friendly default
WHISPER_LANGUAGE = _get("CH_WHISPER_LANGUAGE", "en")  # "auto" to detect
WHISPER_DEVICE = _get("CH_WHISPER_DEVICE", "auto")     # auto / cpu / cuda

# --- AI analysis (Claude). Optional: without a key the free offline analyser runs.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
CLAUDE_MODEL = _get("CH_CLAUDE_MODEL", "claude-opus-5-5")
CLAUDE_EFFORT = _get("CH_CLAUDE_EFFORT", "medium")
ANALYSIS_MODE = _get("CH_ANALYSIS_MODE", "auto")  # auto / claude / offline
# Transcripts longer than this are analysed in overlapping chunks (long stream VODs).
CLAUDE_CHUNK_MINUTES = _float("CH_CLAUDE_CHUNK_MINUTES", 60.0)

# Models offered in the studio (id, label, $ per million input / output tokens).
# Prices from Anthropic's pricing page, Oct 2026; used only for the cost estimate shown in the studio.
CLAUDE_MODELS = [
    {"id": "claude-opus-5-5", "label": "Opus 5.5 · best judgement", "in": 4.00, "out": 20.00},
    {"id": "claude-sonnet-5-5", "label": "Sonnet 5.5 · half the price", "in": 2.00, "out": 10.00},
    {"id": "claude-haiku-5-5", "label": "Haiku 5.5 · cheapest", "in": 0.10, "out": 0.50,
     "tier_at": 100_000, "in_hi": 0.50, "out_hi": 2.50},
]


def estimate_cost(model: str, input_tokens: int | None, output_tokens: int | None) -> float | None:
    m = next((x for x in CLAUDE_MODELS if x["id"] == model), None)
    if not m or input_tokens is None or output_tokens is None:
        return None
    hi = "tier_at" in m and input_tokens > m["tier_at"]
    pin, pout = (m["in_hi"], m["out_hi"]) if hi else (m["in"], m["out"])
    return round((input_tokens * pin + output_tokens * pout) / 1e6, 4)


# --- Content modes -------------------------------------------------------------
# "documentary": narrated explainers (your own channel). "stream": streamers, podcasts, influencers.
MODES = ("documentary", "stream")
LAYOUTS = ("fit", "fill", "split")
CAPTION_STYLES = ("clean", "pop")
FACECAM_CORNERS = ("tl", "tr", "bl", "br")
STREAM_SPEED = _float("CH_STREAM_SPEED", 1.0)  # streams keep natural speech speed by default


def default_settings(mode: str = "documentary") -> dict:
    stream = mode == "stream"
    return {"mode": "stream" if stream else "documentary",
            "model": CLAUDE_MODEL,
            "layout": "fit",
            "facecam": {"corner": "bl", "size": 0.30},
            "captions": "pop" if stream else "clean",
            "max_clips": 0,          # 0 = automatic
            "brief": ""}


def clean_settings(raw: dict | None) -> dict:
    """Validate settings sent by the studio; anything unknown falls back to the default."""
    raw = raw or {}
    mode = raw.get("mode") if raw.get("mode") in MODES else "documentary"
    s = default_settings(mode)
    if raw.get("model") in {m["id"] for m in CLAUDE_MODELS}:
        s["model"] = raw["model"]
    if raw.get("layout") in LAYOUTS:
        s["layout"] = raw["layout"]
    if raw.get("captions") in CAPTION_STYLES:
        s["captions"] = raw["captions"]
    fc = raw.get("facecam") or {}
    if fc.get("corner") in FACECAM_CORNERS:
        s["facecam"]["corner"] = fc["corner"]
    try:
        s["facecam"]["size"] = min(0.5, max(0.15, float(fc.get("size", s["facecam"]["size"]))))
    except (TypeError, ValueError):
        pass
    try:
        s["max_clips"] = max(0, min(40, int(raw.get("max_clips", 0))))
    except (TypeError, ValueError):
        pass
    s["brief"] = str(raw.get("brief") or "")[:6000]
    return s

# --- Rendering ----------------------------------------------------------------
OUT_W, OUT_H = 1080, 1920
X264_PRESET = _get("CH_X264_PRESET", "veryfast")  # "medium" = slightly smaller files, ~3x slower
X264_CRF = _get("CH_X264_CRF", "18")
TARGET_LUFS = _get("CH_TARGET_LUFS", "-14")
MIN_SPEED, MAX_SPEED = 1.05, 1.15

# Target length of a finished short (seconds, after speed-up).
TARGET_MIN_S, TARGET_MAX_S = 60.0, 90.0
ABS_MIN_S = 25.0          # never output a short below this
MIN_CLIPS, MAX_CLIPS = 3, 5
STREAM_MIN_CLIPS, STREAM_MAX_CLIPS = 3, 30

FONTS_DIR = APP_DIR / "assets" / "fonts"
CAPTION_FONT = "Montserrat ExtraBold"
STATIC_DIR = APP_DIR / "static"

JOBS_DIR.mkdir(parents=True, exist_ok=True)


def studio_urls() -> list[str]:
    """Addresses to open in Chrome, best first."""
    if IS_CHROMEOS or HOST == "0.0.0.0":
        return [f"http://penguin.linux.test:{PORT}", f"http://localhost:{PORT}"]
    return [f"http://{HOST}:{PORT}"]


# --- Lead finder ----------------------------------------------------------------
GOOGLE_MAPS_API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY", "").strip()
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
GITHUB_PREVIEW_REPO = os.environ.get("GITHUB_PREVIEW_REPO", "").strip()   # owner/repo with GitHub Pages on
SITE_MODEL = _get("CH_SITE_MODEL", "claude-sonnet-5-5")


def write_env(name: str, value: str | None) -> None:
    """Set (or clear) one line in .env and apply it right away. The file stays readable only by you."""
    env = ROOT / ".env"
    lines = env.read_text(encoding="utf-8").splitlines() if env.exists() else []
    out, done = [], False
    for ln in lines:
        if re.match(rf"\s*{re.escape(name)}\s*=", ln):
            if not done:
                out.append(f"{name}={value or ''}")
                done = True
            continue
        out.append(ln)
    if not done:
        out.append(f"{name}={value or ''}")
    tmp = env.with_suffix(".tmp")
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(env)
    globals()[name] = value or ""
    if value:
        os.environ[name] = value
    else:
        os.environ.pop(name, None)
