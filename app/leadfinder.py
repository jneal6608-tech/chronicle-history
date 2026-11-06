"""Lead finder: local businesses with no website (or a weak one), ranked by priority.

How a search works
  1. Searches Google Maps category by category ("plumber", "hair salon"...) starting with
     the area right around you, then rings of map tiles further out, until it has found
     enough good leads, reached the furthest distance you allow, or used up the free
     monthly Google allowance you set.
  2. Drops businesses that look abandoned: marked closed, no reviews at all, or no
     review in the last two years.
  3. Checks every website it finds (siteaudit.py) and keeps the ones with no site,
     a social-media-only "site", a broken site or a weak one.
  4. Scores each lead 0-100 and sorts by that score.

Google's Places API terms let apps keep place details for up to 30 days, so lead
details older than that are refreshed or removed automatically; your own notes and
status are kept.
"""
from __future__ import annotations

import json
import math
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import config, siteaudit

API = "https://places.googleapis.com/v1/places:searchText"
FIELDS = ",".join("places." + f for f in (
    "id", "displayName", "formattedAddress", "shortFormattedAddress", "location", "primaryType",
    "primaryTypeDisplayName", "types", "businessStatus", "nationalPhoneNumber", "websiteUri", "rating",
    "userRatingCount", "regularOpeningHours", "reviews", "googleMapsUri", "priceLevel")) + ",nextPageToken"
# Text Search with reviews is billed as "Text Search Enterprise + Atmosphere": 1,000 free requests a month,
# then $40 per 1,000 (Google's price list, Nov 2026). Each request returns up to 20 businesses.
FREE_MONTHLY = 1000
TILE_MILES = 8.0
MAX_AGE_DAYS = 30

# What to search for, and how much a website is worth to that kind of business (0-1).
CATEGORIES = [
    ("plumber", "Plumber", 1.0), ("electrician", "Electrician", 1.0), ("roofing contractor", "Roofer", 1.0),
    ("hvac contractor", "HVAC", 1.0), ("general contractor", "Contractor", 1.0), ("landscaping", "Landscaper", 0.95),
    ("lawn care service", "Lawn care", 0.9), ("tree service", "Tree service", 0.95), ("painter", "Painter", 0.95),
    ("handyman", "Handyman", 0.9), ("concrete contractor", "Concrete", 0.95), ("fence contractor", "Fencing", 0.95),
    ("flooring contractor", "Flooring", 0.95), ("pressure washing service", "Pressure washing", 0.9),
    ("house cleaning service", "Cleaning", 0.9), ("pest control", "Pest control", 0.95),
    ("auto repair shop", "Auto repair", 0.9), ("auto body shop", "Auto body", 0.9), ("towing service", "Towing", 0.85),
    ("locksmith", "Locksmith", 0.9), ("moving company", "Movers", 0.9),
    ("hair salon", "Hair salon", 0.85), ("barber shop", "Barber", 0.8), ("nail salon", "Nail salon", 0.8),
    ("spa", "Spa", 0.85), ("massage therapist", "Massage", 0.85), ("tattoo shop", "Tattoo", 0.8),
    ("dog groomer", "Pet groomer", 0.85), ("dog trainer", "Dog trainer", 0.85), ("child care", "Child care", 0.85),
    ("restaurant", "Restaurant", 0.75), ("bakery", "Bakery", 0.75), ("cafe", "Cafe", 0.7), ("food truck", "Food truck", 0.6),
    ("caterer", "Caterer", 0.85), ("photographer", "Photographer", 0.9), ("florist", "Florist", 0.8),
    ("boutique", "Boutique", 0.7), ("gym", "Gym", 0.8), ("martial arts school", "Martial arts", 0.85),
    ("tutoring", "Tutoring", 0.85), ("chiropractor", "Chiropractor", 0.85), ("insurance agency", "Insurance", 0.8),
    ("event venue", "Event venue", 0.9), ("party rental", "Party rental", 0.9),
]
DEFAULT_CATEGORIES = ["plumber", "electrician", "roofing contractor", "hvac contractor", "landscaping",
                      "lawn care service", "painter", "handyman", "house cleaning service", "auto repair shop",
                      "hair salon", "barber shop", "dog groomer", "restaurant", "caterer", "photographer"]
