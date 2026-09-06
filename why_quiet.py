#!/usr/bin/env python3
# Why is the bot silent? Checks runs, Telegram delivery, and the newest listings' verdicts.
#   cd /home/vboxuser/binaizleme && set -a && . ./.env && set +a
#   /usr/bin/flock /tmp/binaizleme.lock .venv/bin/python why_quiet.py
import os, re, json, datetime as dt
import monitor as m

N = int(os.environ.get("N", "20"))          # how many newest unseen listings to check
print("VERSION loaded:", m.VERSION)

# --- 1. did runs happen, and what did they announce? ---
print("\n=== LAST 8 RUNS ===")
try:
    tail = open("run.log", encoding="utf-8", errors="replace").readlines()[-6000:]
    done = [l.strip() for l in tail if l.startswith("Done.")][-8:]
    for d in done:
        mm = re.search(r"notified=(\d+).*records_before=(\d+).*records_after=(\d+)", d)
        print("  ", (f"notified={mm.group(1):>3}  +{int(mm.group(3))-int(mm.group(2)):>4} records"
                     if mm else d[:90]))
    errs = [l.strip() for l in tail if ("Traceback" in l or "Error" in l or "error" in l)][-6:]
    print("  recent errors:", len(errs))
    for e in errs: print("     ", e[:110])
except Exception as e:
    print("  cannot read run.log:", e)

# --- 2. is Telegram actually delivering? ---
print("\n=== TELEGRAM ===")
tok, chat = (m.BOT_TOKEN or ""), (m.CHAT_ID or "")
print("  token set:", bool(tok), "| chat set:", bool(chat))
if "\r" in tok or "\r" in chat or tok != tok.strip() or chat != chat.strip():
    print("  !! token/chat contains stray whitespace or \\r  -> run: sed -i 's/\\r$//' .env")
ok = m.tg_send(f"health probe {dt.datetime.now().strftime('%H:%M')} - if you see this, delivery works")
print("  test message sent:", ok)

# --- 3. what is the pipeline doing with the newest listings? ---
print("\n=== NEWEST BINA LISTINGS: VERDICTS ===")
st, _ = m.load_state(); L = st["listings"]
c = m.bina_check(m.BINA_SEARCH_URL)
items = m.fetch_bina(m.BINA_SEARCH_URL)
unseen = [l for l in items if str(l["id"]) not in L]
unseen.sort(key=lambda l: int(l["id"]) if str(l["id"]).isdigit() else 0, reverse=True)
print(f"  fetched {len(items)} | not in seen.json: {len(unseen)}")
print(f"  newest id this scan: {m._BINA_MAX_ID['v']}")
if not unseen:
    print("  -> nothing new on the site since the last run. Silence is CORRECT.")
tallies = {}
for l in unseen[:N]:
    age = m._listing_age_days(l)
    v, _p = m.bina_is_owner(l["url"])
    if m.is_stale_promoted(l):        reason = "promoted+old -> ignored"
    elif v is True:                   reason = "OWNER -> should announce"
    elif v is False:                  reason = "agent -> skipped"
    else:                             reason = "undetermined -> retry"
    tallies[reason] = tallies.get(reason, 0) + 1
    print(f"   {l['id']}  ~{(age if age is not None else -1):5.1f}d  "
          f"promo={str(l.get('_promoted')):5}  {reason}")
print("\n  tally:", tallies)
