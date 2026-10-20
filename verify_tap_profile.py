#!/usr/bin/env python3
"""Check the OPTIONAL browser-based tap.az profile count before turning it on.

tap.az builds a seller's profile list with JavaScript, so the bot can only count a
seller's apartments by opening the profile in a real (headless) browser - the same
thing you do by hand. This script does that for sellers you already know and prints
what it counted, so you can compare with what you see on your phone.

  cd /home/vboxuser/binaizleme && set -a && . ./.env && set +a
  .venv/bin/python verify_tap_profile.py 6638901 22702915

Expected: 6638901 (agent) -> 5 or more;  22702915 (owner Rəşad) -> 1 or 2.
Only if both look right, add  TAP_PROFILE_BROWSER=1  to .env.

Needs playwright once:
  .venv/bin/pip install playwright && .venv/bin/playwright install --with-deps chromium
"""
import sys

import monitor as m

ids = sys.argv[1:] or ["6638901", "22702915"]
for uid in ids:
    n = m.tap_profile_count_browser(uid)
    got = m.tap_profile_count_browser.last_ids if n is not None else []
    print(f"seller {uid}: " + (f"{n} apartment(s)" if n is not None
                               else "profile could not be read (see the error above)"))
    for i in got[:30]:
        print(f"    https://tap.az/elanlar/dasinmaz-emlak/menziller/{i}")
print("\nCompare each list with the seller's profile in your browser. If the bot counts "
      "ads that are NOT this seller's (for example a VIP block of other people's ads), "
      "do not enable TAP_PROFILE_BROWSER.")
