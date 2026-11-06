"""Turns one selected segment into a finished 1080x1920 H.264/AAC short."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

from . import config, media
from .captions import build_ass


# ----------------------------------------------------------------- planning helpers

def plan_window(seg: dict, words: list[dict], total: float) -> tuple[float, float]:
    """Source in/out points with a little natural breathing room."""
    start, end = seg["start"], seg["end"]
    prev_end = max((w["e"] for w in words if w["e"] <= start + 0.01), default=0.0)
    next_start = min((w["s"] for w in words if w["s"] >= end - 0.01), default=total)
    a = max(prev_end + 0.05, start - 0.20, 0.0)
    b = min(next_start - 0.08, end + 0.55, total)
    if b <= a:
        a, b = start, end
    return a, b


def plan_cuts(words_rel: list[dict], silences: list[tuple[float, float]], length: float) -> list[tuple[float, float]]:
    """Return the intervals to KEEP (relative seconds). Dead air is shortened, not removed entirely."""
    remove: list[tuple[float, float]] = []

    def no_speech(a: float, b: float) -> bool:
        return not any(w["s"] < b and w["e"] > a for w in words_rel)

    # 1) True silence longer than 0.7s -> keep ~0.4s of it.
    for a, b in silences:
        if b - a >= 0.7:
            ra, rb = a + 0.2, b - 0.2
            if rb - ra > 0.15 and no_speech(ra, rb):
                remove.append((ra, rb))
    # 2) Long narration gaps over music / ambience (> 1.8s) -> keep 1.0s.
    for w1, w2 in zip(words_rel, words_rel[1:]):
        gap = w2["s"] - w1["e"]
        if gap > 1.8:
            ra, rb = w1["e"] + 0.5, w2["s"] - 0.5
            if no_speech(ra, rb):
                remove.append((ra, rb))
    remove.sort()
    merged: list[list[float]] = []
    for a, b in remove:
        a, b = max(0.0, a), min(length, b)
        if b <= a:
            continue
        if merged and a <= merged[-1][1] + 0.05:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    keep, t = [], 0.0
    for a, b in merged:
        if a > t:
            keep.append((t, a))
        t = b
    if t < length:
        keep.append((t, length))
    keep = [(round(a, 3), round(b, 3)) for a, b in keep if b - a > 0.05]
    return keep or [(0.0, round(length, 3))]


def map_time(t: float, keep: list[tuple[float, float]]) -> float:
    acc = 0.0
    for a, b in keep:
        if t < a:
            return acc
        if t <= b:
            return acc + (t - a)
        acc += b - a
    return acc


def choose_speed(words_rel: list[dict], kept_len: float, lo: float | None = None, hi: float | None = None,
                 target_max: float | None = None) -> tuple[float, str]:
    """1.05x for already-fast narration, up to 1.15x for slow, deliberate narration.
    Streams pass lo == hi (natural speed by default)."""
    lo = config.MIN_SPEED if lo is None else lo
    hi = config.MAX_SPEED if hi is None else hi
    target_max = config.TARGET_MAX_S if target_max is None else target_max
    span = (words_rel[-1]["e"] - words_rel[0]["s"]) if words_rel else 1
    wpm = len(words_rel) / max(span, 1) * 60
    if hi <= lo:
        return round(lo, 3), f"speech pace {wpm:.0f} wpm, kept at {lo:.2f}x"
    if wpm <= 135:
        sp = hi
    elif wpm >= 185:
        sp = lo
    else:
        sp = hi - (wpm - 135) / 50 * (hi - lo)
    # Nudge faster (within range) if the short would otherwise run over the target length.
    if kept_len / sp > target_max:
        sp = min(hi, max(sp, kept_len / target_max))
    sp = round(sp, 3)
    return sp, f"narration pace {wpm:.0f} wpm -> {sp:.2f}x"


# ----------------------------------------------------------------- render

def _layout(src: Path, info: media.VideoInfo, box, a: float, length: float, settings: dict, fps_s: str,
            vsel: str, speed: float) -> dict:
    """FFmpeg video graph + caption/title positions for the chosen vertical layout."""
    W, H = config.OUT_W, config.OUT_H
    layout = settings.get("layout", "fit")
    head = f"[0:v]fps={fps_s},{vsel}setpts=PTS/{speed}"
    if layout == "fill":
        fr = media.choose_fill(src, a, length, box)
        graph = (f"{head},crop={fr['crop_w']}:{fr['crop_h']}:{fr['crop_x']}:{fr['crop_y']},"
                 f"scale={W}:{H}:force_original_aspect_ratio=increase:flags=lanczos,crop={W}:{H},setsar=1")
        return {"graph": graph, "cap_y": 1330, "title_y": 330, "tag_y": 170, "title_dur": None, "framing": fr}
    if layout == "split":
        corner = (settings.get("facecam") or {}).get("corner", "bl")
        size = float((settings.get("facecam") or {}).get("size", 0.3))
        cw, ch, cx, cy = media.facecam_rect(box, corner, size)
        top_h, bot_h = 700, H - 700
        bw, bh, bx, by = box
        gw = min(bw, int(round(bh * W / bot_h)) // 2 * 2)
        gx = bx + (bw - gw) // 2 // 2 * 2
        graph = (f"{head},split=2[c1][c2];"
                 f"[c1]crop={cw}:{ch}:{cx}:{cy},scale={W}:{top_h}:force_original_aspect_ratio=increase:flags=lanczos,"
                 f"crop={W}:{top_h},setsar=1[cam];"
                 f"[c2]crop={gw}:{bh}:{gx}:{by},scale={W}:{bot_h}:force_original_aspect_ratio=increase:flags=lanczos,"
                 f"crop={W}:{bot_h},setsar=1[game];"
                 f"[cam][game]vstack=inputs=2")
        fr = {"crop_w": gw, "crop_h": bh, "crop_x": gx, "crop_y": by, "kept_pct": int(round(100 * gw / bw)),
              "facecam": [cw, ch, cx, cy],
              "reason": f"split screen: facecam ({ {'tl': 'top-left', 'tr': 'top-right', 'bl': 'bottom-left', 'br': 'bottom-right'}[corner]}) "
                        f"on top, gameplay below"}
        return {"graph": graph, "cap_y": top_h, "title_y": 230, "tag_y": 120, "title_dur": 3.5, "framing": fr}
    # "fit": the whole picture, un-stretched, over a blurred copy of itself (the original layout).
    framing = media.choose_framing(src, a, length, box)
    cw, ch, cx, cy = framing["crop_w"], framing["crop_h"], framing["crop_x"], framing["crop_y"]
    fg_w = W
    fg_h = int(round(fg_w * ch / cw / 2)) * 2
    max_fg_h = 1150
    if fg_h > max_fg_h:  # source is tall / vertical already
        fg_h = max_fg_h
        fg_w = int(round(fg_h * cw / ch / 2)) * 2
    centre_y = 840
    fg_y = int(centre_y - fg_h / 2)
    fg_bottom = fg_y + fg_h
    graph = (f"{head},split=2[v1][v2];"
             f"[v1]crop={cw}:{ch}:{cx}:{cy},scale={fg_w}:{fg_h}:flags=lanczos,setsar=1[fg];"
             f"[v2]crop={box[0]}:{box[1]}:{box[2]}:{box[3]},"
             f"scale=270:480:force_original_aspect_ratio=increase,crop=270:480,"
             f"boxblur=12:3,eq=brightness=-0.16:saturation=1.15,scale={W}:{H},setsar=1[bg];"
             f"[bg][fg]overlay=x=(W-w)/2:y={fg_y}:shortest=1")
    title_y = fg_y - 38
    return {"graph": graph, "cap_y": max(1250, min(1470, fg_bottom + 185)), "title_y": title_y,
            "tag_y": max(150, title_y - 230), "title_dur": None, "framing": framing}


def render_clip(src: Path, info: media.VideoInfo, box, seg: dict, words: list[dict], out_dir: Path,
                progress: Optional[Callable[[float], None]] = None, settings: dict | None = None) -> dict:
    settings = settings or config.default_settings()
    stream = settings.get("mode") == "stream"
    out_dir.mkdir(parents=True, exist_ok=True)
    a, b = plan_window(seg, words, info.duration)
    length = b - a
    words_rel = [{**w, "s": w["s"] - a, "e": w["e"] - a} for w in words if w["s"] >= a - 0.01 and w["e"] <= b + 0.01]
    if not words_rel:
        raise RuntimeError("No speech found inside this segment.")
    silences = media.detect_silences(src, a, length)
    keep = plan_cuts(words_rel, silences, length)
    kept_len = sum(y - x for x, y in keep)
    if stream:
        speed, speed_reason = choose_speed(words_rel, kept_len, config.STREAM_SPEED, config.STREAM_SPEED)
    else:
        speed, speed_reason = choose_speed(words_rel, kept_len)
    out_len = kept_len / speed

    fps = min(60.0, info.fps)
    fps_s = f"{fps:.3f}".rstrip("0").rstrip(".")
    if len(keep) > 1 or keep[0][0] > 0.01 or keep[0][1] < length - 0.01:
        expr = "+".join(f"between(t\\,{x:.3f}\\,{y:.3f})" for x, y in keep)
        vsel = f"select='{expr}',setpts=N/FRAME_RATE/TB,"
        asel = f"aselect='{expr}',asetpts=N/SR/TB,"
    else:
        vsel = asel = ""
    lay = _layout(src, info, box, a, length, settings, fps_s, vsel, speed)

    # Captions in output time.
    words_out = []
    for w in words_rel:
        s = map_time(w["s"], keep) / speed
        e = map_time(w["e"], keep) / speed
        words_out.append({"w": w["w"], "s": round(s, 3), "e": round(max(e, s + 0.05), 3)})
    ass = build_ass(words_out, out_len, lay["cap_y"], seg.get("title", ""), lay["title_y"], lay["tag_y"],
                    seg.get("keywords", []), style=settings.get("captions", "clean"),
                    show_tag=config.SHOW_CHANNEL_TAG and not stream, title_dur=lay["title_dur"])
    (out_dir / "captions.ass").write_text(ass, encoding="utf-8")
    fonts = out_dir / "fonts"
    fonts.mkdir(exist_ok=True)
    for f in config.FONTS_DIR.glob("*.ttf"):
        if not (fonts / f.name).exists():
            shutil.copy2(f, fonts / f.name)

    tempo = "anull" if abs(speed - 1.0) < 1e-3 else (
        f"rubberband=tempo={speed}:pitchq=quality" if media.has_filter("rubberband") else f"atempo={speed}")
    fade_out = max(0.0, out_len - 0.25)
    graph = (
        f"{lay['graph']},ass=captions.ass:fontsdir=fonts,format=yuv420p[vout];"
        f"[0:a]{asel}{tempo},afade=t=in:d=0.06,afade=t=out:st={fade_out:.3f}:d=0.25,"
        f"loudnorm=I={config.TARGET_LUFS}:TP=-1.5:LRA=11,aresample=48000[aout]"
    )
    (out_dir / "filter.txt").write_text(graph, encoding="utf-8")
    tmp = out_dir / "short.part.mp4"
    final = out_dir / "short.mp4"
    cmd = [media.ffmpeg(), "-y", "-hide_banner", "-nostats", "-ss", f"{a:.3f}", "-t", f"{length:.3f}",
           "-i", str(src.resolve()), "-filter_complex_script", "filter.txt",
           "-map", "[vout]", "-map", "[aout]",
           "-c:v", "libx264", "-preset", config.X264_PRESET, "-crf", config.X264_CRF,
           "-profile:v", "high", "-level", "4.2", "-pix_fmt", "yuv420p", "-r", fps_s,
           "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "2",
           "-movflags", "+faststart", "-progress", "pipe:1", tmp.name]
    proc = subprocess.Popen(cmd, cwd=out_dir, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, encoding="utf-8", errors="replace")
    import threading
    err_lines: list[str] = []
    th = threading.Thread(target=lambda: err_lines.extend(proc.stderr), daemon=True)  # type: ignore[arg-type]
    th.start()
    for line in proc.stdout:  # type: ignore[union-attr]
        if line.startswith("out_time_us=") and progress:
            try:
                progress(min(0.99, int(line.split("=")[1]) / 1e6 / max(out_len, 1)))
            except ValueError:
                pass
    proc.wait()
    th.join(timeout=5)
    if proc.returncode != 0 or not tmp.exists():
        tail = "".join(err_lines[-15:])
        raise RuntimeError("FFmpeg failed while rendering this short:\n" + tail[-1500:])
    tmp.replace(final)
    check = verify_output(final)
    return {"source_in": a, "source_out": b, "kept_intervals": keep, "removed_seconds": round(length - kept_len, 2),
            "speed": speed, "speed_reason": speed_reason, "duration": check["duration"],
            "framing": lay["framing"], "layout": settings.get("layout", "fit"),
            "captions": settings.get("captions", "clean"), "caption_words": len(words_out), "check": check,
            "words_out": words_out}


def verify_output(path: Path) -> dict:
    proc = media.run([media.ffprobe(), "-v", "error", "-print_format", "json", "-show_format",
                      "-show_streams", str(path)])
    d = json.loads(proc.stdout or "{}")
    v = next((s for s in d.get("streams", []) if s["codec_type"] == "video"), {})
    a = next((s for s in d.get("streams", []) if s["codec_type"] == "audio"), {})
    res = {"width": v.get("width"), "height": v.get("height"), "vcodec": v.get("codec_name"),
           "acodec": a.get("codec_name"), "pix_fmt": v.get("pix_fmt"),
           "duration": round(float(d.get("format", {}).get("duration", 0)), 2),
           "size_mb": round(int(d.get("format", {}).get("size", 0)) / 1e6, 1)}
    problems = []
    if (res["width"], res["height"]) != (config.OUT_W, config.OUT_H):
        problems.append(f"resolution {res['width']}x{res['height']}")
    if res["vcodec"] != "h264":
        problems.append(f"video codec {res['vcodec']}")
    if res["acodec"] != "aac":
        problems.append(f"audio codec {res['acodec']}")
    if res["duration"] < 5:
        problems.append("duration too short")
    if problems:
        raise RuntimeError("Output failed verification: " + ", ".join(problems))
    return res
