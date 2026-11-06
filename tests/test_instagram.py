"""Instagram posting with Meta's API faked out: upload -> processing -> publish -> permalink,
error handling, and the spaced posting queue.   python -m pytest tests/test_instagram.py"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import instagram, pipeline  # noqa: E402


class FakeMeta:
    """Records calls and answers like the Graph API."""

    def __init__(self, processing_polls=1, fail=None):
        self.calls = []
        self.polls = processing_polls
        self.fail = fail

    def __call__(self, method, url, params=None, headers=None, data=None, body_file=None, timeout=60):
        self.calls.append((method, url, dict(params or {}), dict(headers or {}), body_file))
        if self.fail and self.fail in url:
            raise instagram.InstagramError("Instagram's posting limit was reached for now.", retry_after=3600)
        if url.endswith("/me/accounts"):
            return {"data": [{"name": "Page", "instagram_business_account": {"id": "1784", "username": "clipz"}}]}
        if url.endswith("/1784") and method == "GET":
            return {"username": "clipz"}
        if url.endswith("/1784/media"):
            assert params["media_type"] == "REELS" and params["upload_type"] == "resumable"
            return {"id": "C1", "uri": "https://rupload.facebook.com/ig-api-upload/C1"}
        if "rupload.facebook.com" in url:
            assert headers["Authorization"].startswith("OAuth ") and int(headers["file_size"]) > 0
            return {"success": True, "message": "Upload successful."}
        if url.endswith("/C1") and method == "GET":
            self.polls -= 1
            return {"status_code": "IN_PROGRESS" if self.polls >= 0 else "FINISHED"}
        if url.endswith("/media_publish"):
            assert params["creation_id"] == "C1"
            return {"id": "M9"}
        if url.endswith("/M9"):
            return {"permalink": "https://www.instagram.com/reel/abc/"}
        raise AssertionError(url)


def _setup(tmp: Path, fake):
    instagram.STORE = tmp / "instagram.json"
    instagram.QUEUE = tmp / "instagram_queue.json"
    instagram._http = fake


def test_connect_finds_the_account_and_publish_works():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fake = FakeMeta(processing_polls=2)
        _setup(tmp, fake)
        st = instagram.connect("EAAB" + "x" * 40)
        assert st["connected"] and st["username"] == "clipz"
        video = tmp / "v.mp4"
        video.write_bytes(b"\0" * 1000)
        steps = []
        res = instagram.publish_reel(video, "caption #shorts", step=steps.append, poll_every=0)
        assert res == {"media_id": "M9", "permalink": "https://www.instagram.com/reel/abc/"}
        assert steps == ["uploading", "processing", "publishing"]
        up = next(c for c in fake.calls if "rupload" in c[1])
        assert up[4] == video     # the local file itself is streamed up


def test_queue_spaces_posts_and_marks_clips():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fake = FakeMeta(processing_polls=0)
        _setup(tmp, fake)
        instagram.connect("EAAB" + "x" * 40, "1784")
        instagram.set_options(auto=True, gap_minutes=60)
        st = pipeline.new_job("vod.mp4")
        jid = st["id"]
        jd = pipeline.job_dir(jid)
        clips = []
        for k in (1, 2):
            cd = jd / "clips" / f"short_{k:02d}"
            cd.mkdir(parents=True)
            (cd / f"{k:02d}_x.mp4").write_bytes(b"\0" * 500)
            clips.append({"index": k, "status": "done", "file": f"{k:02d}_x.mp4", "description": "d",
                          "post_kit": {"instagram": f"caption {k}"}})
        st = pipeline.load(jid)
        st["clips"] = clips
        pipeline.save(st)
        try:
            instagram.enqueue(jid, 1)
            instagram.enqueue(jid, 2)
            item, _ = instagram._next_item()
            assert item["index"] == 1
            instagram._post_item(item)
            c1 = pipeline.load(jid)["clips"][0]["instagram"]
            assert c1["status"] == "published" and c1["permalink"].endswith("/abc/")
            # The second one waits for the 60-minute spacing...
            item, wait = instagram._next_item()
            assert item is None and wait > 3000
            # ...unless it is posted with "Post now".
            instagram.enqueue(jid, 2, caption="edited", now=True)
            item, _ = instagram._next_item()
            assert item["index"] == 2 and item["caption"] == "edited"
            # A rate limit keeps it queued and retries later instead of failing it.
            instagram._http = FakeMeta(fail="/media")
            instagram._post_item(item)
            c2 = pipeline.load(jid)["clips"][1]["instagram"]
            assert c2["status"] == "queued" and "limit" in c2["error"]
            assert json.loads(instagram.QUEUE.read_text())[0]["not_before"] > time.time() + 3000
            instagram.dequeue(jid, 2)
            assert instagram.status()["queued"] == 0
        finally:
            pipeline.delete_job(jid)


def test_bad_token_is_explained():
    e = instagram._explain(400, json.dumps({"error": {"code": 190, "message": "Error validating access token"}}))
    assert "token" in str(e) and e.retry_after is None
    e = instagram._explain(400, json.dumps({"error": {"code": 4, "message": "Application request limit reached"}}))
    assert e.retry_after == 3600
