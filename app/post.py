"""Ready-to-paste post text for each platform, built from a clip's title, caption and tags."""
from __future__ import annotations

import re


def _join_tags(tags: list[str]) -> str:
    return " ".join(t for t in tags if t.startswith("#"))


def kit(clip: dict) -> dict:
    title = (clip.get("post_title") or clip.get("title") or "").strip()
    caption = (clip.get("description") or "").strip()
    tags = list(clip.get("hashtags") or [])
    search = [t.strip() for t in clip.get("search_tags") or [] if t.strip()]

    # YouTube Shorts: title max 100 characters; #shorts helps the video land in the Shorts feed.
    yt_title = title[:100]
    if "#shorts" not in yt_title.lower() and len(yt_title) + 8 <= 100:
        yt_title += " #shorts"
    # YouTube shows the first three hashtags above the title, so the most important come first.
    yt_tags_line = _join_tags(tags)
    yt_desc = caption + (f"\n\n{yt_tags_line}" if yt_tags_line else "")
    yt_keywords, total = [], 0
    for t in search or [re.sub(r"^#", "", x) for x in tags if x.lower() != "#shorts"]:
        if total + len(t) + 2 > 480:
            break
        yt_keywords.append(t)
        total += len(t) + 2

    lead = title if title and title.lower().rstrip(".!?") not in caption.lower() else ""
    tiktok = "\n".join(x for x in (lead, caption) if x) + (f"\n\n{_join_tags(tags)}" if tags else "")
    instagram = caption + (f"\n\n{_join_tags(tags)}" if tags else "")
    return {"tiktok": tiktok[:2200], "instagram": instagram[:2200],
            "youtube": {"title": yt_title, "description": yt_desc[:5000], "tags": ", ".join(yt_keywords)}}


def text_block(k: dict) -> str:
    """The post kit as plain text, for post.txt."""
    yt = k["youtube"]
    return ("TIKTOK (paste as the caption)\n" + k["tiktok"] + "\n\n"
            "YOUTUBE SHORTS\nTitle: " + yt["title"] + "\n\nDescription:\n" + yt["description"]
            + ("\n\nTags: " + yt["tags"] if yt["tags"] else "") + "\n\n"
            "INSTAGRAM REELS (paste as the caption)\n" + k["instagram"] + "\n")
