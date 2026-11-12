#!/usr/bin/env python3
"""Collect every bina.az ad that matches the bot's filter and was placed since a date
(default 01.01.2026) into one table with a link per ad. READ-ONLY: sends nothing to
Telegram and does not change seen.json.

  cd /home/vboxuser/binaizleme && set -a && . ./.env && set +a
  /usr/bin/flock /tmp/binaizleme.lock .venv/bin/python collect_bina.py

Result: bina_since_2026-01-01.csv (opens in Excel), newest ads first:
  id, link, placed, placed_how, price, currency, rooms, area_m2, floor, location,
  cixaris, ipoteka, temir, vip_premium, seller, bot_first_seen

What it can and cannot know
  * Only ads that are on bina.az NOW. Ads deleted or sold earlier are gone from the
    site; the bot itself only started in August 2026.
  * "placed": bina's own date when the search gives one (placed_how = "bina"), otherwise
    worked out from the ad number (placed_how = "estimate"; ad numbers grow by a steady
    amount per day - measured from the bot's own history - so it is good to about a week).
  * "seller": owner / agent from the bot's history (what it decided when it checked the
    ad). "?" = the bot never checked that ad (it was already on the site at the bot's
    first run, or bina showed no seller label).
    OWNERS=1 reads the page of the "?" ads to fill this in - newest first, at most
    LIMIT pages (default 300) per run, about 1.5 s each. Answers are remembered in
    collect_bina_cache.json, so running it again continues where it stopped.

Options (env): SINCE=2026-01-01 (SINCE=2000-01-01 = everything on the site now)
               OUT=<file>  SEP=";" (if Excel shows one column)  OWNERS=1  LIMIT=300  TAGS=0
The same filter as the bot is used: BINA_SEARCH_URL and the excluded locations. An ad that
bina SHOWS in an excluded location (Koroğlu, Ağ şəhər, ...) is included when one of its
tags names a wanted area (e.g. "Qara Qarayev m."). Tags are only on the ad's own page, so
each such ad is opened once (at most LIMIT per run, remembered; run again to continue).
TAGS=0 skips opening them.
The scan takes about 10 minutes; run it with flock (as above) so it does not overlap a bot run.
"""
import csv
import datetime as dt
import json
import os
import time

import monitor as m

SINCE = os.environ.get("SINCE", "2026-01-01")
OUT = os.environ.get("OUT", f"bina_since_{SINCE}.csv")
SEP = os.environ.get("SEP", ",")
OWNERS = os.environ.get("OWNERS") == "1"
TAGS = os.environ.get("TAGS", "1") != "0"
LIMIT = int(os.environ.get("LIMIT", "300"))
CACHE = os.environ.get("COLLECT_CACHE", "collect_bina_cache.json")
BAKU = dt.timezone(dt.timedelta(hours=4))


def ids_per_day(seen, default):
    """Ad numbers per day, measured from ads the bot checked when they were new
    (their first_seen is close to the day they were placed). Least-squares slope."""
    pts = []
    for k, v in seen.items():
        if k.isdigit() and isinstance(v, dict) and v.get("source") == "bina.az" and "owner" in v:
            t = m._to_epoch(v.get("first_seen"))
            if t is not None:
                pts.append((t / 86400.0, int(k)))
    if len(pts) < 200:
        return float(default), None, len(pts)
    # the newest ad number seen on each day = that day's top of the series
    top = {}
    for d, i in pts:
        top[int(d)] = max(top.get(int(d), 0), i)
    days = sorted(top)
    if len(days) < 10 or days[-1] - days[0] < 10:
        return float(default), None, len(pts)
    xs, ys = days, [top[d] for d in days]
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    den = sum((x - mx) ** 2 for x in xs)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den if den else 0
    if not (500 <= slope <= 20000):
        return float(default), None, len(pts)
    return slope, (mx, my), len(pts)


def placed(l, slope, anchor, now_ts):
    """(epoch, how) the ad was placed."""
    ts = m._to_epoch(l.get("created_at")) if isinstance(l.get("created_at"), str) else None
    if ts is not None:
        return ts, "bina"
    iid = str(l.get("id") or "")
    if not iid.isdigit():
        return None, ""
    if anchor is not None:
        mx, my = anchor
        return (mx + (int(iid) - my) / slope) * 86400.0 + 43200, "estimate"
    top = m._BINA_MAX_ID["v"]
    if top:
        return now_ts - max(0, top - int(iid)) / slope * 86400.0, "estimate"
    return None, ""


def yn(v):
    return "var" if v is True else ("yox" if v is False else "")


