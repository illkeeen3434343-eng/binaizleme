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
import datetime as dt
import html
import json
import os
import re
import sys
import time
from urllib.parse import parse_qs, unquote, urlencode, urlparse, urlunparse

import requests

VERSION = "2026-09-04-a"   # bump this when you deploy; printed at start of every run

try:
    from curl_cffi import requests as cf_requests   # Chrome-TLS client to beat bot 403s
    HAS_CURL_CFFI = True
except Exception:
    HAS_CURL_CFFI = False


def http_get(url, headers=None, params=None, timeout=30, browser=False):
    """GET a URL. For bot-protected HTML sites (browser=True) use curl_cffi with a
    real Chrome TLS fingerprint; otherwise plain requests."""
    if browser and HAS_CURL_CFFI:
        return cf_requests.get(url, headers=headers, params=params, timeout=timeout,
                               impersonate="chrome")
    return requests.get(url, headers=headers, params=params, timeout=timeout)

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

# Sources this bot tracks. Each listing's price history is tracked independently.
SOURCES = [
    {"name": "bina.az", "type": "bina", "url": BINA_SEARCH_URL, "prefix": "", "mode": "price_owner", "owner_label": "Mülkiyyətçi"},
    {"name": "yeniemlak.az", "type": "yeniemlak", "url": YENIEMLAK_SEARCH_URL, "prefix": "ye:", "mode": "owner_new", "owner_label": "Əmlak sahibi"},
    {"name": "tap.az", "type": "tap", "url": TAP_SEARCH_URL, "prefix": "tap:", "mode": "owner_new", "owner_label": "Sahibindən"},
    {"name": "lalafo.az", "type": "lalafo", "url": LALAFO_SEARCH_URL, "prefix": "lala:", "mode": "owner_new", "owner_label": "Mülkiyyətçi", "prefiltered_owner": True},
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
    f["floorFirst"] = b(one("floor_first")) is True
    f["floorLast"] = b(one("floor_last")) is True
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


def ye_passes(l):
    """True if the parsed address matches one of the wanted areas."""
    if not YE_KEYWORDS:
        return True
    loc = l.get("location") or ""
    return any(k in loc for k in YE_KEYWORDS)


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
        pm = re.search(r"(?:Satılır|Kirayə|Girov)\s*(\d[\d ]*?)\s*(?:Kreditlə\s*)?Baxış", b)
        price = int(pm.group(1).replace(" ", "")) if pm else None
        fl = re.search(r"(\d+)\s*/\s*(\d+)\s*Mərtəbə", b)
        rooms = re.search(r"(\d+)\s*otaq", b)
        area = re.search(r"(\d+)\s*m2", b)
        date = re.search(r"Tarix:\s*([\d.]+)", b)
        loc = None
        lm = re.search(r"Ünvan:\s*(.+)", b)
        if lm:
            raw_loc = lm.group(1).strip()
            mm = re.search(r"(.*?metro\.\s*\S+(?:\s\S+)?)", raw_loc)
            loc = (mm.group(1) if mm else raw_loc[:70]).strip()
        out.append({"id": iid, "url": id_url.get(iid, f"https://yeniemlak.az/elan/{iid}"),
                    "rooms": int(rooms.group(1)) if rooms else None,
                    "area": int(area.group(1)) if area else None, "area_units": "m²",
                    "floor": int(fl.group(2)) if fl else None,
                    "floors": int(fl.group(1)) if fl else None,
                    "price": price, "currency": "AZN", "location": loc, "location_id": None,
                    "updated_at": date.group(1) if date else None,
                    "has_bill_of_sale": None, "has_mortgage": None, "has_repair": None,
                    "photo": None})
    return out, total


def fetch_yeniemlak(url):
    """Page through the whole yeniemlak result set. Stops when a page adds no new
    listing IDs (also the graceful fallback if ?page is ever ignored). Listings the
    site leaked outside the wanted districts are dropped by ye_passes()."""
    all_out, seen_ids = [], set()
    total = None
    parsed = 0
    for page in range(1, YE_MAX_PAGES + 1):
        purl = _with_page(url, page)
        r = http_get(purl, headers=HTML_HEADERS, timeout=30, browser=True)
        if r.status_code != 200:
            if page == 1:
                raise RuntimeError(f"HTTP {r.status_code}")
            break
        listings, tot = _parse_yeniemlak_page(r.text)
        if tot is not None:
            total = tot
        new = [l for l in listings if l["id"] not in seen_ids]
        if not new:                       # no new IDs -> end of results (or page ignored)
            break
        for l in new:
            seen_ids.add(l["id"])         # always mark, so pagination still terminates
            parsed += 1
            if ye_passes(l):
                all_out.append(l)
        if PAGE_DELAY:
            time.sleep(PAGE_DELAY)
    log(f"yeniemlak: kept {len(all_out)} of {parsed} parsed"
        + (f" (~{total} on site)" if total is not None else ""))
    # Alarm on a PARSE failure, never on a legitimately empty filtered result.
    if not parsed and total and total > 0:
        raise RuntimeError("yeniemlak parsed 0 (structure changed?)")
    return all_out


def fetch_for_source(source):
    if source["type"] == "bina":
        return fetch_bina(source["url"])
    if source["type"] == "yeniemlak":
        return fetch_yeniemlak(source["url"])
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


def _parse_lalafo_page(raw):
    nd = _lalafo_next_data(raw)
    if nd is None:
        return [], False
    items, links, meta = _lalafo_feed(nd)
    out = []
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
                    "photo": _lalafo_photo(it)})
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
# Only track tap.az posts whose heading contains one of these location keywords.
TAP_KEYWORDS = ["Q.Qarayev", "Əhmədli", "Xətai r", "Nizami r", "8-ci km",
                "Neftçilər", "Xalqlar", "Həzi", "Köhnə Günəşli"]
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
        if TAP_KEYWORDS and not any(kw in heading for kw in TAP_KEYWORDS):
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

