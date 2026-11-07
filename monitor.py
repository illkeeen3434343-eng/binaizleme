#!/usr/bin/env python3
"""
Apartment monitor -> Telegram. Watches multiple portals and messages you once
per brand-new matching listing.

Sources:
  * bina.az     — current GraphQL API (server-side filters, rich data + photo)
  * yeniemlak.az — server-rendered HTML (parsed directly; no signature needed)

Memory lives in seen.json via the GitHub Contents API (atomic, no git races).
Each source is seeded silently the first time it appears, so adding a new portal
never floods you with its existing listings.
"""
import base64
import bisect
import datetime as dt
import hashlib
import html
import json
import os
import re
import sys
import time
from urllib.parse import parse_qs, unquote, urlencode, urlparse, urlunparse

import requests

VERSION = "2026-11-08-a"   # bump this when you deploy; printed at start of every run

try:
    from curl_cffi import requests as cf_requests   # Chrome-TLS client to beat bot 403s
    HAS_CURL_CFFI = True
except Exception:
    HAS_CURL_CFFI = False


def _raw_get(url, headers=None, params=None, timeout=30, browser=False):
    """GET a URL. For bot-protected HTML sites (browser=True) use curl_cffi with a
    real Chrome TLS fingerprint; otherwise plain requests."""
    if browser and HAS_CURL_CFFI:
        return cf_requests.get(url, headers=headers, params=params, timeout=timeout,
                               impersonate="chrome")
    return requests.get(url, headers=headers, params=params, timeout=timeout)


# Per-site politeness. yeniemlak.az answers HTTP 429 ("too many requests") after about
# 30 quick requests: on 7 Nov 2026 the bot read 30 search pages in ~40 s and then every
# detail page was refused (429), so no yeniemlak owner check could finish. Requests to
# the hosts below are spaced out; a 429 is waited out ONCE (Retry-After, capped); if the
# site still refuses, that host is left alone for the rest of the run (nothing is
# recorded, so those listings are simply checked on the next run).
HOST_MIN_INTERVAL = {"yeniemlak.az": float(os.environ.get("YE_MIN_INTERVAL", "2.5"))}
RATE_LIMIT_WAIT = float(os.environ.get("RATE_LIMIT_WAIT", "45"))         # no Retry-After given
RATE_LIMIT_MAX_WAIT = float(os.environ.get("RATE_LIMIT_MAX_WAIT", "90"))
_HOST_LAST = {}          # host -> time of our last request to it
RATE_LIMITED = {}        # host -> True once a 429 survived the wait (this run only)


def _host(url):
    h = (urlparse(url).hostname or "").lower()
    return h[4:] if h.startswith("www.") else h


def _retry_after_seconds(r):
    """Seconds from a Retry-After header (number or HTTP date); None if absent/odd."""
    try:
        v = (getattr(r, "headers", None) or {}).get("Retry-After")
    except Exception:
        return None
    if not v:
        return None
    v = str(v).strip()
    if v.isdigit():
        return float(v)
    try:
        from email.utils import parsedate_to_datetime
        return max(0.0, parsedate_to_datetime(v).timestamp() - time.time())
    except Exception:
        return None


class _Refused:
    """Stand-in response for a host that already rate-limited us this run."""
    status_code = 429
    text = ""
    content = b""
    headers = {}


def http_get(url, headers=None, params=None, timeout=30, browser=False):
    host = _host(url)
    gap = HOST_MIN_INTERVAL.get(host)
    if not gap:
        return _raw_get(url, headers=headers, params=params, timeout=timeout, browser=browser)
    if RATE_LIMITED.get(host):
        return _Refused()                     # do not keep knocking on a refusing site
    r = None
    for attempt in (1, 2):
        wait = _HOST_LAST.get(host, 0.0) + gap - time.time()
        if wait > 0:
            time.sleep(wait)
        try:
            r = _raw_get(url, headers=headers, params=params, timeout=timeout, browser=browser)
        finally:
            _HOST_LAST[host] = time.time()
        if getattr(r, "status_code", 0) != 429:
            return r
        if attempt == 1:
            ra = _retry_after_seconds(r)
            w = max(gap, min(RATE_LIMIT_MAX_WAIT, ra if ra is not None else RATE_LIMIT_WAIT))
            log(f"{host}: HTTP 429 (too many requests); waiting {w:.0f}s, then one retry")
            time.sleep(w)
    RATE_LIMITED[host] = True
    log(f"{host}: RATE-LIMITED - HTTP 429 again after waiting; no more {host} requests "
        "this run (unchecked listings are checked next run)")
    return r

# --------------------------------------------------------------------------- #
# YOUR SEARCHES  (paste the normal search URL from each site's address bar)
# --------------------------------------------------------------------------- #
BINA_SEARCH_URL = os.environ.get("BINA_SEARCH_URL", (
    "https://bina.az/baki/alqi-satqi/menziller?has_bill_of_sale=true&has_repair=true&location_ids%5B%5D=51&location_ids%5B%5D=100&location_ids%5B%5D=16&location_ids%5B%5D=11&location_ids%5B%5D=74&location_ids%5B%5D=52&location_ids%5B%5D=53&location_ids%5B%5D=54&location_ids%5B%5D=33&location_ids%5B%5D=99&location_ids%5B%5D=200"
))
YENIEMLAK_SEARCH_URL = os.environ.get("YENIEMLAK_SEARCH_URL", (
    "https://yeniemlak.az/elan/axtar?elan_nov=1&emlak=1&menzil_nov=&qiymet=&qiymet2=&mertebe=&mertebe2=&otaq=&otaq2=&sahe_m=&sahe_m2=&sahe_s=&sahe_s2=&seher%5B%5D=7&rayon%5B%5D=2&rayon%5B%5D=9&menteqe%5B%5D=20&menteqe%5B%5D=66&menteqe%5B%5D=72&menteqe%5B%5D=73&metro%5B%5D=1&metro%5B%5D=2&metro%5B%5D=3&metro%5B%5D=4&metro%5B%5D=5&photo=1"
))
TAP_SEARCH_URL = os.environ.get("TAP_SEARCH_URL", (
    "https://tap.az/elanlar/dasinmaz-emlak/menziller?keywords_source=typewritten&p%5B740%5D=3722&q%5Bregion_id%5D=420&q%5Bis_shop%5D=false"
))
LALAFO_SEARCH_URL = os.environ.get("LALAFO_SEARCH_URL", (
    "https://lalafo.az/baku/kvartiry/prodazha-kvartir/owner/nizaminskij/hatainskij/gara-garaev/neftchiler/halglar-dostlugu/ahmedly/azi-aslanov/fresh-renovation/kupchaya/8-oy-kilometr/ahmedlyi/staryiy-gyunyashli/pos-azi-aslanov"
))

# Sources this bot tracks. ORDER MATTERS: tap.az only ever serves its first results
# page (it ignores ?page=N), so it must be read as soon as a run starts. bina's full
# scan takes ~10 minutes and goes last; if a slow run makes cron skip a tick, the
# page-1 sources are not the ones that lose coverage.
SOURCES = [
    {"name": "tap.az", "type": "tap", "url": TAP_SEARCH_URL, "prefix": "tap:", "mode": "owner_new", "owner_label": "Sahibindən"},
    {"name": "lalafo.az", "type": "lalafo", "url": LALAFO_SEARCH_URL, "prefix": "lala:", "mode": "owner_new", "owner_label": "Mülkiyyətçi", "prefiltered_owner": True},
    {"name": "yeniemlak.az", "type": "yeniemlak", "url": YENIEMLAK_SEARCH_URL, "prefix": "ye:", "mode": "owner_new", "owner_label": "Əmlak sahibi"},
    {"name": "bina.az", "type": "bina", "url": BINA_SEARCH_URL, "prefix": "", "mode": "price_owner", "owner_label": "Mülkiyyətçi"},
]

# Run only a subset of sources on a given host (e.g. GitHub vs your VM).
# Set ENABLED_SOURCES="bina.az,yeniemlak.az" on GitHub and "tap.az,lalafo.az" on the VM.
_enabled = os.environ.get("ENABLED_SOURCES", "").strip()
if _enabled:
    _want = {n.strip() for n in _enabled.split(",") if n.strip()}
    SOURCES = [s for s in SOURCES if s["name"] in _want]

# bina.az config
CITY_ID = os.environ.get("BINA_CITY_ID", "1")
CATEGORY_ID = os.environ.get("BINA_CATEGORY_ID", "1")
PERSISTED_HASH = os.environ.get("BINA_PERSISTED_HASH",
    "b781511a943a4d710eefdf811a24dd4ae353e55d836952603ce0b37fde97d073")
GRAPHQL_URL = "https://bina.az/graphql"
OPERATION = "SearchItems"
SORT = "BUMPED_AT_DESC"
PAGE_SIZE = int(os.environ.get("BINA_PAGE_SIZE", "16"))  # bina GraphQL `first:`
# Price tracking needs the FULL result set each run (not just the newest listings),
# so an old listing's price change is still fetched and compared. SCAN_PAGES caps how
# many pages we page through — a safety valve against hammering bina / getting blocked.
# 120 pages * 16 = ~1920 listings. If your search has more matches than this, either
# raise it (block risk) or narrow the search so the whole set fits.
SCAN_PAGES = int(os.environ.get("SCAN_PAGES", "1000"))
PAGE_DELAY = float(os.environ.get("PAGE_DELAY", "0.25"))   # politeness pause between pages

# bina's server matches locations hierarchically, so selecting a district also returns
# its sub-areas (metros / settlements). These are sub-areas you do NOT want even though
# they sit inside a chosen district. Matched against the listing's location name.
# Paid promotion (VIP / Premium / "İrəli çək") re-surfaces OLD ads at the top of a
# BUMPED_AT_DESC sort, so a promoted ad can look brand new. Skip promoted ads, and/or
# require the ad to have been updated recently. 0 disables the age rule.
# A promoted ad is NOT automatically rejected: it may be a genuinely new post whose
# owner also paid to promote it. Rule: promoted + demonstrably OLD -> ignore (it is an
# old ad resurfaced by a paid bump); promoted + new (or age unknown) -> announce.
PROMOTED_MAX_AGE_DAYS = int(os.environ.get("PROMOTED_MAX_AGE_DAYS", "3"))
MAX_LISTING_AGE_DAYS = int(os.environ.get("MAX_LISTING_AGE_DAYS", "0"))
# Confirmed live on bina: isFeatured (Premium/crown) and isVipped (VIP). The regex
# stays as a fallback so a renamed/added flag is still caught and logged.
BINA_PROMO_FIELDS = ("isFeatured", "isVipped")
_PROMO_KEY = re.compile(r"vip|premium|featured|promoted|boost|highlight|paid|sticky|urgent", re.I)
_PROMO_DBG = {"done": False}


# bina item ids increase almost perfectly linearly with time (measured: 6374417 on
# 13 Aug -> 6409595 on 26 Aug -> ~6438584 on 5 Sep, i.e. ~2700-2900 new ids/day site
# wide). When the API gives no creation date we estimate age from how far below the
# newest id a listing sits. Self-calibrating: the newest id in each scan is "now".
BINA_IDS_PER_DAY = int(os.environ.get("BINA_IDS_PER_DAY", "2800"))
_BINA_MAX_ID = {"v": 0}


def _listing_age_days(l):
    """Age in days: exact from creation date if present, else estimated from the id.
    None = genuinely unknown (caller must not treat that as 'old')."""
    ts = l.get("created_at")
    if isinstance(ts, str) and ts[:4].isdigit():
        try:
            d = dt.datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if d.tzinfo is None:
                d = d.replace(tzinfo=dt.timezone.utc)
            return (dt.datetime.now(dt.timezone.utc) - d).days
        except Exception:
            pass
    iid, top = str(l.get("id") or ""), _BINA_MAX_ID["v"]
    if iid.isdigit() and top and BINA_IDS_PER_DAY > 0:
        gap = top - int(iid)
        if gap >= 0:
            return gap / BINA_IDS_PER_DAY
    return None


def _bina_age(l, now_ts):
    """(age_days, kind, posted_ts) for a bina listing, for the message label only.
    kind "exact" = bina's own creation date; "approx" = estimated from the id."""
    ts = _to_epoch(l.get("created_at")) if isinstance(l.get("created_at"), str) else None
    if ts is not None:
        return max(0.0, (now_ts - ts) / 86400.0), "exact", ts
    a = _listing_age_days(l)
    if a is None:
        return None, None, None
    return a, "approx", now_ts - a * 86400.0


def is_stale_promoted(l):
    """True only when the ad is promoted AND provably old -> a paid bump of an old ad."""
    if not l.get("_promoted"):
        return False
    age = _listing_age_days(l)
    if age is None:
        return False                     # unknown age -> do NOT drop a possibly-new post
    return age > PROMOTED_MAX_AGE_DAYS


def _node_promoted(node):
    """True if any promotion-ish boolean on the bina node is set. Field names differ
    across bina releases, so match by key pattern and log what was found once."""
    found = {}
    for k in BINA_PROMO_FIELDS:
        if node.get(k) is True:
            found[k] = True
    for k, v in node.items():
        if v is True and _PROMO_KEY.search(k):
            found[k] = v
    if found and not _PROMO_DBG["done"]:
        _PROMO_DBG["done"] = True
        log("bina promoted-flags seen:", sorted(found.keys()))
    return bool(found)


BINA_EXCLUDE_LOCATIONS = [x.strip() for x in os.environ.get(
    "BINA_EXCLUDE_LOCATIONS",
    "Koroğlu,Ağ şəhər,Şah İsmayıl Xətai"
).split(",") if x.strip()]
# Reject absurd price jumps (parse glitches), but allow any realistic change.
PRICE_GLITCH_LOW = float(os.environ.get("PRICE_GLITCH_LOW", "0.2"))    # new < 20% of old
PRICE_GLITCH_HIGH = float(os.environ.get("PRICE_GLITCH_HIGH", "5.0"))  # new > 5x old
MAX_PRICE_HISTORY = int(os.environ.get("MAX_PRICE_HISTORY", "0"))      # 0 = keep ALL transitions

# general config
STATE_FILE = os.environ.get("STATE_FILE", "seen.json")
# 0 (default) = UNLIMITED, permanent historical retention. seen.json is a permanent
# database, not a cache: nothing is ever dropped for being old. A positive value is
# only a safety valve against GitHub's per-file size limit (~100 MB); leave it 0
# unless you actually approach that (hundreds of thousands of listings).
MAX_SEEN = int(os.environ.get("MAX_SEEN", "0"))
SEND_PHOTOS = os.environ.get("SEND_PHOTOS", "true").lower() == "true"
# Price-drop alerts: notify when a known listing's price falls.
PRICE_DROP_MIN_ABS = int(os.environ.get("PRICE_DROP_MIN_ABS", "0"))   # ignore drops smaller than this (AZN)
DROP_ANOMALY_FLOOR = float(os.environ.get("DROP_ANOMALY_FLOOR", "0.4"))  # ignore >60% "drops" as parse errors

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

GH_TOKEN = os.environ.get("GH_TOKEN", "").strip()
GH_REPO = os.environ.get("GH_REPO", "").strip()
GH_BRANCH = os.environ.get("GH_BRANCH", "main").strip()
USE_API = bool(GH_TOKEN and GH_REPO)
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
API_HEADERS = {"User-Agent": UA, "Accept": "*/*",
               "Accept-Language": "az,en-US;q=0.9,en;q=0.8,ru;q=0.7",
               "Content-Type": "application/json",
               "Referer": "https://bina.az/baki/alqi-satqi/menziller",
               "Origin": "https://bina.az", "x-platform": "desktop"}
HTML_HEADERS = {"User-Agent": UA,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "az,en-US;q=0.9,en;q=0.8,ru;q=0.7"}


