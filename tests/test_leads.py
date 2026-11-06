"""Lead finder checks with a fake Google Maps and real local test websites (no internet needed).
    python -m pytest tests/test_leads.py"""
import json
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, leadfinder, outreach, siteaudit, sitegen  # noqa: E402

GOOD = """<!doctype html><html><head><meta name="viewport" content="width=device-width">
<title>Acme Plumbing | Columbus plumber</title><meta name="description" content="Fast plumbing"></head>
<body><img src="a.jpg"><a href="tel:6145550100">Call</a><form></form><p>""" + "We fix pipes. " * 40 + """</p>
<footer>© """ + str(datetime.now().year) + """ Acme</footer></body></html>"""
OLD = """<html><head><title>Home</title></head><body><table><tr><td><table><tr><td><table><tr><td>
""" + "Welcome to our salon. " * 30 + """ <a href="/contact-us">Contact</a> &copy; 2016 Bella Salon</td></tr></table>
</td></tr></table></td></tr></table></body></html>"""
CONTACT = "<html><body>Email us: <a href='mailto:info@bellasalon.test'>info@bellasalon.test</a></body></html>"
PARKED = "<html><body><h1>This domain may be for sale</h1> Buy this domain today.</body></html>"


class Site(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        pages = {"/good": GOOD, "/old": OLD, "/contact-us": CONTACT, "/parked": PARKED}
        if self.path == "/down":
            self.send_response(500); self.end_headers(); return
        body = pages.get(self.path)
        if body is None:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers()
        self.wfile.write(body.encode())


def _iso(days_ago):
    return (datetime.now(timezone.utc) - timedelta(days=days_ago)).isoformat().replace("+00:00", "Z")


def _place(pid, name, website=None, n=40, rating=4.7, review_days=(10, 40, 90), status="OPERATIONAL",
           phone="(614) 555-0100", lat=40.236, lng=-83.367):
    return {"id": pid, "displayName": {"text": name}, "formattedAddress": f"1 Main St, Marysville, OH 43040, USA",
            "shortFormattedAddress": "1 Main St, Marysville", "location": {"latitude": lat, "longitude": lng},
            "primaryTypeDisplayName": {"text": "Plumber"}, "businessStatus": status, "nationalPhoneNumber": phone,
            "websiteUri": website, "rating": rating, "userRatingCount": n,
            "regularOpeningHours": {"weekdayDescriptions": ["Monday: 8 AM - 5 PM"]},
            "reviews": [{"publishTime": _iso(d), "text": {"text": "Great work, fixed our leak fast."}} for d in review_days],
            "googleMapsUri": f"https://maps.google.com/?cid={pid}"}


def _setup(port):
    base = f"http://127.0.0.1:{port}"
    return {
        "plumber": [
            _place("p_nosite_busy", "Busy Plumbing", None, n=180, rating=4.8),
            _place("p_good_site", "Acme Plumbing", base + "/good"),
            _place("p_closed", "Gone Plumbing", None, status="CLOSED_PERMANENTLY"),
            _place("p_ghost", "Shell LLC Plumbing", None, n=0, review_days=(), phone=None),
            _place("p_stale", "Old Reviews Plumbing", None, n=4, review_days=(1200, 1300)),
        ],
        "hair salon": [
            _place("s_old_site", "Bella Salon", base + "/old", n=25, rating=4.4),
            _place("s_fb", "FB Only Salon", "https://www.facebook.com/fbonlysalon", n=12, rating=4.9),
            _place("s_parked", "Parked Salon", base + "/parked", n=9, rating=4.1, lat=40.6, lng=-83.1),
            _place("s_down", "Down Salon", base + "/down", n=15, rating=3.9),
        ],
    }


def test_full_search_rank_and_outreach():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    data = _setup(srv.server_address[1])
    calls = []

    def fake_post(body, key):
        calls.append(body)
        if "locationRestriction" not in body:     # geocoding the home town
            return {"places": [{"id": "home", "formattedAddress": "Marysville, OH",
                                "location": {"latitude": 40.236, "longitude": -83.367}}]}
        return {"places": data.get(body["textQuery"], [])}

    with tempfile.TemporaryDirectory() as d:
        old_dir, old_post = config.DATA_DIR, leadfinder._post
        config.DATA_DIR, leadfinder._post = Path(d), fake_post
        try:
            leadfinder.run_search("Marysville, OH", "AIzaFAKE", ["plumber", "hair salon"], target=5,
                                  max_miles=20, budget=500)
            assert leadfinder.RUN["error"] is None, leadfinder.RUN
            rows = leadfinder.listing()
            names = [r["name"] for r in rows]
            # abandoned / closed / fine sites are left out; the rest are ranked
            assert "Gone Plumbing" not in names and "Shell LLC Plumbing" not in names
            assert "Old Reviews Plumbing" not in names and "Acme Plumbing" not in names
            assert names[0] == "Busy Plumbing", names
            assert set(names) == {"Busy Plumbing", "Bella Salon", "FB Only Salon", "Parked Salon", "Down Salon"}
            assert [r["priority"] for r in rows] == sorted((r["priority"] for r in rows), reverse=True)
            by = {r["name"]: r for r in rows}
            assert by["FB Only Salon"]["need"] == "no_site" and by["Parked Salon"]["need"] == "broken"
            assert by["Down Salon"]["need"] == "broken"
            bella = by["Bella Salon"]
            assert bella["need"] == "improve" and bella["emails"] == ["info@bellasalon.test"]
            texts = " ".join(i["text"] for i in bella["audit"]["issues"])
            assert "Not built for phones" in texts and "2016" in texts and "tap-to-call" in texts
            assert by["Busy Plumbing"]["business_activity"].startswith("Very busy")
            assert by["Parked Salon"]["miles"] > 20 or by["Parked Salon"]["miles"] > 5
            # stopped at the target and counted every Google request
            assert leadfinder.usage()["requests"] == len(calls)
            # all businesses, including filtered ones, are still available
            assert len(leadfinder.listing(show_all=True)) == 9

            # a second search doesn't re-check businesses it already has
            n_before = len(calls)
            leadfinder.update_lead("s_old_site", status="contacted", notes="called Tue")
            leadfinder.run_search("Marysville, OH", "AIzaFAKE", ["hair salon"], target=50, max_miles=3, budget=500)
            assert len(calls) == n_before + 1   # one tile x one category, no re-geocode
            assert leadfinder.load()["leads"]["s_old_site"]["notes"] == "called Tue"

            # budget is respected
            leadfinder.run_search("Marysville, OH", "AIzaFAKE", ["plumber"], target=50, max_miles=60,
                                  budget=leadfinder.usage()["requests"])
            assert "limit" in (leadfinder.RUN["error"] or "")

            # outreach email
            s = outreach.settings({"my_name": "Jordan", "my_phone": "614-555-0199", "my_address": "PO Box 1, Marysville OH"})
            e = outreach.email(bella, s)
            assert e["to"] == "info@bellasalon.test" and "Bella Salon" in e["subject"]
            assert "Not built for phones" in e["body"] and "PO Box 1" in e["body"] and "no thanks" in e["body"]
            assert e["open"].startswith("https://mail.google.com/mail/?view=cm")
            e2 = outreach.email(by["Busy Plumbing"], s)
            assert e2["needs_preview"] and e2["to"] == "" and "4.8★" in e2["body"]
            assert "Busy Plumbing" in outreach.call_script(by["Busy Plumbing"], s)

            # 30-day rule: old Google details are dropped, your status stays
            dd = leadfinder.load()
            dd["leads"]["s_fb"]["fetched"] -= 31 * 86400
            assert leadfinder.purge_old(dd) == 1 and dd["leads"]["s_fb"]["google"] is None
        finally:
            config.DATA_DIR, leadfinder._post = old_dir, old_post
            srv.shutdown()


def test_create_website_keeps_banner_and_publishes():
    with tempfile.TemporaryDirectory() as d:
        old = (config.DATA_DIR, config.GITHUB_TOKEN, config.GITHUB_PREVIEW_REPO, sitegen._gh)
        config.DATA_DIR = Path(d)
        config.GITHUB_TOKEN, config.GITHUB_PREVIEW_REPO = "tok", "jneal/previews"
        pushed = {}

        def fake_gh(method, path, body=None):
            if method == "PUT":
                pushed["path"], pushed["body"] = path, body
            return {}
        sitegen._gh = fake_gh
        try:
            p = _place("lead1", "Busy Plumbing & Drain", None, n=180)
            lead = leadfinder._lead_from(p, "plumber", {"lat": 40.2, "lng": -83.3})
            lead["activity"] = leadfinder.activity(p)
            lead.update({"audit": {"kind": "none", "issues": [], "severity": 10, "label": "no website"},
                         "need": "no_site", "qualified": True, "priority": 90, "site_excerpt": ""})
            dd = leadfinder.load()
            dd["leads"]["lead1"] = lead
            dd["settings"] = {"my_name": "Jordan"}
            leadfinder.save(dd)
            seen = {}

            class Stream:
                def __init__(self, kw):
                    seen.update(kw)

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def get_final_message(self):
                    page = "Here you go:\n<!doctype html><html><head><title>x</title></head><body><h1>Busy</h1></body></html>"
                    return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=page)],
                                           usage=SimpleNamespace(input_tokens=3000, output_tokens=9000))
            client = SimpleNamespace(messages=SimpleNamespace(stream=lambda **kw: Stream(kw)))
            site = sitegen.generate("lead1", "claude-sonnet-5-5", client=client)
            html = (Path(d) / "sites" / "lead1" / "index.html").read_text()
            assert 'id="concept-banner"' in html and "not the official website of Busy Plumbing" in html
            assert "by Jordan" in html and 'name="robots" content="noindex' in html
            assert "Never write testimonials" in seen["system"] and "180 reviews" in seen["messages"][0]["content"]
            assert site["url"] == "https://jneal.github.io/previews/busy-plumbing-drain/"
            assert pushed["path"] == "/repos/jneal/previews/contents/busy-plumbing-drain/index.html"
            assert abs(site["cost"] - (3000 * 2 + 9000 * 10) / 1e6) < 1e-9
            assert leadfinder.load()["leads"]["lead1"]["site"]["url"] == site["url"]
            row = next(r for r in leadfinder.listing(show_all=True) if r["id"] == "lead1")
            e = outreach.email(row, outreach.settings({}))
            assert site["url"] in e["body"] and not e["needs_preview"]
        finally:
            config.DATA_DIR, config.GITHUB_TOKEN, config.GITHUB_PREVIEW_REPO, sitegen._gh = old


def test_site_classification_and_tiles():
    assert siteaudit.classify_url("https://m.facebook.com/x")[0] == "social"
    assert siteaudit.classify_url("http://joes.business.site")[0] == "social"
    assert siteaudit.classify_url(None)[0] == "none"
    assert siteaudit.classify_url("https://joesplumbing.com")[0] == "site"
    assert siteaudit.find_emails("<a href='mailto:Joe@JoesPlumbing.com'>x</a> logo@2x.png bob@example.com",
                                 "www.joesplumbing.com") == ["joe@joesplumbing.com"]
    rings = leadfinder.tile_rings(40.0, -83.0, 30)
    assert [len(r) for r in rings] == [1, 8, 16, 24, 32]
    assert leadfinder.miles_between((40.0, -83.0), (40.0, -83.0)) == 0


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
