"""Command-line mode: process a documentary without the web UI.

    .venv/bin/python -m app.cli "/mnt/chromeos/MyFiles/Downloads/My Documentary.mp4"  [--out OUTPUT_FOLDER]

Uses exactly the same pipeline as the studio (and shows up in the studio's library).
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from . import media, pipeline


def main() -> int:
    ap = argparse.ArgumentParser(description="Turn a long MP4 into finished shorts (every strong moment, up to 20).")
    ap.add_argument("video", type=Path)
    ap.add_argument("--out", type=Path, default=None, help="copy finished shorts + ZIP here")
    a = ap.parse_args()
    if not a.video.exists():
        print(f"File not found: {a.video}")
        return 2
    st = pipeline.new_job(a.video.name)
    d = pipeline.job_dir(st["id"])
    print(f"Job {st['id']}: copying video…")
    shutil.copy2(a.video, d / "source.mp4")
    try:
        info = media.validate(d / "source.mp4")
    except media.MediaError as e:
        pipeline.update(st["id"], status="failed", error=str(e))
        print("ERROR:", e)
        return 1
    pipeline.update(st["id"], status="ready", info=info.as_dict())
    pipeline.run_job(st["id"])
    st = pipeline.load(st["id"])
    print(f"\nStatus: {st['status']} - {st.get('message')}")
    if st.get("error"):
        print("Error:", st["error"])
    for c in st["clips"]:
        print(f"  [{c['status']}] {c['index']:02d} {c['title']}  ({media.fmt_ts(c['start'])}-{media.fmt_ts(c['end'])})"
              + (f"  -> {c.get('file')}" if c['status'] == 'done' else f"  {c.get('error') or ''}"))
    if a.out:
        a.out.mkdir(parents=True, exist_ok=True)
        z = d / "shorts.zip"
        if z.exists():
            shutil.copy2(z, a.out / z.name)
        for c in st["clips"]:
            if c["status"] == "done":
                cd = d / "clips" / f"short_{c['index']:02d}"
                shutil.copy2(cd / c["file"], a.out / c["file"])
                shutil.copy2(cd / "post.txt", a.out / (Path(c["file"]).stem + ".txt"))
        print(f"Copied to {a.out}")
    return 0 if st["status"] in ("done", "partial") else 1


if __name__ == "__main__":
    sys.exit(main())
