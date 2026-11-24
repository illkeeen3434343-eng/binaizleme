#!/usr/bin/env python3
"""Alerts the bot decided to send but Telegram did NOT deliver.

Before 2026-11-23-a such an alert was lost for good: the ad was already marked "sent"
(tap 49259520 on 19 Nov and bina 6637490 on 18 Nov 2026: Telegram answered 502 Bad
Gateway / timed out on all three ways of sending). This script finds those runs in
run.log, shows which ads they were, and re-sends the ones you choose.

  cd /home/vboxuser/binaizleme
  .venv/bin/python lost_alerts.py                    # list only - changes nothing

  set -a && . ./.env && set +a
  RESEND="49259520 6637490" /usr/bin/flock /tmp/binaizleme.lock .venv/bin/python lost_alerts.py

RESEND takes ids or links. Their messages go into the bot's outbox (seen.json) and
the next bot run (within 15 minutes) sends them, in the usual format; an ad older than
3 days is marked "♻️" with the date the bot first saw it. Each ad is queued only once
(FORCE=1 queues it again). Only ads the bot judged OWNER can be re-sent. The ad's page
is read once for price / rooms / area / address / photo. Nothing else is changed.
Run it with flock (as above) so it never overlaps a bot run.
"""
import datetime as dt
import json
import os
import re
import shutil

STATE = os.environ.get("STATE_FILE", "seen.json")
LOG = os.environ.get("RUN_LOG", "run.log")
BAKU = dt.timezone(dt.timedelta(hours=4))

RUN_START = re.compile(r"^monitor\.py (\S+) starting (\S+)")
DONE = re.compile(r"^Done\..*?elapsed=(\d+)s")
# the LAST way of sending failed -> the message did not arrive (versions before 11-23-a)
FINAL_FAIL = re.compile(r"^(Telegram sendMessage failed:|Telegram sendMessage \(plain\) failed:"
                        r"|Telegram error:)")
QUEUED = re.compile(r"^Telegram: (\S+) NOT delivered")
GAVE_UP = re.compile(r"^Telegram outbox: GAVE UP on (\S+)")
SEGMENT = [(re.compile(r"^tap\.az: \d+ listings"), "tap.az"),
           (re.compile(r"^lalafo\.az: \d+ listings"), "lalafo.az"),
           (re.compile(r"^yeniemlak: read "), "yeniemlak.az"),
           (re.compile(r"^bina: scanned "), "bina.az")]
TAP_OWNER = re.compile(r"^tap\.az: (https://tap\.az/\S+) score=\S+ -> OWNER")
TAP_ANY = re.compile(r"^tap\.az: https://tap\.az/\S+ score=")
VIP = re.compile(r"^(\S+): (https://\S+) -> VIP/Premium owner ad never sent before")
PRICE = re.compile(r"^price change (\S+): (\d+) -> (\d+)")


def to_ts(v):
    try:
        d = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=dt.timezone.utc)
        return d.timestamp()
    except Exception:
        return None


def baku(ts):
    return dt.datetime.fromtimestamp(ts, BAKU).strftime("%d.%m.%Y %H:%M")


def key_of_url(u):
    if "tap.az" in u:
        return "tap:" + re.findall(r"(\d+)", u)[-1]
    if "bina.az" in u:
        return re.findall(r"(\d+)", u)[-1]
    if "lalafo.az" in u:
        m_ = re.search(r"-id-(\d+)", u)
        return "lala:" + (m_.group(1) if m_ else re.findall(r"(\d+)", u)[-1])
    if "yeniemlak.az" in u:
        return "ye:" + re.findall(r"(\d+)", u)[-1]
    return u