def log(*a):
    print(*a, flush=True)


class PersistedQueryError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Telegram
# --------------------------------------------------------------------------- #
def tg_send_message(text):
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                          json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML",
                                "disable_web_page_preview": False}, timeout=30)
        if r.status_code == 200:
            return True
        log("Telegram sendMessage failed:", r.status_code, r.text[:200])
        return False
    except requests.RequestException as e:
        log("Telegram error:", e)
        return False


def _download_image(url):
    """Fetch the image bytes ourselves so we can UPLOAD them to Telegram (far more
    reliable than asking Telegram to fetch a slow/http/hotlink-protected URL).
    Tries both http and https, sends a Referer to defeat hotlink protection."""
    if not url:
        return None
    variants = [url]
    if url.startswith("https://"):
        variants.append("http://" + url[len("https://"):])
    elif url.startswith("http://"):
        variants.append("https://" + url[len("http://"):])
    hdrs = {"User-Agent": UA, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
            "Referer": "https://yeniemlak.az/"}
    for u in variants:
        try:
            r = http_get(u, headers=hdrs, timeout=30, browser=True)
            data = getattr(r, "content", None)
            if getattr(r, "status_code", 0) == 200 and data and len(data) > 1000:
                return data
        except Exception as e:
            log("image download failed:", u, e)
    return None


def tg_send_photo(photo_url, caption):
    # 1) Preferred: download the image ourselves and UPLOAD the bytes (multipart).
    img = _download_image(photo_url)
    if img:
        try:
            r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                              data={"chat_id": CHAT_ID, "caption": caption, "parse_mode": "HTML"},
                              files={"photo": ("photo.jpg", img, "image/jpeg")}, timeout=60)
            if r.status_code == 200:
                return True
            log("sendPhoto upload failed:", r.status_code, r.text[:200])
        except requests.RequestException as e:
            log("sendPhoto upload error:", e)
    # 2) Fallback: let Telegram fetch the URL (works for fast HTTPS CDNs like bina).
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                          json={"chat_id": CHAT_ID, "photo": photo_url, "caption": caption,
                                "parse_mode": "HTML"}, timeout=30)
        if r.status_code == 200:
            return True
        log("sendPhoto by-url failed:", r.status_code, r.text[:160])
    except requests.RequestException as e:
        log("sendPhoto by-url error:", e)
    # 3) Last resort: text so the alert still arrives.
    return tg_send_message(caption)


# --------------------------------------------------------------------------- #
# Source 1: bina.az GraphQL
# --------------------------------------------------------------------------- #
def bina_filter_vars(url):
    q = parse_qs(urlparse(url).query)

    def one(k):
        v = q.get(k)
        return v[0] if v else None

    def b(v):
        return str(v).lower() in ("1", "true", "yes", "on") if v is not None else None

    def num(v):
        try:
            f = float(v)
            return int(f) if f.is_integer() else f
        except (TypeError, ValueError):
            return None

    f = {"cityId": CITY_ID, "categoryId": CATEGORY_ID, "leased": False}
    rooms = [str(v) for v in q.get("room_ids[]", []) if str(v).strip()]
    if rooms:
        f["roomIds"] = rooms
    locs = [str(v) for v in q.get("location_ids[]", []) if str(v).strip()]
    if locs:
        f["locationIds"] = locs
    if num(one("price_to")) is not None:
        f["priceTo"] = num(one("price_to"))
    if num(one("price_from")) is not None:
        f["priceFrom"] = num(one("price_from"))
    if num(one("area_from")) is not None:
        f["areaFrom"] = num(one("area_from"))
    if num(one("area_to")) is not None:
        f["areaTo"] = num(one("area_to"))
    if b(one("has_bill_of_sale")) is not None:
        f["hasBillOfSale"] = b(one("has_bill_of_sale"))
    if b(one("has_mortgage")) is not None:
        f["hasMortgage"] = b(one("has_mortgage"))
    # These were previously sent UNCONDITIONALLY as false. bina treats that as a real
    # constraint, not "unset": measured 11229 listings with them vs 13294 without -- so
    # 2065 listings (~15%) were hidden on every run. Send them only if the URL sets them.
    if b(one("floor_first")) is not None:
        f["floorFirst"] = b(one("floor_first"))
    if b(one("floor_last")) is not None:
        f["floorLast"] = b(one("floor_last"))
    return f


def _bina_params(filter_vars, cursor):
    variables = {"first": PAGE_SIZE, "filter": filter_vars, "sort": SORT}
    if cursor:
        variables["cursor"] = cursor
    return {"operationName": OPERATION,
            "variables": json.dumps(variables, separators=(",", ":"), ensure_ascii=False),
            "extensions": json.dumps(
                {"persistedQuery": {"version": 1, "sha256Hash": PERSISTED_HASH}},
                separators=(",", ":"))}


def _bina_node(node):
    def sub(k, fld):
        o = node.get(k)
        return o.get(fld) if isinstance(o, dict) else None

    preview = node.get("preview") or {}
    photo = preview.get("f460x345") or preview.get("thumbnail")
    area = sub("area", "value")
    try:
        area = float(area) if area is not None else None
    except (TypeError, ValueError):
        area = None
    price = sub("price", "total")
    try:
        price = int(float(price)) if price is not None else None
    except (TypeError, ValueError):
        price = None
    loc_id = sub("location", "id")
    try:
        loc_id = int(loc_id) if loc_id is not None else None
    except (TypeError, ValueError):
        loc_id = None
    path = node.get("path")
    return {"id": str(node["id"]), "rooms": node.get("rooms"), "area": area,
            "area_units": sub("area", "units") or "m²", "floor": node.get("floor"),
            "floors": node.get("floors"), "price": price,
            "currency": sub("price", "currency") or "AZN", "location_id": loc_id,
            "location": sub("location", "fullName") or sub("location", "name") or sub("city", "name"),
            "has_bill_of_sale": node.get("hasBillOfSale"), "has_mortgage": node.get("hasMortgage"),
            "has_repair": node.get("hasRepair"), "updated_at": node.get("updatedAt"),
            "created_at": node.get("createdAt") or node.get("publishedAt"),
            "_promoted": _node_promoted(node),
            "url": f"https://bina.az{path}" if path else f"https://bina.az/items/{node['id']}",
            "photo": photo}


def bina_check(url):
    q = parse_qs(urlparse(url).query)

    def one(k):
        v = q.get(k)
        return v[0] if v else None

    def i(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return None
    return {"rooms": {i(v) for v in q.get("room_ids[]", []) if i(v) is not None},
            "locs": {i(v) for v in q.get("location_ids[]", []) if i(v) is not None},
            "price_to": i(one("price_to")),
            "area_from": float(one("area_from")) if one("area_from") else None}


def bina_passes(l, c):
    # bina_filter_vars already sends roomIds / locationIds / price / area / hasBillOfSale
    # to the GraphQL server, so the server has ALREADY applied every filter. This function
    # is only a thin safety net against persisted-query drift; it must not re-filter.
    #
    # In particular it must NOT re-check location. bina's locations are a TREE: when you
    # select a district (rayon), the server also returns listings whose leaf location.id is
    # a settlement / metro / residential-complex INSIDE that district. That leaf id is not
    # in your district set, so the old `location_id not in c["locs"]` test silently dropped
    # them - ~18% of results (2053 of 11234), including real owner posts like 6428510.
    # We now trust the server for location entirely.
    #
    # The remaining guards only fire on a genuine contradiction (value present AND out of
    # range); a missing value is trusted, never dropped.
    loc = l.get("location") or ""
    if BINA_EXCLUDE_LOCATIONS and any(x in loc for x in BINA_EXCLUDE_LOCATIONS):
        return False
    if MAX_LISTING_AGE_DAYS > 0 and _listing_age_days(l) is not None \
            and _listing_age_days(l) > MAX_LISTING_AGE_DAYS:
        return False
    if c["rooms"] and l.get("rooms") is not None and l.get("rooms") not in c["rooms"]:
        return False
    if c["price_to"] is not None and l.get("price") is not None and l["price"] > c["price_to"]:
        return False
    if c["area_from"] is not None and l.get("area") is not None and l["area"] < c["area_from"]:
        return False
    return True


def fetch_bina(url):
    fv = bina_filter_vars(url)
    check = bina_check(url)
    out, cursor = [], None
    total_count = None
    pages = 0
    hit_cap = False
    for i in range(SCAN_PAGES):
        r = requests.get(GRAPHQL_URL, params=_bina_params(fv, cursor), headers=API_HEADERS, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        try:
            payload = r.json()
        except ValueError:
            raise RuntimeError("response was not JSON")
        if payload.get("errors"):
            msg = "; ".join(str(e.get("message", e)) for e in payload["errors"])
            if "PersistedQueryNotFound" in msg or "PERSISTED_QUERY_NOT_FOUND" in msg:
                raise PersistedQueryError(msg)
            raise RuntimeError(f"GraphQL error: {msg}")
        conn = (payload.get("data") or {}).get("itemsConnection")
        if not conn:
            raise RuntimeError("no itemsConnection")
        if total_count is None:
            total_count = conn.get("totalCount")
        for edge in conn.get("edges", []):
            node = edge.get("node")
            if node and node.get("id") is not None:
                try:
                    l = _bina_node(node)
                    if str(l["id"]).isdigit():       # calibrate "now" for age estimates
                        _BINA_MAX_ID["v"] = max(_BINA_MAX_ID["v"], int(l["id"]))
                    if bina_passes(l, check):
                        out.append(l)
                except Exception as e:
                    log("skip bina node:", e)
        pages += 1
        info = conn.get("pageInfo") or {}
        if not info.get("hasNextPage") or not info.get("endCursor"):
            break
        cursor = info["endCursor"]
        if i == SCAN_PAGES - 1:
            hit_cap = True
        if PAGE_DELAY:
            time.sleep(PAGE_DELAY)
    log(f"bina: scanned {pages} pages, {len(out)} matching listings"
        + (f" of ~{total_count} total" if total_count is not None else ""))
    # surface a coverage gap so you know if changes on deep listings are being missed
    if hit_cap and total_count and total_count > len(out):
        _coverage_warn(len(out), total_count)
    return out


_coverage_warned = {"done": False}


def _coverage_warn(scanned, total):
    if _coverage_warned["done"]:
        return
    _coverage_warned["done"] = True
   
# --------------------------------------------------------------------------- #
# yeniemlak.az  (server-rendered HTML; paginated via ?page=N)
# --------------------------------------------------------------------------- #
YE_MAX_PAGES = int(os.environ.get("YE_MAX_PAGES", "1000"))   # owner-new mode: newest pages only
MAX_OWNER_CHECKS = int(os.environ.get("MAX_OWNER_CHECKS", "100"))  # detail-page checks per run

# yeniemlak's metro[] is a SELLER-DECLARED tag with no tie to the district, so the
# server returns Yasamal / Binəqədi / Sabunçu posts for a Xətai+Nizami search
# (verified: metro=Neftçilər returns Sabunçu/Bakıxanov ads). Re-check the parsed
# address here. Set YE_KEYWORDS="" to disable this guard.
YE_KEYWORDS = [k.strip() for k in os.environ.get(
    "YE_KEYWORDS",
    "Xətai rayonu,Nizami rayonu,Əhmədli,Xalqlar,Neftçilər,Qara Qarayev,Q.Qarayev,"
    "Həzi Aslanov,H.Aslanov,8-ci km,Köhnə Günəşli"
).split(",") if k.strip()]


# The search asks for SALE ads (elan_nov=1), but the site also returns rentals
# ("Kirayə") and "Girov" ads - ad 235014 ("kiraye-1-otaqli", 7 Nov 2026) reached the
# owner check. Only sale ads are wanted. Set YE_DEALS="" to disable this guard.
YE_DEALS = [d.strip() for d in os.environ.get("YE_DEALS", "Satılır").split(",") if d.strip()]
_YE_SLUG_DEAL = {"satilir": "Satılır", "kiraye": "Kirayə", "girov": "Girov"}


def ye_deals(l):
    """Deal type(s) the card text and the link's slug say; empty set if neither is known."""
    out = set()
    if l.get("deal"):
        out.add(l["deal"])
    mm = re.search(r"/elan/(satilir|kiraye|girov)-", l.get("url") or "")
    if mm:
        out.add(_YE_SLUG_DEAL[mm.group(1)])
    return out


def ye_passes(l):
    """True if it is a sale ad (or its type is unknown) AND the parsed address matches
    one of the wanted areas."""
    if YE_DEALS and any(d not in YE_DEALS for d in ye_deals(l)):
        return False                       # rental / girov (either signal is enough)
    if not YE_KEYWORDS:
        return True
    loc = l.get("location") or ""
    return any(k in loc for k in YE_KEYWORDS)


# Baku metro stations as yeniemlak writes them (plus common short forms). Used ONLY to
# cut the station name cleanly out of the card text for the message's address line.
_BAKU_METROS = sorted({
    "İçərişəhər", "Sahil", "28 May", "Gənclik", "Nəriman Nərimanov", "N.Nərimanov",
    "Nərimanov", "Bakmil", "Ulduz", "Koroğlu", "Qara Qarayev", "Q.Qarayev", "Neftçilər",
    "Xalqlar Dostluğu", "Əhmədli", "Həzi Aslanov", "H.Aslanov", "Şah İsmayıl Xətai",
    "Xətai", "Cəfər Cabbarlı", "Nizami", "Elmlər Akademiyası", "İnşaatçılar", "20 Yanvar",
    "Memar Əcəmi", "Nəsimi", "Azadlıq prospekti", "Dərnəgül", "Avtovağzal", "8 Noyabr",
    "Xocəsən"}, key=len, reverse=True)


def _ye_address(raw_loc):
    """Clean address for the message from the text after "Ünvan:" on a yeniemlak card:
      'Bakı Xətai rayonu Əhmədli metro. Əhmədli Xudu Məmmədov küç Təcili satılır ...'
        -> 'Bakı, Xətai rayonu, Əhmədli m.'
      'Bakı Xətai rayonu metro Mənzil təcili olaraq satılır ...' -> 'Bakı, Xətai rayonu'
    On the list page the street runs straight into the ad's description, so the street
    is left out (the link opens the full ad). The old address line showed that run-on
    text (description words, half a street name). "" = nothing usable."""
    raw_loc = re.sub(r"\s+", " ", raw_loc or "").strip()
    mm = re.match(r"(.*?)\s*\bmetro\b(\.?)\s*(.*)$", raw_loc)
    if mm and len(mm.group(1).split()) <= 7:
        head, dotted, tail = mm.group(1).strip(), mm.group(2) == ".", mm.group(3)
    else:
        rm = re.match(r"(.{0,60}?\brayonu)\b", raw_loc)
        head, dotted, tail = (rm.group(1) if rm else ""), False, ""
    hm = re.match(r"(\S+)\s+(.+?\brayonu)\b\s*(.*)$", head)
    parts = [hm.group(1), hm.group(2), hm.group(3)] if hm else [head]
    parts = [p.strip(" ,") for p in parts if p and p.strip(" ,")]
    metro = None
    if dotted:
        low = tail.casefold()
        for name in _BAKU_METROS:
            n = name.casefold()
            if low.startswith(n) and (len(low) == len(n) or not low[len(n)].isalnum()):
                metro = name
                break
    if metro:
        if parts and parts[-1].casefold() == metro.casefold():
            parts.pop()                    # "Əhmədli, Əhmədli m." -> "Əhmədli m."
        parts.append(f"{metro} m.")
    return ", ".join(parts)


def _with_page(url, n):
    parts = urlparse(url)
    q = parse_qs(parts.query, keep_blank_values=True)
    q["page"] = [str(n)]
    new_q = urlencode(q, doseq=True)
    return urlunparse(parts._replace(query=new_q))


def _parse_yeniemlak_page(raw):
    id_url = {}
    for m in re.finditer(r'/elan/([A-Za-z0-9\-]*?-(\d{5,}))', raw):
        id_url.setdefault(m.group(2), "https://yeniemlak.az/elan/" + m.group(1))
    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw)))
    total_m = re.search(r"Nəticə:\s*(\d+)", text)
    total = int(total_m.group(1)) if total_m else None
    # "Kreditlə" sits between the price and "Baxış" on credit-eligible ads; without
    # the optional group those cards never start a block and are silently dropped.
    heads = [m.start() for m in re.finditer(
        r"(?:Satılır|Kirayə|Girov)\s*\d[\d ]*?\s*(?:Kreditlə\s*)?Baxış", text)]
    heads.append(len(text))
    out = []
    for i in range(len(heads) - 1):
        b = text[heads[i]:heads[i + 1]]
        m_id = re.search(r"Elan:\s*(\d+)", b)
        if not m_id:
            continue
        iid = m_id.group(1)
        pm = re.search(r"(Satılır|Kirayə|Girov)\s*(\d[\d ]*?)\s*(?:Kreditlə\s*)?Baxış", b)
        price = int(pm.group(2).replace(" ", "")) if pm else None
        deal = pm.group(1) if pm else None
        fl = re.search(r"(\d+)\s*/\s*(\d+)\s*Mərtəbə", b)
        rooms = re.search(r"(\d+)\s*otaq", b)
        area = re.search(r"(\d+)\s*m2", b)
        date = re.search(r"Tarix:\s*([\d.]+)", b)
        loc = None
        address = None
        lm = re.search(r"Ünvan:\s*(.+)", b)
        if lm:
            raw_loc = lm.group(1).strip()
            mm = re.search(r"(.*?metro\.\s*\S+(?:\s\S+)?)", raw_loc)
            loc = (mm.group(1) if mm else raw_loc[:70]).strip()   # area filter: unchanged
            address = _ye_address(raw_loc)                          # message only
        out.append({"id": iid, "url": id_url.get(iid, f"https://yeniemlak.az/elan/{iid}"),
                    "rooms": int(rooms.group(1)) if rooms else None,
                    "area": int(area.group(1)) if area else None, "area_units": "m²",
                    "floor": int(fl.group(2)) if fl else None,
                    "floors": int(fl.group(1)) if fl else None,
                    "price": price, "currency": "AZN", "location": loc, "address": address,
                    "location_id": None,
                    "updated_at": date.group(1) if date else None, "deal": deal,
                    "has_bill_of_sale": None, "has_mortgage": None, "has_repair": None,
                    "photo": None})
    return out, total


