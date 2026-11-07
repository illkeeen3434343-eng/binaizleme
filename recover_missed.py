#!/usr/bin/env python3
"""Re-judge listings that the September bugs filed wrongly, using today's fixed checks.

Between early September and 20 October:
  * tap.az  - every seller showing "İstifadəçinin bütün elanları" was counted as
              having ~28 apartments, so real owners were filed as AGENT;
  * bina.az - an owner who wrote "Vasitəçilər narahat etməsin" was filed as AGENT;
  * yeniemlak - unreadable detail pages made the bot give up after 4 tries.
From 20 October to 7 November (2026-10-18-a):
  * tap.az  - ads lifted back to the top were hidden as "not new" without a check.
    (yeniemlak and lalafo ads hidden the same way are picked up by the bot itself.)
This script looks at those listings again. Owners it finds are sent ONCE, marked old.

  cd /home/vboxuser/binaizleme && set -a && . ./.env && set +a
  /usr/bin/flock /tmp/binaizleme.lock .venv/bin/python recover_missed.py           # dry run
  SEND=1 /usr/bin/flock /tmp/binaizleme.lock .venv/bin/python recover_missed.py    # send

Dry run (default) only prints; nothing is sent, seen.json is not touched.
Options (env): SOURCES=tap,bina,yeniemlak  LIMIT=150 (per source)
               FROM=2026-09-04  TO=2026-10-21
bina and yeniemlak listings are only checked if they are STILL in today's search.

tap.az - only PROVEN owners are sent (2026-11-07-b):
  proven      the ad's own words say owner ("öz evimdi", "makler deyiləm", ...), or
              the seller's apartment count is known and below 3;
  unverified  the seller has other ads ("İstifadəçinin bütün elanları"), the count
              could not be read and the text says nothing either way. The live bot's
              default calls these "owner", but they are NOT sent from here: many may be
              agents. They stay as they are (nothing changes) so they can be verified
              later with the browser count (TAP_PROFILE_BROWSER=1).
  INCLUDE_UNVERIFIED=1 sends them too (not recommended).
A seller who shows up with 3+ different flats in this batch (plus the bot's history)
is re-judged with that count - the same "3 or more apartments = agent" rule.
"""
import datetime as dt
import os
import shutil
import time

import monitor as m

SEND = os.environ.get("SEND") == "1"
INCLUDE_UNVERIFIED = os.environ.get("INCLUDE_UNVERIFIED") == "1"
LIMIT = int(os.environ.get("LIMIT", "150"))
WANT = {x.strip() for x in os.environ.get("SOURCES", "tap,bina,yeniemlak").split(",") if x.strip()}
FROM = m._to_epoch(os.environ.get("FROM", "2026-09-04") + "T00:00:00+00:00")
TO = m._to_epoch(os.environ.get("TO", "2026-10-21") + "T00:00:00+00:00")
VERDICT = {True: "OWNER", False: "agent", None: "unreadable"}
UNVERIFIED_TAG = "count-unknown:defaulted-to-owner"


def tap_recheck(cands, origin):
    """Judge each tap candidate with today's checks. Returns a list of entries."""
    apts, cache, out = m._seller_apartments(m._RUNTIME["state"]["listings"]), {}, []
    for k, v in cands:
        iid = k.split(":", 1)[1]
        lst = {"id": iid, "url": v["url"], "rooms": v.get("rooms"), "area": v.get("area")}
        verdict, photo, seller = m.tap_check_owner(v["url"], apts, cache, listing=lst)
        reasons = list(getattr(m.tap_check_owner, "last_reasons", None) or [])
        where = getattr(m.tap_check_owner, "last_location", None)
        if where:
            lst["location"] = where            # address line for the message
        out.append({"k": k, "v": v, "lst": lst, "verdict": verdict, "photo": photo,
                    "seller": seller, "reasons": reasons, "origin": origin,
                    "akey": m._apt_key(lst)})
        time.sleep(m.PAGE_DELAY or 0.3)
    return out


