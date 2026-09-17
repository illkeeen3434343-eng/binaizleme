#!/usr/bin/env python3
# Enumerate EVERY gate for the three reported listings. Read-only, sends nothing.
#   cd /home/vboxuser/binaizleme && set -a && . ./.env && set +a
#   /usr/bin/flock /tmp/binaizleme.lock .venv/bin/python why3.py
import re, html, monitor as m

BINA = ["6463748", "6452312"]
TAP  = ["48690954"]
st, _ = m.load_state(); L = st["listings"]
print("VERSION:", m.VERSION, "| records:", len(L))
print("EXCLUDE:", m.BINA_EXCLUDE_LOCATIONS, "| PROMOTED_MAX_AGE_DAYS:", m.PROMOTED_MAX_AGE_DAYS)

print("\n" + "=" * 62 + "\nBINA\n" + "=" * 62)
c = m.bina_check(m.BINA_SEARCH_URL)
items = m.fetch_bina(m.BINA_SEARCH_URL)
byid = {str(l["id"]): l for l in items}
print(f"fetched {len(items)} | newest id {m._BINA_MAX_ID['v']}")
for i in BINA:
    rec = L.get(i)
    print(f"\n--- {i} ---")
    print(f"  GATE 3 in seen : {rec is not None}"
          + (f"  owner={rec.get('owner', '<MISSING>')!r} first_seen={rec.get('first_seen')}"
             if rec else ""))
    l = byid.get(i)
    print(f"  GATE 1 in fetch: {l is not None}")
    if l is None:
        print("  -> server filter excluded it, or paging raced past it")
        continue
    age = m._listing_age_days(l)
    print(f"  location={l.get('location')!r} promoted={l.get('_promoted')} age~{age}")
    print(f"  GATE 2 bina_passes : {m.bina_passes(l, c)}")
    loc = l.get("location") or ""
    if any(x in loc for x in m.BINA_EXCLUDE_LOCATIONS):
        print("     -> BLOCKED by BINA_EXCLUDE_LOCATIONS (multi-tag fix not deployed)")
    print(f"  GATE 4 stale_promoted: {m.is_stale_promoted(l)}"
          + ("   -> BLOCKED: promoted and older than the age limit" if m.is_stale_promoted(l) else ""))
    v, _p = m.bina_is_owner(l["url"])
    print(f"  GATE 5 owner verdict : {{True:'OWNER',False:'AGENT',None:'DEFER'}}[{v}]"
          f" = { {True:'OWNER',False:'AGENT',None:'DEFER'}[v] }")

print("\n" + "=" * 62 + "\nTAP\n" + "=" * 62)
for i in TAP:
    url = f"https://tap.az/elanlar/dasinmaz-emlak/menziller/{i}"
    rec = L.get("tap:" + i)
    print(f"\n--- {i} ---")
    print(f"  GATE 3 in seen : {rec is not None}"
          + (f"  owner={rec.get('owner', '<MISSING>')!r} tries={rec.get('tries')}" if rec else ""))
    try:
        r, raw = m._fetch_tap_html(url, tries=2)
        flat = m.az_normalize(re.sub(r"<[^>]+>", " ", raw))
        sid = (re.search(r"/elanlar\?user_id=(\d+)", raw) or [None, None])[1]
        print(f"  HTTP {getattr(r,'status_code','?')} blocked={m._tap_blocked(raw)} bytes={len(raw)}")
        print(f"  multi_marker={any(m.az_normalize(k).strip() in flat for k in m.TAP_MULTI_MARKERS)}"
              f" seller={sid}")
        if sid:
            print(f"  apartment count: {m.tap_apartment_count(sid, {})} "
                  f"(threshold {m.TAP_MAX_APARTMENTS})")
        d = m._tap_description(raw)
        print(f"  description: {(d or 'NOT FOUND')[:120]!r}")
        sc, why = m.owner_signal_score(d or "", m._tap_seller_block(raw), None)
        print(f"  score={sc} reasons={why}")
        v, _p, _s = m.tap_check_owner(url, {}, {})
        print(f"  VERDICT: { {True:'OWNER',False:'AGENT',None:'DEFER'}[v] }")
    except Exception as e:
        print("  ERROR:", e)
