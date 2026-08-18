#!/usr/bin/env python3
"""
Apartment monitor for Bina.az -> Telegram.
Exclusively tracks:
  1. Price drops
  2. Price increases
  3. Field modifications on existing listings (rooms, area, floor, etc.)
"""
import base64
import datetime as dt
import html
import json
import os
import sys
import time
from urllib.parse import parse_qs, urlparse

import requests

# --------------------------------------------------------------------------- #
# SEARCH URL & CONFIGURATIONS
# --------------------------------------------------------------------------- #
BINA_SEARCH_URL = os.environ.get(
    "BINA_SEARCH_URL",
    (
        "https://bina.az/baki/alqi-satqi/menziller?has_repair=true&location_ids%5B%5D=51&location_ids%5B%5D=100&location_ids%5B%5D=16&location_ids%5B%5D=11&location_ids%5B%5D=74&location_ids%5B%5D=52&location_ids%5B%5D=53&location_ids%5B%5D=54&location_ids%5B%5D=33&location_ids%5B%5D=99&location_ids%5B%5D=200"
    ),
)

SOURCES = [
    {"name": "bina.az", "type": "bina", "url": BINA_SEARCH_URL, "prefix": ""}
]

CITY_ID = os.environ.get("BINA_CITY_ID", "1")
CATEGORY_ID = os.environ.get("BINA_CATEGORY_ID", "1")
PERSISTED_HASH = os.environ.get(
    "BINA_PERSISTED_HASH",
    "b781511a943a4d710eefdf811a24dd4ae353e55d836952603ce0b37fde97d073",
)
GRAPHQL_URL = "https://bina.az/graphql"
OPERATION = "SearchItems"
SORT = "BUMPED_AT_DESC"
PAGE_SIZE = 16
SCAN_PAGES = int(os.environ.get("SCAN_PAGES", "6"))

STATE_FILE = os.environ.get("STATE_FILE", "seen.json")
MAX_SEEN = 8000
SEND_PHOTOS = os.environ.get("SEND_PHOTOS", "true").lower() == "true"

PRICE_DROP_MIN_ABS = int(os.environ.get("PRICE_DROP_MIN_ABS", "0"))
DROP_ANOMALY_FLOOR = float(os.environ.get("DROP_ANOMALY_FLOOR", "0.4"))

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

GH_TOKEN = os.environ.get("GH_TOKEN", "").strip()
GH_REPO = os.environ.get("GH_REPO", "").strip()
GH_BRANCH = os.environ.get("GH_BRANCH", "main").strip()
USE_API = bool(GH_TOKEN and GH_REPO)

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)
API_HEADERS = {
    "User-Agent": UA,
    "Accept": "*/*",
    "Accept-Language": "az,en-US;q=0.9,en;q=0.8,ru;q=0.7",
    "Content-Type": "application/json",
    "Referer": "https://bina.az/baki/alqi-satqi/menziller",
    "Origin": "https://bina.az",
    "x-platform": "desktop",
}


def log(*a):
    print(*a, flush=True)


class PersistedQueryError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Telegram Utilities
# --------------------------------------------------------------------------- #
def tg_send_message(text):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            json={
                "chat_id": CHAT_ID,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=30,
        )
        return r.status_code == 200
    except requests.RequestException as e:
        log("Telegram error:", e)
        return False


def tg_send_photo(photo_url, caption):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendPhoto",
            json={
                "chat_id": CHAT_ID,
                "photo": photo_url,
                "caption": caption,
                "parse_mode": "HTML",
            },
            timeout=30,
        )
        if r.status_code == 200:
            return True
        return tg_send_message(caption)
    except requests.RequestException:
        return tg_send_message(caption)


# --------------------------------------------------------------------------- #
# Bina.az GraphQL Logic
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
    if one("floor_first") is not None:
        f["floorFirst"] = b(one("floor_first"))
    if one("floor_last") is not None:
        f["floorLast"] = b(one("floor_last"))

    return f


