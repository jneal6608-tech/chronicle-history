"""Job state + the end-to-end pipeline. Every stage caches its result on disk,
so an interrupted or failed job resumes where it stopped instead of starting over."""
from __future__ import annotations

import json
import queue
import re
import shutil
import threading
import time
import traceback
import uuid
import zipfile
from pathlib import Path
from typing import Optional

from . import analyze, brief, config, media, render, transcribe

STAGES = ["upload", "transcribe", "analyze", "select", "render", "export"]
STAGE_LABELS = {
    "upload": "Upload MP4",
    "transcribe": "Analyze video",
    "analyze": "Select best moments",
    "select": "Select best moments",
    "render": "Generate clips",
    "export": "Download shorts",
}

_lock = threading.RLock()
_queue: "queue.Queue[str]" = queue.Queue()
_worker: Optional[threading.Thread] = None


# ------------------------------------------------------------------ state helpers

def job_dir(job_id: str) -> Path:
    if not re.fullmatch(r"[a-f0-9]{12}", job_id):
        raise KeyError(job_id)
    return config.JOBS_DIR / job_id


def load(job_id: str) -> dict:
    p = job_dir(job_id) / "state.json"
    if not p.exists():
        raise KeyError(job_id)
    with _lock:
        return json.loads(p.read_text(encoding="utf-8"))


def save(state: dict) -> None:
    state["updated"] = time.time()
    p = job_dir(state["id"]) / "state.json"
    tmp = p.with_suffix(".tmp")
    with _lock:
        tmp.write_text(json.dumps(state, indent=1), encoding="utf-8")
        tmp.replace(p)


def update(job_id: str, **kw) -> dict:
    with _lock:
        st = load(job_id)
        st.update(kw)
        save(st)
        return st


def log(job_id: str, msg: str) -> None:
    with _lock:
        st = load(job_id)
        st.setdefault("log", []).append(f"{time.strftime('%H:%M:%S')}  {msg}")
        st["log"] = st["log"][-200:]
        save(st)
    print(f"[{job_id}] {msg}", flush=True)


def new_job(filename: str) -> dict:
    jid = uuid.uuid4().hex[:12]
    d = config.JOBS_DIR / jid
    d.mkdir(parents=True)
    safe = re.sub(r"[^\w.\- ]+", "_", Path(filename).name)[:120] or "video.mp4"
    st = {"id": jid, "filename": safe, "created": time.time(), "status": "uploading",
          "stage": "upload", "progress": 0.0, "message": "Uploading…", "error": None,
          "info": None, "analysis": None, "clips": [], "log": [], "settings": config.default_settings()}
    (d / "state.json").write_text(json.dumps(st), encoding="utf-8")
    return st


def list_jobs() -> list[dict]:
    out = []
    for p in sorted(config.JOBS_DIR.glob("*/state.json"), key=lambda p: -p.stat().st_mtime):
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        out.append({k: st.get(k) for k in ("id", "filename", "created", "status", "stage", "message")}
                   | {"mode": (st.get("settings") or {}).get("mode", "documentary")}
                   | {"clips_done": sum(1 for c in st.get("clips", []) if c.get("status") == "done"),
                      "clips_total": len(st.get("clips", []))})
    return out


def delete_job(job_id: str) -> None:
    d = job_dir(job_id)
    st = load(job_id)
    if st["status"] in ("running", "queued"):
        raise RuntimeError("This job is still processing.")
    shutil.rmtree(d)


# ------------------------------------------------------------------ queue / worker

def set_settings(job_id: str, raw: dict | None) -> dict:
    """Store the studio's choices for a job. Only allowed before moments are picked,
    because the analysis is cached and would otherwise not match the settings."""
    st = load(job_id)
    if raw is None:
        return st
    if (job_dir(job_id) / "analysis.json").exists():
        return st
    return update(job_id, settings=config.clean_settings(raw))


def enqueue(job_id: str) -> None:
    st = load(job_id)
    if st["status"] in ("running", "queued"):
        return
    update(job_id, status="queued", message="Waiting for the processor…", error=None)
    _queue.put(job_id)
    _ensure_worker()