# Reading all ~30 result pages every 15 minutes is what made yeniemlak rate-limit the
# bot. The results are newest first (by "Tarix", the posted/renewed date), so a normal
# run reads only until it has passed every listing dated today or yesterday AND
# YE_QUIET_PAGES pages in a row held nothing to look at (every wanted-area listing
# already sent / judged / quietly known). Any unrecorded listing keeps it reading.
# A full read still happens every YE_FULL_CRAWL_HOURS, on the first run and after the
# search URL changes.
YE_RECENT_DAYS = float(os.environ.get("YE_RECENT_DAYS", "1"))     # today + N days back
YE_QUIET_PAGES = int(os.environ.get("YE_QUIET_PAGES", "2"))
YE_FULL_CRAWL_HOURS = float(os.environ.get("YE_FULL_CRAWL_HOURS", "3"))


def _ye_recent(l, now_ts):
    """Dated today or within YE_RECENT_DAYS before (Baku dates). Unknown date = recent."""
    d = _dmy_epoch(l.get("updated_at"))
    return d is None or now_ts - d < (YE_RECENT_DAYS + 1) * 86400


def fetch_yeniemlak(url, actionable=None):
    """Page through the yeniemlak result set. Stops when a page adds no new listing IDs
    (also the graceful fallback if ?page is ever ignored). Listings the site leaked
    outside the wanted districts are dropped by ye_passes().

    actionable: None = read every page. Otherwise a function(listing) -> bool ("worth
    a look") and paging stops after YE_QUIET_PAGES quiet pages in a row (see above)."""
    all_out, seen_ids = [], set()
    total = None
    parsed = pages = quiet = 0
    stop = "end of results"
    now_ts = time.time()
    for page in range(1, YE_MAX_PAGES + 1):
        purl = _with_page(url, page)
        r = http_get(purl, headers=HTML_HEADERS, timeout=30, browser=True)
        if r.status_code != 200:
            if page == 1:
                raise RuntimeError(f"HTTP {r.status_code}")
            stop = f"HTTP {r.status_code} on page {page}"
            break
        pages += 1
        listings, tot = _parse_yeniemlak_page(r.text)
        if tot is not None:
            total = tot
        new = [l for l in listings if l["id"] not in seen_ids]
        if not new:                       # no new IDs -> end of results (or page ignored)
            break
        busy = False
        for l in new:
            seen_ids.add(l["id"])         # always mark, so pagination still terminates
            parsed += 1
            if ye_passes(l):
                all_out.append(l)
                if actionable is not None and not busy:
                    busy = _ye_recent(l, now_ts) or bool(actionable(l))
        if actionable is not None:
            quiet = 0 if busy else quiet + 1
            if quiet >= YE_QUIET_PAGES:
                stop = f"{quiet} pages in a row with nothing new"
                break
        if PAGE_DELAY:
            time.sleep(PAGE_DELAY)
    log(f"yeniemlak: read {pages} page(s) [{'full' if actionable is None else 'newest'}; "
        f"stopped: {stop}], kept {len(all_out)} of {parsed} parsed"
        + (f" (~{total} on site)" if total is not None else ""))
    # Alarm on a PARSE failure, never on a legitimately empty filtered result.
    if not parsed and total and total > 0:
        raise RuntimeError("yeniemlak parsed 0 (structure changed?)")
    return all_out


def _ye_crawl_plan(source, now_ts=None):
    """(actionable-or-None, why) for this run's yeniemlak read. None = full read."""
    now_ts = time.time() if now_ts is None else now_ts
    st = _RUNTIME.get("state")
    if not isinstance(st, dict) or source.get("mode") != "owner_new":
        return None, "full read"
    seen = st.get("listings") or {}
    flags = st.get("seeded") or {}
    name, prefix = source["name"], source.get("prefix") or ""
    if not flags.get(name) or not prefix or not any(k.startswith(prefix) for k in seen):
        return None, "first run"           # everything must be recorded silently once
    fp_old = flags.get(name + ":url")
    if fp_old != _url_fingerprint(source.get("url")):
        return None, ("search URL changed" if fp_old else "full read (no URL fingerprint yet)")
    fulls = st.setdefault("full_crawl", {})
    last = _to_epoch(fulls.get(name))
    if last is None or now_ts - last >= YE_FULL_CRAWL_HOURS * 3600:
        # stamped when TRIED (not when finished): a site that keeps refusing must not
        # make every run a full read
        fulls[name] = dt.datetime.fromtimestamp(now_ts, dt.timezone.utc).isoformat(
            timespec="seconds")
        return None, f"full read (every {YE_FULL_CRAWL_HOURS:g} h)"
    stype = source.get("type")

    def actionable(l):
        return _candidate_kind(stype, l, seen.get(prefix + str(l["id"])), now_ts) is not None
    return actionable, "newest pages"


def fetch_for_source(source):
    if source["type"] == "bina":
        return fetch_bina(source["url"])
    if source["type"] == "yeniemlak":
        actionable, why = _ye_crawl_plan(source)
        log(f"yeniemlak: {why}")
        return fetch_yeniemlak(source["url"], actionable=actionable)
    if source["type"] == "tap":
        return fetch_tap(source["url"])
    if source["type"] == "lalafo":
        return fetch_lalafo(source["url"])
    raise RuntimeError(f"unknown source type {source['type']}")


# --------------------------------------------------------------------------- #
# lalafo.az  (Next.js HTML; the /owner/ URL already returns ONLY owner posts,
# so every result is a "Mülkiyyətçi" listing — no per-post owner check needed.)
# --------------------------------------------------------------------------- #
LALAFO_MAX_PAGES = int(os.environ.get("LALAFO_MAX_PAGES", "1000"))


# lalafo renders listings from JavaScript, so the visible HTML tags are empty.
# The real data is embedded in the page's __NEXT_DATA__ JSON blob. We parse that.
LALAFO_MIN_BYTES = int(os.environ.get("LALAFO_MIN_BYTES", "50000"))


def _lalafo_next_data(raw):
    """Return the parsed __NEXT_DATA__ JSON dict, or None if absent/unparseable."""
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', raw, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def _is_lalafo_listing(d):
    """A real ad object: has an integer id, a price, and a /..-id-<n> url."""
    return (isinstance(d, dict)
            and isinstance(d.get("id"), int)
            and "price" in d
            and isinstance(d.get("url"), str)
            and "-id-" in d["url"])


def _lalafo_feed(node):
    """Find the paginated SEARCH feed: a dict holding an 'items' list of listing
    objects together with '_meta'/'_links' pagination. Recommendation/vip blocks
    lack that pagination, so keying on it avoids the out-of-area filler listings.
    Returns (items, links, meta)."""
    best = ([], {}, {})

    def walk(o):
        nonlocal best
        if isinstance(o, dict):
            items = o.get("items")
            if isinstance(items, list) and ("_meta" in o or "_links" in o):
                hits = [x for x in items if _is_lalafo_listing(x)]
                if len(hits) > len(best[0]):
                    best = (hits, o.get("_links") or {}, o.get("_meta") or {})
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(node)
    if not best[0]:                       # fallback: largest listing array anywhere
        flat = []

        def w2(o):
            nonlocal flat
            if isinstance(o, list):
                hits = [x for x in o if _is_lalafo_listing(x)]
                if len(hits) > len(flat):
                    flat = hits
                for x in o:
                    w2(x)
            elif isinstance(o, dict):
                for v in o.values():
                    w2(v)

        w2(node)
        best = (flat, {}, {})
    return best


def _lalafo_photo(item):
    imgs = item.get("images") or []
    if imgs and isinstance(imgs[0], dict):
        for k in ("original_url", "url", "thumbnail_url", "webp_url"):
            u = imgs[0].get(k)
            if isinstance(u, str) and u.startswith("http"):
                return u
    return None


_LALAFO_DBG = {"done": False}


def _to_epoch(v):
    """unix seconds / milliseconds / ISO string -> epoch seconds; None if unusable."""
    if isinstance(v, bool) or v is None:
        return None
    try:
        if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().isdigit()):
            x = float(v)
            if x > 1e12:            # milliseconds
                x /= 1000.0
            return x if x > 1e9 else None
        d = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.timestamp()
    except Exception:
        return None


# Free-text fields are EXCLUDED from the scan: an owner writing "vasitəçi narahat
# etməsin" must never be read as a seller-type of "Vasitəçi".
_LALAFO_FREETEXT = ("description", "text", "body", "content", "title", "seo",
                    "meta", "url", "slug", "comment")
_LALAFO_AGENT_VALUES = {"vasiteci", "makler", "agent", "rieltor", "agentlik",
                        "vasitecilik", "emlak agentliyi"}
_LALAFO_OWNER_VALUES = {"sahibi", "mulkiyyetci", "ev sahibi", "emlak sahibi", "sahib"}


def _lalafo_values(obj, key=""):
    """Every string value in the item, skipping free-text fields."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and any(sk in k.lower() for sk in _LALAFO_FREETEXT):
                continue
            yield from _lalafo_values(v, k)
    elif isinstance(obj, list):
        for v in obj:
            yield from _lalafo_values(v, key)
    elif isinstance(obj, str):
        yield key, obj


def _lalafo_seller_type(it):
    """'agent' / 'owner' / None, from lalafo's own 'Təklifin növü' style parameters.
    Schema-independent: matches on VALUES, so renamed keys cannot break it."""
    try:
        for _k, v in _lalafo_values(it):
            n = az_normalize(v).strip()
            if not n or len(n) > 24:
                continue
            if n in _LALAFO_AGENT_VALUES:
                return "agent"
            if n in _LALAFO_OWNER_VALUES:
                return "owner"
        u = it.get("user") if isinstance(it.get("user"), dict) else {}
        for k in ("pro", "is_pro", "is_business", "business", "is_shop"):
            if u.get(k) is True or it.get(k) is True:
                return "agent"
    except Exception:
        pass
    return None


def _lalafo_is_agent(it):
    return _lalafo_seller_type(it) == "agent"


# Wanted areas for lalafo, matched against the ad's location text AND its URL slug
# (lalafo slugs carry the district: .../baki-hmdli-2-otaqli...). Empty = city check only.
LALAFO_KEYWORDS = [k.strip() for k in os.environ.get(
    "LALAFO_KEYWORDS",
    "ehmedli,ahmadli,hmdli,xalqlar,xalqlar dostlugu,neftciler,qara qarayev,q qarayev,"
    "hezi aslanov,hzi aslanov,haci aslanov,8 km,8-ci km,8-ci kilometr,kohne gunesli,"
    "gunesli,xetai,xtai,nizami r"
    # NOT plain "nizami": lalafo writes "m. Nizami" for the Nizami METRO, which is in
    # Yasamal (verified on ad 42756259). "koroglu" removed: bina excludes Koroğlu.
).split(",") if k.strip()]


def lalafo_ok(l):
    """Two gates: the city (slug must be /baku/) and the district.
    Out-of-city fallback content (/dzhulfa/) and out-of-area Baku ads (Bayıl, Yasamal…)
    both leak into lalafo's owner feed, so the district must be checked too."""
    url = (l.get("url") or "").lower()
    if "/baku/" not in url:
        return False
    if not LALAFO_KEYWORDS:
        return True
    hay = az_normalize(url + " " + str(l.get("location") or ""))
    return any(az_normalize(k).strip() in hay for k in LALAFO_KEYWORDS)


def _parse_lalafo_page(raw):
    nd = _lalafo_next_data(raw)
    if nd is None:
        return [], False
    items, links, meta = _lalafo_feed(nd)
    out = []
    if items and not _LALAFO_DBG["done"]:
        _LALAFO_DBG["done"] = True
        try:
            log("lalafo item keys sample:", sorted(items[0].keys()))
        except Exception:
            pass
    for it in items:
        iid = str(it.get("id"))
        url = it.get("url") or ""
        if url.startswith("/"):
            url = "https://lalafo.az" + url
        elif not url.startswith("http"):
            url = f"https://lalafo.az/baku/ads/id-{iid}"
        title = it.get("title") or ""
        rooms_m = re.search(r"(\d+)\s*otaq", title)
        area_m = re.search(r"(\d+)\s*(?:kv|m2|m²)", title)
        stamp = it.get("updated_time") or it.get("created_time")
        try:
            upd = dt.datetime.fromtimestamp(int(stamp)).strftime("%d.%m.%Y") if stamp else None
        except Exception:
            upd = None
        price = it.get("price")
        if not isinstance(price, int):
            price = _digits(str(price))
        desc = it.get("description") if isinstance(it.get("description"), str) else ""
        out.append({"id": iid,
                    "url": url,
                    "rooms": _digits(rooms_m.group(1)) if rooms_m else None,
                    "area": _digits(area_m.group(1)) if area_m else None, "area_units": "m²",
                    "floor": None, "floors": None,
                    "price": price,
                    "currency": it.get("symbol") or it.get("currency") or "AZN",
                    "location": (title.strip()[:90] or None),
                    "location_id": it.get("city_id"),
                    "updated_at": upd,
                    "has_bill_of_sale": None, "has_mortgage": None, "has_repair": None,
                    "photo": _lalafo_photo(it),
                    "_agent": _lalafo_is_agent(it),
                    # title + description: checked for agency wording ("EKSKLUZIV",
                    # "ofis haqqı") in case the feed item lacks "Təklifin növü"
                    "_text": f"{title} {desc}",
                    # lalafo keeps the ORIGINAL creation date when an ad is edited
                    # (ad 42756259: created 22.12.2025, updated 15.10.2026)
                    "created_ts": _to_epoch(it.get("created_time")),
                    "updated_ts": _to_epoch(it.get("updated_time"))})
    # Does a genuine next page exist? Prefer the server's own "next" link; fall
    # back to page counters. When false, the next ?page= would be filler content.
    has_next = bool(links.get("next"))
    if not has_next and meta:
        try:
            cur = int(meta.get("currentPage") or meta.get("current_page") or 1)
            pc = int(meta.get("pageCount") or meta.get("page_count") or 1)
            has_next = cur < pc
        except Exception:
            has_next = False
    return out, has_next


