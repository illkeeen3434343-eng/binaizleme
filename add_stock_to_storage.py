#!/usr/bin/env python3
"""Put the ads that collect_bina.py found "by tag" into the bot's storage (seen.json),
so the bot follows them like every other bina ad (price changes; VIP/Premium check).

Every bina ad that matches the filter is ALREADY in the bot's storage - the bot records
all of them on every run. The only ones missing are older ads that bina SHOWS in an
excluded location (Koroğlu, Ağ şəhər, ...) but that carry a wanted tag (e.g. "Qara
Qarayev m."): the bot recorded those as "not wanted" before their tags were read.
collect_bina.py has now read the tags; this script marks the wanted ones in seen.json.

  cd /home/vboxuser/binaizleme && set -a && . ./.env && set +a
  /usr/bin/flock /tmp/binaizleme.lock .venv/bin/python add_stock_to_storage.py            # only shows
  APPLY=1 /usr/bin/flock /tmp/binaizleme.lock .venv/bin/python add_stock_to_storage.py    # saves

Nothing is sent to Telegram. With APPLY=1 a backup copy of seen.json is written first.
Ads the bot already decided on (sent / agent) are never changed. It can be run again
after each collect_bina.py run; ads already added are skipped.
"""
import datetime as dt
import json
import os
import shutil

import monitor as m

APPLY = os.environ.get("APPLY") == "1"
CACHE = os.environ.get("COLLECT_CACHE", "collect_bina_cache.json")


def main():
    if not os.path.exists(CACHE):
        print(f"{CACHE} not found - run collect_bina.py first")
        return
    with open(CACHE, encoding="utf-8") as fh:
        cache = json.load(fh)
    state, sha = m.load_state()
    seen = state["listings"]
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    add, already, decided, not_wanted = [], 0, 0, 0
    for k, joined in sorted(cache.items()):
        if not k.startswith("tags:") or not isinstance(joined, str):
            continue
        iid = k[5:]
        tags = [t for t in joined.split(", ") if t]
        if not tags or not m._bina_wanted_tag(tags):
            not_wanted += 1
            continue
        rec = seen.get(iid)
        if isinstance(rec, dict) and rec.get("tags_ok"):
            already += 1
            continue
        if isinstance(rec, dict) and "owner" in rec:
            decided += 1
            continue
        add.append((iid, joined))
    print(f"MODE: {'APPLY (saving)' if APPLY else 'SHOW ONLY (nothing saved)'}")
    print(f"tags read by collect_bina.py: {not_wanted + already + decided + len(add)} ads; "
          f"{not_wanted} have no wanted tag")
    print(f"wanted by tag: {already} already in storage, {decided} already decided by the bot, "
          f"{len(add)} to add:")
    for iid, joined in add:
        print(f"   https://bina.az/items/{iid}   [{joined}]")
    if not APPLY or not add:
        if add:
            print("\nNothing saved. Run again with APPLY=1 to add them.")
        return
    shutil.copy(m.STATE_FILE, m.STATE_FILE + ".bak-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    before = len(seen)
    for iid, joined in add:
        rec = seen.get(iid)
        if not isinstance(rec, dict):
            rec = seen[iid] = {"url": f"https://bina.az/items/{iid}", "first_seen": now,
                               "source": "bina.az"}
        rec.pop("skip", None)
        rec["tags_ok"], rec["tags"] = True, joined
    assert len(seen) >= before
    ok, how = m.save_state(state, sha)
    print(f"\nadded {len(add)}; seen.json saved={ok} ({how}); backup written next to it")
    print("From the next run the bot follows these ads (price changes, VIP/Premium).")


if __name__ == "__main__":
    main()
