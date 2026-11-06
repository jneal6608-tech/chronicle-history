"""A failing short must not lose the others; retry must fix only the failed one.
    python tests/test_failure_isolation.py <existing_job_dir_with_transcript>"""
import json, shutil, sys, zipfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app import pipeline, render  # noqa: E402

src_job = Path(sys.argv[1])
st = pipeline.new_job("isolation-test.mp4")
d = pipeline.job_dir(st["id"])
for f in ("source.mp4", "transcript.json", "visual.json"):
    shutil.copy2(src_job / f, d / f)
pipeline.update(st["id"], status="ready", info=json.loads((src_job / "state.json").read_text())["info"])
calls = []
real = render.render_clip
fake_fail = {"on": True}

def fake_render(src, info, box, seg, words, out_dir, progress=None, **kw):
    calls.append(seg["title"])
    if len(calls) == 2 and fake_fail["on"]:
        raise RuntimeError("Simulated FFmpeg crash")
    out_dir.mkdir(parents=True, exist_ok=True)
    # tiny real 1080x1920 h264/aac file so output verification runs for real
    import subprocess
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "color=s=1080x1920:d=6", "-f", "lavfi", "-i",
                    "sine=d=6", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
                    str(out_dir / "short.mp4")], check=True)
    chk = render.verify_output(out_dir / "short.mp4")
    return {"source_in": seg["start"], "source_out": seg["end"], "kept_intervals": [], "removed_seconds": 0, "speed": 1.1,
            "speed_reason": "test", "duration": chk["duration"], "framing": {"reason": "test", "kept_pct": 100},
            "caption_words": 0, "check": chk}

render.render_clip = fake_render
pipeline.run_job(st["id"])
s = pipeline.load(st["id"])
print("after first run:", s["status"], [c["status"] for c in s["clips"]])
assert s["status"] == "partial" and [c["status"] for c in s["clips"]].count("failed") == 1
names = zipfile.ZipFile(d / "shorts.zip").namelist()
assert len([n for n in names if n.endswith(".mp4")]) == len(s["clips"]) - 1
done_before = [c for c in s["clips"] if c["status"] == "done"]
fake_fail["on"] = False
calls.clear()
failed_idx = next(c["index"] for c in s["clips"] if c["status"] == "failed")
pipeline._set_clip(st["id"], failed_idx, status="pending", error=None)
pipeline.run_job(st["id"])
s = pipeline.load(st["id"])
print("after retry:", s["status"], [c["status"] for c in s["clips"]], "re-rendered:", len(calls))
assert s["status"] == "done" and len(calls) == 1
assert len([n for n in zipfile.ZipFile(d / "shorts.zip").namelist() if n.endswith(".mp4")]) == len(s["clips"])
pipeline.delete_job(st["id"])
print("FAILURE ISOLATION + RETRY: PASS")