def _fetch_lalafo_html(purl, tries=4):
    """Fetch one lalafo page, retrying transient Cloudflare block pages — those
    come back tiny and without __NEXT_DATA__ — with a short back-off."""
    last = None
    for attempt in range(tries):
        r = http_get(purl, headers=HTML_HEADERS, timeout=40, browser=True)
        last = r
        body = getattr(r, "text", "") or ""
        if (getattr(r, "status_code", 0) == 200
                and len(body) >= LALAFO_MIN_BYTES
                and "__NEXT_DATA__" in body):
            return r, body
        time.sleep(3 * (attempt + 1))       # 3s, 6s, 9s before each retry
    return last, (getattr(last, "text", "") or "")


def fetch_lalafo(url):
    all_out, seen_ids = [], set()
    for page in range(1, LALAFO_MAX_PAGES + 1):
        purl = _with_page(url, page)
        r, body = _fetch_lalafo_html(purl)
        if getattr(r, "status_code", 0) != 200 or "__NEXT_DATA__" not in body:
            if page == 1:
                raise RuntimeError("lalafo blocked (Cloudflare) after retries")
            break
        listings, has_next = _parse_lalafo_page(body)
        # Only cry "structure changed" when the page clearly HAS listings we failed
        # to parse; a genuinely empty owner feed (no owner posts) stays silent.
        if page == 1 and not listings and len(re.findall(r"-id-\d+", body)) > 3:
            raise RuntimeError("lalafo parsed 0 (structure changed?)")
        new = [l for l in listings if l["id"] not in seen_ids]
        for l in new:
            seen_ids.add(l["id"])
            all_out.append(l)
        if not has_next:            # server says this is the last real page -> stop
            break                   # (prevents drifting into out-of-area filler ads)
        if not new:                 # safety: nothing new yet still "next" -> stop
            break
        if PAGE_DELAY:
            time.sleep(PAGE_DELAY)
    log(f"lalafo.az: {len(all_out)} listings")
    return all_out


# --------------------------------------------------------------------------- #
# tap.az  (server-rendered HTML; paginated via ?page=N)
# --------------------------------------------------------------------------- #
TAP_MAX_PAGES = int(os.environ.get("TAP_MAX_PAGES", "1000"))
# Only track tap.az posts whose heading names one of these places. tap headings use
# FULL names ("Qara Qarayev m.", "8-ci kilometr qəs.") - the old list had only
# "Q.Qarayev" and "8-ci km", so those two areas were silently dropped. Matched after
# az_normalize, so diacritics/case/punctuation do not matter. Comma-separated env.
TAP_KEYWORDS = [k.strip() for k in os.environ.get(
    "TAP_KEYWORDS",
    "Əhmədli,Xətai r,Nizami r,Qara Qarayev,Q.Qarayev,Neftçilər,Xalqlar,Həzi,"
    "8-ci km,8-ci kilometr,8 km,Köhnə Günəşli"
).split(",") if k.strip()]


def _tap_location_ok(heading):
    """True if the card heading names a wanted place (word-start match, normalized)."""
    if not TAP_KEYWORDS:
        return True
    hay = az_normalize(heading)
    return any(" " + az_normalize(k).strip() in hay for k in TAP_KEYWORDS)
# Owner-only: skip dealer/store cards (they carry a "Mağaza" badge). Set
# TAP_OWNER_ONLY=0 to fall back to keeping every card.
TAP_OWNER_ONLY = os.environ.get("TAP_OWNER_ONLY", "1") not in ("0", "false", "False", "no")


def _digits(s):
    """Extract an int from a messy string; None if no digits (never crashes)."""
    d = re.sub(r"\D", "", s or "")
    return int(d) if d else None


def _parse_tap_page(raw):
    total_m = re.search(r"(\d[\d\s]*)\s*elan\b", re.sub(r"<[^>]+>", " ", html.unescape(raw)))
    total = _digits(total_m.group(1)) if total_m else None
    # each listing card is anchored by a /menziller/<id> link; slice card HTML between anchors
    anchors = [(m.group(1), m.start()) for m in
               re.finditer(r'/elanlar/dasinmaz-emlak/menziller/(\d+)', raw)]
    out, seen_ids = [], set()
    dealers = 0
    for i, (iid, pos) in enumerate(anchors):
        if iid in seen_ids:
            continue
        seen_ids.add(iid)
        end = anchors[i + 1][1] if i + 1 < len(anchors) else min(len(raw), pos + 4000)
        chunk_html = raw[pos:end]
        photo = None
        pm_img = re.search(r'src="(https?://[^"]*(?:tap\.az|azstatic|cdn|umico)[^"]*\.(?:jpg|jpeg|png|webp)[^"]*)"',
                           chunk_html)
        if pm_img:
            photo = pm_img.group(1)
        chunk = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", chunk_html)))
        rooms = re.search(r"(\d+)-otaqlı", chunk)
        area = re.search(r"([\d.]+)\s*m²", chunk)
        price = re.search(r"([\d][\d ]*?)\s*₼", chunk)
        loc = re.search(r"\d+-otaqlı[^,]*,\s*(.+?),\s*[\d.]+\s*m²", chunk)
        if not (rooms or price):
            continue                      # nav/other link, not a listing card
        heading_m = re.search(r"(\d+-otaqlı[^₼]*?m²)", chunk)
        heading = heading_m.group(1) if heading_m else chunk[:120]
        if not _tap_location_ok(heading):
            continue                      # heading doesn't match a wanted location -> skip
        if TAP_OWNER_ONLY:
            back = anchors[i - 1][1] if i > 0 else max(0, pos - 1500)
            pre = html.unescape(raw[back:pos])   # the badge sits just BEFORE this card's link
            if re.search(r"mağaza", pre, re.I):
                dealers += 1
                continue                  # store/dealer card -> skip (owner-only)
        try:
            area_val = float(area.group(1)) if area else None
        except ValueError:
            area_val = None
        out.append({"id": iid,
                    "url": f"https://tap.az/elanlar/dasinmaz-emlak/menziller/{iid}",
                    "rooms": _digits(rooms.group(1)) if rooms else None,
                    "area": area_val, "area_units": "m²",
                    "floor": None, "floors": None,
                    "price": _digits(price.group(1)) if price else None,
                    "currency": "AZN",
                    "location": loc.group(1).strip() if loc else None, "location_id": None,
                    "updated_at": None,
                    "has_bill_of_sale": None, "has_mortgage": None, "has_repair": None,
                    "photo": photo})
    if TAP_OWNER_ONLY and dealers:
        log(f"tap.az: skipped {dealers} dealer (Mağaza) cards on this page")
    return out, total


# --------------------------------------------------------------------------- #
# tap.az: owner vs. realtor
#
# tap.az has NO owner/agent field. q[is_shop]=false and the "Mağaza" badge exclude
# only paid STORE accounts; an individual realtor posts from an ordinary user
# account and is indistinguishable from an owner on the search card. So the card is
# no longer trusted — we open the ad and use three signals:
#   1. a /shops/ link on the page          -> dealer (hard)
#   2. agency wording in the description   -> realtor ("xidmət haqqı", "komissiya", …)
#   3. the same user_id posting many ads   -> realtor (counted from our own history)
# An explicit owner phrase ("vasitəçi narahat etməsin", "sahibindən") beats rule 2,
# because owners often write "no agents please" and would otherwise be filtered out.
# --------------------------------------------------------------------------- #
TAP_MAX_USER_ADS = int(os.environ.get("TAP_MAX_USER_ADS", "3"))
# Count how many APARTMENTS a seller has for sale on their profile; 3+ => agent.
# This mirrors the manual rule: the ad shows "İstifadəçinin bütün elanları" only for
# sellers with multiple listings; you then open the profile and count apartments.
TAP_MAX_APARTMENTS = int(os.environ.get("TAP_MAX_APARTMENTS", "3"))
# tap shows one of these under the seller name when the account has more than one ad:
#   "İstifadəçinin bütün elanları"  /  "İstifadəçinin digər elanları"  (+ Latin variants).
# Matching just one missed the others, so a multi-listing agent read as single-listing
# and their profile was never counted. Stored normalized (diacritic-folded, lowercased).
TAP_MULTI_MARKERS = [m_.strip() for m_ in os.environ.get(
    "TAP_MULTI_MARKERS",
    "bütün elanları,digər elanları,butun elanlari,diger elanlari,"
    "bütün elanlarına bax,digər elanlarına bax,istifadəçinin elanları"
).split(",") if m_.strip()]
TAP_MULTI_MARKER = "bütün elanları"   # kept for back-compat

# A business name on the SELLER ACCOUNT is a structural agent signal - stronger than
# any description wording, because it is account identity rather than free text
# ("Invest", "MMC", "Estate"...). Deliberately excludes "emlak"/"əmlak": tap's own
# breadcrumb says "Daşınmaz əmlak", which would match every single ad.
TAP_BUSINESS_TOKENS = [t.strip().lower() for t in os.environ.get(
    "TAP_BUSINESS_TOKENS",
    "invest,mmc,ltd,llc,group,qrup,estate,realty,realt,property,agency,agentlik,agent,"
    "consulting,konsalt,holding,development,construction,company,şirkət,sirket,"
    "broker,partners,capital,rieltor,realtor,makler,elit,vip elanlar"
).split(",") if t.strip()]

TAP_REALTOR_PHRASES = [p.strip().lower() for p in os.environ.get(
    "TAP_REALTOR_PHRASES",
    "xidmət haqqı,xidmet haqqi,komissiya,komisyon,komissyon,rieltor,rialtor,realtor,"
    "makler,əmlak agentliyi,emlak agentliyi,agentliyi,agentlik,ofis haqqı,ofis haqqi,"
    "ofis xidmət,ofis xidmet,ekskluziv,eksklüziv,bazamızda,bazamizda,müştərilərimiz,"
    "musterilerimiz,açarlar bizdə,acarlar bizde,açar bizdə,portfelimiz"
).split(",") if p.strip()]

TAP_OWNER_PHRASES = [p.strip().lower() for p in os.environ.get(
    "TAP_OWNER_PHRASES",
    "vasitəçi narahat,vasiteci narahat,vasitəçi yoxdur,vasitəçisiz,vasitecisiz,"
    "vasitəçi olmadan,sahibindən,sahibinden,mülkiyyətçidən,mulkiyyetciden,birbaşa sahibi,"
    # supplied by the user + spelling/diacritic variants
    "evin sahibiyəm,evin sahibiyem,ev sahibiyəm,ev sahibiyem,öz evimdi,oz evimdi,"
    "öz evimdir,oz evimdir,özümün evimdi,ozumun evimdi,ev özümündü,ev ozumundu,"
    # close variants worth covering
    "mənim evimdir,menim evimdir,mənim mənzilimdir,menim menzilimdir,"
    "öz mənzilimi,oz menzilimi,öz mənzilimdir,oz menzilimdir,şəxsi mənzilim,sexsi menzilim,"
    "sahibi mənəm,sahibi menem,özüm satıram,ozum satiram,sahibi satır,sahibi satir,"
    "mülkiyyətçiyəm,mulkiyyetciyem,birbaşa sahibindən,birbasa sahibinden"
).split(",") if p.strip()]



# =========================================================================== #
# Azerbaijani owner-signal detection
#
# Normalize -> match intent patterns -> score. Rationale for each piece:
#  * NORMALIZE: fold Azerbaijani letters to ASCII so "ozume mexsusdur" and
#    "özümə məxsusdur" are one string. Python's .lower() turns "İ" into i+U+0307,
#    so letters are mapped BEFORE lowering.
#  * PATTERNS not literals: suffixes vary (mexsusdu/mexsusdur, evimdi/evimdir,
#    deyil/deyilem), so patterns end in \w* rather than fixed endings.
#  * MASKING: owner spans are blanked out before agent patterns run, so
#    "makler deyilem" cannot also register as a makler signal.
#  * SCORE not gate: an unreadable profile must not silently bury an owner post.
# =========================================================================== #
_AZ_MAP = str.maketrans({
    "ə": "e", "Ə": "e", "ç": "c", "Ç": "c", "ş": "s", "Ş": "s",
    "ğ": "g", "Ğ": "g", "ü": "u", "Ü": "u", "ö": "o", "Ö": "o",
    "ı": "i", "I": "i", "İ": "i", "â": "a", "é": "e",
})


def az_normalize(text):
    """Fold Azerbaijani text to lowercase ASCII words separated by single spaces."""
    if not text:
        return ""
    # "%" would be stripped as punctuation below; keep it as a word so fee patterns
    # like "haqqı 1%" still match.
    t = str(text).replace("%", " faiz ").translate(_AZ_MAP).lower()
    t = re.sub(r"[^0-9a-z]+", " ", t)     # punctuation/newlines -> space
    return " " + re.sub(r"\s+", " ", t).strip() + " "


# --- explicit ownership statements ---------------------------------------- #
_OWNER_PATTERNS = [
    r"\b(menzil|ev|obyekt)\w*\s+(ozume|ozumun|mene|menim)\s+mexsus\w*",
    r"\b(ozume|ozumun|mene|menim)\s+mexsus\w*",
    r"\b(menzil|ev)\w*\s+(ozumdu\w*|ozumundu\w*|menimdi\w*)",
    r"\bozumun\s+(evi|menzili)\w*",
    r"\bsexsi\s+(oz\s+)?(ev|menzil)\w*",
    r"\b(oz|menim\s+oz)\s+(ev|menzil)im\w*",
    r"\bmenim\s+(ev|menzil)im\w*",                  # "mənim evimdir / mənzilimdir"
    r"\b(ev|menzil)in\s+sahibi\w*",
    r"\b(ev|menzil)\s*sahibiyem\b",
    r"\bsahibi\s+(ozumem|menem|ozum)\b",            # "sahibi özüməm / mənəm"
    r"\bmulkiyyetci(yem|\s+olaraq)\w*",
    r"\bbirbasa\s+(ozum|sahib|mulkiyyet)\w*",
    r"\bsahibinden\s+(satilir|satiram)\b",
    r"\bozum\s+(satiram|terefinden\s+satilir)\b",
    r"\bvasitecisiz\b",
    r"\bvasiteci\s+yoxdur\b",
    r"\bvasiteci\w*\s+olmadan\b",                   # "vasitəçi olmadan"
]
# --- explicit rejection of maklers/agents (equally strong) ---------------- #
_ANTI_AGENT_PATTERNS = [
    r"\b(men\s+)?makler\w*\s+deyil\w*",
    r"\b(makler|vasiteci|agent|rieltor)\w*\s+(yazmasin|narahat\s+etmesin|"
    r"zeng\s+etmesin|elaqe\s+saxlamasin|qebul\s+etmirem)\b",
    r"\b(makler|vasiteci|agent|rieltor)\w*\s+islemirem\b",
    r"\b(makler|vasiteci|agent|rieltor)\w*\s+lazim\s+deyil\b",
]
# --- agency wording (checked only AFTER owner spans are masked out) ------- #
_AGENCY_PATTERNS = [
    # ANY "... haqqi" fee (ofis haqqi, xidmet haqqi, komissiya haqqi, vasitecilik
    # haqqi). "ofis haqqi 1%" is a paid-service fee only an agent charges; the old
    # list only had "xidmet haqqi" and "ofis xidmet", so "ofis haqqi" slipped through.
    r"\b(ofis|xidmet|komissiya|komisyon|vasitecilik|agentlik|reyestr)\s*haqq\w*",
    r"\bofis\s+xidmet\w*",
    r"\bhaqq\w*\s+\d+\s*(faiz|%)",          # "haqqi 1 faiz" / "haqqi 1 %"
    r"\b\d+\s*(faiz|%)\s*(ofis|xidmet|komissiya)\w*",
    r"\bkomissiya\w*", r"\bkomisyon\w*",
    r"\bagentliy\w*", r"\bagentlik\w*", r"\bekskluziv\w*",
    r"\bbazamizda\b", r"\bmusterilerimiz\w*", r"\bportfel\w*",
    r"\brieltor\w*", r"\bmakler\s+(xidmet|haqq)\w*",
]
# --- business name on the ACCOUNT (identity, not free text) --------------- #
_BUSINESS_PATTERNS = [
    r"\binvest\w*", r"\bmmc\b", r"\bltd\b", r"\bllc\b", r"\bestate\w*",
    r"\brealty\w*", r"\bagency\w*", r"\bagentlik\w*", r"\bholding\w*",
    r"\bconsulting\w*", r"\bdevelopment\w*", r"\bemlak\s+(evi|merkezi|servis)\w*",
    r"\bmakler\w*", r"\brieltor\w*", r"\brealtor\w*", r"\bbroker\w*",
]