def read_runs(lines):
    runs, cur = [], None
    for no, line in enumerate(lines, 1):
        line = line.rstrip("\n")
        ms = RUN_START.match(line)
        if ms:
            cur = {"ver": ms.group(1), "start": to_ts(ms.group(2)), "first": no, "lines": []}
            runs.append(cur)
            continue
        if cur is not None:
            cur["lines"].append((no, line))
    for r in runs:
        r["end"] = (r["start"] or 0) + 900
        for _no, line in r["lines"]:
            md = DONE.match(line)
            if md and r["start"]:
                r["end"] = r["start"] + int(md.group(1))
    return runs


BINA_OWNER_DONE = re.compile(r"^bina\.az: owner-checked \d+ new, announced (\d+)")


def analyse(run, listings):
    """Which alerts of this run Telegram did not take.

    Order inside a run (monitor.py): tap/lalafo/yeniemlak send each owner ad right after
    its check (tap logs "... -> OWNER" just before sending); bina first sends its new
    owner ads, then logs "bina.az: owner-checked N new, announced M", then logs every
    "price change" line and only THEN sends the price changes. So a failure in the bina
    part before that summary line was an owner-ad alert, after it a price change.
    Returns (lost, items, queued, gave_up, price_note)."""
    seg, lost, items, tap_open = "", {}, [], None
    queued, gave_up = [], []
    bina_phase = "owner"
    n_price, lost_price = 0, 0
    for _no, line in run["lines"]:
        for rx, name in SEGMENT:
            if rx.match(line):
                seg = name
        if BINA_OWNER_DONE.match(line):
            bina_phase = "price"
        mq, mg = QUEUED.match(line), GAVE_UP.match(line)
        if mq:
            queued.append(mq.group(1))
        if mg:
            gave_up.append(mg.group(1))
        mt = TAP_OWNER.match(line)
        if mt:
            tap_open = {"key": key_of_url(mt.group(1)), "url": mt.group(1), "source": "tap.az",
                        "what": "new owner ad", "verdict": "delivered"}
            items.append(tap_open)
            continue
        if TAP_ANY.match(line):
            tap_open = None
        mv = VIP.match(line)
        if mv:
            items.append({"key": key_of_url(mv.group(2)), "url": mv.group(2), "source": mv.group(1),
                          "what": "old owner ad (VIP/Premium)", "verdict": "?"})
            tap_open = None
        if PRICE.match(line):
            n_price += 1
        if FINAL_FAIL.match(line):
            if tap_open is not None and seg == "tap.az":
                tap_open["verdict"] = "LOST"       # failed right after this ad's check
                tap_open = None
            elif seg == "bina.az" and bina_phase == "price":
                lost_price += 1
            else:
                lost[seg or "?"] = lost.get(seg or "?", 0) + 1
    # new owner ads of the other sites: decided in this run (first_seen inside the run)
    have = {i["key"] for i in items}
    for k, rec in listings.items():
        if not isinstance(rec, dict) or rec.get("owner") is not True or k in have:
            continue
        t = to_ts(rec.get("first_seen"))
        if t is None or not (run["start"] - 5 <= t <= run["end"] + 5):
            continue
        src = rec.get("source", "?")
        if src == "tap.az":
            continue                               # tap ads are matched by their log line
        items.append({"key": k, "url": rec.get("url", k), "source": src, "what": "new owner ad",
                      "verdict": "?"})
    groups = {}
    for it in items:
        if it["verdict"] == "?":
            groups.setdefault(it["source"], []).append(it)
    for src, same in groups.items():
        n = lost.get(src, 0)
        for it in same:
            if n == 0:
                it["verdict"] = "delivered"
            elif n >= len(same):
                it["verdict"] = "LOST"
            else:
                it["verdict"] = f"maybe ({n} of these {len(same)} {src} owner alerts were lost)"
    price_note = ""
    if lost_price:
        price_note = (f"{lost_price} of {n_price} bina price-change alert(s) lost (not re-sent: "
                      "the price has moved on; the current price is in the ad)")
    return lost, items, queued, gave_up, price_note