def _ensure_worker() -> None:
    global _worker
    with _lock:
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_work_loop, daemon=True, name="pipeline")
            _worker.start()


def _work_loop() -> None:
    while True:
        jid = _queue.get()
        try:
            run_job(jid)
        except Exception as e:  # last-resort guard; run_job handles its own errors
            traceback.print_exc()
            try:
                update(jid, status="failed", error=str(e))
            except Exception:
                pass


def recover_interrupted() -> None:
    """Called on server start: jobs that were mid-run when the app closed become resumable."""
    for p in config.JOBS_DIR.glob("*/state.json"):
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if st.get("status") in ("running", "queued"):
            st["status"] = "interrupted"
            st["message"] = "Processing was interrupted (the app was closed). Click Resume to continue."
            for c in st.get("clips", []):
                if c.get("status") == "rendering":
                    c["status"] = "pending"
            p.write_text(json.dumps(st), encoding="utf-8")
        if st.get("status") == "uploading":
            st["status"] = "failed"
            st["error"] = "The upload did not finish. Please upload the file again."
            p.write_text(json.dumps(st), encoding="utf-8")


# ------------------------------------------------------------------ pipeline

def _prog(jid: str, stage: str, frac: float, msg: str) -> None:
    # Overall progress weights per stage.
    weights = {"transcribe": (0.0, 0.40), "analyze": (0.40, 0.50), "render": (0.50, 0.97), "export": (0.97, 1.0)}
    lo, hi = weights.get(stage, (0, 1))
    update(jid, stage=stage, progress=round(lo + (hi - lo) * max(0.0, min(1.0, frac)), 4), message=msg)


