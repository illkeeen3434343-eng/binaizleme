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
from urllib.parse import parse_qs, urlparse

import requests

# --------------------------------------------------------------------------- #
# YOUR SEARCHES  (paste the normal search URL from each site's address bar)
# --------------------------------------------------------------------------- #
BINA_SEARCH_URL = os.environ.get("BINA_SEARCH_URL", (
    "https://bina.az/baki/alqi-satqi/menziller?has_bill_of_sale=true&has_repair=true&location_ids%5B%5D=51&location_ids%5B%5D=100&location_ids%5B%5D=16&location_ids%5B%5D=11&location_ids%5B%5D=74&location_ids%5B%5D=52&location_ids%5B%5D=53&location_ids%5B%5D=54&location_ids%5B%5D=33&location_ids%5B%5D=99&location_ids%5B%5D=200"
))

# This bot tracks bina.az ONLY.
SOURCES = [
    {"name": "bina.az", "type": "bina", "url": BINA_SEARCH_URL, "prefix": ""},
]

# bina.az config
CITY_ID = os.environ.get("BINA_CITY_ID", "1")
CATEGORY_ID = os.environ.get("BINA_CATEGORY_ID", "1")
PERSISTED_HASH = os.environ.get("BINA_PERSISTED_HASH",
    "b781511a943a4d710eefdf811a24dd4ae353e55d836952603ce0b37fde97d073")
GRAPHQL_URL = "https://bina.az/graphql"
OPERATION = "SearchItems"
SORT = "BUMPED_AT_DESC"
PAGE_SIZE = 16
# Price tracking needs the FULL result set each run (not just the newest listings),
# so an old listing's price change is still fetched and compared. SCAN_PAGES caps how
# many pages we page through — a safety valve against hammering bina / getting blocked.
# 120 pages * 16 = ~1920 listings. If your search has more matches than this, either
# raise it (block risk) or narrow the search so the whole set fits.
SCAN_PAGES = int(os.environ.get("SCAN_PAGES", "120"))
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


def tg_send_photo(photo_url, caption):
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
                          json={"chat_id": CHAT_ID, "photo": photo_url, "caption": caption,
                                "parse_mode": "HTML"}, timeout=30)
        if r.status_code == 200:
            return True
        return tg_send_message(caption)
    except requests.RequestException:
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
    if c["rooms"] and l.get("rooms") not in c["rooms"]:
        return False
    if c["price_to"] is not None and (l.get("price") is None or l["price"] > c["price_to"]):
        return False
    if c["area_from"] is not None and (l.get("area") is None or l["area"] < c["area_from"]):
        return False
    if c["locs"]:
        if l.get("location_id") is None or l["location_id"] not in c["locs"]:
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
    if IN_ACTIONS:
        tg_send_message(
            f"ℹ️ Price tracker is covering <b>{scanned}</b> of ~<b>{total}</b> matching "
            f"listings (scan cap reached). Price changes on the deeper ~{total - scanned} "
            f"are not tracked. To cover everything, narrow the search (add a price cap or "
            f"rooms) or raise SCAN_PAGES.")

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
    head = "🔻 <b>PRICE DROP</b>" if kind == "drop" else "🔺 <b>PRICE INCREASE</b>"
    lines = [f"{head} · {html.escape(source_name)}", ""]
    cur = l.get("currency", "AZN")
    old_txt = f"<s>{_spaced(old_price)}</s>" if kind == "drop" else _spaced(old_price)
    lines.append(f"💰 <b>Price:</b> {old_txt} → <b>{_spaced(new_price)}</b> {cur}")
    if l.get("rooms") is not None:
        lines.append(f"🛏 <b>Rooms:</b> {l['rooms']}")
    if l.get("area") is not None:
        area = int(l["area"]) if float(l["area"]).is_integer() else l["area"]
        lines.append(f"📐 <b>Area:</b> {area} {l.get('area_units', 'm²')}")
    if l.get("floor") and l.get("floors"):
        lines.append(f"🏢 <b>Floor:</b> {l['floor']}/{l['floors']}")
    if l.get("location"):
        lines.append(f"📍 <b>Location:</b> {html.escape(str(l['location']))}")
    lines.append("")
    lines.append(f'🔗 <a href="{html.escape(l["url"])}">Open listing</a>')
    return "\n".join(lines)


def notify_change(l, source_name, kind, old_price, new_price, n_changes):
    text = format_change(l, source_name, kind, old_price, new_price, n_changes)
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


def process_source(items, source, seen):
    prefix, name = source["prefix"], source["name"]
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    events = []
    for l in items:
        key = prefix + str(l["id"])
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
    for source in SOURCES:
        name = source["name"]
        try:
            items = fetch_bina(source["url"])
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
