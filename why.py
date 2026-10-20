#!/usr/bin/env python3
"""Why was a listing announced - or not? Read-only; sends nothing, changes nothing.

  cd /home/vboxuser/binaizleme
  .venv/bin/python why.py 48629527 https://bina.az/items/6463748 168545

Accepts plain ids or full links from tap.az, bina.az, lalafo.az, yeniemlak.az.
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
    if "skip" in rec and str(rec["skip"]).startswith("old"):
        return f"NOT ANNOUNCED - not a new ad ({rec['skip']}): an old ad lifted by bump/VIP/edit"
    if rec.get("owner") is True:
        return "ANNOUNCED as an owner post"
    if rec.get("owner") is False:
        why = rec.get("skip") or "judged an agent (see the tap/lalafo log line below)"
        return f"NOT ANNOUNCED - {why}"
    if "owner" in rec and rec.get("owner") is None:
        return (f"UNDECIDED after {rec.get('tries', 0)} read(s) - page had no clear "
                "seller signal; retried until 4 reads")
    if rec.get("price_history") is not None:
        return ("RECORDED by the bina price tracker without an owner verdict (first run, "
                "URL change, or 'no seller label after 4 reads')")
    return ("RECORDED SILENTLY - seeded on a first run / search-URL change, never "
            "announced by design")


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


if __name__ == "__main__":
    main()
