#!/usr/bin/env python3
"""
Bina.az change monitor — keeps ALL historical data forever.
"""
import datetime as dt
import html
import json
import os
import sys
import time
from urllib.parse import parse_qs, urlparse

import requests

BINA_SEARCH_URL = os.environ.get("BINA_SEARCH_URL", (
    "https://bina.az/baki/alqi-satqi/menziller?has_bill_of_sale=true&has_repair=true&location_ids%5B%5D=51&location_ids%5B%5D=100&location_ids%5B%5D=16&location_ids%5B%5D=11&location_ids%5B%5D=74&location_ids%5B%5D=52&location_ids%5B%5D=53&location_ids%5B%5D=54&location_ids%5B%5D=33&location_ids%5B%5D=99&location_ids%5B%5D=200"
))
CITY_ID = os.environ.get("BINA_CITY_ID", "1")
CATEGORY_ID = os.environ.get("BINA_CATEGORY_ID", "1")
PERSISTED_HASH = os.environ.get("BINA_PERSISTED_HASH",
    "b781511a943a4d710eefdf811a24dd4ae353e55d836952603ce0b37fde97d073")

GRAPHQL_URL = "https://bina.az/graphql"
SORT = "BUMPED_AT_DESC"
PAGE_SIZE = 16
SCAN_PAGES = int(os.environ.get("SCAN_PAGES", "8"))
STATE_FILE = os.environ.get("STATE_FILE", "seen.json")
SEND_PHOTOS = os.environ.get("SEND_PHOTOS", "true").lower() == "true"
DROP_ANOMALY_FLOOR = float(os.environ.get("DROP_ANOMALY_FLOOR", "0.4"))
RISE_ANOMALY_CEIL = float(os.environ.get("RISE_ANOMALY_CEIL", "3.0"))
NOTIFY_INCREASES = os.environ.get("NOTIFY_INCREASES", "true").lower() == "true"
NOTIFY_UPDATES = os.environ.get("NOTIFY_UPDATES", "false").lower() == "true"
NOTIFY_NEW = os.environ.get("NOTIFY_NEW", "false").lower() == "true"

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36")
API_HEADERS = {
    "User-Agent": UA,
    "Accept": "*/*",
    "Accept-Language": "az,en-US;q=0.9,en;q=0.8,ru;q=0.7",
    "Content-Type": "application/json",
    "Referer": "https://bina.az/baki/alqi-satqi/menziller",
    "Origin": "https://bina.az",
    "x-platform": "desktop"
}


def log(*a):
    print(*a, flush=True)


class PersistedQueryError(Exception):
    pass


def tg_send_message(text):
    if not BOT_TOKEN or not CHAT_ID:
        log("Telegram notification skipped: TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID missing")
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False},
            timeout=30
        )
        return r.status_code == 200
    except Exception as e:
        log("Telegram error:", e)
        return False


def tg_send_photo(photo_url, caption):
    if not BOT_TOKEN or not CHAT_ID:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            json={"chat_id": CHAT_ID, "photo": photo_url, "caption": caption, "parse_mode": "HTML"},
            timeout=30
        )
        if r.status_code == 200:
            return True
        return tg_send_message(caption)
    except Exception:
        return tg_send_message(caption)


def _filter_vars(url):
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
        except Exception:
            return None

    f = {"cityId": CITY_ID, "categoryId": CATEGORY_ID, "leased": False}
    rooms = [str(v) for v in q.get("room_ids[]", []) if str(v).strip()]
    if rooms:
        f["roomIds"] = rooms
    locs = [str(v) for v in q.get("location_ids[]", []) if str(v).strip()]
    if locs:
        f["locationIds"] = locs
    for src, dst in (("price_to", "priceTo"), ("price_from", "priceFrom"),
                     ("area_from", "areaFrom"), ("area_to", "areaTo")):
        if num(one(src)) is not None:
            f[dst] = num(one(src))
    if b(one("has_bill_of_sale")) is not None:
        f["hasBillOfSale"] = b(one("has_bill_of_sale"))
    if b(one("has_mortgage")) is not None:
        f["hasMortgage"] = b(one("has_mortgage"))
    f["floorFirst"] = b(one("floor_first")) is True
    f["floorLast"] = b(one("floor_last")) is True
    return f


def _check(url):
    q = parse_qs(urlparse(url).query)

    def i(v):
        try:
            return int(v)
        except Exception:
            return None

    pt = q.get("price_to", [None])[0]
    af = q.get("area_from", [None])[0]
    return {
        "rooms": {i(v) for v in q.get("room_ids[]", []) if i(v) is not None},
        "locs": {i(v) for v in q.get("location_ids[]", []) if i(v) is not None},
        "price_to": i(pt),
        "area_from": float(af) if af else None
    }


def _passes(l, c):
    if c["rooms"] and l.get("rooms") not in c["rooms"]:
        return False
    if c["price_to"] is not None and (l.get("price") is None or l["price"] > c["price_to"]):
        return False
    if c["area_from"] is not None and (l.get("area") is None or l["area"] < c["area_from"]):
        return False
    if c["locs"] and (l.get("location_id") is None or l["location_id"] not in c["locs"]):
        return False
    return True