OWNER_SCORE_STRONG = 4
SCORE_OWNER_AT = int(os.environ.get("TAP_SCORE_OWNER_AT", "3"))
SCORE_AGENT_AT = int(os.environ.get("TAP_SCORE_AGENT_AT", "-3"))
# What to do when the profile cannot be read AND the text gives no signal either way.
# "owner" = announce (never silently lose a post); "defer" = wait and retry.
TAP_UNKNOWN_DEFAULT = os.environ.get("TAP_UNKNOWN_DEFAULT", "owner").strip().lower()


def owner_signal_score(description, seller_block="", apartment_count=None):
    """Score a listing. Returns (score, reasons). Positive = owner, negative = agent."""
    text = az_normalize(description)
    reasons, score = [], 0

    masked = text
    for pat in _OWNER_PATTERNS + _ANTI_AGENT_PATTERNS:
        for mm in list(re.finditer(pat, masked)):
            reasons.append("owner:" + mm.group(0).strip())
            score += OWNER_SCORE_STRONG
            # blank the span so "makler deyilem" cannot also read as an agent signal
            masked = masked[:mm.start()] + " " * (mm.end() - mm.start()) + masked[mm.end():]
    score = min(score, 8)                       # cap: repetition is not more proof

    for pat in _AGENCY_PATTERNS:
        mm = re.search(pat, masked)
        if mm:
            reasons.append("agency:" + mm.group(0).strip())
            score -= 4

    nb = az_normalize(seller_block)
    for pat in _BUSINESS_PATTERNS:
        mm = re.search(pat, nb)
        if mm:
            reasons.append("business-name:" + mm.group(0).strip())
            score -= 5
            break

    # Listing COUNT is a weak signal, and only APARTMENTS count. Unrelated ads
    # (phones, books) must never make someone an agent.
    if apartment_count is not None:
        if apartment_count >= TAP_MAX_APARTMENTS:
            reasons.append(f"count:{apartment_count}-apartments")
            score -= 6
        else:
            reasons.append(f"count:{apartment_count}-apartment(s)-ok")
            score += 1
    return score, reasons


def _tap_seller_block(raw):
    """Just the text immediately around the seller link, so business-name tokens are
    matched against the ACCOUNT NAME rather than the whole page (a wide window would
    pick up breadcrumbs and neighbouring ads)."""
    mm = re.search(r"/elanlar\?user_id=\d+", raw)
    if not mm:
        return ""
    a, b = max(0, mm.start() - 400), min(len(raw), mm.end() + 200)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw[a:b])))


TAP_PROFILE_BROWSER = os.environ.get("TAP_PROFILE_BROWSER", "0").strip() == "1"
TAP_PROFILE_MAX_PER_RUN = int(os.environ.get("TAP_PROFILE_MAX_PER_RUN", "5"))
TAP_PROFILE_TTL_DAYS = float(os.environ.get("TAP_PROFILE_TTL_DAYS", "3"))
_PROFILE_RUN = {"used": 0, "warned": False}
_RUNTIME = {"state": None}          # set by main(): lets helpers keep small caches in state


