#!/usr/bin/env python3
# Is the bot alive and healthy? Prints a report; TG=1 also sends it to Telegram.
#   cd /home/vboxuser/binaizleme && set -a && . ./.env && set +a && .venv/bin/python health.py
import os, re, json, subprocess, datetime as dt
import monitor as m

LOG = os.environ.get("RUN_LOG", "run.log")
STALE_MIN = int(os.environ.get("STALE_MIN", "90"))     # alert if no run in this many minutes
now = dt.datetime.now(dt.timezone.utc)
issues, lines = [], []

def add(k, v): lines.append(f"{k}: {v}")

# --- last run, from the log ---
last_done = last_start = None
try:
    with open(LOG, encoding="utf-8", errors="replace") as fh:
        tail = fh.readlines()[-4000:]
    for ln in tail:
        if "starting" in ln and "monitor.py" in ln:
            last_start = ln.strip()
        if ln.startswith("Done."):
            last_done = ln.strip()
    mt = dt.datetime.fromtimestamp(os.path.getmtime(LOG), dt.timezone.utc)
    age = (now - mt).total_seconds() / 60
    add("log last written", f"{age:.0f} min ago")
    if age > STALE_MIN:
        issues.append(f"no log activity for {age:.0f} min (cron stopped? run stuck?)")
except Exception as e:
    issues.append(f"cannot read {LOG}: {e}")

add("version running", (re.search(r"monitor\.py (\S+) starting", last_start).group(1)
                        if last_start else "unknown"))
add("last completed run", last_done or "none found")
if last_done and "saved=True" not in last_done:
    issues.append("last run did NOT save state")

# --- per-source status from the last Done line ---
if last_done:
    mm = re.search(r"status=(\{.*\})", last_done)
    if mm:
        try:
            st = eval(mm.group(1), {"__builtins__": {}}, {})
            for k, v in st.items():
                add(f"  source {k}", v or "ok")
                if v and v != "ok":
                    issues.append(f"{k}: {v}")
        except Exception:
            pass

# --- state file ---
try:
    sz = os.path.getsize(m.STATE_FILE)
    with open(m.STATE_FILE, encoding="utf-8") as fh:
        d = json.load(fh)
    n = len(d.get("listings", {}))
    add("seen.json", f"{n} records, {sz/1e6:.1f} MB")
    if n == 0:
        issues.append("seen.json has 0 records")
except Exception as e:
    issues.append(f"seen.json unreadable: {e}")

# --- disk & cron ---
try:
    sv = os.statvfs(".")
    free = sv.f_bavail * sv.f_frsize / 1e9
    add("disk free", f"{free:.2f} GB")
    if free < 0.5:
        issues.append(f"low disk: {free:.2f} GB free")
except Exception:
    pass
try:
    cr = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10).stdout
    add("cron entries", str(len([x for x in cr.splitlines()
                                 if x.strip() and not x.strip().startswith("#")])))
    if "monitor.py" not in cr:
        issues.append("no monitor.py line in crontab")
    if "backup.sh" not in cr:
        issues.append("no weekly backup.sh line in crontab")
except Exception:
    pass

head = "OK - bot healthy" if not issues else f"PROBLEM ({len(issues)})"
report = head + "\n" + "\n".join(lines)
if issues:
    report += "\n\nIssues:\n" + "\n".join("- " + i for i in issues)
print(report)

if os.environ.get("TG") == "1":
    try:
        m.tg_send(report)
        print("\n(sent to Telegram)")
    except Exception as e:
        print("telegram send failed:", e)
raise SystemExit(1 if issues else 0)
