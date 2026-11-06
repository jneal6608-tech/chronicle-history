"""Local web server: serves the studio UI and the processing API.

Run with:  ./start.sh  (or the Chronicle Shorts app in the ChromeOS launcher)
Then open: http://127.0.0.1:8765
"""
from __future__ import annotations

import importlib.util
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

from . import config, credits, leadfinder, media, outreach, pipeline, results, sitegen

app = FastAPI(title="Chronicle Shorts", docs_url=None, redoc_url=None)

# The studio page is normally served from this server. CORS + Private-Network-Access
# headers additionally let the hosted Claude Artifact version of the UI talk to it.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
                   expose_headers=["Content-Disposition"])


@app.middleware("http")
async def private_network(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["Access-Control-Allow-Private-Network"] = "true"
    return resp


@app.on_event("startup")
def _startup() -> None:
    pipeline.recover_interrupted()


# ------------------------------------------------------------------ UI

@app.get("/", response_class=HTMLResponse)
def index():
    return HTMLResponse((config.STATIC_DIR / "index.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ system check

@app.get("/api/health")
def health():
    checks = []
    ff = shutil.which("ffmpeg")
    ver = ""
    if ff:
        try:
            ver = subprocess.run([ff, "-version"], capture_output=True, text=True, timeout=20).stdout.split("\n")[0]
        except Exception:
            pass
    checks.append({"name": "FFmpeg", "ok": bool(ff and shutil.which("ffprobe")),
                   "detail": ver[:60] if ff else "Not found - run: sudo apt-get install -y ffmpeg"})
    fw = importlib.util.find_spec("faster_whisper") is not None
    checks.append({"name": "Speech-to-text", "ok": fw,
                   "detail": f"faster-whisper, model '{config.WHISPER_MODEL}'" if fw else "faster-whisper not installed - run: bash install.sh"})
    has_key = bool(config.ANTHROPIC_API_KEY)
    engine = ("Claude " + config.CLAUDE_MODEL) if (config.ANALYSIS_MODE != "offline" and has_key) else "Offline story scorer"
    checks.append({"name": "Story analysis", "ok": True, "warn": not has_key and config.ANALYSIS_MODE != "offline",
                   "detail": engine + ("" if has_key else " (connect Claude for smarter picks)")})
    free = shutil.disk_usage(config.DATA_DIR).free / 1e9
    checks.append({"name": "Disk space", "ok": free > 3, "detail": f"{free:.1f} GB free in {config.DATA_DIR}"})
    return {"ok": all(c["ok"] for c in checks), "checks": checks, "engine": engine,
            "max_upload_gb": config.MAX_UPLOAD_GB, "watermark": config.WATERMARK,
            "chromeos": config.IS_CHROMEOS, "has_key": has_key and config.ANALYSIS_MODE != "offline",
            "models": [{"id": m["id"], "label": m["label"]} for m in config.CLAUDE_MODELS],
            "default_model": config.CLAUDE_MODEL,
            "defaults": {m: config.default_settings(m) for m in config.MODES}}


# ------------------------------------------------------------------ upload

@app.post("/api/upload")
async def upload(request: Request):
    filename = request.headers.get("x-filename", "video.mp4")
    try:
        from urllib.parse import unquote
        filename = unquote(filename)
    except Exception:
        pass
    if not filename.lower().endswith((".mp4", ".m4v", ".mov")):
        raise HTTPException(400, "Please upload an MP4 file (.mp4).")
    max_bytes = int(config.MAX_UPLOAD_GB * 1e9)
    length = int(request.headers.get("content-length") or 0)
    if length > max_bytes:
        raise HTTPException(413, f"This file is {length/1e9:.1f} GB; the limit is {config.MAX_UPLOAD_GB:.0f} GB "
                                 "(change CH_MAX_UPLOAD_GB in .env).")
    free = shutil.disk_usage(config.DATA_DIR).free
    if length and free < length * 1.6 + 1e9:
        raise HTTPException(507, f"Not enough free disk space: {free/1e9:.1f} GB free, about "
                                 f"{(length*1.6+1e9)/1e9:.1f} GB needed.")
    st = pipeline.new_job(filename)
    d = pipeline.job_dir(st["id"])
    part = d / "source.mp4.part"
    written = 0
    try:
        with part.open("wb") as f:
            async for chunk in request.stream():
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(413, f"File exceeds the {config.MAX_UPLOAD_GB:.0f} GB limit.")
                f.write(chunk)
        if written == 0:
            raise HTTPException(400, "The upload was empty.")
        if length and written < length:
            raise HTTPException(400, "The upload was interrupted before it finished. Please try again.")
        part.replace(d / "source.mp4")
        info = media.validate(d / "source.mp4")
    except HTTPException as e:
        part.unlink(missing_ok=True)
        shutil.rmtree(d, ignore_errors=True)
        raise e
    except media.MediaError as e:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(422, str(e))
    except Exception as e:
        shutil.rmtree(d, ignore_errors=True)
        raise HTTPException(500, f"Upload failed: {e}")
    st = pipeline.update(st["id"], status="ready", info=info.as_dict(), message="Ready to generate shorts")
    return st


# ------------------------------------------------------------------ jobs

def _get(job_id: str) -> dict:
    try:
        return pipeline.load(job_id)
    except KeyError:
        raise HTTPException(404, "Job not found")


@app.get("/api/jobs")
def jobs():
    return pipeline.list_jobs()


@app.get("/api/jobs/{job_id}")
def job(job_id: str):
    return _get(job_id)


@app.post("/api/jobs/{job_id}/start")
async def start(job_id: str, request: Request):
    st = _get(job_id)
    if st["status"] == "uploading":
        raise HTTPException(409, "The upload has not finished yet.")
    if st["status"] in ("done",):
        return st
    body = None
    try:
        raw = await request.body()
        if raw:
            body = (await request.json()).get("settings")
    except Exception:
        raise HTTPException(400, "Settings could not be read.")
    pipeline.set_settings(job_id, body)
    pipeline.enqueue(job_id)
    return pipeline.load(job_id)


@app.post("/api/jobs/{job_id}/clips/{index}/retry")
def retry(job_id: str, index: int):
    _get(job_id)
    try:
        pipeline.retry_clip(job_id, index)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return pipeline.load(job_id)


@app.post("/api/jobs/{job_id}/clips/{index}/results")
async def clip_results(job_id: str, index: int, request: Request):
    """Log how a posted clip performed (views, optional earnings and post link)."""
    st = _get(job_id)
    c = next((c for c in st["clips"] if c["index"] == index), None)
    if not c:
        raise HTTPException(404, "Clip not found")
    try:
        body = await request.json()
        views = int(str(body.get("views", "")).replace(",", "").strip())
        if views < 0:
            raise ValueError
    except Exception:
        raise HTTPException(400, "Enter the number of views as a whole number.")
    earnings = None
    if str(body.get("earnings", "")).strip():
        try:
            earnings = round(float(str(body["earnings"]).replace("$", "").replace(",", "")), 2)
        except ValueError:
            raise HTTPException(400, "Earnings should be a number, like 4.50.")
    row = results.record(st, c, views, earnings, str(body.get("url", "")).strip())
    with pipeline._lock:
        stt = pipeline.load(job_id)
        for cc in stt["clips"]:
            if cc["index"] == index:
                cc["results"] = {"views": row["views"], "earnings": row["earnings"], "url": row["url"]}
        pipeline.save(stt)
    return pipeline.load(job_id)


@app.get("/api/results")
def results_log():
    rows = sorted(results.load(), key=lambda r: -(r.get("views") or 0))
    total_views = sum(r.get("views") or 0 for r in rows)
    total_earn = round(sum(r.get("earnings") or 0 for r in rows), 2)
    return {"rows": rows, "total_views": total_views, "total_earnings": total_earn}


# ------------------------------------------------------------------ Claude connection + credits

def _same_origin(request: Request) -> None:
    """Changing the key or balance is only allowed from the studio page itself,
    never from another website (CORS is open for the read-only endpoints)."""
    origin = request.headers.get("origin")
    if origin and origin.split("://", 1)[-1] != request.headers.get("host", ""):
        raise HTTPException(403, "Open the studio itself to change this.")


@app.get("/api/credits")
def credits_summary():
    return credits.summary()


@app.get("/api/credits/estimate")
def credits_estimate(minutes: float, model: str = "", mode: str = "documentary", clips: int = 0):
    model = model if model in {m["id"] for m in config.CLAUDE_MODELS} else config.CLAUDE_MODEL
    mode = mode if mode in config.MODES else "documentary"
    from .analyze import clip_count
    n = clip_count(minutes * 60, mode, max(0, clips))
    return {**credits.estimate(max(0.1, minutes), model, mode, n), "clips": n}


@app.post("/api/credits/key")
async def credits_key(request: Request):
    _same_origin(request)
    body = await request.json()
    key = str(body.get("key", "")).strip()
    problem = credits.check_key(key)
    if problem:
        raise HTTPException(400, problem)
    credits.save_key(key)
    return credits.summary()


@app.delete("/api/credits/key")
def credits_key_remove(request: Request):
    _same_origin(request)
    credits.save_key(None)
    return credits.summary()


@app.post("/api/credits/balance")
async def credits_balance(request: Request):
    _same_origin(request)
    body = await request.json()
    try:
        amount = float(str(body.get("amount", "")).replace("$", "").replace(",", "").strip())
        if amount < 0 or amount > 100000:
            raise ValueError
    except ValueError:
        raise HTTPException(400, "Enter a dollar amount, like 5 or 12.50.")
    return credits.add_credits(amount) if body.get("action") == "add" else credits.set_balance(amount)


# ------------------------------------------------------------------ lead finder

@app.get("/leads", response_class=HTMLResponse)
def leads_page():
    return HTMLResponse((config.STATIC_DIR / "leads.html").read_text(encoding="utf-8"),
                        headers={"Cache-Control": "no-store"})


def _lead_settings() -> dict:
    return outreach.settings(leadfinder.load().get("settings"))


@app.get("/api/leads")
def leads_list(all: int = 0):
    s = _lead_settings()
    rows = leadfinder.listing(bool(all))
    for r in rows:
        r["email"] = outreach.email(r, s)
        r["call_script"] = outreach.call_script(r, s)
        r["find_email"] = outreach.search_email_url(r)
        r["site_job"] = sitegen.JOBS.get(r["id"])
    d = leadfinder.load()
    return {"rows": rows, "run": leadfinder.RUN, "usage": leadfinder.usage(), "free_monthly": leadfinder.FREE_MONTHLY,
            "settings": s, "home": d.get("home"),
            "keys": {"google": bool(config.GOOGLE_MAPS_API_KEY), "claude": bool(config.ANTHROPIC_API_KEY),
                     "github": bool(config.GITHUB_TOKEN and config.GITHUB_PREVIEW_REPO),
                     "github_repo": config.GITHUB_PREVIEW_REPO},
            "categories": [{"id": q, "label": l} for q, l, _ in leadfinder.CATEGORIES],
            "default_categories": leadfinder.DEFAULT_CATEGORIES, "site_model": config.SITE_MODEL,
            "total_found": sum(1 for x in d["leads"].values() if x.get("google"))}


@app.post("/api/leads/settings")
async def leads_settings(request: Request):
    _same_origin(request)
    body = await request.json()
    with leadfinder._lock:
        d = leadfinder.load()
        cur = outreach.settings(d.get("settings"))
        for k in outreach.DEFAULT_SETTINGS:
            if k in body:
                v = body[k]
                if k in ("target", "max_miles", "budget"):
                    try:
                        v = max(1, min({"target": 500, "max_miles": 200, "budget": 20000}[k], int(float(v))))
                    except (TypeError, ValueError):
                        continue
                elif k == "categories":
                    v = [c for c in (v or []) if c in leadfinder.CAT]
                else:
                    v = str(v)[:4000]
                cur[k] = v
        d["settings"] = cur
        leadfinder.save(d)
    return cur


@app.post("/api/leads/keys")
async def leads_keys(request: Request):
    _same_origin(request)
    body = await request.json()
    if "google_key" in body:
        k = str(body["google_key"] or "").strip()
        if k and not re.fullmatch(r"AIza[0-9A-Za-z_\-]{30,}", k):
            raise HTTPException(400, "That doesn't look like a Google API key. They start with AIza.")
        config.write_env("GOOGLE_MAPS_API_KEY", k or None)
    if "github_token" in body or "github_repo" in body:
        token = str(body.get("github_token") or config.GITHUB_TOKEN).strip()
        repo = str(body.get("github_repo") or config.GITHUB_PREVIEW_REPO).strip().removeprefix("https://github.com/").strip("/")
        if token or repo:
            problem = sitegen.check_github(token, repo)
            if problem:
                raise HTTPException(400, problem)
        config.write_env("GITHUB_TOKEN", token or None)
        config.write_env("GITHUB_PREVIEW_REPO", repo or None)
    return {"google": bool(config.GOOGLE_MAPS_API_KEY),
            "github": bool(config.GITHUB_TOKEN and config.GITHUB_PREVIEW_REPO)}


@app.post("/api/leads/search")
async def leads_search(request: Request):
    _same_origin(request)
    if not config.GOOGLE_MAPS_API_KEY:
        raise HTTPException(400, "Add your Google Maps key in Settings first.")
    if leadfinder.RUN["running"]:
        raise HTTPException(409, "A search is already running.")
    s = _lead_settings()
    if not s["location"].strip():
        raise HTTPException(400, "Enter your town or ZIP code in Settings first.")
    threading.Thread(target=leadfinder.run_search, daemon=True,
                     args=(s["location"], config.GOOGLE_MAPS_API_KEY, s["categories"] or leadfinder.DEFAULT_CATEGORIES,
                           int(s["target"]), float(s["max_miles"]), int(s["budget"]))).start()
    return {"started": True}


@app.post("/api/leads/{lead_id}")
async def lead_update(lead_id: str, request: Request):
    _same_origin(request)
    body = await request.json()
    fields = {}
    if body.get("status") in ("new", "contacted", "replied", "won", "not_interested"):
        fields["status"] = body["status"]
        if body["status"] == "contacted":
            fields["contacted_at"] = time.time()
    if "notes" in body:
        fields["notes"] = str(body["notes"])[:2000]
    if not leadfinder.update_lead(lead_id, **fields):
        raise HTTPException(404, "Lead not found")
    return {"ok": True}


@app.post("/api/leads/{lead_id}/site")
async def lead_site(lead_id: str, request: Request):
    _same_origin(request)
    if not config.ANTHROPIC_API_KEY:
        raise HTTPException(400, "Connect Claude first: on the main page, pick Stream / podcast and click Connect Claude.")
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    model = body.get("model") if body.get("model") in {m["id"] for m in config.CLAUDE_MODELS} else None
    sitegen.start(lead_id, model)
    return {"started": True}


@app.get("/leads/site/{lead_id}", response_class=HTMLResponse)
def lead_site_preview(lead_id: str):
    if not re.fullmatch(r"[A-Za-z0-9_\-]{5,200}", lead_id):
        raise HTTPException(404)
    f = config.DATA_DIR / "sites" / lead_id / "index.html"
    if not f.exists():
        raise HTTPException(404, "No site made for this business yet.")
    return HTMLResponse(f.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store"})


@app.delete("/api/jobs/{job_id}")
def delete(job_id: str):
    _get(job_id)
    try:
        pipeline.delete_job(job_id)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"deleted": job_id}


@app.get("/api/jobs/{job_id}/source")
def source(job_id: str):
    _get(job_id)
    p = pipeline.job_dir(job_id) / "source.mp4"
    if not p.exists():
        raise HTTPException(404, "Source video not found")
    return FileResponse(p, media_type="video/mp4")


@app.get("/api/jobs/{job_id}/clips/{index}/video")
def clip_video(job_id: str, index: int, download: int = 0):
    st = _get(job_id)
    c = next((c for c in st["clips"] if c["index"] == index and c["status"] == "done"), None)
    if not c:
        raise HTTPException(404, "This short is not ready")
    p = pipeline.job_dir(job_id) / "clips" / f"short_{index:02d}" / c["file"]
    if not p.exists():
        raise HTTPException(404, "File missing")
    return FileResponse(p, media_type="video/mp4", filename=c["file"] if download else None,
                        content_disposition_type="attachment" if download else "inline")


@app.get("/api/jobs/{job_id}/zip")
def zip_all(job_id: str):
    st = _get(job_id)
    p = pipeline.job_dir(job_id) / "shorts.zip"
    if not p.exists():
        p = pipeline.build_zip(job_id)
    if not p or not p.exists():
        raise HTTPException(404, "No finished shorts to download yet")
    name = pipeline._slug(Path(st["filename"]).stem) + "-shorts.zip"
    return FileResponse(p, media_type="application/zip", filename=name)


@app.exception_handler(HTTPException)
async def _http_err(request: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code,
                        headers={"Access-Control-Allow-Origin": "*"})
