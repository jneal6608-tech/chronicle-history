"""FFmpeg / FFprobe helpers: probing, validation, audio extraction, scene
detection, letterbox detection, silence detection and framing analysis."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from . import config


class MediaError(Exception):
    """A problem with the input file the user should see verbatim."""


def _bin(name: str) -> str:
    path = shutil.which(name)
    if not path:
        raise MediaError(
            f"{name} was not found. In the Linux Terminal run: sudo apt-get install -y ffmpeg "
            "- then stop and restart Chronicle Shorts.")
    return path


def ffmpeg() -> str:
    return _bin("ffmpeg")


def ffprobe() -> str:
    return _bin("ffprobe")


def run(cmd: list[str], cwd: Optional[Path] = None, timeout: Optional[float] = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def has_filter(name: str) -> bool:
    try:
        out = run([ffmpeg(), "-hide_banner", "-filters"]).stdout
    except MediaError:
        return False
    return re.search(rf"^\s*\S+\s+{re.escape(name)}\s", out, re.M) is not None


@dataclass
class VideoInfo:
    duration: float
    width: int
    height: int
    fps: float
    vcodec: str
    has_audio: bool
    size_bytes: int

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def probe(path: Path) -> VideoInfo:
    proc = run([ffprobe(), "-v", "error", "-print_format", "json",
                "-show_format", "-show_streams", str(path)], timeout=120)
    if proc.returncode != 0:
        raise MediaError("This file could not be read as a video. It may be corrupted, "
                         "incomplete, or not a real MP4. Details: "
                         + (proc.stderr.strip() or "unknown error").replace(str(path), "file").splitlines()[-1][:200])
    data = json.loads(proc.stdout or "{}")
    streams = data.get("streams", [])
    v = next((s for s in streams if s.get("codec_type") == "video"
              and s.get("disposition", {}).get("attached_pic", 0) == 0), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if v is None:
        raise MediaError("No video track was found in this file.")
    fmt = data.get("format", {})
    try:
        duration = float(fmt.get("duration") or v.get("duration") or 0)
    except ValueError:
        duration = 0.0
    if duration <= 0:
        raise MediaError("The video's duration could not be determined - the file is probably "
                         "incomplete (for example an interrupted export).")
    num, _, den = (v.get("avg_frame_rate") or v.get("r_frame_rate") or "30/1").partition("/")
    try:
        fps = float(num) / float(den or 1)
    except (ValueError, ZeroDivisionError):
        fps = 30.0
    if not (5 <= fps <= 240):
        fps = 30.0
    return VideoInfo(duration=duration, width=int(v.get("width", 0)), height=int(v.get("height", 0)),
                     fps=fps, vcodec=v.get("codec_name", "?"), has_audio=a is not None,
                     size_bytes=int(fmt.get("size") or path.stat().st_size))


def validate(path: Path) -> VideoInfo:
    """Probe and decode-test the file. Raises MediaError with a readable reason."""
    info = probe(path)
    if not info.has_audio:
        raise MediaError("This video has no audio track. Chronicle Shorts needs the speech "
                         "to find the best moments and write captions.")
    if info.duration < 60:
        raise MediaError(f"The video is only {info.duration:.0f}s long. Upload a long-form video "
                         "(at least a few minutes) so there is enough material for several shorts.")
    if info.duration > config.MAX_SOURCE_MINUTES * 60:
        raise MediaError(f"The video is {info.duration/60:.0f} minutes long; the limit is "
                         f"{config.MAX_SOURCE_MINUTES:.0f} minutes (change CH_MAX_SOURCE_MINUTES in .env).")
    if info.width < 320 or info.height < 240:
        raise MediaError(f"The video resolution ({info.width}x{info.height}) is too small.")
    # Decode a few seconds at the start, middle and end to catch truncated / corrupt files.
    for t in (1.0, info.duration / 2, max(0.0, info.duration - 3)):
        proc = subprocess.run([ffmpeg(), "-v", "error", "-ss", f"{t:.2f}", "-i", str(path),
                               "-frames:v", "1", "-vf", "scale=64:-2", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
                              capture_output=True, timeout=180)
        if proc.returncode != 0 or len(proc.stdout) == 0:
            raise MediaError(f"The video appears to be damaged or incomplete near {fmt_ts(t)} and cannot be "
                             "decoded (for example an export or copy that did not finish). Re-export it from "
                             "your editor and upload again.")
    return info


def extract_audio(src: Path, wav: Path) -> None:
    proc = run([ffmpeg(), "-y", "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le", str(wav)])
    if proc.returncode != 0 or not wav.exists():
        raise MediaError("Could not extract the audio track: " + proc.stderr.strip()[:300])


def detect_scenes(src: Path, duration: float, progress: Callable[[float], None] | None = None) -> list[float]:
    """Return timestamps of visual cuts / big scene changes (low-res, fast)."""
    cmd = [ffmpeg(), "-hide_banner", "-nostats", "-i", str(src), "-an", "-sn",
           "-vf", "fps=6,scale=256:-2,select='gt(scene,0.30)',showinfo",
           "-f", "null", "-", "-progress", "pipe:1"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", errors="replace")
    times: list[float] = []
    import threading

    def _read_err():
        for line in proc.stderr:  # type: ignore[union-attr]
            m = re.search(r"pts_time:([0-9.]+)", line)
            if m:
                times.append(float(m.group(1)))

    th = threading.Thread(target=_read_err, daemon=True)
    th.start()
    for line in proc.stdout:  # type: ignore[union-attr]
        if line.startswith("out_time_us=") and progress:
            try:
                progress(min(1.0, int(line.split("=")[1]) / 1e6 / duration))
            except ValueError:
                pass
    proc.wait()
    th.join(timeout=5)
    return sorted(times)


def detect_content_box(src: Path, info: VideoInfo) -> tuple[int, int, int, int]:
    """Detect black bars (letterbox / pillarbox). Returns (w, h, x, y) of real picture."""
    boxes = []
    for frac in (0.15, 0.35, 0.55, 0.75):
        t = info.duration * frac
        proc = run([ffmpeg(), "-hide_banner", "-ss", f"{t:.2f}", "-i", str(src), "-t", "3",
                    "-vf", "cropdetect=limit=24:round=2:reset=0", "-an", "-f", "null", "-"], timeout=120)
        found = re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", proc.stderr)
        if found:
            boxes.append(tuple(int(x) for x in found[-1]))
    full = (info.width, info.height, 0, 0)
    if not boxes:
        return full
    # Use the largest box seen (dark scenes can fool cropdetect into over-cropping).
    w = max(b[0] for b in boxes)
    h = max(b[1] for b in boxes)
    if w < info.width * 0.6 or h < info.height * 0.6:
        return full
    x = (info.width - w) // 2
    y = (info.height - h) // 2
    if w >= info.width - 8 and h >= info.height - 8:
        return full
    return (w - w % 2, h - h % 2, x, y)


def detect_silences(src: Path, start: float, dur: float, noise_db: int = -38, min_d: float = 0.55) -> list[tuple[float, float]]:
    """Silent stretches inside [start, start+dur], relative to `start`."""
    proc = run([ffmpeg(), "-hide_banner", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
                "-vn", "-af", f"silencedetect=noise={noise_db}dB:d={min_d}", "-f", "null", "-"], timeout=600)
    out: list[tuple[float, float]] = []
    cur = None
    for line in proc.stderr.splitlines():
        m = re.search(r"silence_start: (-?[0-9.]+)", line)
        if m:
            cur = max(0.0, float(m.group(1)))
        m = re.search(r"silence_end: ([0-9.]+)", line)
        if m and cur is not None:
            out.append((cur, float(m.group(1))))
            cur = None
    if cur is not None:
        out.append((cur, dur))
    return out


def sample_gray_frames(src: Path, start: float, dur: float, box: tuple[int, int, int, int],
                       w: int = 192, fps: float = 1.0) -> np.ndarray:
    """Return (N, h, w) float32 grayscale frames from the content box."""
    bw, bh, bx, by = box
    h = max(2, int(round(w * bh / bw / 2)) * 2)
    proc = subprocess.run([ffmpeg(), "-v", "error", "-ss", f"{start:.3f}", "-t", f"{dur:.3f}", "-i", str(src),
                           "-vf", f"crop={bw}:{bh}:{bx}:{by},fps={fps},scale={w}:{h},format=gray",
                           "-f", "rawvideo", "-"], capture_output=True, timeout=600)
    raw = np.frombuffer(proc.stdout, dtype=np.uint8)
    n = raw.size // (w * h)
    if n == 0:
        return np.zeros((0, h, w), np.float32)
    return raw[: n * w * h].reshape(n, h, w).astype(np.float32)


def choose_framing(src: Path, start: float, dur: float, box: tuple[int, int, int, int]) -> dict:
    """Decide how much of the 16:9 picture to keep for the vertical layout.

    The animation is shown un-stretched in the middle of the phone screen over a
    blurred, darkened copy of itself. If the important detail (edges + motion)
    is concentrated in the middle, the sides are trimmed so the picture can be
    shown larger; otherwise the full frame is kept so nothing is lost.
    """
    bw, bh, bx, by = box
    frames = sample_gray_frames(src, start, dur, box)
    result = {"crop_w": bw, "crop_h": bh, "crop_x": bx, "crop_y": by, "kept_pct": 100, "reason": "full frame"}
    src_aspect = bw / bh
    if frames.shape[0] < 2 or src_aspect < 1.2:
        return result
    gy, gx = np.gradient(frames, axis=(1, 2))
    edges = np.hypot(gx, gy).mean(axis=0)
    motion = np.abs(np.diff(frames, axis=0)).mean(axis=0)
    sal = edges / (edges.mean() + 1e-6) + 1.5 * motion / (motion.mean() + 1e-6)
    sal = np.clip(sal - np.median(sal), 0, None)
    col = sal.sum(axis=0)
    total = col.sum()
    if total <= 0:
        return result
    cs = np.concatenate([[0.0], np.cumsum(col)])
    W = col.size
    # Candidate aspect ratios, most zoomed first, with the share of detail each must keep.
    for aspect, need in ((4 / 3, 0.90), (3 / 2, 0.93)):
        if aspect >= src_aspect - 0.02:
            continue
        win = int(round(W * aspect / src_aspect))
        sums = cs[win:] - cs[:-win]
        best = int(np.argmax(sums))
        share = sums[best] / total
        # Prefer centred windows: only move off-centre if it clearly helps.
        centre = (W - win) // 2
        if sums[centre] / total >= share - 0.02:
            best = centre
            share = sums[centre] / total
        if share >= need:
            cw = int(round(bw * aspect / src_aspect)) // 2 * 2
            cx = bx + int(round(best / W * bw))
            cx = max(bx, min(cx, bx + bw - cw)) // 2 * 2
            return {"crop_w": cw, "crop_h": bh, "crop_x": cx, "crop_y": by,
                    "kept_pct": int(round(100 * cw / bw)),
                    "reason": f"{share*100:.0f}% of on-screen detail sits in the {('4:3' if aspect < 1.4 else '3:2')} "
                              f"window, so the picture is shown larger"}
    return {**result, "reason": "important detail spans the whole frame, so the full frame is kept"}


def audio_energy(src: Path, duration: float, hop: float = 0.5,
                 progress: Callable[[float], None] | None = None) -> dict:
    """Loudness (dB) every `hop` seconds plus a robust z-score against the local baseline.

    Streams the audio through FFmpeg in small blocks so a multi-hour VOD never has
    to fit in memory. A high z means the moment is much louder than what came just
    before and after it: shouting, laughing, hype, a crowd or chat going wild."""
    sr = 8000
    n = int(sr * hop)
    proc = subprocess.Popen([ffmpeg(), "-v", "error", "-i", str(src), "-vn", "-ac", "1", "-ar", str(sr),
                             "-f", "s16le", "-"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    db: list[float] = []
    buf = b""
    while True:
        chunk = proc.stdout.read(n * 2 * 240)  # type: ignore[union-attr]  # ~2 minutes at a time
        if not chunk:
            break
        buf += chunk
        k = len(buf) // (n * 2)
        if k:
            a = np.frombuffer(buf[: k * n * 2], dtype=np.int16).astype(np.float32).reshape(k, n) / 32768.0
            rms = np.sqrt((a * a).mean(axis=1) + 1e-10)
            db.extend((20 * np.log10(rms)).round(2).tolist())
            buf = buf[k * n * 2:]
            if progress:
                progress(min(1.0, len(db) * hop / max(duration, 1)))
    proc.wait()
    arr = np.array(db, dtype=np.float32)
    if arr.size == 0:
        return {"hop": hop, "db": [], "z": []}
    # Local baseline: median of each 30 s block, interpolated (cheap rolling median).
    blk = max(1, int(30 / hop))
    nb = int(np.ceil(arr.size / blk))
    med = np.array([np.median(arr[i * blk:(i + 1) * blk]) for i in range(nb)], dtype=np.float32)
    centres = (np.arange(nb) * blk + blk / 2)
    base = np.interp(np.arange(arr.size), centres, med) if nb > 1 else np.full(arr.size, med[0])
    dev = arr - base
    spread = float(np.median(np.abs(dev - np.median(dev)))) * 1.4826 or 1.0
    z = np.clip(dev / max(spread, 1.0), -5, 8)
    return {"hop": hop, "db": arr.round(1).tolist(), "z": z.round(2).tolist()}


def peak_energy(energy: dict | None, start: float, end: float) -> float:
    """Highest loudness z-score between start and end (0 when unknown)."""
    if not energy or not energy.get("z"):
        return 0.0
    hop = energy["hop"]
    a, b = int(start / hop), int(np.ceil(end / hop))
    seg = energy["z"][max(0, a):max(a + 1, b)]
    return float(max(seg)) if seg else 0.0


def choose_fill(src: Path, start: float, dur: float, box: tuple[int, int, int, int]) -> dict:
    """Full-screen 9:16 crop, placed over the busiest part of the picture (faces and motion
    attract the most edges and movement). Centred unless the action clearly sits elsewhere."""
    bw, bh, bx, by = box
    cw = min(bw, int(round(bh * 9 / 16)) // 2 * 2)
    centre_x = bx + (bw - cw) // 2 // 2 * 2
    out = {"crop_w": cw, "crop_h": bh, "crop_x": centre_x, "crop_y": by, "kept_pct": int(round(100 * cw / bw)),
           "reason": "full-screen crop, centred"}
    frames = sample_gray_frames(src, start, dur, box, fps=1.0)
    if frames.shape[0] < 2 or cw >= bw:
        return out
    gy, gx = np.gradient(frames, axis=(1, 2))
    edges = np.hypot(gx, gy).mean(axis=0)
    motion = np.abs(np.diff(frames, axis=0)).mean(axis=0)
    sal = edges / (edges.mean() + 1e-6) + 2.0 * motion / (motion.mean() + 1e-6)
    sal = np.clip(sal - np.median(sal), 0, None)
    col = sal.sum(axis=0)
    W = col.size
    win = max(1, int(round(W * cw / bw)))
    cs = np.concatenate([[0.0], np.cumsum(col)])
    sums = cs[win:] - cs[:-win]
    if sums.size == 0 or sums.max() <= 0:
        return out
    best = int(np.argmax(sums))
    centre = (W - win) // 2
    if sums[centre] >= 0.9 * sums[best]:
        return out
    cx = bx + int(round(best / W * bw))
    cx = max(bx, min(cx, bx + bw - cw)) // 2 * 2
    side = "left" if cx < centre_x else "right"
    return {**out, "crop_x": cx, "reason": f"full-screen crop, moved {side} to follow the action"}


def facecam_rect(box: tuple[int, int, int, int], corner: str, size: float) -> tuple[int, int, int, int]:
    """(w, h, x, y) of the facecam in the source, from the corner and its width as a share of the frame.
    Facecams are assumed to be 16:9, like nearly every streaming overlay."""
    bw, bh, bx, by = box
    w = int(round(bw * size)) // 2 * 2
    h = min(bh, int(round(w * 9 / 16)) // 2 * 2)
    x = bx if corner in ("tl", "bl") else bx + bw - w
    y = by if corner in ("tl", "tr") else by + bh - h
    return w, h, x, y


def fmt_ts(t: float) -> str:
    t = max(0.0, t)
    h, rem = divmod(int(t), 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