TAP_REALTOR_PHRASES = [p.strip().lower() for p in os.environ.get(
    "TAP_REALTOR_PHRASES",
    "xidmət haqqı,xidmet haqqi,komissiya,komisyon,komissyon,rieltor,rialtor,realtor,"
    "makler,əmlak agentliyi,emlak agentliyi,agentliyi,agentlik,ofis haqqı,ofis haqqi,"
    "ofis xidmət,ofis xidmet,ekskluziv,eksklüziv,bazamızda,bazamizda,müştərilərimiz,"
    "musterilerimiz,açarlar bizdə,acarlar bizde,açar bizdə,portfelimiz,ətraflı məlumat və digər əmlaklar,diger emlaklar"
).split(",") if p.strip()]

TAP_OWNER_PHRASES = [p.strip().lower() for p in os.environ.get(
    "TAP_OWNER_PHRASES",
    "vasitəçi narahat,vasiteci narahat,vasitəçi yoxdur,vasitəçisiz,vasitecisiz,"
    "vasitəçi olmadan,sahibindən,sahibinden,mülkiyyətçidən,mulkiyyetciden,birbaşa sahibi"
).split(",") if p.strip()]


def tap_check_owner(url, seller_counts=None):
    """Open one tap.az ad and decide owner vs. realtor.
    Returns (is_owner, photo, seller_id); is_owner None = undecidable, retry later."""
    try:
        r, raw = _fetch_tap_html(url, tries=3)
    except Exception as e:
        log("tap detail fetch error:", e)
        return None, None, None
    if getattr(r, "status_code", 0) != 200 or _tap_blocked(raw) or len(raw) < 2000:
        return None, None, None            # blocked/short -> undecidable, do NOT record

    seller = None
    m = re.search(r"/elanlar\?user_id=(\d+)", raw)
    if m:
        seller = m.group(1)

    photo = None
    pm = re.search(r'(https?://tap\.azstatic\.com/uploads/[^\s"\'<>]+\.(?:jpg|jpeg|png|webp))', raw)
    if pm:
        photo = pm.group(1)

    # 1) hard dealer: the ad belongs to a paid store account
    if re.search(r'href="[^"]*/shops/', raw):
        return False, photo, seller

    text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw))).lower()

    # 2) an explicit "no agents" / "from the owner" line wins over the wording check
    if any(p in text for p in TAP_OWNER_PHRASES):
        return True, photo, seller

    # 3) agency wording anywhere in the ad text
    hit = next((p for p in TAP_REALTOR_PHRASES if p in text), None)
    if hit:
        log(f"tap.az: {url} -> realtor (phrase: {hit})")
        return False, photo, seller

    # 4) the same account has already posted several ads we recorded
    if seller and seller_counts and seller_counts.get(seller, 0) >= TAP_MAX_USER_ADS:
        log(f"tap.az: {url} -> realtor (user {seller} has "
            f"{seller_counts[seller]} recorded ads)")
        return False, photo, seller

    return True, photo, seller


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
            with open(STATE_FILE, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=1)
            return True, "local"
        except Exception as e:
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
def check_is_owner(url, owner_label="Əmlak sahibi"):
    """Returns (is_owner, photo_url). is_owner: True=owner, False=agent, None=unknown.
    Photo is the listing's first image, pulled from the same detail page (no extra request)."""
    try:
        r = http_get(url, headers=HTML_HEADERS, timeout=30, browser=True)
        if r.status_code != 200:
            return None, None
        raw = r.text
        text = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw)))
    except Exception:
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
    if owner_label in text:
        return True, photo
    if "Vasitəçi" in text or "Rieltor" in text or "Vasitəç:" in text:
        return False, photo
    return False, photo   # no explicit owner label -> treat as not-owner (conservative)


def format_new_owner(l, source_name, owner_label="Əmlak sahibi"):
    lines = [f"‼️ <b>Yeni elan</b> · {html.escape(source_name)} · <i>{html.escape(owner_label)}</i>", ""]
    if l.get("price") is not None:
        lines.append(f"💰 <b>Qiymət:</b> {_spaced(l['price'])} {l.get('currency', 'AZN')}")
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