def _node(node):
    def sub(k, fld):
        o = node.get(k)
        return o.get(fld) if isinstance(o, dict) else None

    preview = node.get("preview") or {}
    photo = preview.get("f460x345") or preview.get("thumbnail")
    area = sub("area", "value")
    try:
        area = float(area) if area is not None else None
    except Exception:
        area = None
    price = sub("price", "total")
    try:
        price = int(float(price)) if price is not None else None
    except Exception:
        price = None
    loc_id = sub("location", "id")
    try:
        loc_id = int(loc_id) if loc_id is not None else None
    except Exception:
        loc_id = None
    path = node.get("path")
    iid = str(node["id"])
    return {
        "id": iid,
        "rooms": node.get("rooms"),
        "area": area,
        "area_units": sub("area", "units") or "m²",
        "floor": node.get("floor"),
        "floors": node.get("floors"),
        "price": price,
        "currency": sub("price", "currency") or "AZN",
        "location_id": loc_id,
        "location": sub("location", "fullName") or sub("location", "name") or sub("city", "name"),
        "has_bill_of_sale": node.get("hasBillOfSale"),
        "has_mortgage": node.get("hasMortgage"),
        "has_repair": node.get("hasRepair"),
        "updated_at": node.get("updatedAt"),
        "url": f"https://bina.az{path}" if path else f"https://bina.az/items/{iid}",
        "photo": photo
    }


def fetch_listings():
    fv = _filter_vars(BINA_SEARCH_URL)
    chk = _check(BINA_SEARCH_URL)
    out, cursor = [], None
    for _ in range(SCAN_PAGES):
        variables = {"first": PAGE_SIZE, "filter": fv, "sort": SORT}
        if cursor:
            variables["cursor"] = cursor
        params = {
            "operationName": "SearchItems",
            "variables": json.dumps(variables, separators=(",", ":"), ensure_ascii=False),
            "extensions": json.dumps(
                {"persistedQuery": {"version": 1, "sha256Hash": PERSISTED_HASH}},
                separators=(",", ":")
            )
        }
        r = requests.get(GRAPHQL_URL, params=params, headers=API_HEADERS, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        payload = r.json()
        if payload.get("errors"):
            msg = "; ".join(str(e.get("message", e)) for e in payload["errors"])
            if "PersistedQueryNotFound" in msg:
                raise PersistedQueryError(msg)
            raise RuntimeError(f"GraphQL error: {msg}")
        conn = (payload.get("data") or {}).get("itemsConnection")
        if not conn:
            raise RuntimeError("no itemsConnection")
        for edge in conn.get("edges", []):
            node = edge.get("node")
            if node and node.get("id") is not None:
                try:
                    l = _node(node)
                    if _passes(l, chk):
                        out.append(l)
                except Exception as e:
                    log("skip node:", e)
        info = conn.get("pageInfo") or {}
        if not info.get("hasNextPage") or not info.get("endCursor"):
            break
        cursor = info["endCursor"]
    return out


def load_state():
    if not os.path.exists(STATE_FILE):
        return {"listings": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
            if not isinstance(data, dict):
                return {"listings": {}}
            data.setdefault("listings", {})
            return data
    except Exception as e:
        log(f"Error reading state file {STATE_FILE}: {e}")
        return {"listings": {}}


def save_state(state):
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        return True
    except Exception as e:
        log(f"Error writing to {STATE_FILE}: {e}")
        return False


def snapshot_of(l):
    return {"rooms": l.get("rooms"), "area": l.get("area"), "floor": l.get("floor"), "floors": l.get("floors")}


def merge_snap(old, new):
    out = dict(old) if old else {}
    for k, v in (new or {}).items():
        if v is not None:
            out[k] = v
    return out


def main():
    state = load_state()
    seen = state["listings"]
    original_count = len(seen)
    log(f"Loaded {original_count} historical listings from {STATE_FILE}")

    try:
        items = fetch_listings()
    except PersistedQueryError:
        log("bina.az signature expired — update BINA_PERSISTED_HASH.")
        tg_send_message("⚠️ bina.az signature expired — update BINA_PERSISTED_HASH.")
        return
    except Exception as e:
        log("Fetch failed:", e)
        return

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    # Accumulate all items in seen dictionary without dropping old ones
    for l in items:
        key = str(l["id"])
        cur = l.get("price")
        snap = snapshot_of(l)

        if key not in seen:
            seen[key] = {
                "url": l["url"],
                "price": cur,
                "snapshot": snap,
                "first_seen": now,
                "source": "bina.az"
            }
        else:
            rec = seen[key]
            rec["price"] = cur
            rec["snapshot"] = merge_snap(rec.get("snapshot") or {}, snap)

    ok = save_state(state)
    log(f"Done. Scraped={len(items)}, Saved={ok}, Total in memory={len(seen)}")


if __name__ == "__main__":
    main()
