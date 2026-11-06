"""Unit checks for stream mode, campaign briefs, chunked Claude analysis and the results log.
No video or network needed:  python -m pytest tests  (or python tests/test_stream.py)"""
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import analyze, brief, captions, config, results  # noqa: E402


def _words(text, start=0.0, wps=3.0):
    out, t = [], start
    for w in text.split():
        out.append({"w": w, "s": round(t, 3), "e": round(t + 0.28, 3), "p": 0.9})
        t += 1 / wps
    return out


def _stream_words(minutes=40):
    """Chatty filler with a loud funny moment every ~5 minutes."""
    words, t = [], 0.0
    filler = "So I was just looking at the map and thinking about where we should go next today."
    funny = "Wait wait what. No way he just did that. Bro that is insane, I'm crying. Clip that right now!"
    k = 0
    while t < minutes * 60:
        text = funny if k % 25 == 12 else filler
        ws = _words(text, t)
        words += ws
        t = ws[-1]["e"] + 0.6
        k += 1
    return words


def _energy_for(words, total):
    hop = 0.5
    z = [0.0] * int(total / hop + 2)
    for w in words:
        if w["w"].startswith(("insane", "crying", "way")):
            for k in range(int(w["s"] / hop) - 2, int(w["e"] / hop) + 3):
                if 0 <= k < len(z):
                    z[k] = 4.5
    return {"hop": hop, "db": [0.0] * len(z), "z": z}


def test_brief_parsing_and_checks():
    text = ("Get paid $2/1000 views clipping for Seal!\n- Clips must be 15-60 seconds\n"
            "- Must include #sealclips and tag @sealstream in the caption\n"
            "- Caption must say \"Watch Seal live on Twitch\"\n- Do not post gambling segments.")
    r = brief.parse(text)
    assert r["required_hashtags"] == ["#sealclips"] and r["required_mentions"] == ["@sealstream"]
    assert (r["min_seconds"], r["max_seconds"]) == (15, 60)
    assert r["other_rules"] == ["Do not post gambling segments."]   # auto-checked lines aren't repeated
    c = brief.apply({"description": "He lost it", "hashtags": ["#shorts"]}, r)
    assert c["hashtags"][0] == "#sealclips" and "@sealstream" in c["description"]
    assert brief.check(c, 42, r)["ok"] and not brief.check(c, 75, r)["ok"]
    merged = brief.merge(r, {"max_seconds": 45, "required_hashtags": ["ad"], "required_mentions": [],
                             "required_phrases": [], "min_seconds": 0, "other_rules": []})
    assert merged["max_seconds"] == 45 and "#ad" in merged["required_hashtags"]


def test_limits_follow_brief():
    L = analyze.limits_for("stream", {"min_seconds": 20, "max_seconds": 40})
    assert L.src_max <= 39.5 and L.src_min >= 20 and L.ideal_min <= L.ideal_max <= L.src_max
    assert analyze.limits_for("documentary") == analyze.DOC_LIMITS


def test_clip_count_by_mode():
    assert analyze.clip_count(10 * 60, "documentary") == 4
    assert analyze.clip_count(4 * 3600, "stream") == 30
    assert analyze.clip_count(40 * 60, "stream") == 5
    assert analyze.clip_count(40 * 60, "stream", 12) == 12


def test_offline_stream_finds_the_loud_funny_moments():
    words = _stream_words(40)
    total = words[-1]["e"]
    energy = _energy_for(words, total)
    res = analyze.select_segments({"words": words}, [], total, "vod.mp4",
                                  settings=config.clean_settings({"mode": "stream", "brief": "Clips 15-45 seconds. #ad"}),
                                  energy=energy)
    assert res["engine"] == "offline" and res["mode"] == "stream"
    assert len(res["clips"]) == 5
    hits = sum(1 for c in res["clips"] if "insane" in c["transcript"])
    assert hits >= 4, [c["transcript"][:80] for c in res["clips"]]
    for c in res["clips"]:
        assert c["end"] - c["start"] <= res["limits"]["src_max"] + 0.01
        assert "#ad" in c["hashtags"]
        assert "Chronicle" not in c["description"]


def _fake_client(calls):
    """Stands in for anthropic.Anthropic: answers each chunk with its first and last sentence windows."""
    class Stream:
        def __init__(self, kw):
            self.kw = kw

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            user = self.kw["messages"][0]["content"]
            idx = [int(x.split("]")[0]) for x in user.split("\n[")[1:]]
            calls.append((idx[0], idx[-1], self.kw["model"]))
            a, b = idx[0], idx[-1]
            cand = lambda i, j, o: {"start_sentence": i, "end_sentence": j, "working_title": f"w{i}", "hook": 7,
                                    "payoff": 7, "entertainment": 7, "energy": 7, "standalone_value": 7,
                                    "overall": o, "assessment": "ok"}
            data = {"documentary_summary": "s", "candidates": [cand(a + 2, a + 6, 80), cand(b - 6, b - 2, 70)],
                    "selected": [{"candidate_index": k, "title": f"T{a}-{k}", "description": "d",
                                  "hashtags": ["shorts"], "why_selected": "w", "highlight_words": []} for k in (0, 1)],
                    "brief_rules": {"required_hashtags": ["#fromclaude"], "required_mentions": [],
                                    "required_phrases": [], "min_seconds": 0, "max_seconds": 0, "other_rules": []}}
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=json.dumps(data))],
                                   usage=SimpleNamespace(input_tokens=50_000, output_tokens=2_000))

    return SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: Stream(kw))))


