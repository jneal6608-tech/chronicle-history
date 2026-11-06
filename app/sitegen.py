"""'Create website': Claude designs a one-page site for a lead, published as a preview link.

Every preview carries a visible banner saying it's a concept made by you, not the
business's official site, and asks search engines not to list it. Facts come only from
Google Maps and the business's current site; Claude is told not to invent prices,
awards, licences, staff names or testimonials.
"""
from __future__ import annotations

import base64
import html as htmllib
import json
import re
import threading
import time
import urllib.error
import urllib.request

from . import config, credits, leadfinder

JOBS: dict[str, dict] = {}   # lead id -> {"state", "message"}

SYSTEM = """You are a senior web designer and conversion copywriter who builds websites for local small
businesses. You produce one complete, self-contained, production-quality HTML page.

Design: modern, confident and specific to this kind of business (a roofer's site should not look like a
nail salon's). Mobile-first and responsive, generous spacing, strong type hierarchy, accessible contrast,
CSS in a <style> tag, no JavaScript frameworks; a little vanilla JS is fine. You may load fonts from Google
Fonts. No other external files. Use CSS shapes, gradients and inline SVG icons instead of photos; where a
photo would go, use a tasteful placeholder block labelled with what photo belongs there.

Content: a hero with a clear promise and a primary call to action (tap-to-call and/or "Get a free quote"),
services, why choose them, service area, hours, a contact section with the real phone number, address and a
link to their Google Maps listing, and a footer.

Honesty rules - these matter more than anything else:
- Use only the facts given. Do not invent prices, years in business, awards, licences, certifications,
  guarantees, staff names, project counts or service details that are not implied by the business type.
- Never write testimonials or quotes in anyone's voice. You may state the Google rating and review count
  exactly as given (e.g. "Rated 4.8 on Google from 126 reviews").
- Describe services in general terms typical of the business type, not specific claims.
- Keep the banner element exactly as provided, as the first thing inside <body>.

Return only the HTML document, starting with <!doctype html>."""


def _facts(lead: dict) -> str:
    g = lead["google"]
    hours = (g.get("regularOpeningHours") or {}).get("weekdayDescriptions") or []
    lines = [f"Business name: {(g.get('displayName') or {}).get('text', '')}",
             f"Type of business: {lead['label']}",
             f"Address: {g.get('formattedAddress', '')}",
             f"Phone: {g.get('nationalPhoneNumber') or 'not listed'}",
             f"Google Maps link: {g.get('googleMapsUri') or ''}"]
    if g.get("rating") and g.get("userRatingCount"):
        lines.append(f"Google rating: {g['rating']} from {g['userRatingCount']} reviews")
    if hours:
        lines.append("Opening hours: " + "; ".join(hours))
    themes = [r.get("text", {}).get("text", "") for r in (g.get("reviews") or [])][:5]
    themes = [t[:300] for t in themes if t]
    if themes:
        lines.append("What customers mention in reviews (use only to understand what they're known for; "
                     "do NOT quote or paraphrase as testimonials):\n- " + "\n- ".join(themes))
    if lead.get("site_excerpt"):
        lines.append("Text from their current website (reuse their real services and wording where useful):\n"
                     + lead["site_excerpt"][:5000])
    return "\n".join(lines)


def banner(name: str, me: str) -> str:
    who = f" by {htmllib.escape(me)}" if me else ""
    return (f'<div id="concept-banner" style="background:#111;color:#fff;font:14px/1.4 system-ui,sans-serif;'
            f'padding:10px 16px;text-align:center">Website concept{who} for {htmllib.escape(name)}. '
            f'This is a preview, not the official website of {htmllib.escape(name)}.</div>')


def _extract_html(text: str) -> str:
    m = re.search(r"(?is)<!doctype html.*</html>", text)
    if not m:
        raise RuntimeError("Claude didn't return a complete web page. Try again.")
    return m.group(0)


def _ensure_safeguards(page: str, name: str, me: str) -> str:
    """Whatever Claude returned: keep the concept banner and the no-index tag."""
    if 'id="concept-banner"' not in page:
        page = re.sub(r"(?i)(<body[^>]*>)", lambda m: m.group(1) + banner(name, me), page, count=1)
    if "noindex" not in page.lower():
        page = re.sub(r"(?i)<head([^>]*)>", lambda m: f'<head{m.group(1)}><meta name="robots" content="noindex,nofollow">',
                      page, count=1)
    return page


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:50] or "site"


