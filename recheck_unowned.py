#!/usr/bin/env python3
# Recover listings seeded WITHOUT an owner decision (e.g. 6438833 / 6437375 / 6437888).
# DRY-RUN by default. SEND=1 announces the owners found and stamps every verdict.
#   /usr/bin/flock /tmp/binaizleme.lock .venv/bin/python recheck_unowned.py
#   DAYS=3 LIMIT=60 SEND=1 /usr/bin/flock /tmp/binaizleme.lock .venv/bin/python recheck_unowned.py
import os, time, shutil, datetime as dt
import monitor as m

DAYS  = int(os.environ.get("DAYS", "3"))
LIMIT = int(os.environ.get("LIMIT", "60"))
SEND  = os.environ.get("SEND") == "1"

st, sha = m.load_state(); L = st["listings"]
cut = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=DAYS)).isoformat()
cands = [(k, v) for k, v in L.items()
         if v.get("source") == "bina.az" and str(k).isdigit()
         and "owner" not in v                      # never got a verdict
         and (v.get("first_seen") or "") >= cut]
cands.sort(key=lambda kv: int(kv[0]), reverse=True)
print(f"MODE: {'SEND' if SEND else 'DRY-RUN'} | records with NO owner verdict, "
      f"first_seen within {DAYS}d: {len(cands)} (checking newest {min(LIMIT,len(cands))})")

owners, agents, unknown = [], 0, 0
for k, v in cands[:LIMIT]:
    verdict, photo = m.bina_is_owner(v["url"])
    if verdict is True:
        owners.append((k, v, photo)); print(f"  OWNER  {v['url']}")
    elif verdict is False:
        agents += 1
    else:
        unknown += 1
    time.sleep(m.PAGE_DELAY or 0.3)

print(f"\nowners={len(owners)} agents={agents} undetermined={unknown}")
if not SEND:
    print("DRY-RUN: nothing sent, seen.json untouched. Re-run with SEND=1.")
    raise SystemExit(0)

shutil.copy(m.STATE_FILE, m.STATE_FILE + ".bak-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
sent = 0
for k, v, photo in owners:
    l = {"id": k, "url": v["url"], "price": v.get("price"),
         "location": v.get("location"), "photo": photo}
    if m.notify_new_owner_msg(l, "bina.az", "Mülkiyyətçi"):
        sent += 1
    L[k]["owner"] = True
    time.sleep(1.5)
for k, v in cands[:LIMIT]:
    L[k].setdefault("owner", False)        # stamp the rest so they are never re-checked
ok, how = m.save_state(st, sha)
print(f"announced {sent}/{len(owners)}; all {len(cands[:LIMIT])} stamped; saved={ok} ({how})")
