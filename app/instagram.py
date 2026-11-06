"""Posts finished shorts to Instagram as Reels, straight from this Chromebook.

Uses Meta's official Instagram Graph API (Facebook Login for Business), whose resumable
upload accepts a local file, so nothing has to be hosted anywhere:

  1. POST graph.facebook.com/<ig-user-id>/media        media_type=REELS, upload_type=resumable
  2. POST rupload.facebook.com/ig-api-upload/<container> the MP4 bytes
  3. GET  graph.facebook.com/<container>?fields=status_code   until FINISHED
  4. POST graph.facebook.com/<ig-user-id>/media_publish creation_id=<container>
  5. GET  graph.facebook.com/<media-id>?fields=permalink

Posts go through a queue (data/instagram_queue.json) worked by one background thread, spaced
`gap_minutes` apart so a run of 20 shorts doesn't all land at once. A queue survives restarts.

Needs an Instagram Professional (creator or business) account linked to a Facebook Page, and
a user access token with instagram_basic, instagram_content_publish, pages_show_list and
pages_read_engagement. See the README.

Posting to Instagram does NOT enter a clip in a Whop campaign: the link still has to be
submitted on Whop. The studio says so under the shorts.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from . import config

GRAPH = "https://graph.facebook.com"
RUPLOAD = "https://rupload.facebook.com/ig-api-upload"
# Empty = the app's default Graph API version. Set e.g. CH_GRAPH_VERSION=v24.0 to pin one.
VERSION = os.environ.get("CH_GRAPH_VERSION", "").strip().strip("/")

STORE = config.DATA_DIR / "instagram.json"
QUEUE = config.DATA_DIR / "instagram_queue.json"
GAPS = (0, 15, 30, 60, 90, 120, 180, 240)
_lock = threading.RLock()
_worker: threading.Thread | None = None
_wake = threading.Event()


class InstagramError(RuntimeError):
    """A plain-English problem to show in the studio."""

    def __init__(self, msg: str, retry_after: float | None = None):
        super().__init__(msg)
        self.retry_after = retry_after


# ----------------------------------------------------------------- settings

def _load() -> dict:
    d = {"token": "", "user_id": "", "username": "", "auto": False, "gap_minutes": 60, "last_post_at": 0.0}
    try:
        d.update(json.loads(STORE.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        pass
    # .env overrides, for people who prefer to keep secrets there.
    if os.environ.get("IG_ACCESS_TOKEN"):
        d["token"] = os.environ["IG_ACCESS_TOKEN"].strip()
    if os.environ.get("IG_USER_ID"):
        d["user_id"] = os.environ["IG_USER_ID"].strip()
    return d


def _save(d: dict) -> None:
    STORE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    os.chmod(tmp, 0o600)   # holds the access token
    tmp.replace(STORE)


def connected() -> bool:
    d = _load()
    return bool(d["token"] and d["user_id"])


def auto_enabled() -> bool:
    d = _load()
    return bool(d["auto"] and d["token"] and d["user_id"])


# ----------------------------------------------------------------- HTTP

def _url(host: str, path: str) -> str:
    return f"{host}/{VERSION + '/' if VERSION else ''}{path.lstrip('/')}"


def _explain(status: int, body: str) -> InstagramError:
    try:
        err = json.loads(body).get("error", {})
    except ValueError:
        err = {}
    msg = err.get("error_user_msg") or err.get("message") or body[:300] or f"HTTP {status}"
    code = err.get("code")
    if code == 190 or status == 401:
        return InstagramError("Instagram didn't accept the access token (it may have expired). "
                              "Reconnect Instagram with a fresh token.")
    if code in (4, 17, 32, 613) or err.get("error_subcode") == 2207042 or status == 429:
        return InstagramError("Instagram's posting limit was reached for now. The queue will try again in an hour.",
                              retry_after=3600)
    if code == 10 or code == 200:
        return InstagramError("The token is missing a permission. It needs instagram_basic, "
                              "instagram_content_publish, pages_show_list and pages_read_engagement.")
    return InstagramError(f"Instagram said: {msg}")


def _http(method: str, url: str, params: dict | None = None, headers: dict | None = None,
          data: bytes | None = None, body_file: Path | None = None, timeout: float = 60) -> dict:
    """One Graph API request. Returns the JSON answer or raises InstagramError."""
    headers = dict(headers or {})
    if params:
        enc = urllib.parse.urlencode(params)
        if method == "GET" or body_file is not None or data is not None:
            url += ("&" if "?" in url else "?") + enc
        else:
            data = enc.encode()
            headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
    fh = None
    try:
        if body_file is not None:
            fh = open(body_file, "rb")
            headers["Content-Length"] = str(body_file.stat().st_size)
            req = urllib.request.Request(url, data=fh, headers=headers, method=method)  # type: ignore[arg-type]
        else:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        raise _explain(e.code, e.read().decode("utf-8", "replace")) from None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise InstagramError(f"Couldn't reach Instagram ({getattr(e, 'reason', e)}). "
                             "Check the internet connection; the queue will retry.", retry_after=600) from None
    finally:
        if fh:
            fh.close()
    try:
        return json.loads(raw) if raw else {}
    except ValueError:
        return {"raw": raw}


# ----------------------------------------------------------------- connecting

def connect(token: str, user_id: str = "", app_id: str = "", app_secret: str = "") -> dict:
    """Check the token, find the Instagram account if no ID was given, and save it.
    With an app ID + secret, a short-lived Graph API Explorer token is swapped for a 60-day one."""
    token = token.strip()
    if len(token) < 20:
        raise InstagramError("Paste the full access token.")
    if app_id.strip() and app_secret.strip():
        r = _http("GET", _url(GRAPH, "oauth/access_token"),
                  {"grant_type": "fb_exchange_token", "client_id": app_id.strip(),
                   "client_secret": app_secret.strip(), "fb_exchange_token": token})
        token = r.get("access_token") or token
    user_id = user_id.strip()
    if not user_id:
        pages = _http("GET", _url(GRAPH, "me/accounts"),
                      {"fields": "name,instagram_business_account{id,username}", "access_token": token})
        accts = [p["instagram_business_account"] for p in pages.get("data", []) if p.get("instagram_business_account")]
        if not accts:
            raise InstagramError("No Instagram Professional account was found on this token's Facebook Pages. "
                                 "Switch the Instagram account to Creator or Business, link it to a Facebook Page, "
                                 "then make a new token.")
        user_id = accts[0]["id"]
    me = _http("GET", _url(GRAPH, user_id), {"fields": "username", "access_token": token})
    d = _load()
    d.update(token=token, user_id=user_id, username=me.get("username", ""))
    _save(d)
    ensure_worker()
    return status()


def disconnect() -> dict:
    d = _load()
    d.update(token="", user_id="", username="", auto=False)
    _save(d)
    return status()


def set_options(auto: bool | None = None, gap_minutes: int | None = None) -> dict:
    d = _load()
    if auto is not None:
        d["auto"] = bool(auto)
    if gap_minutes is not None:
        d["gap_minutes"] = min(GAPS, key=lambda g: abs(g - int(gap_minutes)))
    _save(d)
    _wake.set()
    return status()


def status() -> dict:
    d = _load()
    q = _queue()
    nxt = None
    if q:
        nxt = max(time.time(), min(x.get("not_before", 0) for x in q),
                  d.get("last_post_at", 0) + d["gap_minutes"] * 60)
    return {"connected": bool(d["token"] and d["user_id"]), "username": d.get("username", ""),
            "auto": bool(d["auto"]), "gap_minutes": d["gap_minutes"], "gaps": list(GAPS),
            "queued": len(q), "next_post_at": nxt}


# ----------------------------------------------------------------- posting one reel

def publish_reel(video: Path, caption: str, step: Callable[[str], None] = lambda s: None,
                 poll_every: float = 5.0, max_wait: float = 900.0) -> dict:
    """Upload and publish one Reel. Returns {"media_id", "permalink"}."""
    d = _load()
    if not (d["token"] and d["user_id"]):
        raise InstagramError("Instagram isn't connected.")
    tok, uid = d["token"], d["user_id"]
    step("uploading")
    c = _http("POST", _url(GRAPH, f"{uid}/media"),
              {"media_type": "REELS", "upload_type": "resumable", "caption": caption[:2200],
               "share_to_feed": "true", "access_token": tok})
    cid = c.get("id")
    if not cid:
        raise InstagramError("Instagram didn't create an upload slot for the video.")
    r = _http("POST", _url(RUPLOAD, cid), headers={"Authorization": f"OAuth {tok}", "offset": "0",
                                                  "file_size": str(video.stat().st_size)},
              body_file=video, timeout=1800)
    if r.get("success") is False:
        raise InstagramError("The video upload to Instagram failed: " + str(r.get("message", r))[:300],
                             retry_after=600)
    step("processing")
    waited = 0.0
    while True:
        s = _http("GET", _url(GRAPH, cid), {"fields": "status_code,status", "access_token": tok})
        code = s.get("status_code")
        if code == "FINISHED":
            break
        if code in ("ERROR", "EXPIRED"):
            raise InstagramError("Instagram couldn't process the video: " + str(s.get("status") or code))
        if waited >= max_wait:
            raise InstagramError("Instagram is taking unusually long to process the video. It will be retried.",
                                 retry_after=900)
        time.sleep(poll_every)
        waited += poll_every
    step("publishing")
    p = _http("POST", _url(GRAPH, f"{uid}/media_publish"), {"creation_id": cid, "access_token": tok})
    mid = p.get("id")
    if not mid:
        raise InstagramError("Instagram didn't confirm the post.")
    link = ""
    try:
        link = _http("GET", _url(GRAPH, mid), {"fields": "permalink", "access_token": tok}).get("permalink", "")
    except InstagramError:
        pass
    return {"media_id": mid, "permalink": link}


# ----------------------------------------------------------------- queue

def _queue() -> list[dict]:
    try:
        return json.loads(QUEUE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save_queue(q: list[dict]) -> None:
    QUEUE.parent.mkdir(parents=True, exist_ok=True)
    tmp = QUEUE.with_suffix(".tmp")
    tmp.write_text(json.dumps(q, indent=1), encoding="utf-8")
    tmp.replace(QUEUE)


def _mark(job_id: str, index: int, **kw) -> None:
    from . import pipeline
    with pipeline._lock:
        try:
            st = pipeline.load(job_id)
        except Exception:
            return
        for c in st.get("clips", []):
            if c["index"] == index:
                c["instagram"] = {**(c.get("instagram") or {}), **kw, "at": time.time()}
        pipeline.save(st)


def enqueue(job_id: str, index: int, caption: str | None = None, now: bool = False) -> dict:
    """Add a finished short to the posting queue. `now` skips the spacing (the Post now button)."""
    with _lock:
        q = [x for x in _queue() if not (x["job"] == job_id and x["index"] == index)]
        q.append({"job": job_id, "index": index, "caption": caption, "now": bool(now),
                  "added": time.time(), "not_before": 0.0, "tries": 0})
        _save_queue(q)
    _mark(job_id, index, status="queued", error=None)
    ensure_worker()
    _wake.set()
    return status()


def dequeue(job_id: str, index: int) -> dict:
    with _lock:
        q = _queue()
        left = [x for x in q if not (x["job"] == job_id and x["index"] == index)]
        _save_queue(left)
    if len(left) != len(q):
        _mark(job_id, index, status="cancelled")
    return status()


def _next_item() -> tuple[dict | None, float]:
    """The item to post now, or (None, seconds to wait)."""
    d = _load()
    q = _queue()
    if not q or not (d["token"] and d["user_id"]):
        return None, 3600.0
    now = time.time()
    spaced_at = d.get("last_post_at", 0) + d["gap_minutes"] * 60
    ready = [x for x in q if x.get("not_before", 0) <= now and (x.get("now") or spaced_at <= now)]
    if ready:
        ready.sort(key=lambda x: (not x.get("now"), x["added"]))
        return ready[0], 0.0
    soon = min(max(x.get("not_before", 0), now if x.get("now") else spaced_at) for x in q)
    return None, max(5.0, soon - now)


def _post_item(item: dict) -> None:
    from . import pipeline
    jid, k = item["job"], item["index"]
    try:
        st = pipeline.load(jid)
        clip = next(c for c in st["clips"] if c["index"] == k)
    except Exception:
        _drop(item)
        return
    if clip.get("status") != "done" or not clip.get("file"):
        _drop(item)
        _mark(jid, k, status="failed", error="The short isn't rendered yet.")
        return
    video = pipeline.job_dir(jid) / "clips" / f"short_{k:02d}" / clip["file"]
    caption = item.get("caption")
    if caption is None:
        caption = (clip.get("post_kit") or {}).get("instagram") or clip.get("description", "")
    try:
        res = publish_reel(video, caption, step=lambda s: _mark(jid, k, status=s, error=None))
    except InstagramError as e:
        if e.retry_after and item.get("tries", 0) < 3:
            with _lock:
                q = _queue()
                for x in q:
                    if x["job"] == jid and x["index"] == k:
                        x["tries"] = x.get("tries", 0) + 1
                        x["not_before"] = time.time() + e.retry_after
                _save_queue(q)
            _mark(jid, k, status="queued", error=str(e))
        else:
            _drop(item)
            _mark(jid, k, status="failed", error=str(e))
        return
    _drop(item)
    d = _load()
    d["last_post_at"] = time.time()
    _save(d)
    _mark(jid, k, status="published", error=None, permalink=res["permalink"], media_id=res["media_id"])
    pipeline.log(jid, f"Short {k} posted to Instagram{': ' + res['permalink'] if res['permalink'] else ''}")


def _drop(item: dict) -> None:
    with _lock:
        _save_queue([x for x in _queue() if not (x["job"] == item["job"] and x["index"] == item["index"])])


def _loop() -> None:
    while True:
        try:
            item, wait = _next_item()
            if item:
                _post_item(item)
                continue
        except Exception:
            traceback.print_exc()
            wait = 60.0
        _wake.wait(timeout=min(wait, 3600.0))
        _wake.clear()


def ensure_worker() -> None:
    """Start the posting thread (once). Clips stuck mid-upload by a restart go back in the queue."""
    global _worker
    with _lock:
        if _worker and _worker.is_alive():
            return
        _worker = threading.Thread(target=_loop, name="instagram-poster", daemon=True)
        _worker.start()
