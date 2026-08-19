#!/usr/bin/env python3
"""
Bina.az change monitor — keeps ALL historical data forever.
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

BINA_SEARCH_URL = os.environ.get("BINA_SEARCH_URL", (
    "https://bina.az/baki/alqi-satqi/menziller?has_bill_of_sale=true&has_repair=true"
    "&location_ids%5B%5D=51&location_ids%5B%5D=100&location_ids%5B%5D=16"
    "&location_ids%5B%5D=11&location_ids%5B%5D=74&location_ids%5B%5D=52"
    "&location_ids%5B%5D=53&location_ids%5B%5D=54&location_ids%5B%5D=33"
    "&location_ids%5B%5D=99&location_ids%5B%5D=200"
))
CITY_ID = os.environ.get("BINA_CITY_ID", "1")
CATEGORY_ID = os.environ.get("BINA_CATEGORY_ID", "1")
PERSISTED_HASH = os.environ.get(
    "BINA_PERSISTED_HASH",
    "b781511a943a4d710eefdf811a24dd4ae353e55d836952603ce0b37fde97d073"
)

GRAPHQL_URL = "https://bina.az/graphql"
SORT = "BUMPED_AT_DESC"
PAGE_SIZE = 16
SCAN_PAGES = int(os.environ.get("SCAN_PAGES", "8"))
STATE_FILE = os.environ.get("STATE_FILE", "seen.json")
MAX_SEEN = 0          # 0 = never prune
SEND_PHOTOS = os.environ.get("SEND_PHOTOS", "true").lower() == "true"
DROP_ANOMALY_FLOOR = float(os.environ.get("DROP_ANOMALY_FLOOR", "0.4"))
RISE_ANOMALY_CEIL = float(os.environ.get("RISE_ANOMALY_CEIL", "3.0"))
NOTIFY_INCREASES = os.environ.get("NOTIFY_INCREASES", "true").lower() == "true"
NOTIFY_UPDATES = os.environ.get("NOTIFY_UPDATES", "false").lower() == "true"
NOTIFY_NEW = os.environ.get("NOTIFY_NEW", "false").lower() == "true"

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
GH_TOKEN = os.environ.get("GH_TOKEN", "").strip()
GH_REPO = os.environ.get("GH_REPO", "").strip()
GH_BRANCH = os.environ.get("GH_BRANCH", "main").strip()
USE_API = bool(GH_TOKEN and GH_REPO)
IN_ACTIONS = os.environ.get("GITHUB_ACTIONS") == "true"

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
    except Exception as e:
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
    for src, dst in (
        ("price_to", "priceTo"),
        ("price_from", "priceFrom"),
        ("area_from", "areaFrom"),
        ("area_to", "areaTo"),
    ):
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
        "area_from": float(af) if af else None,
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
        "location": sub("location", "fullName")
        or sub("location", "name")
        or sub("city", "name"),
        "has_bill_of_sale": node.get("hasBillOfSale"),
        "has_mortgage": node.get("hasMortgage"),
        "has_repair": node.get("hasRepair"),
        "updated_at": node.get("updatedAt"),
        "url": f"https://bina.az{path}" if path else f"https://bina.az/items/{iid}",
        "photo": photo,
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
                separators=(",", ":"),
            ),
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
    if MAX_SEEN <= 0:
        return
    L = state["listings"]
    if len(L) > MAX_SEEN:
        kept = sorted(
            L.items(), key=lambda kv: kv[1].get("first_seen", ""), reverse=True
        )[:MAX_SEEN]
        state["listings"] = dict(kept)


def save_state(state, sha, original_count=0):
    """Refuse to destroy a large history."""
    _prune(state)
    new_count = len(state.get("listings", {}))

    # Strong safety
    if original_count > 300 and new_count < original_count * 0.6:
        msg = f"SAFETY: refusing to overwrite {original_count} → {new_count} entries"
        log(msg)
        if IN_ACTIONS:
            tg_send_message(f"⚠️ {msg}")
        return False, "safety-refuse"

    if not USE_API:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=1)
        return True, "local"

    reason = "unknown"
    for attempt in range(6):
        body = {
            "message": "Update seen listings",
            "branch": GH_BRANCH,
            "content": base64.b64encode(
                json.dumps(state, ensure_ascii=False).encode("utf-8")
            ).decode("ascii"),
        }
        if sha:
            body["sha"] = sha
        try:
            r = requests.put(_gh_url(), headers=_gh_headers(), json=body, timeout=30)
        except Exception as e:
            reason = f"network: {e}"
            time.sleep(2 * (attempt + 1))
            continue

        if r.status_code in (200, 201):
            return True, "saved"

        if r.status_code in (409, 422):
            reason = f"HTTP {r.status_code} (retry)"
            g = requests.get(
                _gh_url(), headers=_gh_headers(), params={"ref": GH_BRANCH}, timeout=30
            )
            if g.status_code == 200:
                j = g.json()
                sha = j["sha"]
                raw = (
                    base64.b64decode(j.get("content", "")).decode("utf-8")
                    if j.get("content")
                    else ""
                )
                latest = json.loads(raw) if raw.strip() else {"listings": {}}
                latest.setdefault("listings", {})

                # Always keep existing keys
                for k, v in state["listings"].items():
                    if k not in latest["listings"]:
                        latest["listings"][k] = v
                    else:
                        op = v.get("price")
                        lp = latest["listings"][k].get("price")
                        if (
                            isinstance(op, (int, float))
                            and isinstance(lp, (int, float))
                            and op < lp
                        ):
                            merged = dict(latest["listings"][k])
                            merged.update(v)
                            latest["listings"][k] = merged
                        else:
                            for fk, fv in v.items():
                                if (
                                    fk not in latest["listings"][k]
                                    or latest["listings"][k][fk] is None
                                ):
                                    latest["listings"][k][fk] = fv
                state = latest
                _prune(state)
                continue
            reason = f"re-read HTTP {g.status_code}"
            break

        if r.status_code in (500, 502, 503, 504, 429):
            reason = f"HTTP {r.status_code}"
            time.sleep(3 * (attempt + 1))
            continue

        reason = f"HTTP {r.status_code}: {r.text[:140]}"
        return False, reason

    return False, reason


def snapshot_of(l):
    return {
        "rooms": l.get("rooms"),
        "area": l.get("area"),
        "floor": l.get("floor"),
        "floors": l.get("floors"),
    }


def merge_snap(old, new):
    out = dict(old) if old else {}
    for k, v in (new or {}).items():
        if v is not None:
            out[k] = v
    return out


def is_real_drop(old, new):
    return (
        isinstance(old, (int, float))
        and isinstance(new, (int, float))
        and 0 < new < old
        and new >= old * DROP_ANOMALY_FLOOR
    )


def is_real_rise(old, new):
    return (
        isinstance(old, (int, float))
        and isinstance(new, (int, float))
        and new > old > 0
        and new <= old * RISE_ANOMALY_CEIL
    )


def _spaced(n):
    return f"{int(n):,}".replace(",", " ")


def _fmt_pub(v):
    if not v:
        return None
    try:
        return dt.datetime.fromisoformat(str(v)).strftime("%d %b %Y, %H:%M")
    except Exception:
        return str(v)


def describe_changes(old_snap, new_snap):
    labels = {"rooms": "Rooms", "area": "Area", "floor": "Floor", "floors": "Floors"}
    out = []
    for k in ("rooms", "area", "floor", "floors"):
        o, n = old_snap.get(k), new_snap.get(k)
        if o is None or n is None:
            continue
        if o != n:
            out.append(f"{labels[k]} {o}→{n}")
    return out


def format_message(l, kind, old_price=None, changes=None):
    heads = {
        "new": "🏠 <b>NEW LISTING</b>",
        "drop": "🔻 <b>PRICE DROP</b>",
        "increase": "🔺 <b>PRICE INCREASE</b>",
        "update": "✏️ <b>LISTING UPDATED</b>",
    }
    lines = [f"{heads.get(kind, heads['new'])} · bina.az", ""]
    cur = l.get("currency", "AZN")
    if (
        kind in ("drop", "increase")
        and isinstance(old_price, (int, float))
        and l.get("price") is not None
    ):
        diff = int(l["price"]) - int(old_price)
        sign = "−" if diff < 0 else "+"
        oldtxt = f"<s>{_spaced(old_price)}</s>" if kind == "drop" else _spaced(old_price)
        lines.append(
            f"💰 <b>Price:</b> {oldtxt} → <b>{_spaced(l['price'])}</b> {cur} "
            f"({sign}{_spaced(abs(diff))})"
        )
    elif l.get("price") is not None:
        lines.append(f"💰 <b>Price:</b> {_spaced(l['price'])} {cur}")
    if kind == "update" and changes:
        lines.append("🔧 <b>Changed:</b> " + ", ".join(changes))
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
        lines.append(
            f"📅 <b>{'Published' if kind == 'new' else 'Updated'}:</b> {html.escape(pub)}"
        )
    tags = [
        t
        for t, on in (
            ("kupçalı", l.get("has_bill_of_sale")),
            ("ipoteka", l.get("has_mortgage")),
            ("təmirli", l.get("has_repair")),
        )
        if on
    ]
    if tags:
        lines.append("✅ " + ", ".join(tags))
    lines.append("")
    lines.append(f'🔗 <a href="{html.escape(l["url"])}">Open listing</a>')
    return "\n".join(lines)


def notify(l, kind, old_price=None, changes=None):
    text = format_message(l, kind, old_price, changes)
    if SEND_PHOTOS and l.get("photo"):
        return tg_send_photo(l["photo"], text)
    return tg_send_message(text)


def main():
    if not BOT_TOKEN or not CHAT_ID:
        log("ERROR: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set.")
        sys.exit(1)

    try:
        state, sha = load_state()
    except Exception as e:
        log("Could not load state; skipping:", e)
        if IN_ACTIONS:
            tg_send_message(
                f"⚠️ Could not read memory this run ({html.escape(str(e))}). Skipping."
            )
        return

    seen = state["listings"]
    original_count = len(seen)
    first_run = original_count == 0
    log(f"Loaded {original_count} historical listings")

    try:
        items = fetch_listings()
    except PersistedQueryError:
        tg_send_message(
            "⚠️ bina.az signature expired — update BINA_PERSISTED_HASH."
        )
        return
    except Exception as e:
        if first_run:
            tg_send_message(
                f"🟡 Bot running but could not read bina.az: {html.escape(str(e))}"
            )
        else:
            log("fetch failed:", e)
        return

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    events = []

    for l in items:
        key = l["id"]
        cur = l.get("price")
        snap = snapshot_of(l)

        if key not in seen:
            if first_run or not NOTIFY_NEW:
                seen[key] = {
                    "url": l["url"],
                    "price": cur,
                    "snapshot": snap,
                    "first_seen": now,
                    "source": "bina.az",
                }
            else:
                events.append(("new", l, None, None))
            continue

        rec = seen[key]
        old_price = rec.get("price")
        old_snap = rec.get("snapshot") or {}

        if is_real_drop(old_price, cur):
            events.append(("drop", l, old_price, None))
        elif NOTIFY_INCREASES and is_real_rise(old_price, cur):
            events.append(("increase", l, old_price, None))
        else:
            changed = describe_changes(old_snap, snap) if NOTIFY_UPDATES else []
            if changed:
                events.append(("update", l, None, changed))
            else:
                if isinstance(cur, (int, float)):
                    rec["price"] = cur
                rec["snapshot"] = merge_snap(old_snap, snap)
                if "source" not in rec:
                    rec["source"] = "bina.az"

    if first_run:
        ok, reason = save_state(state, sha, original_count)
        msg = (
            f"✅ Monitoring started. Recorded {len(items)} listings. "
            f"All future history will be kept forever."
        )
        tg_send_message(msg if ok else f"🟡 Seeded but save failed: {reason}")
        return

    notified = 0
    for kind, l, old_price, changes in events:
        if notify(l, kind, old_price, changes):
            prev = seen.get(l["id"], {})
            updated = dict(prev)
            updated.update(
                {
                    "url": l["url"],
                    "price": l.get("price"),
                    "snapshot": merge_snap(prev.get("snapshot") or {}, snapshot_of(l)),
                    "first_seen": prev.get("first_seen", now),
                    "source": prev.get("source", "bina.az"),
                }
            )
            seen[l["id"]] = updated
            notified += 1
        else:
            log("send failed:", l["id"])

    ok, reason = save_state(state, sha, original_count)
    if not ok:
        tg_send_message(f"⚠️ Could not save memory: {html.escape(reason)}")
    log(
        f"Done. scanned={len(items)} notified={notified} "
        f"saved={ok}({reason}) total_seen={len(seen)}"
    )


if __name__ == "__main__":
    main()
