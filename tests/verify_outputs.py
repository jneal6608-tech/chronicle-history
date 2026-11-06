"""Independent checks on finished shorts:  python tests/verify_outputs.py <job_dir> [contact_sheet.png]

* container/codec/dimension checks (ffprobe)
* integrated loudness (ffmpeg ebur128)
* caption sync: re-transcribes the *exported* short with Whisper and compares each
  spoken word's real start time with the start time of the caption that shows it
* contact sheet of frames for a visual check
"""
import difflib
import json
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import config, transcribe  # noqa: E402


def probe(p):
    d = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(p)],
                                  capture_output=True, text=True).stdout)
    v = next(s for s in d["streams"] if s["codec_type"] == "video")
    a = next(s for s in d["streams"] if s["codec_type"] == "audio")
    return {"w": v["width"], "h": v["height"], "dar": v.get("display_aspect_ratio"), "vcodec": v["codec_name"],
            "profile": v.get("profile"), "pix": v["pix_fmt"], "fps": v["r_frame_rate"], "acodec": a["codec_name"],
            "ar": a["sample_rate"], "ch": a["channels"], "dur": round(float(d["format"]["duration"]), 2),
            "faststart": None}


def loudness(p):
    err = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(p), "-af", "ebur128", "-f", "null", "-"],
                         capture_output=True, text=True).stderr
    m = re.findall(r"I:\s+(-?[0-9.]+) LUFS", err)
    return float(m[-1]) if m else None


def caption_words(ass_path):
    """(start_time, WORD) for each word as it becomes the highlighted word."""
    out = []
    for line in Path(ass_path).read_text(encoding="utf-8").splitlines():
        if not line.startswith("Dialogue: 0,"):
            continue
        parts = line.split(",", 9)
        h, m, s = parts[1].split(":")
        t = int(h) * 3600 + int(m) * 60 + float(s)
        text = parts[9]
        act = re.search(r"\\fscx114\\fscy114\}([^{]+)\{", text)
        if act:
            out.append((t, act.group(1).strip()))
    return out


def norm(w):
    return re.sub(r"[^a-z0-9]", "", w.lower())


def sync_check(mp4, ass):
    wav = mp4.with_suffix(".verify.wav")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(mp4), "-ac", "1", "-ar", "16000", str(wav)], check=True)
    data = transcribe.transcribe(wav, 120, mp4.with_suffix(".verify.json"))
    wav.unlink()
    spoken = [(w["s"], norm(w["w"])) for w in data["words"]]
    caps = [(t, norm(w)) for t, w in caption_words(ass)]
    sm = difflib.SequenceMatcher(a=[w for _, w in spoken], b=[w for _, w in caps], autojunk=False)
    offs = []
    for blk in sm.get_matching_blocks():
        for k in range(blk.size):
            offs.append(caps[blk.b + k][0] - spoken[blk.a + k][0])
    matched = len(offs) / max(1, len(caps))
    offs_abs = sorted(abs(o) for o in offs)
    med = offs_abs[len(offs_abs) // 2] if offs_abs else None
    p90 = offs_abs[int(len(offs_abs) * 0.9)] if offs_abs else None
    mean_signed = sum(offs) / len(offs) if offs else None
    return {"caption_words": len(caps), "matched": f"{matched*100:.0f}%", "median_abs_offset_s": round(med, 3),
            "p90_abs_offset_s": round(p90, 3), "mean_signed_offset_s": round(mean_signed, 3)}


def main(job_dir, sheet=None):
    job_dir = Path(job_dir)
    st = json.loads((job_dir / "state.json").read_text())
    frames = []
    ok = True
    for c in st["clips"]:
        if c["status"] != "done":
            print(f"short {c['index']}: {c['status']} {c.get('error')}")
            ok = False
            continue
        cdir = job_dir / "clips" / f"short_{c['index']:02d}"
        mp4 = cdir / c["file"]
        pr = probe(mp4)
        lu = loudness(mp4)
        sy = sync_check(mp4, cdir / "captions.ass")
        good = (pr["w"], pr["h"], pr["vcodec"], pr["acodec"]) == (1080, 1920, "h264", "aac")
        ok &= good and sy["median_abs_offset_s"] < 0.25
        print(f"short {c['index']}: {mp4.name}\n  {pr}\n  loudness {lu} LUFS\n  sync {sy}\n  {'PASS' if good else 'FAIL'}")
        for frac in (0.12, 0.5, 0.85):
            frames.append((mp4, pr["dur"] * frac))
    if sheet and frames:
        inputs, filt = [], []
        for k, (p, t) in enumerate(frames):
            inputs += ["-ss", f"{t:.2f}", "-i", str(p)]
            filt.append(f"[{k}:v]scale=360:640,setsar=1[f{k}]")
        n = len(frames)
        filt.append("".join(f"[f{k}]" for k in range(n)) + f"xstack=inputs={n}:layout=" +
                    "|".join(f"{(k % 3) * 360 + (k // 3) * 0}_{0}" if False else f"{(k % 9) * 360}_0" for k in range(n)) + "[out]")
        # simple: one row per short
        rows = (n + 2) // 3
        layout = "|".join(f"{(k % 3) * 360}_{(k // 3) * 640}" for k in range(n))
        filt[-1] = "".join(f"[f{k}]" for k in range(n)) + f"xstack=inputs={n}:layout={layout}:fill=black[out]"
        subprocess.run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", ";".join(filt),
                        "-map", "[out]", "-frames:v", "1", str(sheet)], check=True)
        print("contact sheet:", sheet, f"({rows} rows)")
    print("ALL PASS" if ok else "SOME CHECKS FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
