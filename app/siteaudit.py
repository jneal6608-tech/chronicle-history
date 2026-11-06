"""Checks a business's website for problems a new site would fix, and looks for a contact email.

Everything here is free: it just downloads the page the way a browser would and
looks at it. No AI is used, so it can check hundreds of sites at no cost.
"""
from __future__ import annotations

import gzip
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import date

UA = ("Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0.0.0 Safari/537.36")
MAX_BYTES = 2_500_000

# A "website" that is really a social profile or a listing counts as no website.
NOT_A_SITE = {"facebook.com": "a Facebook page", "fb.com": "a Facebook page", "instagram.com": "an Instagram page",
              "linktr.ee": "a Linktree", "yelp.com": "a Yelp listing", "business.site": "a Google Business Site "
              "(Google shut these down in 2024)", "g.page": "a Google Maps link", "goo.gl": "a Google Maps link",
              "maps.app.goo.gl": "a Google Maps link", "tiktok.com": "a TikTok page", "nextdoor.com": "a Nextdoor page",
              "x.com": "an X page", "twitter.com": "an X page", "square.site": "a basic Square page",
              "doordash.com": "a DoorDash listing", "grubhub.com": "a Grubhub listing", "ubereats.com": "an Uber Eats listing",
              "angi.com": "an Angi listing", "homeadvisor.com": "a HomeAdvisor listing", "thumbtack.com": "a Thumbtack listing"}
PARKED = ["domain is for sale", "this domain may be for sale", "buy this domain", "domain parking",
          "parked free", "parkingcrew", "sedoparking", "hugedomains", "dan.com", "afternic",
          "this site can't be reached", "account suspended", "website coming soon", "under construction",
          "future home of", "site is not available", "hosting account has expired", "default web site page",
          "welcome to nginx", "it works!"]
OLD_BUILDERS = {"homestead": "Homestead", "web.com": "Web.com", "yola": "Yola", "weebly": "Weebly",
                "jimdo": "Jimdo", "webs.com": "Webs", "godaddy website builder": "GoDaddy's old builder",
                "wsimg.com": "GoDaddy's builder", "frontpage": "FrontPage", "dreamweaver": "Dreamweaver",
                "iweb": "iWeb", "angelfire": "Angelfire", "tripod": "Tripod"}
EMAIL_RX = re.compile(r"[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9.\-]{1,190}\.[A-Za-z]{2,12}")
BAD_EMAIL = re.compile(r"(\.(png|jpe?g|gif|webp|svg|css|js)$|example\.|sentry|wixpress|godaddy\.com$|domain\.com$|"
                       r"yourdomain|email\.com$|@2x|squarespace\.com$|wordpress\.com$|schema\.org)", re.I)


def classify_url(url: str | None) -> tuple[str, str]:
    """('none' | 'social' | 'site', label)"""
    if not url:
        return "none", "no website"
    host = (urllib.parse.urlparse(url if "://" in url else "http://" + url).hostname or "").lower()
    for dom, label in NOT_A_SITE.items():
        if host == dom or host.endswith("." + dom):
            return "social", label
    return "site", host


def fetch(url: str, timeout: float = 12.0) -> dict:
    """GET a page. Never raises: returns ok=False with a reason instead."""
    t0 = time.time()
    ctx = ssl.create_default_context()
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*;q=0.8",
                                               "Accept-Encoding": "gzip, deflate", "Accept-Language": "en-US"})
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            raw = r.read(MAX_BYTES + 1)
            enc = (r.headers.get("Content-Encoding") or "").lower()
            if enc == "gzip":
                raw = gzip.decompress(raw)
            elif enc == "deflate":
                raw = zlib.decompress(raw)
            ctype = r.headers.get("Content-Type", "")
            cs = re.search(r"charset=([\w-]+)", ctype)
            html = raw.decode(cs.group(1) if cs else "utf-8", errors="replace")
            return {"ok": True, "status": r.status, "final_url": r.geturl(), "html": html,
                    "bytes": len(raw), "seconds": round(time.time() - t0, 2)}
    except urllib.error.HTTPError as e:
        return {"ok": False, "status": e.code, "reason": f"the site returns an error ({e.code})"}
    except ssl.SSLError:
        return {"ok": False, "status": None, "reason": "the site's security certificate is broken", "ssl": True}
    except urllib.error.URLError as e:
        r = str(e.reason)
        if "CERTIFICATE" in r.upper() or "SSL" in r.upper():
            return {"ok": False, "status": None, "reason": "the site's security certificate is broken", "ssl": True}
        return {"ok": False, "status": None, "reason": "the site doesn't load (the address may have expired)"}
    except Exception:
        return {"ok": False, "status": None, "reason": "the site doesn't load"}