def tap_batch_counts(entries, history):
    """A seller with 3+ different flats among these ads (plus history) is an agent by
    the user's rule, even if each single ad looked fine. Re-judge their 'owner' ads."""
    by_seller = {}
    for e in entries:
        if e["seller"] and e["verdict"] is not None:
            by_seller.setdefault(e["seller"], set()).add(e["akey"])
    for e in entries:
        if e["verdict"] is not True or not e["seller"]:
            continue
        flats = set(history.get(e["seller"], set())) | by_seller.get(e["seller"], set())
        if len(flats) < m.TAP_MAX_APARTMENTS:
            continue
        print(f"   batch: seller {e['seller']} has {len(flats)} different flats -> re-judging "
              f"{e['lst']['url']}")
        verdict, photo, _s = m.tap_check_owner(e["lst"]["url"], {e["seller"]: flats}, {},
                                               listing=e["lst"])
        if verdict is not None:
            e["verdict"] = verdict
            e["reasons"] = list(getattr(m.tap_check_owner, "last_reasons", None) or [])
            e["photo"] = photo or e["photo"]
        time.sleep(m.PAGE_DELAY or 0.3)


def tap_line(e):
    tag = VERDICT[e["verdict"]]
    if e["verdict"] is True and UNVERIFIED_TAG in e["reasons"]:
        tag = "UNVERIFIED"
    return f"   {tag:10} {e['lst']['url']}  seller={e['seller']}  {e['reasons']}"


def in_window(rec):
    t = m._to_epoch(rec.get("first_seen"))
    return t is not None and FROM <= t < TO and "rechecked" not in rec


def newest_first(pairs):
    return sorted(pairs, key=lambda kv: kv[1].get("first_seen", ""), reverse=True)[:LIMIT]