CAT = {q: (label, w) for q, label, w in CATEGORIES}

_lock = threading.RLock()
_run_lock = threading.Lock()
RUN = {"running": False, "message": "", "progress": 0.0, "found": 0, "error": None, "started": None}


# ------------------------------------------------------------------ storage

def _store_path():
    return config.DATA_DIR / "leads.json"


def _usage_path():
    return config.DATA_DIR / "leads_usage.json"


def load() -> dict:
    try:
        d = json.loads(_store_path().read_text(encoding="utf-8"))
        if isinstance(d, dict):
            d.setdefault("leads", {})
            d.setdefault("settings", {})
            d.setdefault("home", None)
            return d
    except Exception:
        pass
    return {"leads": {}, "settings": {}, "home": None}


def save(d: dict) -> None:
    p = _store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1), encoding="utf-8")
    tmp.replace(p)


def month_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def usage() -> dict:
    try:
        u = json.loads(_usage_path().read_text(encoding="utf-8"))
    except Exception:
        u = {}
    return {"month": month_key(), "requests": int(u.get(month_key(), 0))}


def _count_request() -> int:
    with _lock:
        try:
            u = json.loads(_usage_path().read_text(encoding="utf-8"))
        except Exception:
            u = {}
        u[month_key()] = int(u.get(month_key(), 0)) + 1
        _usage_path().parent.mkdir(parents=True, exist_ok=True)
        _usage_path().write_text(json.dumps(u), encoding="utf-8")
        return u[month_key()]


def purge_old(d: dict) -> int:
    """Remove Google place details older than 30 days (your status and notes stay)."""
    now, n = time.time(), 0
    for lead in d["leads"].values():
        if lead.get("google") and now - lead.get("fetched", 0) > MAX_AGE_DAYS * 86400:
            lead["google"] = None
            lead["stale"] = True
            n += 1
    return n


# ------------------------------------------------------------------ Google Places

class PlacesError(RuntimeError):
    pass


def _post(body: dict, key: str) -> dict:
    req = urllib.request.Request(API, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "X-Goog-Api-Key": key,
                                          "X-Goog-FieldMask": FIELDS})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = json.loads(e.read().decode()).get("error", {}).get("message", "")
        except Exception:
            pass
        if e.code in (401, 403):
            raise PlacesError("Google rejected the key. Check it's copied correctly and that the "
                              "Places API (New) is enabled for your project. " + detail[:200])
        if e.code == 429:
            raise PlacesError("Google says you've hit a usage limit. Try again later. " + detail[:200])
        raise PlacesError(f"Google Maps error {e.code}: {detail[:300]}")
    except urllib.error.URLError:
        raise PlacesError("Couldn't reach Google Maps. Check your internet connection.")


def search_text(query: str, key: str, rect: dict | None = None, budget: int | None = None) -> list[dict]:
    if budget is not None and usage()["requests"] >= budget:
        raise PlacesError(f"Reached this month's Google request limit ({budget}). "
                          "Raise it in Settings or wait until next month.")
    body = {"textQuery": query, "pageSize": 20}
    if rect:
        body["locationRestriction"] = {"rectangle": rect}
    data = _post(body, key)
    _count_request()
    return data.get("places", [])


def geocode(text: str, key: str) -> dict:
    """Turn 'Marysville, OH' or a ZIP code into a point, using one Places request."""
    places = search_text(text, key)
    if not places:
        raise PlacesError(f"Couldn't find '{text}'. Try a city and state, or a ZIP code.")
    p = places[0]
    return {"text": text, "label": p.get("formattedAddress", text),
            "lat": p["location"]["latitude"], "lng": p["location"]["longitude"]}


