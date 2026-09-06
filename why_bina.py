#!/usr/bin/env python3
# Why did bina skip these ids? Also dumps the real node field names (promoted/date/agency).
#   cd /home/vboxuser/binaizleme && set -a && . ./.env && set +a
#   /usr/bin/flock /tmp/binaizleme.lock .venv/bin/python why_bina.py
import json, requests, monitor as m

IDS = ["6436955", "6435951"]
st, _ = m.load_state(); L = st["listings"]
c = m.bina_check(m.BINA_SEARCH_URL)
fv = m.bina_filter_vars(m.BINA_SEARCH_URL)

print("=== SEARCH-NODE FIELDS (first node) ===")
r = requests.get(m.GRAPHQL_URL, params=m._bina_params(fv, None), headers=m.API_HEADERS, timeout=30)
n0 = r.json()["data"]["itemsConnection"]["edges"][0]["node"]
print("all keys:", sorted(n0.keys()))
print("bool-True keys:", sorted(k for k, v in n0.items() if v is True))
print("date-ish:", {k: n0.get(k) for k in n0 if "at" == k[-2:].lower() or "date" in k.lower()})
print("agency-ish:", {k: n0.get(k) for k in n0
                      if any(x in k.lower() for x in ("agen", "shop", "owner", "leaser", "seller"))})

print("\n=== TARGETS ===")
found, cur = {}, None
for _ in range(m.SCAN_PAGES):
    rr = requests.get(m.GRAPHQL_URL, params=m._bina_params(fv, cur), headers=m.API_HEADERS, timeout=30)
    conn = rr.json()["data"]["itemsConnection"]
    for e in conn.get("edges", []):
        nd = e.get("node") or {}
        if str(nd.get("id")) in IDS:
            found[str(nd["id"])] = nd
    pi = conn.get("pageInfo") or {}
    if not pi.get("hasNextPage") or not pi.get("endCursor"):
        break
    cur = pi["endCursor"]

for iid in IDS:
    print("-" * 54); print("id", iid, "| in seen.json:", iid in L, "|", L.get(iid, {}).get("first_seen", ""))
    nd = found.get(iid)
    if not nd:
        print("  NOT returned by the search query -> outside the filter (or paging raced past it)")
        continue
    l = m._bina_node(nd)
    print("  location :", l.get("location"), "| id", l.get("location_id"))
    print("  promoted :", l.get("_promoted"), "| created:", l.get("created_at"), "| updated:", l.get("updated_at"))
    print("  bina_passes:", m.bina_passes(l, c))
    if not m.bina_passes(l, c):
        loc = l.get("location") or ""
        why = ("excluded area" if any(x in loc for x in m.BINA_EXCLUDE_LOCATIONS)
               else "promoted" if (m.SKIP_PROMOTED and l.get("_promoted")) else "rooms/price/area")
        print("  -> filtered out:", why)
    else:
        v, _p = m.bina_is_owner(l["url"])
        print("  owner verdict:", v, "(None = undetermined/retry)")
        print("  ->", "would announce" if v is True and iid not in L
              else ("already in seen -> gate 3" if iid in L else "agent/undetermined"))