def _bina_params(filter_vars, cursor):
    variables = {"first": PAGE_SIZE, "filter": filter_vars, "sort": SORT}
    if cursor:
        variables["cursor"] = cursor
    return {
        "operationName": OPERATION,
        "variables": json.dumps(variables, separators=(",", ":"), ensure_ascii=False),
        "extensions": json.dumps(
            {"persistedQuery": {"version": 1, "sha256Hash": PERSISTED_HASH}},
            separators=(",", ":"),
        ),
    }


def _bina_node(node):
    def sub(k, fld):
        o = node.get(k)
        return o.get(fld) if isinstance(o, dict) else None

    preview = node.get("preview") or {}
    photo = preview.get("f460x345") or preview.get("thumbnail")

    def parse_num(val, cast_fn):
        try:
            return cast_fn(float(val)) if val is not None else None
        except (TypeError, ValueError):
            return None

    path = node.get("path")
    return {
        "id": str(node["id"]),
        "rooms": node.get("rooms"),
        "area": parse_num(sub("area", "value"), float),
        "area_units": sub("area", "units") or "m²",
        "floor": node.get("floor"),
        "floors": node.get("floors"),
        "price": parse_num(sub("price", "total"), int),
        "currency": sub("price", "currency") or "AZN",
        "location_id": parse_num(sub("location", "id"), int),
        "location": sub("location", "fullName")
        or sub("location", "name")
        or sub("city", "name"),
        "has_bill_of_sale": node.get("hasBillOfSale"),
        "has_mortgage": node.get("hasMortgage"),
        "has_repair": node.get("hasRepair"),
        "updated_at": node.get("updatedAt"),
        "url": f"https://bina.az{path}" if path else f"https://bina.az/items/{node['id']}",
        "photo": photo,
    }


def fetch_bina(url):
    fv = bina_filter_vars(url)
    out, cursor = [], None

    for _ in range(SCAN_PAGES):
        r = requests.get(GRAPHQL_URL, params=_bina_params(fv, cursor), headers=API_HEADERS, timeout=30)
        if r.status_code != 200:
            raise RuntimeError(f"HTTP {r.status_code}")
        try:
            payload = r.json()
        except ValueError:
            raise RuntimeError("Response was not JSON")

        if payload.get("errors"):
            msg = "; ".join(str(e.get("message", e)) for e in payload["errors"])
            if "PersistedQueryNotFound" in msg or "PERSISTED_QUERY_NOT_FOUND" in msg:
                raise PersistedQueryError(msg)
            raise RuntimeError(f"GraphQL error: {msg}")

        conn = (payload.get("data") or {}).get("itemsConnection")
        if not conn:
            raise RuntimeError("No itemsConnection found")

        for edge in conn.get("edges", []):
            node = edge.get("node")
            if node and node.get("id") is not None:
                try:
                    out.append(_bina_node(node))
                except Exception as e:
                    log("Skip node error:", e)

        info = conn.get("pageInfo") or {}
        if not info.get("hasNextPage") or not info.get("endCursor"):
            break
        cursor = info["endCursor"]

    return out


