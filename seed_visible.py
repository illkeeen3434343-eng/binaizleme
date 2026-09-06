#!/usr/bin/env python3
# ONE-TIME: after widening the bina filter, record every newly-visible listing WITHOUT
# announcing, so ~2000 pre-existing ads don't flood Telegram as "new".
#   /usr/bin/flock /tmp/binaizleme.lock .venv/bin/python seed_visible.py        # dry run
#   APPLY=1 /usr/bin/flock /tmp/binaizleme.lock .venv/bin/python seed_visible.py
import os, shutil, datetime as dt
import monitor as m

APPLY = os.environ.get("APPLY") == "1"
st, sha = m.load_state(); L = st["listings"]
c = m.bina_check(m.BINA_SEARCH_URL)
items = m.fetch_bina(m.BINA_SEARCH_URL)
new = [l for l in items if str(l["id"]) not in L and m.bina_passes(l, c)]
print(f"site listings: {len(items)} | already known: {len(items)-len(new)} | NEWLY VISIBLE: {len(new)}")
if not APPLY:
    print("\nDRY RUN. These would be recorded silently (no Telegram).")
    for l in new[:15]:
        print("   ", l["id"], (l.get("location") or "")[:44])
    print("Re-run with APPLY=1 to seed them.")
    raise SystemExit(0)
now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
shutil.copy(m.STATE_FILE, m.STATE_FILE + ".bak-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
for l in new:
    L[str(l["id"])] = {"url": l["url"], "first_seen": now, "source": "bina.az",
                       "price": m.normalize_price(l.get("price")), "price_history": []}
ok, how = m.save_state(st, sha)
print(f"seeded {len(new)} listings silently; saved={ok} ({how}); backup written")