# ------------------------------------------------------------------ geometry

def miles_between(a: tuple[float, float], b: tuple[float, float]) -> float:
    la1, lo1, la2, lo2 = map(math.radians, (*a, *b))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 3958.8 * 2 * math.asin(math.sqrt(h))


def tile_rings(lat: float, lng: float, max_miles: float) -> list[list[dict]]:
    """Square map tiles around home: ring 0 is home's tile, ring 1 the 8 around it, and so on."""
    dlat = TILE_MILES / 69.0
    dlng = TILE_MILES / (69.0 * max(0.2, math.cos(math.radians(lat))))
    rings, r = [], 0
    while r * TILE_MILES - TILE_MILES / 2 <= max_miles:
        ring = []
        for i in range(-r, r + 1):
            for j in range(-r, r + 1):
                if max(abs(i), abs(j)) != r:
                    continue
                c_lat, c_lng = lat + i * dlat, lng + j * dlng
                ring.append({"low": {"latitude": c_lat - dlat / 2, "longitude": c_lng - dlng / 2},
                             "high": {"latitude": c_lat + dlat / 2, "longitude": c_lng + dlng / 2},
                             "_d": math.hypot(i, j)})
        ring.sort(key=lambda t: t["_d"])
        rings.append(ring)
        r += 1
    return rings


# ------------------------------------------------------------------ is it a real, active business?