def find_emails(html: str, site_host: str = "") -> list[str]:
    found = []
    for m in re.findall(r"mailto:([^\"'?>\s]+)", html, re.I) + EMAIL_RX.findall(html):
        e = urllib.parse.unquote(m).strip().strip(".").lower()
        if EMAIL_RX.fullmatch(e) and not BAD_EMAIL.search(e) and e not in found:
            found.append(e)
    # Emails on the business's own domain first.
    base = site_host.removeprefix("www.")
    found.sort(key=lambda e: (not (base and e.endswith("@" + base)), len(e)))
    return found[:3]


def _text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html)).strip()


def audit(url: str) -> dict:
    """Problems with a site, each with a severity 1-3, plus emails and a text excerpt."""
    kind, label = classify_url(url)
    if kind == "none":
        return {"kind": "none", "label": label, "issues": [], "severity": 10, "emails": []}
    if kind == "social":
        return {"kind": "social", "label": label, "severity": 9, "emails": [],
                "issues": [{"sev": 3, "text": f"Their only 'website' is {label}, so they have no site of their own"}]}
    full = url if "://" in url else "http://" + url
    page = fetch(full)
    if not page["ok"] and page.get("ssl") and full.startswith("https://"):
        page = fetch("http://" + full.split("://", 1)[1])
        if page["ok"]:
            page["cert_broken"] = True
    if not page["ok"]:
        return {"kind": "broken", "label": label, "severity": 9, "emails": [],
                "issues": [{"sev": 3, "text": f"Website is down: {page['reason']}"}]}
    html, low = page["html"], page["html"].lower()
    text = _text(html)
    issues: list[dict] = []

    def add(sev, msg):
        issues.append({"sev": sev, "text": msg})

    if any(p in low for p in PARKED) and len(text) < 2500:
        return {"kind": "broken", "label": label, "severity": 9, "emails": [],
                "issues": [{"sev": 3, "text": "The address shows a parked, expired or 'coming soon' page, not their business"}]}
    if len(text) < 200:
        add(3, "The page is almost empty")
    if page.get("cert_broken"):
        add(3, "Browsers show a 'Not secure' warning: the security certificate is broken")
    elif not page["final_url"].startswith("https://"):
        add(2, "No secure connection (https), so browsers mark it 'Not secure'")
    if not re.search(r"<meta[^>]+name=[\"']viewport", low):
        add(3, "Not built for phones: it shows a tiny desktop page on mobile, where most customers look")
    years = [int(y) for y in re.findall(r"(?:©|&copy;|copyright)\s*(?:\d{4}\s*[-–]\s*)?((?:19|20)\d{2})", low)]
    this_year = date.today().year
    if years and max(years) <= this_year - 3:
        add(2, f"Looks unmaintained: the copyright still says {max(years)}")
    for key, name in OLD_BUILDERS.items():
        if key in low:
            add(2, f"Built with {name}, which looks dated")
            break
    if re.search(r"<table[^>]*>\s*<tr", low) and low.count("<table") >= 3 and "display:grid" not in low:
        add(1, "Old table-based layout")
    if "jquery-1." in low or "jquery/1." in low:
        add(1, "Runs very old code (jQuery 1.x)")
    if ".swf" in low or "shockwave-flash" in low:
        add(3, "Uses Flash, which no browser supports any more")
    title = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    t = re.sub(r"\s+", " ", title.group(1)).strip() if title else ""
    if not t or t.lower() in ("home", "index", "welcome", "untitled", "my site", "home page"):
        add(1, "No real page title, which hurts Google search results")
    if not re.search(r"<meta[^>]+name=[\"']description", low):
        add(1, "No search description, so Google picks random text to show")
    if "tel:" not in low:
        add(2, "No tap-to-call phone button")
    if "<form" not in low and not re.search(r"book (now|online|an appointment)|schedule|request (a )?(quote|estimate)", low):
        add(1, "No way to request a quote or book online")
    if page["bytes"] > 4_000_000 or page["seconds"] > 6:
        add(2, f"Slow to load ({page['seconds']:.0f}s)")
    if not re.search(r"<img|background-image", low):
        add(1, "No photos")

    emails = find_emails(html, label)
    if not emails:  # look on a contact page as well
        m = re.search(r"href=[\"']([^\"'#]*contact[^\"']*)[\"']", html, re.I)
        if m:
            cp = fetch(urllib.parse.urljoin(page["final_url"], m.group(1)), timeout=8)
            if cp["ok"]:
                emails = find_emails(cp["html"], label)
    sev = sum(i["sev"] for i in issues)
    return {"kind": "site", "label": label, "final_url": page["final_url"], "issues": issues,
            "severity": sev, "emails": emails, "excerpt": text[:6000], "title": t[:120]}


def improvable(a: dict) -> bool:
    """Worth pitching a redesign: one serious problem, or several smaller ones."""
    if a["kind"] != "site":
        return True
    return any(i["sev"] >= 3 for i in a["issues"]) or a["severity"] >= 4


def summary(a: dict) -> str:
    if a["kind"] == "none":
        return "No website at all."
    if not a["issues"]:
        return "The website looks fine."
    worst = sorted(a["issues"], key=lambda i: -i["sev"])
    return " ".join(i["text"].rstrip(".") + "." for i in worst[:5])