def _seller_counts(seen):
    """How many ads each seller account already has in our history."""
    counts = {}
    for v in seen.values():
        sid = v.get("seller")
        if sid:
            counts[sid] = counts.get(sid, 0) + 1
    return counts


def process_new_owner_checks(items, source, seen, seeded_flags):
    """For a price-tracked source that should ALSO announce new OWNER posts (bina):
    owner-check listings not yet in `seen`. MUST run before process_source seeds them.

    Returns (notified, deferred_ids). Anything in deferred_ids was NOT decided this
    run — over the per-run check budget, or the detail page was blocked. The caller
    MUST keep those out of process_source, otherwise they get seeded as "known" and
    can never be announced again."""
    name = source["name"]
    owner_label = source.get("owner_label", "Mülkiyyətçi")
    flag = name + ":owner_seeded"
    if not seeded_flags.get(flag):
        seeded_flags[flag] = True
        return 0, set()               # first run: don't owner-check the existing backlog
    checks = notified = 0
    deferred = set()
    for l in items:
        key = source["prefix"] + str(l["id"])
        if key in seen:
            continue                  # already known (price-tracked or checked before)
        if checks >= MAX_OWNER_CHECKS:
            deferred.add(str(l["id"]))    # over budget -> retry next run
            continue
        owner, photo = check_is_owner(l["url"], owner_label)
        checks += 1
        if PAGE_DELAY:
            time.sleep(PAGE_DELAY)
        if owner is None:
            deferred.add(str(l["id"]))    # blocked/timeout -> retry next run
            continue
        if owner:
            if photo:
                l["photo"] = photo    # keep API photo if detail gave none
            if notify_new_owner_msg(l, name, owner_label):
                notified += 1
    if checks or deferred:
        log(f"{name}: owner-checked {checks} new, announced {notified}, "
            f"deferred {len(deferred)}")
    return notified, deferred


def process_owner_new(items, source, seen, seeded_flags):
    prefix, name = source["prefix"], source["name"]
    owner_label = source.get("owner_label", "Əmlak sahibi")
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    # Seed silently if this source was never seeded OR has no recorded listings yet
    # (the second check stops a flood if a prior empty run set the flag with 0 ids).
    has_recorded = bool(prefix) and any(k.startswith(prefix) for k in seen)
    if not seeded_flags.get(name) or (bool(prefix) and not has_recorded):
        # First real run for this source: seed ALL current listings silently (no detail
        # fetches, no announcements) so owner-checking only runs on future new posts.
        for l in items:
            seen[prefix + str(l["id"])] = {"url": l["url"], "first_seen": now, "source": name}
        seeded_flags[name] = True
        log(f"{name}: seeded {len(items)} listings silently (owner-check starts next run)")
        return 0

    prefiltered = source.get("prefiltered_owner", False)  # URL already returns only owner posts
    seller_counts = _seller_counts(seen)
    checks = 0
    notified = 0
    for l in items:
        key = prefix + str(l["id"])
        if key in seen:
            continue                       # already handled; never re-check or price-track
        if prefiltered:
            # every listing is already an owner post (e.g. lalafo /owner/ URL) -> announce, no fetch
            seen[key] = {"url": l["url"], "first_seen": now, "source": name, "owner": True}
            if notify_new_owner_msg(l, name, owner_label):
                notified += 1
            continue
        if checks >= MAX_OWNER_CHECKS:
            break                          # spread detail-page load; rest handled next run
        if source.get("type") == "tap":
            owner, photo, seller = tap_check_owner(l["url"], seller_counts)
        else:
            owner, photo = check_is_owner(l["url"], owner_label)
            seller = None
        checks += 1
        if PAGE_DELAY:
            time.sleep(PAGE_DELAY)
        if owner is None:
            continue                       # couldn't determine -> leave unrecorded, retry later
        if photo:
            l["photo"] = photo
        rec = {"url": l["url"], "first_seen": now, "source": name, "owner": bool(owner)}
        if seller:
            rec["seller"] = seller
            seller_counts[seller] = seller_counts.get(seller, 0) + 1
        seen[key] = rec
        if owner and notify_new_owner_msg(l, name, owner_label):
            notified += 1
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


def process_source(items, source, seen, skip_ids=None):
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
            seen[key] = {"url": l["url"], "price": cur, "first_seen": now, "last_seen": now,
                         "source": name,
                         "price_history": ([{"price": cur, "date": now}] if cur is not None else [])}
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
    log(f"monitor.py {VERSION} starting")
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
            n_new, deferred = process_new_owner_checks(items, source, seen, seeded_flags)
            total_notified += n_new
            total_notified += process_source(items, source, seen, skip_ids=deferred)
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
        f"status={ {k: (v or 'ok') for k, v in status.items()} }")


if __name__ == "__main__":
    main()
