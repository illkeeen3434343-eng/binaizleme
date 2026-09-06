#!/usr/bin/env python3
# Verify tap profile counting against a real seller.
#   cd /home/vboxuser/binaizleme && set -a && . ./.env && set +a
#   UID=6638901 AD=46970831 .venv/bin/python check_tap_profile.py
import os, re, html
import monitor as m

UID = os.environ.get("UID", "6638901")
AD  = os.environ.get("AD",  "46970831")
print("VERSION:", m.VERSION)

for purl in (f"https://tap.az/elanlar?user_id={UID}",
             f"https://tap.az/elanlar/dasinmaz-emlak/menziller?user_id={UID}"):
    try:
        r, raw = m._fetch_tap_html(purl, tries=2)
        ids = set(re.findall(r"/elanlar/dasinmaz-emlak/menziller/(\d+)", raw))
        allids = set(re.findall(r"/elanlar/[a-z0-9\-/]+/(\d+)", raw))
        print(f"\n{purl}\n  HTTP {getattr(r,'status_code','?')} blocked={m._tap_blocked(raw)} "
              f"bytes={len(raw)}\n  apartment ids: {len(ids)} | any-category ids: {len(allids)}")
        if ids:
            print("  sample:", sorted(ids)[:8])
    except Exception as e:
        print(purl, "-> error", e)

print("\ncounter result:", m.tap_apartment_count(UID, {}), f"(threshold {m.TAP_MAX_APARTMENTS})")

adurl = f"https://tap.az/elanlar/dasinmaz-emlak/menziller/{AD}"
r, raw = m._fetch_tap_html(adurl, tries=2)
flat = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw)))
print("\nad page:", adurl)
print("  multi-listing marker present:", m.TAP_MULTI_MARKER in flat)
print("  seller id found:", (re.search(r"/elanlar\?user_id=(\d+)", raw) or [None, "NONE"])[1])
v, _p, sel = m.tap_check_owner(adurl, {}, {})
print("  VERDICT:", {True: "OWNER", False: "AGENT", None: "DEFER"}[v])
