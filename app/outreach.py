"""Prefilled outreach emails and the links that open them in Gmail or your mail app.

US cold-email law (CAN-SPAM) asks business emails to include your physical mailing
address and an easy way to opt out, so both go in every email's sign-off.
"""
from __future__ import annotations

import re
import urllib.parse

DEFAULT_SETTINGS = {
    "my_name": "", "my_business": "", "my_phone": "", "my_email": "", "my_address": "",
    "mail_app": "gmail",
    "location": "", "target": 40, "max_miles": 60, "budget": 900, "categories": [],
    "subject_new": "A website for {business}",
    "body_new": (
        "Hi {business} team,\n\n"
        "I came across {business} on Google Maps{rating_line}, and noticed you don't have a website yet. "
        "A lot of people check a business's site before they call, so I went ahead and put together a free "
        "preview of what yours could look like:\n\n{preview_link}\n\n"
        "It works on phones, has tap-to-call, and shows your hours and service area. If you like it, I can "
        "make it live under your own name and adjust anything you want.\n\n"
        "Would you be open to a quick call this week?\n\n"),
    "subject_improve": "A few quick wins for the {business} website",
    "body_improve": (
        "Hi {business} team,\n\n"
        "I was looking at {business}{rating_line} and checked out your website. A few things are likely costing "
        "you customers:\n\n{improvements}\n\n"
        "I build modern, mobile-friendly sites for local businesses. {preview_sentence}\n\n"
        "Would you be open to a quick call this week?\n\n"),
    "signoff": "{my_name}\n{my_business}\n{my_phone}\n{my_address}\n\n"
               "If you'd rather not hear from me, just reply \"no thanks\" and I won't email again.",
}


def settings(stored: dict | None) -> dict:
    s = dict(DEFAULT_SETTINGS)
    for k, v in (stored or {}).items():
        if k in s and v is not None:
            s[k] = v
    return s


def _fill(t: str, vals: dict) -> str:
    return re.sub(r"\{(\w+)\}", lambda m: str(vals.get(m.group(1), m.group(0))), t)


def email(row: dict, s: dict) -> dict:
    """Subject + body for one lead (a listing row), and Gmail / mailto links."""
    rating_line = (f" (your {row['rating']}★ rating from {row['reviews']} reviews stood out)"
                   if row.get("rating") and row.get("reviews", 0) >= 10 and row["rating"] >= 4.3 else "")
    site = row.get("site") or {}
    link = site.get("url") or "[preview link: click Create website first]"
    issues = [i["text"] for i in sorted((row.get("audit") or {}).get("issues", []), key=lambda i: -i["sev"])][:4]
    vals = {"business": row["name"], "rating_line": rating_line, "preview_link": link,
            "improvements": "\n".join(f"- {t}" for t in issues) or "- The site could be faster and easier to use on phones",
            "preview_sentence": (f"I put together a free preview of a refreshed version: {site['url']}"
                                 if site.get("url") else "I'd be happy to put together a free preview of a refreshed version."),
            **{k: s.get(k, "") for k in ("my_name", "my_business", "my_phone", "my_email", "my_address")}}
    kind = "new" if row["need"] in ("no_site", "broken") else "improve"
    subject = _fill(s[f"subject_{kind}"], vals)
    # Blank fields (say, no business name yet) collapse instead of leaving gaps.
    signoff = re.sub(r"\n{3,}", "\n\n", "\n".join(ln.rstrip() for ln in _fill(s["signoff"], vals).splitlines())).strip()
    body = _fill(s[f"body_{kind}"], vals).rstrip() + "\n\n" + signoff
    to = (row.get("emails") or [""])[0]
    q = urllib.parse.urlencode({"view": "cm", "fs": "1", "to": to, "su": subject, "body": body},
                               quote_via=urllib.parse.quote)
    gmail = "https://mail.google.com/mail/?" + q
    mailto = f"mailto:{urllib.parse.quote(to)}?" + urllib.parse.urlencode(
        {"subject": subject, "body": body}, quote_via=urllib.parse.quote)
    return {"to": to, "subject": subject, "body": body, "gmail": gmail, "mailto": mailto,
            "open": gmail if s.get("mail_app", "gmail") == "gmail" else mailto,
            "needs_preview": kind == "new" and not site.get("url"),
            "missing_address": not s.get("my_address")}


def call_script(row: dict, s: dict) -> str:
    me = s.get("my_name") or "[your name]"
    if row["need"] in ("no_site", "broken"):
        pitch = "I noticed you don't have a working website, and I build simple, mobile-friendly sites for local businesses."
    else:
        pitch = "I took a look at your website and spotted a few quick fixes that could bring in more calls."
    return (f"Hi, is this {row['name']}? My name is {me}, I'm local. {pitch} "
            "I can show you a free preview first. Would it be okay if I emailed it over? What's the best email?")


def search_email_url(row: dict) -> str:
    town = (row.get("address") or "").split(",")[-2:] if row.get("address") else []
    q = f'"{row["name"]}" {" ".join(t.strip() for t in town)} email'
    return "https://www.google.com/search?" + urllib.parse.urlencode({"q": q})