def list_lost(st):
    listings = st.get("listings", {})
    if not os.path.exists(LOG):
        print(f"{LOG} not found")
        return []
    with open(LOG, encoding="utf-8", errors="replace") as fh:
        runs = read_runs(fh.readlines())
    out = []
    for run in runs:
        n_fail = sum(1 for _n, ln in run["lines"] if FINAL_FAIL.match(ln))
        has_q = any(QUEUED.match(ln) or GAVE_UP.match(ln) for _n, ln in run["lines"])
        if not n_fail and not has_q:
            continue
        lost, items, queued, gave_up, price_note = analyse(run, listings)
        when = baku(run["start"]) if run["start"] else "?"
        print("=" * 72)
        print(f"run {when} Baku ({run['ver']}), log line {run['first']}: "
              f"{n_fail} message(s) Telegram did not take")
        for _n, ln in run["lines"]:
            if FINAL_FAIL.match(ln):
                print(f"   line {_n}: {ln[:110]}")
        if queued:
            print(f"   kept in the outbox (sent again automatically): {', '.join(queued)}")
        if gave_up:
            print(f"   GAVE UP after many tries: {', '.join(gave_up)}")
        shown = [i for i in items if i["verdict"] != "delivered"]
        if price_note:
            print("   " + price_note)
        if not shown and not price_note and n_fail and not queued:
            print("   (no ad alert matched - probably a status message, e.g. a source warning)")
        elif not shown and n_fail and not queued:
            print("   no owner-ad alert lost in this run")
        for it in shown:
            if it["url"] in queued:
                continue
            rec = listings.get(it["key"]) or {}
            note = "  [re-sent already]" if rec.get("resent") else ""
            print(f"   {it['verdict']:9} {it['source']:13} {it['what']:30} {it['url']}{note}")
            if not rec.get("resent") and it["what"].startswith(("new owner", "old owner")):
                out.append(it)
    lost_ids = [i["key"].split(":")[-1] for i in out if i["verdict"] == "LOST"]
    maybe_ids = [i["key"].split(":")[-1] for i in out if i["verdict"].startswith("maybe")]
    print("=" * 72)
    if lost_ids or maybe_ids:
        print("Owner ads whose alert was lost: " + (" ".join(lost_ids) or "-"))
        if maybe_ids:
            print("Maybe lost (check Telegram): " + " ".join(maybe_ids))
        print('To re-send: RESEND="<ids>" /usr/bin/flock /tmp/binaizleme.lock '
              ".venv/bin/python lost_alerts.py   (after: set -a && . ./.env && set +a)")
    else:
        print("No lost owner-ad alerts found in the log.")
    return out


# --------------------------------------------------------------------------- #
# re-send
# --------------------------------------------------------------------------- #
def keys_for(arg):
    a = arg.strip()
    if "://" in a:
        return [key_of_url(a)]
    n = re.sub(r"\D", "", a)
    return [n, "tap:" + n, "lala:" + n, "ye:" + n]


def bina_page_listing(raw, iid):
    """price / rooms / area / floor / address / photo of a bina ad from its own page."""
    d = {}
    for mm in re.finditer(r'<script[^>]+type="application/ld\+json"[^>]*>(.*?)</script>', raw, re.S):
        try:
            data = json.loads(mm.group(1).strip())
        except Exception:
            continue
        if not isinstance(data, dict) or data.get("@type") != "Product":
            continue
        offers = data.get("offers") or {}
        try:
            d["price"] = int(float(offers.get("price")))
            d["currency"] = offers.get("priceCurrency") or "AZN"
        except (TypeError, ValueError):
            pass
        apt = offers.get("itemOffered") or {}
        if apt.get("numberOfRooms") is not None:
            d["rooms"] = apt["numberOfRooms"]
        fs = (apt.get("floorSize") or {}).get("value")
        if fs is not None:
            d["area"] = float(fs)
    head = r'"id"\s*:\s*"' + re.escape(iid) + r'"(?:[^{}]|\{[^{}]*\})*?'
    mm = re.search(head + r'"location"\s*:\s*\{[^{}]*"fullName"\s*:\s*"([^"]*)"', raw)
    if mm:
        d["location"] = mm.group(1)
    for k in ("floor", "floors"):
        mm = re.search(head + r'"' + k + r'"\s*:\s*(\d+)', raw)
        if mm:
            d[k] = int(mm.group(1))
    mo = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', raw)
    if mo:
        d["photo"] = mo.group(1)
    return d