def main():
    since_ts = dt.datetime.strptime(SINCE, "%Y-%m-%d").replace(tzinfo=BAKU).timestamp()
    state, _sha = m.load_state()
    seen = state["listings"]
    slope, anchor, npts = ids_per_day(seen, m.BINA_IDS_PER_DAY)
    print(f"collect_bina (monitor {m.VERSION}) - ads placed since {SINCE}; read-only")
    print(f"ad numbers per day: {slope:.0f} "
          + (f"(measured from {npts} checked ads)" if anchor else "(default; too little history)"))
    print("scanning bina.az with the bot's filter (about 10 minutes)...")
    cache = {}
    if os.path.exists(CACHE):
        try:
            with open(CACHE, encoding="utf-8") as fh:
                cache = json.load(fh)
        except Exception:
            cache = {}

    items = m.fetch_bina(m.BINA_SEARCH_URL)
    # Ads bina SHOWS in an excluded location (Koroğlu, Ağ şəhər, ...) belong in the table
    # when one of their page tags is a wanted area (6618907: Koroğlu + Qara Qarayev). The
    # tags are only on the ad's own page, so each such ad is opened once; the answer is
    # remembered (bot history or the side file), so later runs do not open it again.
    cands, m._BINA_TAG_CANDS["items"] = m._BINA_TAG_CANDS["items"], []
    todo, by_tag = [], 0
    for l in cands:
        iid = str(l["id"])
        rec = seen.get(iid) if isinstance(seen.get(iid), dict) else {}
        known = rec.get("tags") if (rec.get("tags_ok") or rec.get("skip") == "area") \
            else cache.get("tags:" + iid)
        if known is None:
            todo.append(l)
        elif known and m._bina_wanted_tag(known.split(", ")):
            l["location"] = known
            items.append(l)
            by_tag += 1
    if cands:
        print(f"{len(cands)} ads are shown in an excluded location; tags already known for "
              f"{len(cands) - len(todo)}")
    if TAGS and todo:
        print(f"opening {min(len(todo), LIMIT)} of {len(todo)} such ad(s) to read their tags...")
        fails = done = 0
        for l in todo[:LIMIT]:
            tags = m.bina_location_tags(l)
            m._BINA_PAGE_CACHE.pop(l["url"], None)
            if tags is None and m.bina_location_tags.last_reason == "fetch":
                fails += 1
                if fails >= 5:
                    print("   5 pages in a row unreadable - stopping the tag reads here")
                    break
            else:
                fails = 0
                done += 1
                joined = ", ".join(tags or [])
                cache["tags:" + str(l["id"])] = joined
                if tags and m._bina_wanted_tag(tags):
                    l["location"] = joined
                    items.append(l)
                    by_tag += 1
            time.sleep(max(m.PAGE_DELAY, 1.0))
        left = len(todo) - done
        if left > 0:
            print(f"   {left} still unread - run the script again to continue")
    if by_tag:
        print(f"{by_tag} ad(s) added because a tag names a wanted area")
    now_ts = time.time()

    rows, dated_by = [], {"bina": 0, "estimate": 0}
    for l in items:
        ts, how = placed(l, slope, anchor, now_ts)
        if ts is None or ts < since_ts:
            continue
        dated_by[how] = dated_by.get(how, 0) + 1
        iid = str(l["id"])
        rec = seen.get(iid) if isinstance(seen.get(iid), dict) else {}
        v = rec.get("owner") if "owner" in rec else cache.get(iid)
        rows.append({"l": l, "ts": ts, "how": how, "rec": rec, "v": v})
    rows.sort(key=lambda r: (r["ts"], int(r["l"]["id"]) if str(r["l"]["id"]).isdigit() else 0),
              reverse=True)

    if OWNERS:
        todo = [r for r in rows if r["v"] is None][:LIMIT]
        print(f"OWNERS=1: reading {len(todo)} ad page(s) the bot never checked "
              f"({sum(1 for r in rows if r['v'] is None)} unknown in total)...")
        fails = 0
        for n, r in enumerate(todo, 1):
            v, _p = m.bina_is_owner(r["l"]["url"])
            if v is None and m.bina_is_owner.last_reason == "fetch":
                fails += 1
                if fails >= 5:
                    print("   5 pages in a row unreadable - stopping the page reads here")
                    break
            else:
                fails = 0
                if v is not None:
                    r["v"] = cache[str(r["l"]["id"])] = bool(v)
            if n % 50 == 0:
                print(f"   {n}/{len(todo)}")
            time.sleep(max(m.PAGE_DELAY, 1.0))
    if cache:
        tmp = CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cache, fh)
        os.replace(tmp, CACHE)

    def seller(v):
        return "owner" if v is True else ("agent" if v is False else "?")

    with open(OUT, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh, delimiter=SEP)
        w.writerow(["id", "link", "placed", "placed_how", "price", "currency", "rooms",
                    "area_m2", "floor", "location", "cixaris", "ipoteka", "temir",
                    "vip_premium", "seller", "bot_first_seen"])
        for r in rows:
            l, rec = r["l"], r["rec"]
            floor = (f"{l.get('floor')}/{l.get('floors')}" if l.get("floor") and l.get("floors")
                     else (l.get("floor") or ""))
            fs = m._to_epoch(rec.get("first_seen"))
            w.writerow([l["id"], l["url"],
                        dt.datetime.fromtimestamp(r["ts"], BAKU).strftime("%d.%m.%Y"), r["how"],
                        l.get("price") if l.get("price") is not None else "", l.get("currency") or "",
                        l.get("rooms") if l.get("rooms") is not None else "",
                        f"{l['area']:g}" if isinstance(l.get("area"), (int, float)) else "",
                        floor, l.get("location") or "", yn(l.get("has_bill_of_sale")),
                        yn(l.get("has_mortgage")), yn(l.get("has_repair")),
                        "yes" if l.get("_promoted") else "", seller(r["v"]),
                        dt.datetime.fromtimestamp(fs, BAKU).strftime("%d.%m.%Y %H:%M") if fs else ""])

    counts = {"owner": 0, "agent": 0, "?": 0}
    for r in rows:
        counts[seller(r["v"])] += 1
    print(f"\n{len(items)} ads match the filter now; {len(rows)} of them placed since {SINCE}")
    print(f"   dates: {dated_by.get('bina', 0)} from bina, {dated_by.get('estimate', 0)} estimated "
          "from the ad number")
    print(f"   seller: {counts['owner']} owner, {counts['agent']} agent, {counts['?']} not checked")
    print(f"saved: {os.path.abspath(OUT)}")


if __name__ == "__main__":
    main()
