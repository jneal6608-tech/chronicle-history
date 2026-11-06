"""Fast unit checks that need no video:  python -m pytest tests  (or python tests/test_units.py)"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import analyze, captions, render  # noqa: E402


def _words(text, start=0.0, wps=2.5, gap_every=None):
    out, t = [], start
    for k, w in enumerate(text.split()):
        out.append({"w": w, "s": round(t, 3), "e": round(t + 0.32, 3), "p": 0.9})
        t += 1 / wps
        if gap_every and (k + 1) % gap_every == 0:
            t += 2.5
    return out


def test_sentences_split_on_punctuation():
    s = analyze.build_sentences(_words("The walls fell. The emperor died fighting. Rome was gone."))
    assert [x["text"] for x in s] == ["The walls fell.", "The emperor died fighting.", "Rome was gone."]


def test_claude_answer_is_validated_against_transcript():
    sents = analyze.build_sentences(_words(" ".join(f"Sentence number {i} about the empire." for i in range(200))))
    fake = {
        "documentary_summary": "x",
        "candidates": [
            {"start_sentence": 10, "end_sentence": 40, "working_title": "A", "storytelling": 8,
             "historical_significance": 8, "curiosity": 8, "pacing": 7, "standalone_value": 8, "overall": 90, "assessment": "good"},
            {"start_sentence": 30, "end_sentence": 60, "working_title": "Overlaps A", "storytelling": 5,
             "historical_significance": 5, "curiosity": 5, "pacing": 5, "standalone_value": 5, "overall": 60, "assessment": "overlap"},
            {"start_sentence": 900, "end_sentence": 950, "working_title": "Out of range", "storytelling": 9,
             "historical_significance": 9, "curiosity": 9, "pacing": 9, "standalone_value": 9, "overall": 99, "assessment": "bad idx"},
            {"start_sentence": 100, "end_sentence": 199, "working_title": "Too long", "storytelling": 7,
             "historical_significance": 7, "curiosity": 7, "pacing": 7, "standalone_value": 7, "overall": 80, "assessment": "long"},
        ],
        "selected": [
            {"candidate_index": 0, "title": "T0", "description": "d", "hashtags": ["history", "#shorts", "#bad tag!"],
             "why_selected": "w", "highlight_words": ["empire", "not-in-text-zzz"]},
            {"candidate_index": 1, "title": "T1", "description": "d", "hashtags": [], "why_selected": "w", "highlight_words": []},
            {"candidate_index": 2, "title": "T2", "description": "d", "hashtags": [], "why_selected": "w", "highlight_words": []},
            {"candidate_index": 3, "title": "T3", "description": "d", "hashtags": [], "why_selected": "w", "highlight_words": []},
        ],
    }
    res = analyze._snap_claude(sents, fake)
    titles = [c["title"] for c in res["clips"]]
    assert titles == ["T0", "T3"], titles               # overlap + out-of-range rejected
    long_clip = res["clips"][1]
    assert long_clip["end"] - long_clip["start"] <= analyze.SRC_MAX   # trimmed to the hard max
    assert res["clips"][0]["keywords"] == ["empire"]    # invented highlight words dropped
    assert res["clips"][0]["hashtags"] == ["#history", "#shorts", "#badtag"]


def test_offline_engine_avoids_promo_and_overlap():
    promo = "Welcome back to the channel. Make sure to subscribe and hit the notification bell. "
    story = ("In 1453 the greatest city in the world was besieged by a vast army. The walls were battered by giant cannons. "
             * 12)
    words = _words(promo * 3 + story * 3, gap_every=60)
    sents = analyze.build_sentences(words)
    res = analyze.offline_select(sents, [], words[-1]["e"])
    clips = res["clips"]
    assert clips
    spans = [{"start": sents[c["start_sentence"]]["s"], "end": sents[c["end_sentence"]]["e"]} for c in clips]
    for i, a in enumerate(spans):
        for b in spans[i + 1:]:
            assert analyze._overlap(a, b) == 0
    for c in clips:
        text = " ".join(s["text"] for s in sents[c["start_sentence"]:c["end_sentence"] + 1]).lower()
        assert "subscribe" not in text


def test_cut_planning_and_time_mapping():
    w = [{"w": "a", "s": 0.0, "e": 0.5}, {"w": "b", "s": 3.0, "e": 3.5}]
    keep = render.plan_cuts(w, [(0.5, 3.0)], 4.0)
    assert keep[0][0] == 0.0 and len(keep) == 2
    removed = 4.0 - sum(b - a for a, b in keep)
    assert 2.0 < removed < 2.2
    assert abs(render.map_time(3.0, keep) - (3.0 - removed)) < 1e-6


def test_speed_range():
    slow = _words("word " * 120, wps=2.0)   # 120 wpm
    fast = _words("word " * 200, wps=3.3)   # ~200 wpm
    assert render.choose_speed(slow, 60)[0] == 1.15
    assert render.choose_speed(fast, 60)[0] == 1.05


def test_ass_has_highlight_and_safe_text():
    words = _words("In 1453 Constantinople {fell} to Mehmed.")
    ass = captions.build_ass(words, 5.0, 1400, "Title {x}", 400, 200, ["Mehmed"])
    assert captions.GOLD in ass and "{fell}" not in ass and "FELL" in ass


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
