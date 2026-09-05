#!/usr/bin/env python3
# Temporary one-shot: reconcile bina.az site vs seen.json, find MISSED owner posts.
# DRY-RUN by default (reports only). Set SEND=1 to actually announce + record them.
# Run UNDER the monitor's lock so it can't collide with cron:
#   cd /home/vboxuser/binaizleme && set -a && . ./.env && set +a
#   /usr/bin/flock /tmp/binaizleme.lock .venv/bin/python catchup_bina.py           # dry run
#   SEND=1 /usr/bin/flock /tmp/binaizleme.lock .venv/bin/python catchup_bina.py    # for real
import os, sys, time, shutil, datetime as dt
import monitor as m

SEND      = os.environ.get("SEND", "0") == "1"
MAX_CHECK = int(os.environ.get("MAX_CHECK", "0"))     # 0 = check all; e.g. 50 for a quick sample
SLEEP     = float(os.environ.get("SEND_SLEEP", "1.5")) # pause between Telegram sends

src = next((s for s in m.SOURCES if s["name"] == "bina.az"), None)
if src is None:
    sys.exit("bina.az is not in ENABLED_SOURCES; add it before running this.")
owner_label = src.get("owner_label", "owner")

state, sha = m.load_state()
L = state["listings"]
print("MODE           :", "SEND (announce + record)" if SEND else "DRY-RUN (report only)")
print("seen records   :", len(L))

print("fetching full site result set (several minutes)...")
c = m.bina_check(m.BINA_SEARCH_URL)
items = m.fetch_bina(m.BINA_SEARCH_URL)
print("site listings  :", len(items))

not_seen = [l for l in items if str(l["id"]) not in L]
print("not in seen.json:", len(not_seen))

owners, agents, undecided, excluded, checked = [], 0, 0, 0, 0
for l in not_seen:
    if not m.bina_passes(l, c):
        excluded += 1
        continue
    if MAX_CHECK and checked >= MAX_CHECK:
        break
    v, photo = m.bina_is_owner(l["url"])
    checked += 1
    if m.PAGE_DELAY:
        time.sleep(m.PAGE_DELAY)
    if v is True:
        if photo:
            l["photo"] = photo
        owners.append(l)
    elif v is False:
        agents += 1
    else:
        undecided += 1
    if checked % 25 == 0:
        print(f"  checked {checked}/{len(not_seen)} ... missed owners so far: {len(owners)}")

print("\n=== RESULT ===")
print("excluded by filter :", excluded)
print("agents (skip)      :", agents)
print("undetermined       :", undecided)
print("MISSED OWNERS      :", len(owners))
for l in owners[:40]:
    print("   ", l["id"], (l.get("location") or "")[:48], l["url"])
if len(owners) > 40:
    print("    ... and", len(owners) - 40, "more")

if not SEND:
    print("\nDRY-RUN: no messages sent, seen.json untouched.")
    print("Re-run with SEND=1 to announce these and record them so the bot won't repeat.")
    sys.exit(0)

shutil.copy(m.STATE_FILE, m.STATE_FILE + ".bak-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
sent = 0
for l in owners:
    ok = m.notify_new_owner_msg(l, "bina.az", owner_label)
    L[str(l["id"])] = {"url": l["url"], "first_seen": now, "source": "bina.az",
                       "owner": True, "price": m.normalize_price(l.get("price")),
                       "price_history": []}
    if ok:
        sent += 1
    time.sleep(SLEEP)
ok2, how = m.save_state(state, sha)
print(f"\nSENT {sent}/{len(owners)} owner announcements; seen.json saved: {ok2} ({how})")
print("Backup written next to seen.json.")
