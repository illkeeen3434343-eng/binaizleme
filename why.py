#!/usr/bin/env python3
"""Why was a listing announced - or not? Read-only; sends nothing, changes nothing.

  cd /home/vboxuser/binaizleme
  .venv/bin/python why.py 48629527 https://bina.az/items/6463748 168545

Accepts plain ids or full links from tap.az, bina.az, lalafo.az, yeniemlak.az.
LIVE=1 also reads a bina ad's page now (seller label, text check, Çıxarış, location tags):
  set -a && . ./.env && set +a && LIVE=1 .venv/bin/python why.py https://bina.az/items/6616971
"""
import json
import os
import re
import sys

STATE = os.environ.get("STATE_FILE", "seen.json")
LOG = os.environ.get("RUN_LOG", "run.log")


def keys_for(arg):
    a = arg.strip()
    if "tap.az" in a:
        return ["tap:" + re.findall(r"(\d+)", a)[-1]]
    if "bina.az" in a:
        return [re.findall(r"(\d+)", a)[-1]]
    if "lalafo.az" in a:
        m = re.search(r"-id-(\d+)", a)
        return ["lala:" + (m.group(1) if m else re.findall(r"(\d+)", a)[-1])]
    if "yeniemlak.az" in a:
        return ["ye:" + re.findall(r"(\d+)", a)[-1]]
    n = re.sub(r"\D", "", a)
    return [n, "tap:" + n, "lala:" + n, "ye:" + n]


def explain(rec):
    if rec.get("recovered"):
        return f"SENT by recover_missed.py on {rec['recovered'][:10]} (was wrongly filed earlier)"
    if rec.get("owner") is True:
        return "SENT as an owner post (never sent twice)"
    if rec.get("owner") is False:
        why = rec.get("skip") or "judged an agent (see the log line below for the reasons)"
        extra = "; re-checked by recover_missed.py" if rec.get("rechecked") else ""
        return f"NOT SENT - {why}{extra}"
    if "owner" in rec and rec.get("owner") is None:
        t = rec.get("tries", rec.get("owner_tries", 0))
        return (f"UNDECIDED after {t} read(s) - the page had no clear seller signal"
                + ("; retried up to 4 reads" if t < 4 else "; gave up"))
    if str(rec.get("skip", "")).startswith("area"):
        how = {"area": f"its tags [{rec.get('tags', '')}] have no wanted area",
               "area-old": "it was already old when the tag check was added (2026-11-12-a)",
               "area-unknown": "its tags could not be read after 4 tries"}.get(rec["skip"], "")
        return f"NOT SENT - bina shows it in an excluded location (Koroğlu, Ağ şəhər, ...): {how}"
    if rec.get("tags_ok"):
        return (f"KEPT although bina shows it in an excluded location: tags [{rec.get('tags', '')}] "
                "include a wanted area; no owner verdict stored yet")
    if str(rec.get("skip", "")).startswith("old"):
        return ("HIDDEN by 2026-10-18-a as 'not new' (that rule was wrong). From 2026-11-07-a "
                "it is checked and, if an owner ad, sent once - marked old - the next time "
                "it appears in the search")
    base = ("RECORDED SILENTLY (first run, search-URL change, or no seller label) - never sent. "
            "It is sent once, marked old, if its owner renews it (yeniemlak/lalafo) or puts "
            "it on VIP/Premium (bina)")
    if rec.get("price_history") is not None:
        base += "; bina also tracks its price"
    return base


def live_bina(url):
    """LIVE=1: read the bina ad's page now and show what decides it (read-only)."""
    import html as H
    import monitor as m
    try:
        r = m.http_get(url, headers=m.HTML_HEADERS, timeout=30, browser=True)
    except Exception as e:
        print(f"  live: page not read ({e})")
        return
    raw = r.text or ""
    print(f"  live: HTTP {r.status_code}, {len(raw)} bytes")
    if r.status_code != 200:
        return
    text = re.sub(r"\s+", " ", H.unescape(re.sub(r"<[^>]+>", " ", raw)))
    for label in ("Çıxarış", "Təmir", "İpoteka", "Kateqoriya", "Mərtəbə", "Sahə", "Otaq sayı"):
        mm = re.search(re.escape(label) + r"\s*:?\s*(.{0,28})", text)
        if mm:
            print(f"  live: {label}: {mm.group(1).strip()!r}")
    title = m._meta_tags(raw).get("og:title")
    print(f"  live: title: {title}")
    m._BINA_PAGE_CACHE[url] = raw
    v, _p = m.bina_is_owner(url)
    print(f"  live: seller verdict: {v} (True=owner, False=agent) | text check: {m.bina_is_owner.last_text}")
    groups = []
    for x in m.BINA_EXCLUDE_LOCATIONS + [title or ""]:
        t = m._bina_tags_from_html(raw, x)
        if t and t not in groups:
            groups.append(t)
    print(f"  live: location tags: {groups or 'not found'}")
    for t in groups:
        print(f"  live: wanted tag: {m._bina_wanted_tag(t)} | excluded words in tags: "
              f"{[x for x in m.BINA_EXCLUDE_LOCATIONS if any(x in y for y in t)]}")
    print("  live: the search asks for: Çıxarış var (has_bill_of_sale) and the areas in "
          "BINA_SEARCH_URL; an ad without Çıxarış, or outside those areas, never reaches the bot")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return
    st = json.load(open(STATE, encoding="utf-8"))
    L = st.get("listings", {})
    tries = st.get("owner_tries", {})
    log_lines = []
    if os.path.exists(LOG):
        with open(LOG, encoding="utf-8", errors="replace") as fh:
            log_lines = fh.readlines()
    for arg in sys.argv[1:]:
        print("=" * 64)
        print(arg)
        found = False
        for k in keys_for(arg):
            rec = L.get(k)
            if rec is None:
                continue
            found = True
            print(f"  record [{k}]: {json.dumps(rec, ensure_ascii=False)}")
            print(f"  => {explain(rec)}")
        num = re.sub(r"\D", "", keys_for(arg)[0])
        if not found:
            if num in tries:
                print(f"  => PENDING (bina): seller label unread {tries[num]} time(s); retried next run")
            else:
                print("  => NEVER RECORDED: the bot did not receive it from the search. "
                      "Usual causes: outside the search URL's area, tap heading without a "
                      "wanted place name, tap ad pushed off page 1 between runs (tap only "
                      "serves page 1), bina excluded area (Koroğlu / Ağ şəhər / Şah İsmayıl "
                      "Xətai), or still waiting in the per-run check budget.")
        hits = [ln.rstrip() for ln in log_lines if num and num in ln][-8:]
        if hits:
            print("  log:")
            for h in hits:
                print("    " + h[:220])
        if os.environ.get("LIVE") == "1" and "bina.az" in arg:
            live_bina(arg.strip())


if __name__ == "__main__":
    main()
