#!/usr/bin/env python3
# Which filter key hides these ids? Re-runs the newest N pages under relaxed filters.
#   cd /home/vboxuser/binaizleme && set -a && . ./.env && set +a
#   /usr/bin/flock /tmp/binaizleme.lock .venv/bin/python bisect_bina.py
import copy, json, os, requests, monitor as m

IDS   = set(os.environ.get("IDS", "6436955,6435951").split(","))
PAGES = int(os.environ.get("PAGES", "60"))          # newest 60*16 = 960 listings
base  = m.bina_filter_vars(m.BINA_SEARCH_URL)
print("base filter:", json.dumps(base, ensure_ascii=False))
print(f"probing newest {PAGES*16} listings per variant, sort={m.SORT}\n")

def probe(fv):
    cur, hits, seen, total = None, set(), 0, None
    for _ in range(PAGES):
        r = requests.get(m.GRAPHQL_URL, params=m._bina_params(fv, cur),
                         headers=m.API_HEADERS, timeout=30)
        try:
            conn = r.json()["data"]["itemsConnection"]
        except Exception:
            return None, 0, "HTTP %s" % r.status_code
        if total is None:
            total = conn.get("totalCount")
        for e in conn.get("edges", []):
            nd = e.get("node") or {}
            if nd.get("id") is None:
                continue
            seen += 1
            if str(nd["id"]) in IDS:
                hits.add(str(nd["id"]))
        pi = conn.get("pageInfo") or {}
        if not pi.get("hasNextPage") or not pi.get("endCursor"):
            break
        cur = pi["endCursor"]
    return hits, seen, total

variants = [("BASE (exactly what the bot sends)", base)]
v = copy.deepcopy(base); v.pop("floorFirst", None); v.pop("floorLast", None)
variants.append(("no floorFirst/floorLast", v))
v = copy.deepcopy(base); v["floorFirst"] = None; v["floorLast"] = None
variants.append(("floorFirst/Last = null", v))
v = copy.deepcopy(base); v.pop("hasBillOfSale", None)
variants.append(("no hasBillOfSale", v))
v = copy.deepcopy(base); v.pop("locationIds", None)
variants.append(("no locationIds", v))
v = {"cityId": m.CITY_ID, "categoryId": m.CATEGORY_ID, "leased": False}
variants.append(("minimal (city+category only)", v))
v = {"cityId": m.CITY_ID, "leased": False}
variants.append(("no categoryId", v))

for name, fv in variants:
    hits, seen, total = probe(fv)
    if hits is None:
        print(f"{name:34} -> ERROR {total}")
        continue
    mark = "  <== FOUND " + ",".join(sorted(hits)) if hits else ""
    print(f"{name:34} total={str(total):>7} scanned={seen:>5} found={len(hits)}/{len(IDS)}{mark}")

print("\nThe FIRST variant that finds them names the filter that was hiding them.")