def tap_profile_count_browser(user_id):
    """Open the seller's profile in headless Chromium (the page builds its list with
    JavaScript) and count APARTMENT links. None if playwright is missing or the page
    cannot be rendered. Only used when TAP_PROFILE_BROWSER=1."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        if not _PROFILE_RUN["warned"]:
            _PROFILE_RUN["warned"] = True
            log("tap profile browser unavailable (pip install playwright && "
                "playwright install chromium):", e)
        return None
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=UA, locale="az-AZ")
                page.goto(f"https://tap.az/elanlar?user_id={user_id}",
                          wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_selector('a[href*="/elanlar/"]', timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(2500)
                rendered = page.content()
            finally:
                browser.close()
    except Exception as e:
        log(f"tap profile browser error for user {user_id}:", e)
        return None
    if _tap_blocked(rendered):
        return None
    ids = set(re.findall(r"/elanlar/dasinmaz-emlak/menziller/(\d+)", rendered))
    tap_profile_count_browser.last_ids = sorted(ids)      # for verify_tap_profile.py
    return len(ids) if ids else None    # >= 1 always (their own ad) or unreadable


tap_profile_count_browser.last_ids = []


def tap_apartment_count(user_id, cache):
    """How many APARTMENTS this tap.az seller has for sale, from their profile.

    Returns an int >= 1, or None when unknown. Plain HTTP can NOT read this: the
    category URL ignores user_id (it returns the general Baku feed - counting that
    made every seller look like a 28-apartment agent), and the real profile page is
    rendered by JavaScript. So: per-run cache -> saved cache (TTL) -> headless
    browser if TAP_PROFILE_BROWSER=1 -> otherwise None (unknown)."""
    if user_id in cache:
        return cache[user_id]
    n = None
    st = _RUNTIME.get("state")
    saved = st.setdefault("tap_profiles", {}) if isinstance(st, dict) else {}
    ent = saved.get(str(user_id))
    fresh = False
    if isinstance(ent, dict):
        t = _to_epoch(ent.get("t"))
        fresh = t is not None and (time.time() - t) <= TAP_PROFILE_TTL_DAYS * 86400
    if fresh:
        n = ent.get("n")
    elif TAP_PROFILE_BROWSER and _PROFILE_RUN["used"] < TAP_PROFILE_MAX_PER_RUN:
        _PROFILE_RUN["used"] += 1
        n = tap_profile_count_browser(user_id)
        if n is not None:
            saved[str(user_id)] = {"n": n, "t": dt.datetime.now(dt.timezone.utc)
                                   .isoformat(timespec="seconds")}
            log(f"tap.az: seller {user_id} profile shows {n} apartment(s)")
    cache[user_id] = n
    return n


def _tap_description(raw):
    """Just the ad's own description. Matching agency wording against the WHOLE page
    is wrong: words like "Agentlik"/"komissiya" also appear in nav, footer and in the
    'similar ads' cards, which silently rejected genuine owner posts."""
    mo = re.search(r'<meta[^>]+property="og:description"[^>]+content="([^"]*)"', raw)
    if mo and mo.group(1).strip():
        return html.unescape(mo.group(1))
    mo = re.search(r'class="[^"]*(?:lot-text|product-description|lot__description)[^"]*"[^>]*>(.*?)</div>',
                   raw, re.S)
    if mo:
        return html.unescape(re.sub(r"<[^>]+>", " ", mo.group(1)))
    return None


def _apt_key(l):
    """Identity of a FLAT for counting: rooms+area, so re-posts of the same flat
    count once. Falls back to the listing id when rooms/area are unknown."""
    r, a = l.get("rooms"), l.get("area")
    if r is not None and a is not None:
        try:
            return f"{int(r)}r-{int(round(float(a)))}m"
        except (TypeError, ValueError):
            pass
    return "id-" + str(l.get("id"))


TAP_HISTORY_DAYS = float(os.environ.get("TAP_HISTORY_DAYS", "60"))


def _seller_apartments(seen):
    """seller id -> set of DISTINCT flats we recorded for that seller (tap) in the last
    TAP_HISTORY_DAYS. The rule is "3+ apartments ON SALE", so flats seen long ago
    (probably sold) do not count."""
    out = {}
    cutoff = time.time() - TAP_HISTORY_DAYS * 86400
    for k, v in seen.items():
        sid = v.get("seller")
        if not sid:
            continue
        t = _to_epoch(v.get("first_seen"))
        if t is not None and t < cutoff:
            continue
        # id WITHOUT the "tap:" prefix, so it matches _apt_key(listing) for the same ad
        # (re-checking a recorded ad must not count it twice)
        out.setdefault(sid, set()).add(
            _apt_key({"rooms": v.get("rooms"), "area": v.get("area"),
                      "id": k.split(":", 1)[-1]}))
    return out


def tap_check_owner(url, seller_counts=None, profile_cache=None, listing=None):
    """Owner vs makler for one tap.az ad, by weighted signals.
    Returns (is_owner, photo, seller_id); None = undecided.
    tap_check_owner.last_reason: "fetch" (page unreadable -> retry, no try used) or
    "undecided" (read fine, signals inconclusive)."""
    tap_check_owner.last_reason = "fetch"
    tap_check_owner.last_score, tap_check_owner.last_reasons = None, []
    tap_check_owner.last_location = None
    try:
        r, raw = _fetch_tap_html(url, tries=2)
    except Exception as e:
        log("tap detail fetch error:", e)
        return None, None, None
    if getattr(r, "status_code", 0) != 200 or _tap_blocked(raw) or len(raw) < 2000:
        return None, None, None
    tap_check_owner.last_reason = "undecided"
    # The ad's own title carries the same "2-otaqlı mənzil, Əhmədli m., 60 m²" heading as
    # the list card: keep its location so a message built from this page has an address.
    tm = (re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"', raw)
          or re.search(r"<title>(.*?)</title>", raw, re.S))
    lm_ = re.search(r"\d+-otaqlı[^,]*,\s*(.+?),\s*[\d.]+\s*m²",
                    html.unescape(tm.group(1)) if tm else "")
    tap_check_owner.last_location = lm_.group(1).strip() if lm_ else None

    seller = (re.search(r"/elanlar\?user_id=(\d+)", raw) or [None, None])[1]
    photo = None
    pm = re.search(r'(https?://tap\.azstatic\.com/uploads/[^\s"\'<>]+\.(?:jpg|jpeg|png|webp))', raw)
    if pm:
        photo = pm.group(1)

    # Hard reject: a paid STORE account is unambiguously a business.
    if re.search(r'href="[^"]*/shops/', raw):
        tap_check_owner.last_reason = "ok"
        return False, photo, seller

    desc = _tap_description(raw)
    block = _tap_seller_block(raw)
    flat = az_normalize(re.sub(r"<[^>]+>", " ", raw))
    has_multi = any(az_normalize(mk).strip() in flat for mk in TAP_MULTI_MARKERS)

    # How many APARTMENTS does this seller have? Unrelated ads (phones, books) never
    # count. Source 1: flats WE have recorded from this seller (re-posts of the same
    # flat count once). Source 2: their profile, only via a real browser (optional).
    this_key = _apt_key(listing or {"id": url.rstrip("/").rsplit("/", 1)[-1]})
    prior = (seller_counts or {}).get(seller) if seller else None
    if isinstance(prior, (set, frozenset, list, tuple)):
        hist = len(set(prior) | {this_key})
    elif isinstance(prior, int):
        hist = prior + 1
    else:
        hist = 1
    count = None
    if seller and hist >= TAP_MAX_APARTMENTS:
        count = hist                   # we have SEEN them sell 3+ different flats
    elif seller and has_multi:
        prof = tap_apartment_count(seller, profile_cache if profile_cache is not None else {})
        count = max(prof, hist) if prof is not None else None    # None = unknown
    elif seller:
        count = 1                      # no multi-listing marker => this is their only ad

    score, reasons = owner_signal_score(desc or "", block, count)
    if seller and hist >= TAP_MAX_APARTMENTS:
        reasons.append(f"history:{hist}-flats-seen")

    verdict = None
    if score >= SCORE_OWNER_AT:
        verdict = True
    elif score <= SCORE_AGENT_AT:
        verdict = False
    elif count is not None:
        verdict = count < TAP_MAX_APARTMENTS      # no strong wording: fall back to count
    elif TAP_UNKNOWN_DEFAULT == "owner":
        # Apartment count unknown AND no wording either way. The multi-listing marker
        # alone is NOT evidence of being a makler (owners sell phones too).
        verdict = True
        reasons.append("count-unknown:defaulted-to-owner")
    # else: leave undecided (TAP_UNKNOWN_DEFAULT=defer)
    if verdict is not None:
        tap_check_owner.last_reason = "ok"
    tap_check_owner.last_score, tap_check_owner.last_reasons = score, list(reasons)

    log(f"tap.az: {url} score={score} -> "
        f"{ {True: 'OWNER', False: 'AGENT', None: 'DEFER'}[verdict] } {reasons}")
    return verdict, photo, seller


tap_check_owner.last_reason = ""
tap_check_owner.last_score = None
tap_check_owner.last_reasons = []
tap_check_owner.last_location = None


def _tap_blocked(body):
    low = (body or "").lower()
    return any(x in low for x in ("just a moment", "cf-chl", "challenge-platform", "attention required"))


def _fetch_tap_html(purl, tries=4):
    """Fetch one tap.az page, retrying transient Cloudflare block pages."""
    last = None
    for attempt in range(tries):
        r = http_get(purl, headers=HTML_HEADERS, timeout=30, browser=True)
        last = r
        body = getattr(r, "text", "") or ""
        if getattr(r, "status_code", 0) == 200 and not _tap_blocked(body):
            return r, body
        time.sleep(3 * (attempt + 1))     # 3s, 6s, 9s before each retry
    return last, (getattr(last, "text", "") or "")


def fetch_tap(url):
    """Page through tap.az results. An empty result (no owner posts matching the
    filter right now) is a normal, quiet state - only a real block raises."""
    all_out, seen_ids = [], set()
    for page in range(1, TAP_MAX_PAGES + 1):
        purl = _with_page(url, page)
        r, body = _fetch_tap_html(purl)
        if getattr(r, "status_code", 0) != 200 or _tap_blocked(body):
            if page == 1:
                raise RuntimeError("tap.az blocked (Cloudflare) after retries")
            break
        listings, _ = _parse_tap_page(body)
        new = [l for l in listings if l["id"] not in seen_ids]
        if not new:
            break
        for l in new:
            seen_ids.add(l["id"])
            all_out.append(l)
        if PAGE_DELAY:
            time.sleep(PAGE_DELAY)
    log(f"tap.az: {len(all_out)} listings")
    return all_out


# --------------------------------------------------------------------------- #
# State (GitHub Contents API, atomic)
# --------------------------------------------------------------------------- #
def _gh_headers():
    return {"Authorization": f"Bearer {GH_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def _gh_url():
    return f"https://api.github.com/repos/{GH_REPO}/contents/{STATE_FILE}"


def _content_from_contents_json(j):
    """Return the file text from a Contents-API response, fetching the raw blob
    when the file is > 1 MB (content field empty)."""
    cb = j.get("content", "") or ""
    if cb.strip():
        return base64.b64decode(cb).decode("utf-8")
    if (j.get("size") or 0) > 0:
        rh = dict(_gh_headers())
        rh["Accept"] = "application/vnd.github.raw"
        rr = requests.get(_gh_url(), headers=rh, params={"ref": GH_BRANCH}, timeout=60)
        rr.raise_for_status()
        return rr.text
    return ""


def _merge_records(a, b):
    """Merge two versions of the same listing: keep the LONGER price_history so no
    recorded transition is ever lost; carry the most recent last_seen."""
    ha = a.get("price_history") if isinstance(a.get("price_history"), list) else []
    hb = b.get("price_history") if isinstance(b.get("price_history"), list) else []
    keep = dict(a) if len(ha) >= len(hb) else dict(b)
    seens = [x for x in (a.get("last_seen"), b.get("last_seen")) if x]
    if seens:
        keep["last_seen"] = max(seens)
    if keep.get("price_history"):
        lastp = keep["price_history"][-1].get("price")
        if isinstance(lastp, (int, float)):
            keep["price"] = int(lastp)
    return keep


def load_state():
    if not USE_API:
        if not os.path.exists(STATE_FILE):
            return {"listings": {}}, None
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        data.setdefault("listings", {})
        return data, None
    r = requests.get(_gh_url(), headers=_gh_headers(), params={"ref": GH_BRANCH}, timeout=30)
    if r.status_code == 404:
        return {"listings": {}}, None          # genuinely no file yet -> first run
    r.raise_for_status()
    j = r.json()
    sha = j["sha"]
    size = j.get("size", 0) or 0
    raw = _content_from_contents_json(j)
    if not raw.strip() and size > 0:
        raise RuntimeError(f"seen.json is {size} bytes but its content came back empty; "
                           "aborting run to protect history.")
    try:
        state = json.loads(raw) if raw.strip() else {"listings": {}}
    except json.JSONDecodeError as e:
        raise RuntimeError(f"seen.json failed to parse ({e}); aborting to protect history.")
    if not isinstance(state, dict):
        raise RuntimeError("seen.json is not a JSON object; aborting to protect history.")
    state.setdefault("listings", {})
    return state, sha


def _prune(state):
    # MAX_SEEN <= 0 means unlimited: keep ALL history, never truncate.
    if MAX_SEEN and MAX_SEEN > 0:
        L = state["listings"]
        if len(L) > MAX_SEEN:
            kept = sorted(L.items(), key=lambda kv: kv[1].get("first_seen", ""), reverse=True)[:MAX_SEEN]
            state["listings"] = dict(kept)


def save_state(state, sha):
    _prune(state)
    if not USE_API:
        try:
            # Atomic write: dump to a temp file, fsync, then os.replace(). A reader
            # (or the next run) can never see a half-written or empty seen.json even
            # if the process is killed or the disk fills mid-write.
            tmp = STATE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, STATE_FILE)
            return True, "local"
        except Exception as e:
            try:
                if os.path.exists(STATE_FILE + ".tmp"):
                    os.remove(STATE_FILE + ".tmp")
            except Exception:
                pass
            return False, f"local write: {e}"
    reason = "unknown"
    for attempt in range(6):
        try:
            r = requests.put(_gh_url(), headers=_gh_headers(), json=body_bytes(state, sha),
                             timeout=30)
        except requests.RequestException as e:   # network blip -> wait and retry
            reason = f"network error: {e}"
            time.sleep(2 * (attempt + 1))
            continue
        if r.status_code in (200, 201):
            return True, "saved"
        if r.status_code in (409, 422):
            reason = f"HTTP {r.status_code} (retrying)"
            g = requests.get(_gh_url(), headers=_gh_headers(), params={"ref": GH_BRANCH}, timeout=30)
            if g.status_code == 200:
                j = g.json()
                sha = j["sha"]
                raw = _content_from_contents_json(j)
                latest = json.loads(raw) if raw.strip() else {"listings": {}}
                latest.setdefault("listings", {})
                for k, v in state["listings"].items():
                    if k not in latest["listings"]:
                        latest["listings"][k] = v
                    else:  # same listing touched by both -> keep the fuller price history
                        latest["listings"][k] = _merge_records(latest["listings"][k], v)
                if state.get("source_status"):
                    latest["source_status"] = state["source_status"]
                if state.get("seeded"):
                    latest["seeded"] = state["seeded"]
                state = latest
                _prune(state)
                continue
            reason = f"re-read failed HTTP {g.status_code}"
            break
        if r.status_code in (500, 502, 503, 504, 429):   # transient GitHub outage -> back off & retry
            reason = f"HTTP {r.status_code} (GitHub temporary; retrying)"
            log("save_state transient:", reason)
            time.sleep(3 * (attempt + 1))
            continue
        reason = f"HTTP {r.status_code}: {r.text[:140]}"
        log("save_state failed:", reason)
        return False, reason
    return False, reason


def body_bytes(state, sha):
    body = {"message": "Update seen listings", "branch": GH_BRANCH,
            "content": base64.b64encode(
                json.dumps(state, ensure_ascii=False).encode("utf-8")).decode("ascii")}
    if sha:
        body["sha"] = sha
    return body


# --------------------------------------------------------------------------- #
# Message
# --------------------------------------------------------------------------- #
def _fmt_pub(v):
    if not v:
        return None
    try:
        return dt.datetime.fromisoformat(str(v)).strftime("%d %b %Y, %H:%M")
    except Exception:
        return str(v)


def _spaced(n):
    return f"{int(n):,}".replace(",", " ")


def format_change(l, source_name, kind, old_price, new_price, n_changes):
    head = "🔻 <b>QİYMƏT DÜŞDÜ</b>" if kind == "drop" else "🔺 <b>QİYMƏT ARTIMI</b>"
    lines = [f"{head} · {html.escape(source_name)}", ""]
    cur = l.get("currency", "AZN")
    old_txt = f"<s>{_spaced(old_price)}</s>" if kind == "drop" else _spaced(old_price)
    lines.append(f"💰 <b>Qiymət:</b> {old_txt} → <b>{_spaced(new_price)}</b> {cur}")
    if l.get("rooms") is not None:
        lines.append(f"🛏 <b>Otaq sayı:</b> {l['rooms']}")
    if l.get("area") is not None:
        area = int(l["area"]) if float(l["area"]).is_integer() else l["area"]
        lines.append(f"📐 <b>Sahə:</b> {area} {l.get('area_units', 'm²')}")
    if l.get("floor") and l.get("floors"):
        lines.append(f"🏢 <b>Mərtəbə:</b> {l['floor']}/{l['floors']}")
    if l.get("location"):
        lines.append(f"📍 <b>Ünvan:</b> {html.escape(str(l['location']))}")
    lines.append("")
    lines.append(f'🔗 <a href="{html.escape(l["url"])}">Elana bax</a>')
    return "\n".join(lines)


def notify_change(l, source_name, kind, old_price, new_price, n_changes):
    text = format_change(l, source_name, kind, old_price, new_price, n_changes)
    if SEND_PHOTOS and l.get("photo"):
        return tg_send_photo(l["photo"], text)
    return tg_send_message(text)


# --------------------------------------------------------------------------- #
# yeniemlak "new owner post" mode: announce new listings posted by the property
# OWNER ("Əmlak sahibi") — not agents/realtors ("Vasitəçi" / "Rieltor"). No price
# tracking for this source. The owner label lives on the detail page, so we only
# fetch detail pages for genuinely NEW listings (few per run).
# --------------------------------------------------------------------------- #
def _iter_dicts(obj):
    """Yield every dict nested anywhere inside a JSON structure."""
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _iter_dicts(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _iter_dicts(v)


def _bina_owner_from_ldjson(raw):
    """schema.org seller @type RealEstateAgent/Organization -> agent. A "Person" is
    NOT evidence of an owner (an individual realtor is a Person too), and site-level
    keys like author/provider describe the website, not the seller."""
    for m in re.finditer(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>',
                         raw, re.S):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        for obj in _iter_dicts(data):
            for key in ("seller", "offeredBy", "broker"):
                v = obj.get(key)
                if isinstance(v, dict):
                    t = v.get("@type") or v.get("type") or ""
                    t = " ".join(t) if isinstance(t, list) else str(t)
                    t = t.lower()
                    if any(x in t for x in ("realestateagent", "organization",
                                            "localbusiness", "corporation")):
                        return False
    return None


def _bina_owner_from_nextdata(raw):
    m = re.search(r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', raw, re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(1))
    except Exception:
        return None
    for obj in _iter_dicts(data):
        for key in ("leaser_type", "leaserType", "ownerType", "userType", "seller_type"):
            v = obj.get(key)
            if isinstance(v, str) and v.strip():
                lv = v.lower()
                if any(x in lv for x in ("owner", "mülk", "mulk", "sahib")):
                    return True
                if any(x in lv for x in ("agent", "agency", "makler", "vasit")):
                    return False
        for key in ("has_agency", "hasAgency", "is_agency", "isAgency", "is_shop", "isShop"):
            if obj.get(key) is True:
                return False
        for key in ("leaser_name", "leaserName", "contact_name"):
            v = obj.get(key)
            if isinstance(v, str) and v.strip():
                lv = v.lower()
                if any(x in lv for x in ("vasit", "agent", "makler", "agentlik")):
                    return False
                if any(x in lv for x in ("mülk", "mulk", "sahib")):
                    return True
    return None


_BINA_DBG = {"done": False}
_BINA_OWNER_LABELS = {"mulkiyyetci"}
_BINA_AGENT_LABELS = {"vasiteci", "vasiteci agent"}
_LABELISH = re.compile(r"k[iıİI]yy|vas[iıİI]t", re.I)


def _bina_label_verdict(raw):
    """Seller type from a STANDALONE label element, e.g. <div>mülkiyyətçi</div> or
    <span>vasitəçi (agent)</span>. Words inside the description are never labels:
    the old substring test turned an owner who wrote "Vasitəçilər narahat etməsin"
    into an agent, because "Vasitəçi" was checked first and anywhere on the page."""
    found_owner = found_agent = False
    for m in re.finditer(r">\s*([^<>]{2,40}?)\s*<", raw):
        txt = m.group(1)
        if not _LABELISH.search(txt):
            continue
        n = az_normalize(html.unescape(txt)).strip()
        if n in _BINA_OWNER_LABELS:
            found_owner = True
        elif n in _BINA_AGENT_LABELS:
            found_agent = True
    if found_owner and not found_agent:
        return True
    if found_agent and not found_owner:
        return False
    return None


def bina_is_owner(url):
    """bina.az owner vs agent from the detail page. Returns (is_owner, photo).
    None = undetermined. bina_is_owner.last_reason: "fetch" (page not read) or
    "nolabel" (read, but no seller label found)."""
    bina_is_owner.last_reason = "fetch"
    try:
        r = http_get(url, headers=HTML_HEADERS, timeout=30, browser=True)
        if getattr(r, "status_code", 0) != 200:
            return None, None
        raw = r.text or ""
    except Exception:
        return None, None
    if len(raw) < 1500:
        return None, None
    photo = None
    mo = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', raw)
    if mo:
        photo = mo.group(1)
    verdict, src = _bina_owner_from_ldjson(raw), "ldjson"
    if verdict is None:
        verdict, src = _bina_owner_from_nextdata(raw), "nextdata"
    if verdict is None:
        verdict, src = _bina_label_verdict(raw), "label"
    if verdict is None:
        # Last resort, description-safe: the owner label word, or the agent label in
        # its exact "vasitəçi (agent)" form. A bare "Vasitəçi" in free text decides
        # nothing (owners write "vasitəçilər narahat etməsin").
        text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw)))
        owner_sub = "Mülkiyyətçi" in text
        agent_sub = re.search(r"vasitəçi\s*\(\s*agent\s*\)", text, re.I) is not None
        if owner_sub and not agent_sub:
            verdict, src = True, "text"
        elif agent_sub and not owner_sub:
            verdict, src = False, "text"
    bina_is_owner.last_reason = "ok" if verdict is not None else "nolabel"
    if not _BINA_DBG["done"]:
        _BINA_DBG["done"] = True
        log(f"bina owner-detect: {url} -> {verdict} (via {src if verdict is not None else 'none'})")
    return verdict, photo


bina_is_owner.last_reason = ""


OWNER_MAX_TRIES = int(os.environ.get("OWNER_MAX_TRIES", "4"))
# Each failing detail page used to cost 3 attempts + ~9 s of sleep; at 100 checks a run
# that was ~15 minutes of yeniemlak alone, so cron skipped ticks and tap (page 1 only)
# was read half as often. Now: 2 attempts, and a run stops checking a source after this
# many unreadable pages in a row (they are retried next run, not given up).
OWNER_FETCH_TRIES = int(os.environ.get("OWNER_FETCH_TRIES", "2"))
OWNER_FETCH_FAIL_STREAK = int(os.environ.get("OWNER_FETCH_FAIL_STREAK", "5"))


def check_is_owner(url, owner_label="Əmlak sahibi"):
    """Returns (is_owner, photo_url). is_owner: True=owner, False=agent, None=unknown.
    Photo is the listing's first image, pulled from the same detail page (no extra request).

    Detail fetches DO fail intermittently (yeniemlak 168545 returned nothing from the VM
    while the page plainly contains "Əmlak sahibi"), so: retry, log the reason, and treat
    an unreadable page as UNKNOWN rather than "agent"."""
    check_is_owner.last_reason = "fetch"
    raw = text = None
    host = _host(url)
    for attempt in range(max(1, OWNER_FETCH_TRIES)):
        if RATE_LIMITED.get(host):
            # the site refused us even after waiting: not this listing's fault. No try is
            # used and nothing is recorded; it is checked on the next run.
            check_is_owner.last_reason = "ratelimit"
            return None, None
        try:
            r = http_get(url, headers=HTML_HEADERS, timeout=30, browser=True)
            if r.status_code == 200 and len(r.text or "") > 1500:
                raw = r.text
                text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw)))
                break
            log(f"owner-check {url} -> HTTP {r.status_code} "
                f"len={len(getattr(r, 'text', '') or '')} (attempt {attempt + 1})")
        except Exception as e:
            log(f"owner-check {url} -> {e} (attempt {attempt + 1})")
        if attempt + 1 < OWNER_FETCH_TRIES and not RATE_LIMITED.get(host):
            time.sleep(2.0)
    if raw is None:
        if RATE_LIMITED.get(host):
            check_is_owner.last_reason = "ratelimit"
        return None, None
    photo = None
    # Try several patterns, most specific first, so we catch the main photo even when
    # the path/extension varies (this fixes the ~2-in-10 "no image" cases).
    for pat in (r'https?://yeniemlak\.az/get-img/[^\s"\'<>]+\.(?:jpg|jpeg|png|webp)',
                r'https?://[^\s"\'<>]*yeniemlak\.az/[^\s"\'<>]*(?:img|shekil|photo|foto)[^\s"\'<>]+\.(?:jpg|jpeg|png|webp)',
                r'https?://[^\s"\'<>]+\.(?:jpg|jpeg)\b'):
        m = re.search(pat, raw)
        if m:
            cand = m.group(0)
            if "logo" not in cand.lower() and "icon" not in cand.lower():
                photo = cand
                break
    check_is_owner.last_reason = "ok"
    if owner_label in text:
        return True, photo
    if "Vasitəçi" in text or "Rieltor" in text or "Vasitəç:" in text:
        return False, photo
    check_is_owner.last_reason = "nolabel"
    # Page read fine but carries no seller label -> UNKNOWN, not "agent". Returning
    # False here is what silently buried owner posts; the caller retries a bounded
    # number of times (OWNER_MAX_TRIES) and only then settles.
    log(f"owner-check {url} -> page read but no seller label found")
    return None, photo


check_is_owner.last_reason = ""


def format_new_owner(l, source_name, owner_label="Əmlak sahibi"):
    # Ads older than NEW_MAX_AGE_DAYS (renewed, bumped, VIP/Premium, or missed earlier)
    # are still sent - once - but marked so you can tell them from brand-new ones.
    age = l.get("_age_days")
    old = isinstance(age, (int, float)) and age > NEW_MAX_AGE_DAYS
    head = "♻️ <b>Yenilənmiş elan</b>" if old else "‼️ <b>Yeni elan</b>"
    lines = [f"{head} · {html.escape(source_name)} · <i>{html.escape(owner_label)}</i>", ""]
    if old:
        lines.append(f"🕓 <b>Yerləşdirilib:</b> {_posted_text(l)}")
    if l.get("price") is not None:
        lines.append(f"💰 <b>Qiymət:</b> {_spaced(l['price'])} {l.get('currency', 'AZN')}")
    if l.get("rooms") is not None:
        lines.append(f"🛏 <b>Otaq sayı:</b> {l['rooms']}")
    if l.get("area") is not None:
        area = int(l["area"]) if float(l["area"]).is_integer() else l["area"]
        lines.append(f"📐 <b>Sahə:</b> {area} {l.get('area_units', 'm²')}")
    if l.get("floor") and l.get("floors"):
        lines.append(f"🏢 <b>Mərtəbə:</b> {l['floor']}/{l['floors']}")
    addr = l.get("address")              # clean display address (yeniemlak); "" = none
    if addr is None:
        addr = l.get("location")
    if addr:
        lines.append(f"📍 <b>Ünvan:</b> {html.escape(str(addr))}")
    lines.append("")
    lines.append(f'🔗 <a href="{html.escape(l["url"])}">Elana bax</a>')
    return "\n".join(lines)


def _posted_text(l, now_ts=None):
    """Date the ad was placed, as dd.mm.yyyy (Baku):
    exact  = the site's own creation date          -> "12.04.2026"
    approx = estimate (bina id)                     -> "~12.04.2026"
    min    = we only know it existed by then        -> "ən gec 12.04.2026" (at the latest)"""
    ts = l.get("_posted_ts")
    if not isinstance(ts, (int, float)):
        now_ts = time.time() if now_ts is None else now_ts
        ts = now_ts - float(l.get("_age_days") or 0) * 86400.0
    d = dt.datetime.fromtimestamp(ts, _BAKU_TZ).strftime("%d.%m.%Y")
    kind = l.get("_age_kind")
    if kind == "exact":
        return d
    if kind == "min":
        return f"ən gec {d}"
    return f"~{d}"


def _seller_counts(seen):
    """How many ads each seller account already has in our history."""
    counts = {}
    for v in seen.values():
        sid = v.get("seller")
        if sid:
            counts[sid] = counts.get(sid, 0) + 1
    return counts


# --------------------------------------------------------------------------- #
# "Is this listing really NEW?"  (tap / lalafo / yeniemlak)
#
# A listing the bot has never recorded is not necessarily new: "İrəli çək", VIP,
# Premium or a simple edit lifts an OLD ad to the top of the feed. Listing ids only
# ever grow, so our own history (each record's first_seen + id) tells us when ids of
# a given size first existed. For an unseen id we look up the earliest moment we saw
# an id at least that large: the ad existed by then, so (now - that moment) is a
# LOWER BOUND on its age. Lower bound => we can only under-estimate age, so a genuinely
# new ad is never suppressed. lalafo also gives the real creation time; that wins.
# --------------------------------------------------------------------------- #
NEW_MAX_AGE_DAYS = float(os.environ.get("NEW_MAX_AGE_DAYS", "3"))


OLD_MAX_PER_RUN = int(os.environ.get("OLD_MAX_PER_RUN", "15"))     # per source per run
IDCLOCK_GAP_MULT = float(os.environ.get("IDCLOCK_GAP_MULT", "50"))


class IdClock:
    """LOWER BOUND on a listing's age, learned from our own history. Used ONLY to label
    messages; it can never hide an ad.

    Within one id series, ids grow over time: if we recorded an id y >= x at time t,
    listing x already existed at t. But a site can run SEVERAL series at once
    (yeniemlak: sequential 1xx,xxx-2xx,xxx plus a separate 9xx,xxx range). The
    2026-10-18-a version used the earliest time ANY larger id was seen, so 9xx ids
    recorded in August made every new yeniemlak ad look ~75 days old and hid it
    (ad 234096, Nov 2026). Now only ids a short distance above x count (same series);
    with no close neighbour the age is unknown."""

    def __init__(self):
        self.ids, self.ts, self.gap = [], [], 0      # sorted by id; gap in id units

    @classmethod
    def from_seen(cls, seen, source_name, prefix):
        best = {}
        for k, v in seen.items():
            if not isinstance(v, dict) or v.get("source") != source_name:
                continue
            if not k.startswith(prefix):
                continue
            num = k[len(prefix):]
            if not num.isdigit():
                continue
            t = _to_epoch(v.get("first_seen"))
            if t is None:
                continue
            i = int(num)
            if i not in best or t < best[i]:
                best[i] = t
        c = cls()
        c.ids = sorted(best)
        c.ts = [best[i] for i in c.ids]
        if len(c.ids) >= 3:
            diffs = sorted(b - a for a, b in zip(c.ids, c.ids[1:]))
            c.gap = max(1, int(diffs[len(diffs) // 2] * IDCLOCK_GAP_MULT))
        return c

    def age_days(self, iid, now_ts):
        """Lower bound on the age of listing `iid` in days; None if unknown."""
        try:
            iid = int(iid)
        except (TypeError, ValueError):
            return None
        if not self.ids or self.gap <= 0:
            return None
        j = bisect.bisect_left(self.ids, iid)
        earliest = None
        while j < len(self.ids) and self.ids[j] - iid <= self.gap:
            if earliest is None or self.ts[j] < earliest:
                earliest = self.ts[j]
            j += 1
        if earliest is None:
            return None
        return max(0.0, (now_ts - earliest) / 86400.0)


_BAKU_TZ = dt.timezone(dt.timedelta(hours=4))


def _dmy_epoch(v):
    """'06.11.2026' (Baku date) -> epoch of that day's 00:00 Baku; None if unusable."""
    m_ = re.fullmatch(r"\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*", str(v or ""))
    if not m_:
        return None
    try:
        return dt.datetime(int(m_.group(3)), int(m_.group(2)), int(m_.group(1)),
                           tzinfo=_BAKU_TZ).timestamp()
    except ValueError:
        return None