def _parse_time(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def activity(p: dict) -> dict:
    """'active' / 'uncertain' / 'inactive', with the reasons, from what Google knows."""
    status = p.get("businessStatus", "OPERATIONAL")
    n = int(p.get("userRatingCount") or 0)
    times = [t for t in (_parse_time(r.get("publishTime")) for r in p.get("reviews") or []) if t]
    last = max(times) if times else None
    months = (time.time() - last) / (30.4 * 86400) if last else None
    hours = bool(p.get("regularOpeningHours"))
    phone = bool(p.get("nationalPhoneNumber"))
    why = []
    if status != "OPERATIONAL":
        return {"state": "inactive", "why": ["Google lists it as closed"], "last_review_months": months}
    if n == 0:
        why.append("no reviews at all")
    if months is not None and months > 24:
        why.append(f"last review {months / 12:.0f} years ago")
    if not phone and not hours:
        why.append("no phone number or opening hours listed")
    if why and (n == 0 or (months or 0) > 24):
        return {"state": "inactive", "why": why, "last_review_months": months}
    good = []
    if months is not None and months <= 12:
        good.append(f"reviewed {('this month' if months < 1 else f'{months:.0f} months ago')}")
    if n:
        good.append(f"{n} Google reviews")
    if hours:
        good.append("opening hours listed")
    state = "active" if (months is not None and months <= 12 and n >= 3) else "uncertain"
    return {"state": state, "why": good + why, "last_review_months": months}


def reviews_per_year(p: dict) -> float | None:
    """Rough review pace from the dates of the latest reviews Google returns (up to 5)."""
    times = sorted(t for t in (_parse_time(r.get("publishTime")) for r in p.get("reviews") or []) if t)
    if len(times) < 2:
        return None
    span_years = max((time.time() - times[0]) / (365 * 86400), 1 / 12)
    return round(len(times) / span_years, 1)


# ------------------------------------------------------------------ priority

def score(lead: dict) -> tuple[int, list[str]]:
    """0-100, and the main reasons, in plain words."""
    g = lead["google"]
    a = lead["audit"]
    reasons = []
    need = {"none": 1.0, "social": 0.95, "broken": 0.95}.get(a["kind"], min(0.85, 0.3 + 0.07 * a["severity"]))
    reasons.append({"none": "no website", "social": f"only has {a['label']}", "broken": "website is broken"}.get(
        a["kind"], f"website has {len(a['issues'])} problems"))
    n = int(g.get("userRatingCount") or 0)
    demand = min(1.0, math.log10(n + 1) / math.log10(300))
    if n >= 50:
        reasons.append(f"busy: {n} reviews")
    act = lead["activity"]
    m = act.get("last_review_months")
    recency = 1.0 if m is not None and m <= 3 else 0.7 if m is not None and m <= 12 else 0.3 if m is not None else 0.2
    rating = float(g.get("rating") or 0)
    rate = 1.0 if rating >= 4.5 else 0.85 if rating >= 4.0 else 0.6 if rating >= 3.5 else 0.35
    if rating >= 4.5 and n >= 10:
        reasons.append(f"{rating}★ rating")
    value = CAT.get(lead["category"], ("", 0.75))[1]
    contact = 1.0 if lead.get("emails") else 0.75 if g.get("nationalPhoneNumber") else 0.3
    dist = lead.get("miles") or 0
    near = 1.0 if dist <= 10 else 0.9 if dist <= 20 else 0.75 if dist <= 35 else 0.6
    if dist <= 10:
        reasons.append("close by")
    s = 100 * (0.30 * need + 0.18 * demand + 0.12 * recency + 0.08 * rate + 0.12 * value + 0.10 * contact + 0.10 * near)
    if act["state"] == "uncertain":
        s *= 0.85
    return int(round(s)), reasons


def activity_label(g: dict) -> str:
    """Honest stand-in for 'sales': Google doesn't share revenue, so this reads review volume and pace."""
    n = int(g.get("userRatingCount") or 0)
    pace = reviews_per_year(g)
    if n >= 150 or (pace and pace >= 40):
        lvl = "Very busy"
    elif n >= 40 or (pace and pace >= 12):
        lvl = "Busy"
    elif n >= 10:
        lvl = "Steady"
    else:
        lvl = "Small / new"
    return lvl + (f" · about {pace:.0f} reviews a year" if pace else "")


# ------------------------------------------------------------------ the search run

def _lead_from(p: dict, category: str, home: dict) -> dict:
    loc = p.get("location") or {}
    miles = miles_between((home["lat"], home["lng"]), (loc.get("latitude", home["lat"]), loc.get("longitude", home["lng"])))
    return {"id": p["id"], "category": category,
            "label": p.get("primaryTypeDisplayName", {}).get("text") or CAT.get(category, (category.title(),))[0],
            "google": {k: p.get(k) for k in ("displayName", "formattedAddress", "shortFormattedAddress", "location",
                                             "businessStatus", "nationalPhoneNumber", "websiteUri", "rating",
                                             "userRatingCount", "regularOpeningHours", "reviews", "googleMapsUri",
                                             "priceLevel", "primaryType")},
            "miles": round(miles, 1), "fetched": time.time(), "status": "new", "notes": ""}


def _settle(lead: dict) -> dict:
    """Audit the website (if any), find emails and score it."""
    g = lead["google"]
    a = siteaudit.audit(g.get("websiteUri"))
    lead["audit"] = {k: v for k, v in a.items() if k != "excerpt"}
    lead["site_excerpt"] = a.get("excerpt", "")[:6000]
    lead["emails"] = a.get("emails", [])
    lead["need"] = "no_site" if a["kind"] in ("none", "social") else "broken" if a["kind"] == "broken" else "improve"
    lead["qualified"] = lead["activity"]["state"] != "inactive" and siteaudit.improvable(a)
    lead["improvement_summary"] = siteaudit.summary(a)
    lead["business_activity"] = activity_label(g)
    lead["priority"], lead["reasons"] = score(lead)
    return lead


def run_search(location_text: str, key: str, categories: list[str], target: int, max_miles: float,
               budget: int) -> None:
    """Runs in a background thread; progress is in RUN."""
    if not _run_lock.acquire(blocking=False):
        return
    try:
        RUN.update(running=True, message="Finding your location…", progress=0.0, found=0, error=None,
                   started=time.time())
        with _lock:
            d = load()
            home = d.get("home")
            if not home or home.get("text") != location_text:
                home = geocode(location_text, key)
                d["home"] = home
            purge_old(d)
            save(d)
        cats = [c for c in categories if c in CAT] or DEFAULT_CATEGORIES
        rings = tile_rings(home["lat"], home["lng"], max_miles)
        total_steps = sum(len(r) for r in rings) * len(cats)
        step = 0
        pool = ThreadPoolExecutor(max_workers=6)
        try:
            for ri, ring in enumerate(rings):
                ring_miles = ri * TILE_MILES + TILE_MILES / 2
                for tile in ring:
                    rect = {k: tile[k] for k in ("low", "high")}
                    for cat in cats:
                        step += 1
                        RUN.update(progress=step / max(total_steps, 1),
                                   message=f"Searching {CAT[cat][0].lower()} within ~{ring_miles:.0f} miles…")
                        places = search_text(cat, key, rect, budget)
                        known = load()["leads"]
                        fresh = []
                        for p in places:
                            if not p.get("id"):
                                continue
                            old = known.get(p["id"])
                            if old and old.get("google") and not old.get("stale"):
                                continue
                            lead = _lead_from(p, cat, home)
                            lead["activity"] = activity(p)
                            fresh.append(lead)
                        settled = list(pool.map(_settle, fresh))
                        qualified = _merge(settled)
                        RUN["found"] = qualified
                        if qualified >= target:
                            RUN.update(message=f"Done: {qualified} leads found within ~{ring_miles:.0f} miles.")
                            return
            RUN.update(message=f"Done: searched out to {max_miles:.0f} miles and found {RUN['found']} leads.")
        finally:
            pool.shutdown(wait=True)
    except PlacesError as e:
        RUN.update(error=str(e), message=str(e))
    except Exception as e:
        RUN.update(error=f"{type(e).__name__}: {e}", message="The search stopped because of an error.")
    finally:
        RUN.update(running=False, progress=1.0)
        _run_lock.release()


def _merge(settled: list[dict]) -> int:
    """Write new results without overwriting anything you changed meanwhile (status, notes, sites)."""
    with _lock:
        d = load()
        for lead in settled:
            old = d["leads"].get(lead["id"])
            if old:
                lead.update({k: old[k] for k in ("status", "notes", "site", "contacted_at") if k in old})
            d["leads"][lead["id"]] = lead
        save(d)
        return sum(1 for x in d["leads"].values() if x.get("qualified") and x.get("google"))


def listing(show_all: bool = False) -> list[dict]:
    d = load()
    rows = []
    for lead in d["leads"].values():
        if not lead.get("google"):
            continue
        if not show_all and not lead.get("qualified"):
            continue
        g = lead["google"]
        rows.append({
            "id": lead["id"], "name": (g.get("displayName") or {}).get("text", "Unknown"),
            "label": lead["label"], "category": lead["category"], "address": g.get("shortFormattedAddress")
            or g.get("formattedAddress"), "miles": lead["miles"], "phone": g.get("nationalPhoneNumber"),
            "emails": lead.get("emails", []), "website": g.get("websiteUri"), "maps": g.get("googleMapsUri"),
            "rating": g.get("rating"), "reviews": g.get("userRatingCount") or 0,
            "activity": lead["activity"], "business_activity": lead.get("business_activity"),
            "need": lead["need"], "audit": lead["audit"], "summary": lead.get("improvement_summary", ""),
            "priority": lead["priority"], "reasons": lead.get("reasons", []), "qualified": lead.get("qualified"),
            "status": lead.get("status", "new"), "notes": lead.get("notes", ""), "site": lead.get("site"),
            "contacted_at": lead.get("contacted_at")})
    rows.sort(key=lambda r: (r["status"] in ("not_interested", "won"), -r["priority"]))
    return rows


def update_lead(lead_id: str, **fields) -> dict | None:
    with _lock:
        d = load()
        lead = d["leads"].get(lead_id)
        if not lead:
            return None
        lead.update(fields)
        save(d)
        return lead