def live_details(m, src, url, iid):
    try:
        if src == "tap.az":
            r, raw = m._fetch_tap_html(url, tries=2)
            if getattr(r, "status_code", 0) != 200:
                return {}
            d = m._tap_details(raw)
            pm = re.search(r'(https?://tap\.azstatic\.com/uploads/[^\s"\'<>]+\.(?:jpg|jpeg|png|webp))', raw)
            if pm:
                d["photo"] = pm.group(1)
            return d
        if src == "bina.az":
            r = m.http_get(url, headers=m.HTML_HEADERS, timeout=30, browser=True)
            if getattr(r, "status_code", 0) != 200:
                return {}
            return bina_page_listing(r.text or "", iid)
    except Exception as e:
        print(f"   page not read ({e}); the message is sent with what the bot saved")
    return {}


def resend(args):
    import time
    import monitor as m
    state, sha = m.load_state()
    seen = state["listings"]
    labels = {s_["name"]: s_.get("owner_label", "Əmlak sahibi") for s_ in m.SOURCES}
    box = state.setdefault("outbox", [])
    now_ts = time.time()
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    queued = 0
    for arg in args:
        key = next((k for k in keys_for(arg) if isinstance(seen.get(k), dict)), None)
        if key is None:
            print(f"{arg}: not in the bot's records - nothing to re-send")
            continue
        rec = seen[key]
        if rec.get("owner") is not True:
            print(f"{arg}: the bot did not judge it an owner ad (owner={rec.get('owner')!r}) - skipped")
            continue
        if rec.get("resent") and os.environ.get("FORCE") != "1":
            print(f"{arg}: already re-sent on {rec['resent'][:16]} - skipped (FORCE=1 to repeat)")
            continue
        src = rec.get("source", "")
        url = rec.get("url", "")
        iid = key.split(":")[-1]
        l = {"id": iid, "url": url}
        for k in ("price", "rooms", "area"):
            if rec.get(k) is not None:
                l[k] = rec[k]
        for k, v in live_details(m, src, url, iid).items():
            if v not in (None, ""):
                l[k] = v                       # the page is newer than our record
        fs = to_ts(rec.get("first_seen"))
        if fs is not None and (now_ts - fs) / 86400.0 > m.NEW_MAX_AGE_DAYS:
            l["_age_days"], l["_age_kind"], l["_posted_ts"] = (now_ts - fs) / 86400.0, "min", fs
        text = m.format_new_owner(l, src, labels.get(src, "Əmlak sahibi"))
        box.append({"text": text, "photo": l.get("photo") if m.SEND_PHOTOS else None,
                    "key": url, "n": 1, "t": now})
        rec["resent"] = now
        queued += 1
        print(f"{arg}: queued -> {url}")
        print("   " + text.replace("\n", "\n   "))
    if not queued:
        return
    if os.path.exists(m.STATE_FILE):
        shutil.copy(m.STATE_FILE, m.STATE_FILE + ".bak-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
    ok, how = m.save_state(state, sha)
    print(f"\n{queued} message(s) put in the outbox; seen.json saved={ok} ({how}). "
          "The next bot run sends them.")


def main():
    args = os.environ.get("RESEND", "").replace(",", " ").split()
    if args:
        resend(args)
        return
    with open(STATE, encoding="utf-8") as fh:
        st = json.load(fh)
    list_lost(st)
    box = st.get("outbox") or []
    if box:
        print(f"Outbox now: {len(box)} message(s) waiting: " + ", ".join(str(e.get('key')) for e in box))


if __name__ == "__main__":
    main()