# --------------------------------------------------------------------------- #
# State & Persistence
# --------------------------------------------------------------------------- #
def _gh_headers():
    return {
        "Authorization": f"Bearer {GH_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _gh_url():
    return f"https://api.github.com/repos/{GH_REPO}/contents/{STATE_FILE}"


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
        return {"listings": {}}, None
    r.raise_for_status()

    j = r.json()
    raw = base64.b64decode(j.get("content", "")).decode("utf-8") if j.get("content") else ""
    state = json.loads(raw) if raw.strip() else {"listings": {}}
    state.setdefault("listings", {})
    return state, j["sha"]


def _prune(state):
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
            r = requests.put(_gh_url(), headers=_gh_headers(), json=body_bytes(state, sha), timeout=30)
        except requests.RequestException as e:
            reason = f"network error: {e}"
            time.sleep(2 * (attempt + 1))
            continue

        if r.status_code in (200, 201):
            return True, "saved"

        if r.status_code in (409, 422):
            g = requests.get(_gh_url(), headers=_gh_headers(), params={"ref": GH_BRANCH}, timeout=30)
            if g.status_code == 200:
                j = g.json()
                sha = j["sha"]
                raw = base64.b64decode(j.get("content", "")).decode("utf-8") if j.get("content") else ""
                latest = json.loads(raw) if raw.strip() else {"listings": {}}
                latest.setdefault("listings", {})

                for k, v in state["listings"].items():
                    latest["listings"][k] = v

                if state.get("source_status"):
                    latest["source_status"] = state["source_status"]

                state = latest
                _prune(state)
                continue

        if r.status_code in (500, 502, 503, 504, 429):
            time.sleep(3 * (attempt + 1))
            continue

        return False, f"HTTP {r.status_code}: {r.text[:140]}"

    return False, reason


def body_bytes(state, sha):
    body = {
        "message": "Update seen listings",
        "branch": GH_BRANCH,
        "content": base64.b64encode(json.dumps(state, ensure_ascii=False).encode("utf-8")).decode("ascii"),
    }
    if sha:
        body["sha"] = sha
    return body


# --------------------------------------------------------------------------- #
# Modification Tracking & Formatting
# --------------------------------------------------------------------------- #
def _fmt_pub(v):
    if not v:
        return None
    try:
        return dt.datetime.fromisoformat(str(v)).strftime("%d %b %Y, %H:%M")
    except Exception:
        return str(v)


def detect_changes(old_data, new_item):
    """
    Compares the existing record against the freshly fetched listing.
    Returns: (kind, changes_dict)
    kind options: 'drop', 'increase', 'modified', or None
    """
    changes = {}

    # 1. Price checks
    old_p = old_data.get("price")
    new_p = new_item.get("price")

    if isinstance(old_p, (int, float)) and isinstance(new_p, (int, float)) and old_p != new_p:
        diff = new_p - old_p
        if new_p < old_p:
            if (old_p - new_p) >= PRICE_DROP_MIN_ABS and new_p >= (old_p * DROP_ANOMALY_FLOOR):
                return "drop", {"old_price": old_p, "new_price": new_p, "diff": abs(diff)}
        else:
            return "increase", {"old_price": old_p, "new_price": new_p, "diff": diff}

    # 2. Field modifications check
    field_labels = {
        "rooms": "Rooms",
        "area": "Area",
        "floor": "Floor",
        "floors": "Total Floors",
        "has_bill_of_sale": "Kupça",
        "has_mortgage": "Mortgage",
        "has_repair": "Repair Status",
        "location": "Location",
    }

    for key, label in field_labels.items():
        old_val = old_data.get(key)
        new_val = new_item.get(key)
        if old_val is not None and new_val is not None and old_val != new_val:
            changes[label] = (old_val, new_val)

    if changes:
        return "modified", changes

    return None, {}


def format_message(l, source_name, kind, change_details):
    cur = l.get("currency", "AZN")

    if kind == "drop":
        lines = [f"🔻 <b>PRICE DROP</b> · {html.escape(source_name)}", ""]
        old_p = change_details["old_price"]
        new_p = change_details["new_price"]
        diff = change_details["diff"]
        lines.append(
            f"💰 <b>Price:</b> <s>{old_p:,}</s> → <b>{new_p:,}</b> {cur} (−{diff:,})".replace(",", " ")
        )

    elif kind == "increase":
        lines = [f"🔺 <b>PRICE INCREASE</b> · {html.escape(source_name)}", ""]
        old_p = change_details["old_price"]
        new_p = change_details["new_price"]
        diff = change_details["diff"]
        lines.append(
            f"💰 <b>Price:</b> <s>{old_p:,}</s> → <b>{new_p:,}</b> {cur} (+{diff:,})".replace(",", " ")
        )

    elif kind == "modified":
        lines = [f"✏️ <b>LISTING MODIFIED</b> · {html.escape(source_name)}", ""]
        lines.append("<b>What changed:</b>")
        for field, (old_v, new_v) in change_details.items():
            lines.append(f"  • <b>{field}:</b> <s>{old_v}</s> → <b>{new_v}</b>")
        lines.append("")
        if l.get("price") is not None:
            lines.append(f"💰 <b>Current Price:</b> {l['price']:,} {cur}".replace(",", " "))

    # General overview details
    lines.append(f"🛏 <b>Rooms:</b> {l.get('rooms') if l.get('rooms') is not None else '-'}")

    if l.get("area") is not None:
        area = int(l["area"]) if float(l["area"]).is_integer() else l["area"]
        lines.append(f"📐 <b>Area:</b> {area} {l.get('area_units', 'm²')}")

    if l.get("floor") and l.get("floors"):
        lines.append(f"🏢 <b>Floor:</b> {l['floor']}/{l['floors']}")

    if l.get("location"):
        lines.append(f"📍 <b>Location:</b> {html.escape(str(l['location']))}")

    pub = _fmt_pub(l.get("updated_at"))
    if pub:
        lines.append(f"📅 <b>Updated:</b> {html.escape(pub)}")

    lines.append("")
    lines.append(f'🔗 <a href="{html.escape(l["url"])}">Open listing</a>')
    return "\n".join(lines)


def notify(l, source_name, kind, change_details):
    text = format_message(l, source_name, kind, change_details)
    if SEND_PHOTOS and l.get("photo"):
        return tg_send_photo(l["photo"], text)
    return tg_send_message(text)


# --------------------------------------------------------------------------- #
# Process Bina.az Items
# --------------------------------------------------------------------------- #
def process_source(items, source, seen):
    prefix, name = source["prefix"], source["name"]
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    notified = 0

    for l in items:
        key = prefix + str(l["id"])

        if key not in seen:
            # Seed/record new listing silently without triggering Telegram notification
            seen[key] = {**l, "first_seen": now}
            continue

        # Detect modifications/price changes on existing items
        kind, details = detect_changes(seen[key], l)

        if kind in ("drop", "increase", "modified"):
            if notify(l, name, kind, details):
                # Update saved listing state after successful alert
                seen[key] = {**l, "last_updated": now}
                notified += 1

    return notified


# --------------------------------------------------------------------------- #
# Main Execution
# --------------------------------------------------------------------------- #
def main():
    if not BOT_TOKEN or not CHAT_ID:
        log("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set.")
        sys.exit(1)

    try:
        state, sha = load_state()
    except Exception as e:
        log("Error loading state:", e)
        return

    seen = state["listings"]
    status = state.setdefault("source_status", {})
    total_notified = 0

    for source in SOURCES:
        name = source["name"]
        try:
            items = fetch_bina(source["url"])
        except PersistedQueryError:
            err = "BINA_PERSISTED_HASH signature expired."
            if status.get(name) != "PQ":
                tg_send_message(f"⚠️ <b>{html.escape(name)}</b> stopped working: {err}")
                status[name] = "PQ"
            continue
        except Exception as e:
            err = str(e)[:200]
            if status.get(name) != err:
                tg_send_message(f"⚠️ Error fetching <b>{html.escape(name)}</b>: {html.escape(err)}")
                status[name] = err
            continue

        if status.get(name):
            tg_send_message(f"✅ <b>{html.escape(name)}</b> recovered and is working.")
        status[name] = ""

        total_notified += process_source(items, source, seen)

    ok, reason = save_state(state, sha)
    log(f"Done. Notified: {total_notified}, Memory Saved: {ok} ({reason})")


if __name__ == "__main__":
    main()
