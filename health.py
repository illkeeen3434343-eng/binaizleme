#!/usr/bin/env python3
# Is the bot alive and healthy? Prints a report; TG=1 also sends it to Telegram.
#   cd /home/vboxuser/binaizleme && set -a && . ./.env && set +a && .venv/bin/python health.py
#
# 2026-11-07-b:
#  * TG=1 never worked: it called m.tg_send(), which does not exist (the error was
#    printed, not sent). Now it uses tg_send_message and says if Telegram refused it.
#    HEALTH_CHAT_ID (optional) sends the report to a different chat than the alerts.
#  * "Source ok" only meant the search page was read. It did not show that yeniemlak
#    refused every detail page (HTTP 429), so no owner check finished. The report now
#    reads the last 24 h of run.log: owner posts sent per source, detail pages checked,
#    rate limits and paused checks.
import datetime as dt
import html
import json
import os
import re
import subprocess

import monitor as m

LOG = os.environ.get("RUN_LOG", "run.log")
STALE_MIN = int(os.environ.get("STALE_MIN", "90"))     # alert if no run in this many minutes
WINDOW_H = float(os.environ.get("HEALTH_WINDOW_H", "24"))
MIN_RUNS = int(os.environ.get("HEALTH_MIN_RUNS", "20"))  # judge "never checked" only with enough runs
CHECKED_SOURCES = ("tap.az", "yeniemlak.az", "bina.az")   # sources that read detail pages

_START = re.compile(r"monitor\.py (\S+) starting (\S+)")
_COUNTS = re.compile(r"^(\S+): (?:owner-)?checked (\d+) new, announced (\d+)")
_LIMITED = re.compile(r"^(\S+): RATE-LIMITED")
_PAUSED = re.compile(r"^(\S+): \d+ detail pages in a row unreadable")


def scan_runs(lines, now, hours=WINDOW_H):
    """Per-source totals over the runs that STARTED in the last `hours`."""
    runs, cur = [], None
    for ln in lines:
        mm = _START.search(ln)
        if mm:
            t = m._to_epoch(mm.group(2))
            cur = {"t": t, "done": False, "limited": set(), "paused": set(), "counts": {}}
            runs.append(cur)
            continue
        if cur is None:
            continue
        s = ln.strip()
        if s.startswith("Done."):
            cur["done"] = True
        mm = _COUNTS.match(s)
        if mm:
            c, a = cur["counts"].get(mm.group(1), (0, 0))
            cur["counts"][mm.group(1)] = (c + int(mm.group(2)), a + int(mm.group(3)))
        mm = _LIMITED.match(s)
        if mm:
            cur["limited"].add(mm.group(1))
        mm = _PAUSED.match(s)
        if mm:
            cur["paused"].add(mm.group(1))
    cutoff = now.timestamp() - hours * 3600
    recent = [r for r in runs if r["t"] is not None and r["t"] >= cutoff]
    out = {"runs": len(recent), "done": sum(1 for r in recent if r["done"]),
           "checked": {}, "announced": {}, "limited": {}, "paused": {}}
    for r in recent:
        for src, (c, a) in r["counts"].items():
            out["checked"][src] = out["checked"].get(src, 0) + c
            out["announced"][src] = out["announced"].get(src, 0) + a
        for h in r["limited"]:
            out["limited"][h] = out["limited"].get(h, 0) + 1
        for src in r["paused"]:
            out["paused"][src] = out["paused"].get(src, 0) + 1
    return out


def activity_issues(act, sources=CHECKED_SOURCES):
    """Problems a 'source ok' status does not show."""
    issues = []
    n = act["runs"]
    for host, k in sorted(act["limited"].items()):
        if k >= 3:
            issues.append(f"{host} refused the bot (HTTP 429, rate limit) in {k} of {n} runs "
                          f"in the last {WINDOW_H:g} h")
    for src, k in sorted(act["paused"].items()):
        if k >= 3:
            issues.append(f"{src}: detail pages unreadable (owner checks paused) in {k} of {n} runs")
    if n >= MIN_RUNS:
        for src in sources:
            if act["checked"].get(src, 0) == 0:
                issues.append(f"{src}: not a single listing could be owner-checked in "
                              f"{n} runs ({WINDOW_H:g} h)")
    return issues


def main():
    now = dt.datetime.now(dt.timezone.utc)
    issues, lines = [], []

    def add(k, v):
        lines.append(f"{k}: {v}")

    # --- last run, from the log ---
    last_done = last_start = None
    tail = []
    try:
        with open(LOG, encoding="utf-8", errors="replace") as fh:
            tail = fh.readlines()[-40000:]
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

    vm = re.search(r"monitor\.py (\S+) starting", last_start) if last_start else None
    add("version running", vm.group(1) if vm else "unknown")
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

    # --- what the bot actually did in the last 24 h ---
    if tail:
        act = scan_runs(tail, now)
        add(f"runs in last {WINDOW_H:g} h", f"{act['runs']} started, {act['done']} finished")
        srcs = sorted(set(act["announced"]) | set(act["checked"]))
        for src in srcs:
            add(f"  {src}", f"sent {act['announced'].get(src, 0)} owner post(s), "
                            f"owner-checked {act['checked'].get(src, 0)}")
        for host, k in sorted(act["limited"].items()):
            add(f"  rate-limited {host}", f"{k} run(s)")
        issues += activity_issues(act)

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
        chat = os.environ.get("HEALTH_CHAT_ID", "").strip()
        if chat:
            m.CHAT_ID = chat
        ok = m.tg_send_message("🩺 <b>binaizleme health</b>\n<pre>" + html.escape(report) + "</pre>")
        print("\n(sent to Telegram)" if ok else "\nTELEGRAM SEND FAILED (see the line above)")
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