def _age_estimate(l, clock, now_ts, rec=None):
    """(age_days or None, kind). kind "exact" = the site's creation date (lalafo);
    "min" = a lower bound (our id history, or when we first recorded the ad)."""
    created = l.get("created_ts")
    if isinstance(created, (int, float)) and created > 0:
        return max(0.0, (now_ts - created) / 86400.0), "exact"
    a = clock.age_days(l.get("id"), now_ts) if clock is not None else None
    # yeniemlak "Tarix" (dd.mm.yyyy) is the posted/renewed DAY: the ad is at least as old
    # as the end of that day (a renewed ad carries today's date -> adds nothing)
    d = _dmy_epoch(l.get("updated_at"))
    if d is not None:
        t = max(0.0, (now_ts - (d + 86399)) / 86400.0)    # last second of that day
        a = t if a is None else max(a, t)
    fs = _to_epoch((rec or {}).get("first_seen"))
    if fs is not None:
        b = max(0.0, (now_ts - fs) / 86400.0)
        a = b if a is None else max(a, b)
        # tap.az has ONE id series, so the age 2026-10-18-a wrote when it hid an ad
        # ("old ~100d") was a valid lower bound. NOT for yeniemlak: there it came from
        # the two-series bug (new ads were written down as "old ~75d").
        sk = re.match(r"old ~(\d+)d", str((rec or {}).get("skip", "")))
        if sk and (rec or {}).get("source") == "tap.az":
            c = float(sk.group(1)) + b
            a = max(a, c)
    return a, "min"


def _renewed_since_recorded(stype, l, rec, now_ts):
    """A listing we recorded silently (never sent) that its owner renewed or bumped
    AFTER we recorded it, recently. Only for sites that publish a trustworthy date:
    yeniemlak "Tarix" (posted/renewed date, ad 152232: recorded 24 Aug, Tarix 06.11)
    and lalafo updated_time."""
    fs = _to_epoch(rec.get("first_seen"))
    if fs is None:
        return False
    if stype == "yeniemlak":
        d = _dmy_epoch(l.get("updated_at"))
        return (d is not None and d >= fs + 2 * 86400
                and now_ts - d <= (NEW_MAX_AGE_DAYS + 1) * 86400)
    if stype == "lalafo":
        u = l.get("updated_ts")
        return (isinstance(u, (int, float)) and u >= fs + 86400
                and now_ts - u <= NEW_MAX_AGE_DAYS * 86400)
    return False


def _candidate_kind(stype, l, rec, now_ts):
    """Why this listing should be looked at now - or None.
      new      never recorded
      retry    an earlier check was inconclusive (tries left)
      hidden   recorded by 2026-10-18-a as "not new" and never checked or sent
      renewed  recorded silently, then renewed/bumped by its owner
    A listing that was sent (owner True) or judged an agent (owner False) is never
    looked at again: nothing is ever sent twice."""
    if rec is None:
        return "new"
    if rec.get("owner") is True or rec.get("owner") is False:
        return None
    if _is_retryable(rec):
        return "retry"
    if "owner" in rec:
        return None                        # inconclusive after max reads: gave up
    if str(rec.get("skip", "")).startswith("old"):
        return "hidden"
    if _renewed_since_recorded(stype, l, rec, now_ts):
        return "renewed"
    return None


def listing_age_days(l, clock, now_ts):
    created = l.get("created_ts")
    if isinstance(created, (int, float)) and created > 0:
        return max(0.0, (now_ts - created) / 86400.0)
    return clock.age_days(l.get("id"), now_ts) if clock is not None else None


def _url_fingerprint(url):
    return hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:12]


def _url_changed(seeded_flags, name, url):
    """True once, when a source's search URL differs from the one last run. The very
    first time (no fingerprint stored yet) it just records the fingerprint."""
    key = name + ":url"
    fp = _url_fingerprint(url)
    old = seeded_flags.get(key)
    seeded_flags[key] = fp
    return bool(old) and old != fp


def _is_retryable(rec):
    """Re-examine a recorded listing ONLY if an earlier check explicitly left it
    undecided (owner=None) and it still has tries left. A record WITHOUT an "owner"
    key comes from silent seeding or the price tracker: it is KNOWN and stays quiet.
    (2026-09-07-b treated such records as undecided, so old seeded listings were
    re-checked and announced as new - e.g. the old lalafo ads re-sent on 8 Sep.)"""
    return ("owner" in rec and rec.get("owner") is None
            and rec.get("tries", 0) < OWNER_MAX_TRIES)


def process_new_owner_checks(items, source, seen, seeded_flags, tries_map=None):
    """For a price-tracked source that should ALSO announce new OWNER posts (bina):
    owner-check listings not yet in `seen`. MUST run before process_source seeds them.

    Returns (notified, deferred_ids, decided). deferred_ids were NOT decided this run
    (over budget, or the page was unreadable) and MUST be kept out of process_source, or
    they get seeded as "known" and can never be announced again. `decided` maps id ->
    bool so process_source can STAMP the verdict on the record it seeds: a seeded record
    with NO `owner` key is exactly how 6438833 / 6437375 / 6437888 became invisible."""
    name = source["name"]
    owner_label = source.get("owner_label", "Mülkiyyətçi")
    flag = name + ":owner_seeded"
    changed = _url_changed(seeded_flags, name, source.get("url"))
    if not seeded_flags.get(flag) or changed:
        seeded_flags[flag] = True
        if changed:
            log(f"{name}: search URL changed - newly matching listings are recorded "
                "silently this run (no announcements)")
        return 0, set(), {}           # don't owner-check (and announce) a whole backlog
    # Retry counter for pages that were READ but showed no seller label. Without a
    # cap these were re-fetched every run forever ("deferred 371"), burning the
    # 100-check budget on the same pages and inviting rate limits.
    tries_map = tries_map if tries_map is not None else {}
    prefix = source["prefix"]
    now_ids = {str(l["id"]) for l in items}
    for k in list(tries_map):
        if k not in now_ids or (prefix + k) in seen:
            tries_map.pop(k, None)
    checks = notified = 0
    fail_streak = 0
    deferred = set()
    decided = {}
    for l in items:
        iid = str(l["id"])
        key = prefix + iid
        if key in seen:
            continue                  # already known (price-tracked or checked before)
        if checks >= MAX_OWNER_CHECKS or fail_streak >= OWNER_FETCH_FAIL_STREAK:
            deferred.add(iid)         # over budget / site refusing -> retry next run
            continue
        if source.get("type") == "bina":
            owner, photo = bina_is_owner(l["url"])
            reason = getattr(bina_is_owner, "last_reason", "")
        else:
            owner, photo = check_is_owner(l["url"], owner_label)
            reason = getattr(check_is_owner, "last_reason", "")
        checks += 1
        if PAGE_DELAY:
            time.sleep(PAGE_DELAY)
        if owner is None:
            if reason == "ratelimit":
                fail_streak = OWNER_FETCH_FAIL_STREAK     # stop checking; rest deferred
                deferred.add(iid)
                continue
            if reason == "fetch":
                fail_streak += 1
                deferred.add(iid)     # page not read: retry next run, no try used
                if fail_streak == OWNER_FETCH_FAIL_STREAK:
                    log(f"{name}: {fail_streak} detail pages in a row unreadable; "
                        "pausing owner checks until next run")
                continue
            fail_streak = 0
            n = tries_map.get(iid, 0) + 1
            if n >= OWNER_MAX_TRIES:
                tries_map.pop(iid, None)
                log(f"{name}: {l['url']} -> no seller label after {n} reads; "
                    "recorded without announcing")
                continue              # NOT deferred: process_source records it once
            tries_map[iid] = n
            deferred.add(iid)
            continue
        fail_streak = 0
        tries_map.pop(iid, None)
        decided[iid] = bool(owner)
        if not owner:
            log(f"{name}: {l['url']} -> AGENT (not announced)")
        if owner:
            age, akind, posted = _bina_age(l, time.time())
            if age is not None and age > NEW_MAX_AGE_DAYS:
                # old ad (promoted or not) that you were never sent: send, marked old
                l["_age_days"], l["_age_kind"], l["_posted_ts"] = age, akind, posted
            if photo:
                l["photo"] = photo    # keep API photo if detail gave none
            if notify_new_owner_msg(l, name, owner_label):
                notified += 1

    # Known listings you were NEVER sent (recorded silently: first run, URL change,
    # or no seller label) that are now VIP/Premium: the owner paid to lift an old ad.
    # Check each one once; owners are sent once, marked old. Capped per run.
    now_ts = time.time()
    old_used = 0
    for l in items:
        if (checks >= MAX_OWNER_CHECKS or fail_streak >= OWNER_FETCH_FAIL_STREAK
                or old_used >= OLD_MAX_PER_RUN):
            break
        if not l.get("_promoted"):
            continue
        rec = seen.get(prefix + str(l["id"]))
        if rec is None or "owner" in rec:
            continue                  # new (handled above), or already decided/sent
        old_used += 1
        if source.get("type") == "bina":
            owner, photo = bina_is_owner(l["url"])
            reason = getattr(bina_is_owner, "last_reason", "")
        else:
            owner, photo = check_is_owner(l["url"], owner_label)
            reason = getattr(check_is_owner, "last_reason", "")
        checks += 1
        if PAGE_DELAY:
            time.sleep(PAGE_DELAY)
        if owner is None:
            if reason == "ratelimit":
                break                 # site refusing: retry next run, no try used
            if reason == "fetch":
                fail_streak += 1      # page not read: retry next run, no try used
                continue
            fail_streak = 0
            rec["owner_tries"] = rec.get("owner_tries", 0) + 1
            if rec["owner_tries"] >= OWNER_MAX_TRIES:
                rec["owner"] = None   # gave up: never looked at again
            continue
        fail_streak = 0
        rec["owner"] = bool(owner)    # decided: never looked at again, never sent twice
        if not owner:
            continue
        age, akind, posted = _bina_age(l, now_ts)
        fs = _to_epoch(rec.get("first_seen"))
        if fs is not None and akind != "exact":
            seen_days = max(0.0, (now_ts - fs) / 86400.0)
            if age is None or seen_days > age:
                age, akind, posted = seen_days, "min", fs   # we saw it already then
        if age is not None and age > NEW_MAX_AGE_DAYS:
            l["_age_days"], l["_age_kind"], l["_posted_ts"] = age, akind, posted
        if photo:
            l["photo"] = photo
        log(f"{name}: {l['url']} -> VIP/Premium owner ad never sent before; sending once")
        if notify_new_owner_msg(l, name, owner_label):
            notified += 1
    if checks or deferred:
        log(f"{name}: owner-checked {checks} new, announced {notified}, "
            f"deferred {len(deferred)}")
    return notified, deferred, decided