def run_job(jid: str, only_clip: Optional[int] = None) -> None:
    d = job_dir(jid)
    st = update(jid, status="running", error=None)
    src = d / "source.mp4"
    settings = st.get("settings") or config.default_settings()
    stream = settings.get("mode") == "stream"
    try:
        info = media.VideoInfo(**st["info"])

        # 1. Transcribe (cached)
        tpath = d / "transcript.json"
        if tpath.exists():
            transcript = json.loads(tpath.read_text(encoding="utf-8"))
        else:
            _prog(jid, "transcribe", 0.01, "Extracting narration audio…")
            wav = d / "audio.wav"
            media.extract_audio(src, wav)
            _prog(jid, "transcribe", 0.03, f"Loading speech model ({config.WHISPER_MODEL})… first run downloads it")
            if info.duration > 45 * 60:
                log(jid, f"Long video ({media.fmt_ts(info.duration)}): transcription can take a while on a Chromebook. "
                         "You can close this tab; progress is saved.")
            transcribe.load_model()
            log(jid, f"Transcribing with faster-whisper '{config.WHISPER_MODEL}'")
            transcript = transcribe.transcribe(
                wav, info.duration, tpath,
                progress=lambda f, t: _prog(jid, "transcribe", 0.05 + 0.80 * f, f"Transcribing speech… “{t}”"))
            wav.unlink(missing_ok=True)
            log(jid, f"Transcript: {len(transcript['words'])} words")

        # 2. Visual analysis (cached). Scene pacing only matters for documentaries, and decoding
        # a multi-hour stream just to count cuts would take ages, so streams skip it.
        vpath = d / "visual.json"
        if vpath.exists():
            visual = json.loads(vpath.read_text(encoding="utf-8"))
        else:
            if stream:
                scenes = []
            else:
                _prog(jid, "transcribe", 0.86, "Analysing visuals: scene changes and framing…")
                scenes = media.detect_scenes(src, info.duration,
                                             progress=lambda f: _prog(jid, "transcribe", 0.86 + 0.08 * f,
                                                                      "Analysing visuals: scene changes…"))
            box = media.detect_content_box(src, info)
            visual = {"scenes": scenes, "box": list(box)}
            vpath.write_text(json.dumps(visual), encoding="utf-8")
            log(jid, f"Visual analysis: {len(scenes)} scene changes, picture area {box[0]}x{box[1]}")

        # 2b. Loudness signal for streams (cached): reactions, laughter and hype show up as spikes.
        energy = None
        if stream:
            epath = d / "energy.json"
            if epath.exists():
                energy = json.loads(epath.read_text(encoding="utf-8"))
            else:
                _prog(jid, "transcribe", 0.95, "Measuring loudness to find reactions and hype…")
                energy = media.audio_energy(src, info.duration,
                                            progress=lambda f: _prog(jid, "transcribe", 0.95 + 0.05 * f,
                                                                     "Measuring loudness…"))
                epath.write_text(json.dumps(energy), encoding="utf-8")
                spikes = sum(1 for z in energy["z"] if z >= 3)
                log(jid, f"Loudness: {spikes} strong spikes (possible reactions/laughter)")

        # 3. Story analysis + selection (cached)
        apath = d / "analysis.json"
        if apath.exists():
            analysis = json.loads(apath.read_text(encoding="utf-8"))
        else:
            _prog(jid, "analyze", 0.02, "Reading everything and ranking candidate moments…")
            analysis = analyze.select_segments(
                transcript, visual["scenes"], info.duration, st["filename"],
                progress=lambda f, m: _prog(jid, "analyze", f, m), log=lambda m: log(jid, m),
                settings=settings, energy=energy)
            if not analysis["clips"]:
                raise RuntimeError("No usable moments were found in the speech.")
            apath.write_text(json.dumps(analysis, indent=1), encoding="utf-8")
            u = analysis.get("usage") or {}
            cost = f", Claude cost about ${u['cost']:.3f}" if u.get("cost") is not None else ""
            log(jid, f"Analysis engine: {analysis['engine']} - selected {len(analysis['clips'])} shorts "
                     f"from {len(analysis['candidates'])} ranked candidates{cost}")
        slim = {k: v for k, v in analysis.items() if k != "clips"}
        st = load(jid)
        if not st.get("clips"):
            st["clips"] = [{"index": k + 1, "status": "pending", "error": None, **c}
                           for k, c in enumerate(analysis["clips"])]
        st["analysis"] = slim
        save(st)

        # 4. Render each clip; a failure in one never loses the others.
        clips = st["clips"]
        todo = [c for c in clips if (only_clip is None and c["status"] != "done") or c["index"] == only_clip]
        n = len(clips)
        for c in todo:
            k = c["index"]
            cdir = d / "clips" / f"short_{k:02d}"

            def cprog(f, k=k):
                _prog(jid, "render", ((k - 1) + f) / n, f"Rendering short {k} of {n}…")

            _set_clip(jid, k, status="rendering", error=None)
            log(jid, f"Rendering short {k}: {media.fmt_ts(c['start'])}-{media.fmt_ts(c['end'])}")
            try:
                res = render.render_clip(src, info, tuple(visual["box"]), c, transcript["words"], cdir,
                                         progress=cprog, settings=settings)
                res.pop("words_out", None)
                fname = f"{k:02d}_{_slug(c['title'])}.mp4"
                (cdir / "short.mp4").replace(cdir / fname)
                compliance = brief.check(c, res["duration"], analysis.get("brief_rules") or {})
                meta = _clip_meta(c, res, st["filename"], compliance)
                (cdir / "metadata.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
                (cdir / "post.txt").write_text(_post_text(meta), encoding="utf-8")
                _set_clip(jid, k, status="done", file=fname, render=res, compliance=compliance)
                log(jid, f"Short {k} done: {res['duration']:.1f}s at {res['speed']}x")
            except Exception as e:
                traceback.print_exc()
                _set_clip(jid, k, status="failed", error=str(e)[:2000])
                log(jid, f"Short {k} FAILED: {str(e)[:300]}")

        # 5. Export ZIP of everything that succeeded.
        _prog(jid, "export", 0.2, "Packaging ZIP…")
        st = load(jid)
        done = [c for c in st["clips"] if c["status"] == "done"]
        build_zip(jid)
        failed = [c for c in st["clips"] if c["status"] == "failed"]
        if not done:
            update(jid, status="failed", stage="render", error="Every short failed to render. See the log "
                   "and the per-short errors, then click Retry.", message="Rendering failed")
        elif failed:
            update(jid, status="partial", stage="export", progress=1.0,
                   message=f"{len(done)} shorts ready, {len(failed)} failed - you can retry the failed ones.")
        else:
            update(jid, status="done", stage="export", progress=1.0, message=f"{len(done)} shorts ready")
    except (media.MediaError, transcribe.TranscriptionError) as e:
        log(jid, f"ERROR: {e}")
        update(jid, status="failed", error=str(e), message="Processing stopped")
    except Exception as e:
        traceback.print_exc()
        log(jid, f"ERROR: {type(e).__name__}: {e}")
        update(jid, status="failed", error=f"{type(e).__name__}: {e}", message="Processing stopped")


def _set_clip(jid: str, index: int, **kw) -> None:
    with _lock:
        st = load(jid)
        for c in st["clips"]:
            if c["index"] == index:
                c.update(kw)
        save(st)


def _slug(s: str) -> str:
    s = re.sub(r"[^A-Za-z0-9]+", "-", s).strip("-").lower()
    return (s[:50].rstrip("-") or "short")


def _clip_meta(c: dict, res: dict, source_name: str, compliance: dict | None = None) -> dict:
    return {"title": c["title"], "description": c["description"], "hashtags": c["hashtags"],
            "campaign_check": compliance or {"ok": True, "checks": [], "manual": []},
            "why_selected": c["why_selected"], "source_file": source_name,
            "source_start": media.fmt_ts(res["source_in"]), "source_end": media.fmt_ts(res["source_out"]),
            "source_start_seconds": round(res["source_in"], 2), "source_end_seconds": round(res["source_out"], 2),
            "duration_seconds": res["duration"], "playback_speed": res["speed"],
            "dead_air_removed_seconds": res["removed_seconds"], "framing": res["framing"]["reason"],
            "transcript": c.get("transcript", "")}


def _post_text(m: dict) -> str:
    return (f"TITLE\n{m['title']}\n\nCAPTION / DESCRIPTION\n{m['description']}\n\n"
            f"HASHTAGS\n{' '.join(m['hashtags'])}\n\n"
            f"SOURCE TIMESTAMPS\n{m['source_start']} - {m['source_end']} of {m['source_file']}\n\n"
            f"WHY THIS MOMENT\n{m['why_selected']}\n\n"
            f"DETAILS\nLength {m['duration_seconds']}s, speed {m['playback_speed']}x, "
            f"{m['dead_air_removed_seconds']}s of dead air removed. Framing: {m['framing']}.\n"
            + _campaign_text(m.get("campaign_check")))


def _campaign_text(cc: dict | None) -> str:
    if not cc or not (cc.get("checks") or cc.get("manual")):
        return ""
    lines = ["", "CAMPAIGN CHECK"]
    lines += [f"[{'OK' if c['ok'] else 'FIX'}] {c['label']}" for c in cc.get("checks", [])]
    lines += [f"[CHECK BY EYE] {r}" for r in cc.get("manual", [])]
    return "\n".join(lines) + "\n"


def build_zip(jid: str) -> Optional[Path]:
    d = job_dir(jid)
    st = load(jid)
    done = [c for c in st["clips"] if c["status"] == "done"]
    zp = d / "shorts.zip"
    if not done:
        zp.unlink(missing_ok=True)
        return None
    tmp = d / "shorts.zip.tmp"
    base = _slug(Path(st["filename"]).stem)
    with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED) as z:
        for c in done:
            cdir = d / "clips" / f"short_{c['index']:02d}"
            z.write(cdir / c["file"], f"{base}-shorts/{c['file']}")
            z.write(cdir / "post.txt", f"{base}-shorts/{Path(c['file']).stem}.txt")
            z.write(cdir / "metadata.json", f"{base}-shorts/{Path(c['file']).stem}.json")
    tmp.replace(zp)
    return zp


def retry_clip(jid: str, index: int) -> None:
    st = load(jid)
    if st["status"] in ("running", "queued"):
        raise RuntimeError("This job is still processing.")
    _set_clip(jid, index, status="pending", error=None)
    enqueue(jid)