def test_claude_reads_long_vods_in_chunks():
    words = _stream_words(150)
    total = words[-1]["e"]
    calls = []
    old_key, old_mode = config.ANTHROPIC_API_KEY, config.ANALYSIS_MODE
    config.ANTHROPIC_API_KEY, config.ANALYSIS_MODE = "test", "auto"
    try:
        res = analyze.select_segments({"words": words}, [], total, "vod.mp4",
                                      settings=config.clean_settings({"mode": "stream", "model": "claude-sonnet-5-5"}),
                                      energy=_energy_for(words, total), client=_fake_client(calls))
    finally:
        config.ANTHROPIC_API_KEY, config.ANALYSIS_MODE = old_key, old_mode
    assert len(calls) == 3 and all(m == "claude-sonnet-5-5" for *_, m in calls)   # 150 min in 60-min parts
    assert res["engine"].startswith("claude") and res["usage"]["chunks"] == 3
    assert abs(res["usage"]["cost"] - 3 * (50_000 * 2 + 2_000 * 10) / 1e6) < 1e-6
    assert all("#fromclaude" in c["hashtags"] for c in res["clips"])
    spans = sorted((c["start"], c["end"]) for c in res["clips"])
    assert all(b[0] >= a[1] for a, b in zip(spans, spans[1:]))                    # no overlaps


def test_pop_captions_style():
    ass = captions.build_ass(_words("In 1453 Mehmed took the city."), 5.0, 700, "Hook", 230, 120, [],
                             style="pop", show_tag=False, title_dur=3.5)
    assert captions.YELLOW in ass and "CHRONICLE" not in ass and ",Cap,{font}" not in ass
    assert "0:00:03.50,Title" in ass
    groups = captions.group_words(_words("one two three four five"), 2, 14)
    assert max(len(g) for g in groups) == 2


def test_results_log_feeds_examples():
    with tempfile.TemporaryDirectory() as d:
        old = config.RESULTS_FILE
        config.RESULTS_FILE = Path(d) / "results.json"
        try:
            job = {"id": "abc", "filename": "vod.mp4", "settings": {"mode": "stream"}}
            for k, v in enumerate([500, 90_000, 3_000, 12]):
                results.record(job, {"index": k, "title": f"clip {k}", "transcript": "hook text"}, v, None, "")
            results.record(job, {"index": 1, "title": "clip 1", "transcript": "hook text"}, 95_000, 95.0, "")
            ex = results.examples("stream")
            assert ex["best"][0]["views"] == 95_000 and len(results.load()) == 4
            assert "95,000 views" in results.prompt_block("stream") and results.prompt_block("documentary") == ""
        finally:
            config.RESULTS_FILE = old


def test_settings_are_validated():
    s = config.clean_settings({"mode": "stream", "layout": "nope", "facecam": {"corner": "xx", "size": 9},
                               "max_clips": "500", "model": "gpt"})
    assert s["layout"] == "fit" and s["facecam"] == {"corner": "bl", "size": 0.5}
    assert s["max_clips"] == 40 and s["model"] == config.CLAUDE_MODEL and s["captions"] == "pop"


def test_credit_ledger_and_estimate():
    from app import credits
    with tempfile.TemporaryDirectory() as d:
        old_dir, old_root, old_key = config.DATA_DIR, config.ROOT, config.ANTHROPIC_API_KEY
        config.DATA_DIR, config.ROOT = Path(d), Path(d)
        (Path(d) / ".env").write_text("CH_WHISPER_MODEL=base\nANTHROPIC_API_KEY=\n")
        try:
            assert credits.summary()["remaining"] is None
            credits.set_balance(5)
            credits.record_run("j1", "claude-sonnet-5-5", 0.06, 10, 3, "stream")
            credits.record_run("j1", "claude-sonnet-5-5", 0.05, 10, 3, "stream")   # same job replaces
            credits.add_credits(10)
            s = credits.summary()
            assert s["remaining"] == 14.95 and s["runs"] == 1 and abs(s["avg_per_clip"] - 0.05 / 3) < 1e-4
            e = credits.estimate(240, "claude-opus-5-5", "stream", 30)
            assert e["parts"] == 4 and 0 < e["low"] < e["high"] < 3
            assert credits.estimate(10, "claude-haiku-5-5", "documentary", 4)["high"] < 0.05
            credits.save_key("sk-ant-api03-" + "x" * 40)
            env = (Path(d) / ".env").read_text()
            assert env.count("ANTHROPIC_API_KEY=sk-ant-") == 1 and "CH_WHISPER_MODEL=base" in env
            assert credits.summary()["connected"] and credits.key_hint() == "…xxxx"
            assert credits.check_key("hello") is not None
            credits.save_key(None)
            assert not credits.summary()["connected"]
        finally:
            config.DATA_DIR, config.ROOT, config.ANTHROPIC_API_KEY = old_dir, old_root, old_key


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