def generate(lead_id: str, model: str | None = None, client=None) -> dict:
    d = leadfinder.load()
    lead = d["leads"].get(lead_id)
    if not lead or not lead.get("google"):
        raise RuntimeError("Lead not found.")
    if not config.ANTHROPIC_API_KEY and client is None:
        raise RuntimeError("Connect Claude first (on the upload page, Stream / podcast settings).")
    me = (d.get("settings") or {}).get("my_name", "")
    name = (lead["google"].get("displayName") or {}).get("text", "this business")
    model = model or config.SITE_MODEL
    if client is None:
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY, max_retries=2)
    user = (_facts(lead) + "\n\nPut this banner exactly as the first element inside <body>:\n" + banner(name, me)
            + '\n\nAlso put <meta name="robots" content="noindex,nofollow"> in the <head>.')
    with client.messages.stream(model=model, max_tokens=24000, system=SYSTEM,
                                messages=[{"role": "user", "content": user}]) as stream:
        resp = stream.get_final_message()
    if resp.stop_reason == "refusal":
        raise RuntimeError("Claude declined to build this site.")
    text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
    page = _ensure_safeguards(_extract_html(text), name, me)
    u = getattr(resp, "usage", None)
    cost = config.estimate_cost(model, getattr(u, "input_tokens", 0) or 0, getattr(u, "output_tokens", 0) or 0)
    credits.record_run(f"site:{lead_id}:{int(time.time())}", model, cost, 0, 0, "site")
    folder = config.DATA_DIR / "sites" / lead_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "index.html").write_text(page, encoding="utf-8")
    site = {"made": time.time(), "model": model, "cost": cost, "slug": slug(name), "url": None, "publish_error": None}
    if config.GITHUB_TOKEN and config.GITHUB_PREVIEW_REPO:
        try:
            site["url"] = publish(site["slug"], page)
        except Exception as e:
            site["publish_error"] = str(e)
    leadfinder.update_lead(lead_id, site=site)
    return site


def start(lead_id: str, model: str | None = None) -> None:
    if JOBS.get(lead_id, {}).get("state") == "running":
        return
    JOBS[lead_id] = {"state": "running", "message": "Claude is designing the site…", "started": time.time()}

    def work():
        try:
            site = generate(lead_id, model)
            msg = "Preview published." if site["url"] else (
                "Site created. Set up GitHub in Settings to get a shareable link." if not site["publish_error"]
                else "Site created, but publishing failed: " + site["publish_error"])
            JOBS[lead_id] = {"state": "done", "message": msg}
        except Exception as e:
            msg = str(e)
            if credits.is_credit_error(e):
                msg = "Your Claude credits have run out. Buy more to create sites."
            JOBS[lead_id] = {"state": "failed", "message": msg[:400]}
    threading.Thread(target=work, daemon=True).start()


# ------------------------------------------------------------------ GitHub Pages

def _gh(method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request("https://api.github.com" + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {config.GITHUB_TOKEN}",
                                          "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "chronicle-shorts"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        if e.code == 404 and method == "GET":
            return {}
        msg = ""
        try:
            msg = json.loads(e.read().decode()).get("message", "")
        except Exception:
            pass
        if e.code in (401, 403):
            raise RuntimeError("GitHub rejected the token. It needs 'Contents: Read and write' on your previews repo.")
        if e.code == 404:
            raise RuntimeError(f"GitHub repo '{config.GITHUB_PREVIEW_REPO}' wasn't found. Check the name in Settings.")
        raise RuntimeError(f"GitHub error {e.code}: {msg[:200]}")


def pages_url(slug_: str) -> str:
    owner, repo = config.GITHUB_PREVIEW_REPO.split("/", 1)
    base = f"https://{owner.lower()}.github.io" + ("" if repo.lower() == f"{owner.lower()}.github.io" else f"/{repo}")
    return f"{base}/{slug_}/"


def publish(slug_: str, page: str) -> str:
    repo = config.GITHUB_PREVIEW_REPO
    path = f"/repos/{repo}/contents/{slug_}/index.html"
    existing = _gh("GET", path)
    body = {"message": f"Preview for {slug_}", "content": base64.b64encode(page.encode()).decode()}
    if existing.get("sha"):
        body["sha"] = existing["sha"]
    _gh("PUT", path, body)
    return pages_url(slug_)


def check_github(token: str, repo: str) -> str | None:
    """None if the token can see the repo, else the problem."""
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", repo or ""):
        return "Enter the repo as yourname/repo-name."
    old = config.GITHUB_TOKEN, config.GITHUB_PREVIEW_REPO
    config.GITHUB_TOKEN, config.GITHUB_PREVIEW_REPO = token, repo
    try:
        info = _gh("GET", f"/repos/{repo}")
        if not info:
            return f"Couldn't find the repo {repo}. Create it on GitHub first (public)."
        if info.get("private"):
            return "The repo is private. GitHub Pages previews need a public repo on a free account."
        if not (info.get("permissions") or {}).get("push", True):
            return "The token can't write to that repo."
        return None
    except RuntimeError as e:
        return str(e)
    finally:
        config.GITHUB_TOKEN, config.GITHUB_PREVIEW_REPO = old
