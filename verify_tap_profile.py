#!/usr/bin/env python3
"""Check the OPTIONAL browser-based tap.az profile count before turning it on.
Read-only: sends nothing, changes nothing.

tap.az builds a seller's profile list with JavaScript, so the bot can only count a
seller's ads by opening the profile in a real (headless) browser - the same thing you
do by hand. This script does that for the sellers you name and prints what it counted,
so you can compare with what you see on your phone.

  cd /home/vboxuser/binaizleme && set -a && . ./.env && set +a
  .venv/bin/python verify_tap_profile.py 6638901 22702915

Checked 8 Nov 2026: 6638901 = 6 ads (1 flat, 2 houses, 2 offices, 1 land); 22702915 = 0.
TAP_COUNT_SCOPE=property counts every real-estate ad, the default counts flats only.

Needs playwright once:
  .venv/bin/pip install playwright
  sudo .venv/bin/playwright install-deps chromium
  .venv/bin/playwright install chromium
"""
import sys

import monitor as m

print("VERSION:", m.VERSION, "| TAP_COUNT_SCOPE:", m.TAP_COUNT_SCOPE,
      "| limit:", m.TAP_MAX_APARTMENTS)
for uid in sys.argv[1:] or ["6638901", "22702915"]:
    n = m.tap_profile_count_browser(uid)
    d = getattr(m.tap_profile_count_browser, "last_detail", {}) or {}
    print(f"\nseller {uid}: profile says {d.get('total')} ad(s); seen on the page: "
          f"{d.get('links')} ad(s), of them {d.get('flats')} flat(s) and "
          f"{d.get('property')} real-estate ad(s)")
    if n is None:
        print(f"   -> NOT trusted ({d.get('why', 'page could not be read')}); the bot would "
              "treat this seller as unknown")
    else:
        verdict = "AGENT (3 or more)" if n >= m.TAP_MAX_APARTMENTS else "below the limit"
        print(f"   -> counts as {n} for the rule -> {verdict}")
    for u in (getattr(m.tap_profile_count_browser, "last_links", None) or [])[:30]:
        print(f"      {u}")
print("\nCompare with each seller's profile on your phone.")