def main():
    state, sha = m.load_state()
    seen = state["listings"]
    m._RUNTIME["state"] = state
    now_ts = time.time()
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    print(f"MODE: {'SEND' if SEND else 'DRY RUN (nothing sent, nothing saved)'}")
    print(f"window {os.environ.get('FROM', '2026-09-04')} .. {os.environ.get('TO', '2026-10-21')}, "
          f"up to {LIMIT} per source\n")
    found = []                          # (key, listing-for-message, source name, label)
    unverified = []                     # tap "owners" by default only - not sent

    if "tap" in WANT:
        cands = newest_first([(k, v) for k, v in seen.items()
                              if v.get("source") == "tap.az" and v.get("owner") is False
                              and in_window(v)])
        print(f"tap.az: {len(cands)} listings filed as agent in the window")
        entries = tap_recheck(cands, "agent-filed")
        # tap ads that 2026-10-18-a hid as "not new" (never checked, never sent). The bot
        # only reads tap's first page, so ones that already fell off it are looked up here.
        hidden = sorted([(k, v) for k, v in seen.items()
                         if v.get("source") == "tap.az" and "owner" not in v
                         and str(v.get("skip", "")).startswith("old")],
                        key=lambda kv: kv[1].get("first_seen", ""), reverse=True)[:LIMIT]
        print(f"\ntap.az: {len(hidden)} listings hidden as 'not new' since 20 Oct (newest first)")
        entries += tap_recheck(hidden, "hidden")
        print("\ntap.az: same seller with 3+ different flats in this batch?")
        tap_batch_counts(entries, m._seller_apartments(seen))
        print("\ntap.az results:")
        for e in entries:
            print(tap_line(e))
            if e["seller"]:
                e["v"].setdefault("seller", e["seller"])   # helps future flat counting
            if e["verdict"] is True:
                e["lst"]["photo"] = e["photo"]
                item = (e["k"], e["lst"], "tap.az", "Sahibindən")
                if UNVERIFIED_TAG in e["reasons"]:
                    unverified.append(item)
                    if INCLUDE_UNVERIFIED:
                        found.append(item)
                else:
                    found.append(item)
            elif e["verdict"] is False:
                e["v"]["_hidden_agent" if e["origin"] == "hidden" else "_recheck_agent"] = True

    if "bina" in WANT:
        cands = [(k, v) for k, v in seen.items()
                 if v.get("source") == "bina.az" and v.get("owner") is False and in_window(v)]
        print(f"\nbina.az: {len(cands)} listings filed as agent in the window; "
              "scanning today's search to keep only live ones (~10 min)...")
        live = {str(l["id"]): l for l in m.fetch_bina(m.BINA_SEARCH_URL)} if cands else {}
        cands = newest_first([(k, v) for k, v in cands if k in live])
        print(f"bina.az: {len(cands)} of them are still live; checking")
        for k, v in cands:
            verdict, photo = m.bina_is_owner(v["url"])
            print(f"   {VERDICT[verdict]:10} {v['url']}")
            if verdict is True:
                lst = dict(live[k])
                if photo:
                    lst["photo"] = photo
                found.append((k, lst, "bina.az", "Mülkiyyətçi"))
            elif verdict is False:
                v["_recheck_agent"] = True
            time.sleep(m.PAGE_DELAY or 0.3)

    if "yeniemlak" in WANT:
        cands = [(k, v) for k, v in seen.items()
                 if v.get("source") == "yeniemlak.az" and "owner" in v and v.get("owner") is None
                 and v.get("tries", 0) >= m.OWNER_MAX_TRIES and in_window(v)]
        print(f"\nyeniemlak.az: {len(cands)} listings the bot gave up on in the window")
        live = {"ye:" + str(l["id"]): l for l in m.fetch_yeniemlak(m.YENIEMLAK_SEARCH_URL)} if cands else {}
        cands = newest_first([(k, v) for k, v in cands if k in live])
        print(f"yeniemlak.az: {len(cands)} of them are still live; checking")
        for k, v in cands:
            verdict, photo = m.check_is_owner(v["url"], "Əmlak sahibi")
            print(f"   {VERDICT[verdict]:10} {v['url']}")
            if verdict is True:
                lst = dict(live[k])
                if photo:
                    lst["photo"] = photo
                found.append((k, lst, "yeniemlak.az", "Əmlak sahibi"))
            elif verdict is False:
                v["_recheck_agent"] = True
            time.sleep(m.PAGE_DELAY or 0.3)

    if unverified:
        print(f"\n--> {len(unverified)} tap.az listing(s) UNVERIFIED (seller has other ads, "
              "apartment count unknown, no owner wording):")
        for k, lst, src, _ in unverified:
            print(f"   {src:13} {lst['url']}")
        print("    " + ("INCLUDE_UNVERIFIED=1: these ARE in the send list below." if INCLUDE_UNVERIFIED
                        else "NOT sent and not changed. They can be verified later with the "
                             "browser count (TAP_PROFILE_BROWSER=1)."))
    print(f"\n==> {len(found)} owner listing(s) to send (proven"
          + (" + unverified" if INCLUDE_UNVERIFIED else "") + "):")
    for k, lst, src, _ in found:
        print(f"   {src:13} {lst['url']}")
    if not SEND:
        print("\nDRY RUN: nothing sent, seen.json unchanged. Re-run with SEND=1 to send them once.")
        return

    shutil.copy(m.STATE_FILE, m.STATE_FILE + ".bak-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    sent = 0
    clocks = {src: m.IdClock.from_seen(seen, src, pfx)
              for src, pfx in (("tap.az", "tap:"), ("yeniemlak.az", "ye:"))}
    for k, lst, src, label in found:
        rec = seen[k]
        age, kind = m._age_estimate(lst, clocks.get(src), now_ts, rec)
        if age is not None:
            lst["_age_days"], lst["_age_kind"] = age, kind
            lst["_posted_ts"] = now_ts - age * 86400.0   # shown as "ən gec dd.mm.yyyy"
        if m.notify_new_owner_msg(lst, src, label):
            sent += 1
        rec["owner"] = True                  # never sent twice
        rec.pop("tries", None)
        rec.pop("skip", None)
        rec["recovered"] = now
        time.sleep(1.5)
    for v in seen.values():                  # re-confirmed agents: don't check them again
        if v.pop("_recheck_agent", None):
            v["rechecked"] = now
        if v.pop("_hidden_agent", None):
            v["owner"] = False
            v.pop("skip", None)
            v["rechecked"] = now
    ok, how = m.save_state(state, sha)
    print(f"\nSENT {sent}/{len(found)}; seen.json saved={ok} ({how}); backup written next to it")


if __name__ == "__main__":
    main()
