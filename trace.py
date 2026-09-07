#!/usr/bin/env python3
# Trace exactly why specific listings were not announced. Read-only, sends nothing.
#   cd /home/vboxuser/binaizleme && set -a && . ./.env && set +a
#   /usr/bin/flock /tmp/binaizleme.lock .venv/bin/python trace.py
import re, html
import monitor as m

TAP  = ["48407292"]
BINA = ["6438833", "6437375", "6437888"]
YE   = ["168545"]

st, _ = m.load_state(); L = st["listings"]
print("VERSION:", m.VERSION, "| records:", len(L))

print("\n" + "=" * 60 + "\nTAP.AZ\n" + "=" * 60)
for i in TAP:
    url = f"https://tap.az/elanlar/dasinmaz-emlak/menziller/{i}"
    print(f"\n{i}  in seen: {('tap:'+i) in L}  {L.get('tap:'+i, {})}")
    try:
        r, raw = m._fetch_tap_html(url, tries=2)
        flat = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw)))
        sid = (re.search(r"/elanlar\?user_id=(\d+)", raw) or [None, None])[1]
        print(f"  HTTP {getattr(r,'status_code','?')} blocked={m._tap_blocked(raw)} bytes={len(raw)}")
        print(f"  seller={sid} multi_marker={m.TAP_MULTI_MARKER in flat}")
        blk = m._tap_seller_block(raw).lower()
        print(f"  seller block: {blk[:110]!r}")
        print(f"  business token hit: {next((t for t in m.TAP_BUSINESS_TOKENS if t in blk), None)}")
        if sid:
            print(f"  apartment count: {m.tap_apartment_count(sid, {})} (threshold {m.TAP_MAX_APARTMENTS})")
        d = m._tap_description(raw)
        print(f"  description: {(d or 'NOT FOUND')[:90]!r}")
        if d:
            dl = d.lower()
            print(f"  owner phrase: {next((p for p in m.TAP_OWNER_PHRASES if p in dl), None)}"
                  f" | realtor phrase: {next((p for p in m.TAP_REALTOR_PHRASES if p in dl), None)}")
        v, _p, _s = m.tap_check_owner(url, {}, {})
        print(f"  >>> VERDICT: {{True:'OWNER',False:'AGENT',None:'DEFER'}}[{v}] = "
              f"{ {True:'OWNER',False:'AGENT',None:'DEFER'}[v] }")
    except Exception as e:
        print("  ERROR:", e)

print("\n" + "=" * 60 + "\nBINA.AZ\n" + "=" * 60)
c = m.bina_check(m.BINA_SEARCH_URL)
items = m.fetch_bina(m.BINA_SEARCH_URL)
byid = {str(l["id"]): l for l in items}
print(f"fetched {len(items)} | newest id {m._BINA_MAX_ID['v']}")
for i in BINA:
    print(f"\n{i}  in seen: {i in L}  {L.get(i, {})}")
    l = byid.get(i)
    if not l:
        print("  NOT in the fetched set -> filtered out or paging missed it")
        continue
    print(f"  location={l.get('location')!r} promoted={l.get('_promoted')} "
          f"age~{m._listing_age_days(l)}")
    print(f"  bina_passes: {m.bina_passes(l, c)} | stale_promoted: {m.is_stale_promoted(l)}")
    v, _p = m.bina_is_owner(l["url"])
    print(f"  >>> owner verdict: { {True:'OWNER',False:'AGENT',None:'DEFER'}[v] }")

print("\n" + "=" * 60 + "\nYENIEMLAK.AZ\n" + "=" * 60)
items = m.fetch_yeniemlak(m.YENIEMLAK_SEARCH_URL)
byid = {str(l["id"]): l for l in items}
print(f"fetched {len(items)}")
for i in YE:
    print(f"\n{i}  in seen: {('ye:'+i) in L}")
    l = byid.get(i)
    if not l:
        print("  NOT in fetched set -> ye_passes/pagination dropped it")
        print("  YE_KEYWORDS:", m.YE_KEYWORDS)
        continue
    print(f"  location={l.get('location')!r} ye_passes={m.ye_passes(l)}")
    rr = m.http_get(l["url"], headers=m.HTML_HEADERS, timeout=30, browser=True)
    txt = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", rr.text or "")))
    labels = [w for w in ("\u018fmlak sahibi", "M\u00fclkiyy\u0259t\u00e7i",
                          "Vasit\u0259\u00e7i", "Sahibi", "Agentlik") if w in txt]
    v, _p = m.check_is_owner(l["url"], "\u018fmlak sahibi")
    print(f"  labels present: {labels}")
    print(f"  >>> owner verdict: { {True:'OWNER',False:'AGENT',None:'DEFER'}[v] }")
