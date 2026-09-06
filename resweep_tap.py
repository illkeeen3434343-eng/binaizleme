#!/usr/bin/env python3
# Re-evaluate ALREADY-RECORDED tap.az listings against the current agent rules.
# DRY-RUN by default: reports which stored listings are now judged agents.
#   /usr/bin/flock /tmp/binaizleme.lock .venv/bin/python resweep_tap.py
#   APPLY=1 ... .venv/bin/python resweep_tap.py     # mark them agent in seen.json
import os, time, shutil, datetime as dt
import monitor as m

APPLY = os.environ.get("APPLY") == "1"
LIMIT = int(os.environ.get("LIMIT", "120"))
st, sha = m.load_state(); L = st["listings"]
targets = [(k, v) for k, v in L.items()
           if v.get("source") == "tap.az" and v.get("owner") is True]
targets.sort(key=lambda kv: kv[1].get("first_seen", ""), reverse=True)
targets = targets[:LIMIT]
print(f"re-checking {len(targets)} stored tap.az owner records (newest first)\n")

cache, flips = {}, []
for k, v in targets:
    owner, _photo, seller = m.tap_check_owner(v["url"], {}, cache)
    if owner is False:
        flips.append((k, v["url"], seller))
        print(f"  NOW AGENT  {v['url']}")
    time.sleep(m.PAGE_DELAY or 0.3)

print(f"\n{len(flips)} of {len(targets)} previously-announced owners are now judged agents.")
if not APPLY:
    print("DRY RUN - seen.json unchanged. Re-run with APPLY=1 to mark them.")
    raise SystemExit(0)
shutil.copy(m.STATE_FILE, m.STATE_FILE + ".bak-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
for k, _u, seller in flips:
    L[k]["owner"] = False
    L[k]["reclassified"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    if seller:
        L[k]["seller"] = seller
ok, how = m.save_state(st, sha)
print(f"marked {len(flips)} records as agent; saved={ok} ({how}); backup written")