def process_owner_new(items, source, seen, seeded_flags):
    prefix, name = source["prefix"], source["name"]
    owner_label = source.get("owner_label", "Əmlak sahibi")
    stype = source.get("type")
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    now_ts = time.time()

    # Seed silently on the first real run, or when the search URL was changed (a new
    # URL matches many listings the bot never saw - most of them old). Existing
    # records are never overwritten.
    has_recorded = bool(prefix) and any(k.startswith(prefix) for k in seen)
    first_run = not seeded_flags.get(name) or (bool(prefix) and not has_recorded)
    changed = _url_changed(seeded_flags, name, source.get("url"))
    if first_run or changed:
        n = 0
        for l in items:
            k = prefix + str(l["id"])
            if k not in seen:
                seen[k] = {"url": l["url"], "first_seen": now, "source": name}
                n += 1
        seeded_flags[name] = True
        why = "first run" if first_run else "search URL changed"
        log(f"{name}: {why} - recorded {n} current listings silently (no announcements)")
        return 0

    prefiltered = source.get("prefiltered_owner", False)  # URL already asks for owner posts
    seller_apts = _seller_apartments(seen) if stype == "tap" else {}
    tap_profile_cache = {}
    clock = IdClock.from_seen(seen, name, prefix)
    # yeniemlak: since 2026-11-07-b the bot reads ALL result pages (~74), not just the
    # first 30 (the 429 used to cut it off there). Listings dated before the bot's first
    # yeniemlak record were never on top while the bot ran and never came back to the
    # top since, so they are recorded silently - not sent as "old" ads. If one of them
    # is renewed later, its new date brings it back and it is sent once (marked old).
    ye_start = min(clock.ts) if (stype == "yeniemlak" and clock.ts) else None
    checks = notified = fail_streak = old_used = quiet_old = 0
    for l in items:
        key = prefix + str(l["id"])
        rec = seen.get(key)
        kind = _candidate_kind(stype, l, rec, now_ts)
        if kind is None:
            continue                       # sent, agent, given up, or quietly known
        first_seen = (rec or {}).get("first_seen", now)
        if kind == "new" and ye_start is not None:
            d = _dmy_epoch(l.get("updated_at"))
            if d is not None and d + 86400 <= ye_start:
                seen[key] = {"url": l["url"], "first_seen": now, "source": name}
                quiet_old += 1
                continue

        # An OLD ad you were never sent (renewed, İrəli çək, VIP/Premium, or missed
        # earlier) is sent once, marked old. Age only chooses the label. Old ads are
        # capped per run so brand-new ones are never delayed behind a backlog.
        age, age_kind = _age_estimate(l, clock, now_ts, rec)
        is_old = age is not None and age > NEW_MAX_AGE_DAYS
        if is_old or kind in ("hidden", "renewed"):
            if old_used >= OLD_MAX_PER_RUN:
                continue                   # not recorded: looked at again next run
            old_used += 1
        if is_old:
            l["_age_days"], l["_age_kind"] = age, age_kind
            l["_posted_ts"] = now_ts - age * 86400.0     # shown as a date in the message
        if kind != "new":
            log(f"{name}: {l['url']} -> looking again ({kind}"
                + (f", ~{age:.0f} days old" if age is not None else "") + ")")

        if prefiltered:
            # "prefiltered" only means the SEARCH URL asked for owner posts. lalafo still
            # leaks agent posts and out-of-area content, so check before announcing.
            if stype == "lalafo":
                why = None
                if not lalafo_ok(l):
                    why = "location"
                elif l.get("_agent"):
                    why = "agent"
                else:
                    sc, rs = owner_signal_score(l.get("_text") or "", "", None)
                    if sc <= SCORE_AGENT_AT:
                        why = "agent-text " + ",".join(rs)
                if why:
                    log(f"lalafo.az: {l['url']} -> skipped ({why})")
                    seen[key] = {"url": l["url"], "first_seen": first_seen,
                                 "source": name, "owner": False, "skip": why}
                    continue
            seen[key] = {"url": l["url"], "first_seen": first_seen, "source": name,
                         "owner": True}
            if notify_new_owner_msg(l, name, owner_label):
                notified += 1
            continue

        if checks >= MAX_OWNER_CHECKS:
            break                          # spread detail-page load; rest handled next run
        if fail_streak >= OWNER_FETCH_FAIL_STREAK:
            log(f"{name}: {fail_streak} detail pages in a row unreadable; "
                "pausing owner checks until next run")
            break
        if stype == "tap":
            owner, photo, seller = tap_check_owner(l["url"], seller_apts,
                                                   tap_profile_cache, listing=l)
            reason = getattr(tap_check_owner, "last_reason", "")
        else:
            owner, photo = check_is_owner(l["url"], owner_label)
            seller = None
            reason = getattr(check_is_owner, "last_reason", "")
        checks += 1
        if PAGE_DELAY:
            time.sleep(PAGE_DELAY)
        if owner is None:
            if reason == "ratelimit":
                checks -= 1                # refused, not checked (keeps the log honest)
                log(f"{name}: site is rate-limiting us (HTTP 429); the rest is checked "
                    "next run (no try used, nothing recorded)")
                break
            if reason == "fetch":
                fail_streak += 1           # page not read: retry next run, no try used
                continue
            fail_streak = 0
            tries = (rec or {}).get("tries", 0) + 1
            seen[key] = {"url": l["url"], "first_seen": first_seen, "source": name,
                         "owner": None, "tries": tries}
            if tries >= OWNER_MAX_TRIES:
                log(f"{name}: {l['url']} undetermined after {tries} reads; giving up")
            continue
        fail_streak = 0
        if photo:
            l["photo"] = photo
        new_rec = {"url": l["url"], "first_seen": first_seen, "source": name,
                   "owner": bool(owner)}
        if seller:
            new_rec["seller"] = seller
            if stype == "tap":
                new_rec["rooms"] = l.get("rooms")
                new_rec["area"] = l.get("area")
                seller_apts.setdefault(seller, set()).add(_apt_key(l))
        seen[key] = new_rec
        if owner and notify_new_owner_msg(l, name, owner_label):
            notified += 1
    if quiet_old:
        log(f"{name}: recorded {quiet_old} old listing(s) silently (dated before the bot "
            f"first read {name}; never came back to the top)")
    log(f"{name}: checked {checks} new, announced {notified} owner posts")
    return notified


def notify_new_owner_msg(l, name, owner_label="Əmlak sahibi"):
    text = format_new_owner(l, name, owner_label)
    if SEND_PHOTOS and l.get("photo"):
        return tg_send_photo(l["photo"], text)
    return tg_send_message(text)


def normalize_price(v):
    """200000 / '200,000' / '200 000 AZN' -> 200000 (int) ; unparseable -> None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v) if v > 0 else None
    if isinstance(v, str):
        d = re.sub(r"[^\d]", "", v)
        return int(d) if d else None
    return None


def is_price_glitch(old, new):
    """Reject absurd jumps that are almost certainly parse errors, not real changes."""
    if new <= 0:
        return True
    if new < old * PRICE_GLITCH_LOW or new > old * PRICE_GLITCH_HIGH:
        return True
    return False


# --------------------------------------------------------------------------- #
# Per-source processing  —  PRICE-CHANGE TRACKER
#
# Model: every listing carries a permanent price_history. On each run we compare
# the current price to the last recorded price and, on a real change, APPEND a
# {price, date, change} event (never overwrite/lose past events). New listings are
# recorded silently (seed initial price); we alert only on price changes, in BOTH
# directions. Old age is irrelevant — any listing in the fetched set is compared.
# --------------------------------------------------------------------------- #
def _last_price(rec):
    ph = rec.get("price_history")
    if isinstance(ph, list) and ph:
        p = ph[-1].get("price")
        if isinstance(p, (int, float)):
            return int(p)
    p = rec.get("price")
    return int(p) if isinstance(p, (int, float)) else None


def _ensure_history(rec, now):
    """Backward-compat migration: seed price_history from an old flat 'price'."""
    if not isinstance(rec.get("price_history"), list):
        rec["price_history"] = []
    if not rec["price_history"] and isinstance(rec.get("price"), (int, float)):
        rec["price_history"].append({"price": int(rec["price"]),
                                     "date": rec.get("first_seen", now)})


def process_source(items, source, seen, skip_ids=None, owner_map=None):
    prefix, name = source["prefix"], source["name"]
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    events = []
    for l in items:
        key = prefix + str(l["id"])
        if skip_ids and str(l["id"]) in skip_ids and key not in seen:
            continue          # not owner-checked yet -> do NOT seed, or it is lost forever
        cur = normalize_price(l.get("price"))
        rec = seen.get(key)

        if rec is None:
            # First time we ever see this listing -> record it silently with its
            # initial price. We do NOT announce new listings (this is a price tracker).
            _rec = {"url": l["url"], "price": cur, "first_seen": now, "last_seen": now,
                    "source": name,
                    "price_history": ([{"price": cur, "date": now}] if cur is not None else [])}
            _v = (owner_map or {}).get(str(l["id"]))
            if _v is not None:
                _rec["owner"] = _v     # stamp the owner verdict so it is never "unknown"
            seen[key] = _rec
            continue

        _ensure_history(rec, now)
        rec["last_seen"] = now
        rec["url"] = l.get("url", rec.get("url"))
        old = _last_price(rec)

        if cur is None:
            continue                       # price missing/garbled this run -> ignore, no fake event
        if old is None:
            rec["price"] = cur             # establish a baseline for a record that had none
            if not rec["price_history"]:
                rec["price_history"].append({"price": cur, "date": now})
            continue
        if cur == old:
            continue                       # unchanged -> no event
        if is_price_glitch(old, cur):
            log("ignoring glitchy price:", key, old, "->", cur)
            continue

        change = cur - old
        # RECORD the transition permanently FIRST (source of truth), then alert.
        rec["price_history"].append({"price": cur, "date": now, "change": change})
        if MAX_PRICE_HISTORY and len(rec["price_history"]) > MAX_PRICE_HISTORY:
            rec["price_history"] = rec["price_history"][-MAX_PRICE_HISTORY:]
        rec["price"] = cur                 # latest/current price
        kind = "increase" if change > 0 else "drop"
        events.append((l, kind, old, cur, len(rec["price_history"])))
        log(f"price change {key}: {old} -> {cur} ({'+' if change>0 else ''}{change})")

    notified = 0
    for l, kind, old, cur, n in events:
        if notify_change(l, name, kind, old, cur, n):
            notified += 1
        # note: the transition is already saved in price_history regardless of send
    return notified


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    t0 = time.time()
    log(f"monitor.py {VERSION} starting "
        f"{dt.datetime.now(dt.timezone.utc).isoformat(timespec='seconds')}")
    if not BOT_TOKEN or not CHAT_ID:
        log("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set.")
        sys.exit(1)

    try:
        state, sha = load_state()
    except Exception as e:
        log("Could not load state; skipping to avoid duplicates:", e)
        if IN_ACTIONS:
            tg_send_message(f"⚠️ Could not READ memory this run ({html.escape(str(e))}). Skipping.")
        return
    seen = state["listings"]
    loaded_count = len(seen)          # invariant: we must never save fewer than this
    _RUNTIME["state"] = state         # small caches (tap profile counts) live in state

    if IN_ACTIONS and not USE_API:
        tg_send_message("⚠️ Memory storage not configured (GH_TOKEN/GH_REPO missing); listings "
                        "will repeat. Check the workflow env block.")

    total_notified = 0
    status = state.setdefault("source_status", {})   # source name -> last error ("" = healthy)
    seeded_flags = state.setdefault("seeded", {})     # owner_new sources -> initial seed done
    for source in SOURCES:
        name = source["name"]
        try:
            items = fetch_for_source(source)
        except PersistedQueryError:
            err = ("signature expired (persisted query). Re-capture it from the browser "
                   "Network tab and update BINA_PERSISTED_HASH.")
            if status.get(name) != "PQ":
                tg_send_message(f"⚠️ <b>{html.escape(name)}</b> stopped working: {err}\n"
                                "Other sources keep running.")
                status[name] = "PQ"
            continue
        except Exception as e:
            err = str(e)[:200]
            if status.get(name) != err:   # alert only when the error is new/changed (no spam)
                tg_send_message(f"⚠️ <b>{html.escape(name)}</b> could not be read this run.\n"
                                f"Reason: {html.escape(err)}\n"
                                "Other sources keep running; I'll tell you when it recovers.")
                status[name] = err
            continue
        # success — announce recovery if it was previously broken
        if status.get(name):
            tg_send_message(f"✅ <b>{html.escape(name)}</b> is working again.")
        status[name] = ""
        if source.get("mode") == "owner_new":
            total_notified += process_owner_new(items, source, seen, seeded_flags)
        elif source.get("mode") == "price_owner":
            # owner check must run BEFORE process_source seeds the new listings
            n_new, deferred, decided = process_new_owner_checks(
                items, source, seen, seeded_flags,
                tries_map=state.setdefault("owner_tries", {}))
            total_notified += n_new
            total_notified += process_source(items, source, seen, skip_ids=deferred,
                                             owner_map=decided)
        else:
            total_notified += process_source(items, source, seen)

    # HARD SAFETY INVARIANT: a run must never shrink the historical dataset.
    # We only ever add to `seen`, so this can only trip on a bug/partial read.
    if len(seen) < loaded_count:
        log(f"ABORT SAVE: would shrink history {loaded_count} -> {len(seen)}")
        if IN_ACTIONS:
            tg_send_message(f"🛑 Save aborted to protect history: had {loaded_count} records, "
                            f"about to write {len(seen)}. Nothing was overwritten.")
        return

    ok, reason = save_state(state, sha)
    if not ok:
        tg_send_message("⚠️ Read listings fine, but could NOT save memory — listings will "
                        f"repeat until fixed.\nReason: {html.escape(reason)}")
    log(f"Done. notified={total_notified} saved={ok}({reason}) "
        f"records_before={loaded_count} records_after={len(seen)} "
        f"elapsed={int(time.time() - t0)}s "
        f"status={ {k: (v or 'ok') for k, v in status.items()} }")


if __name__ == "__main__":
    main()
