#!/usr/bin/env bash
# Audit every place a scheduled monitor run can hide. Read-only, changes nothing.
echo "=============================================================="
echo " 1. YOUR CRONTAB"
echo "=============================================================="
crontab -l 2>/dev/null | grep -vE '^\s*(#|$)' | nl -ba || echo "  (no crontab)"
N=$(crontab -l 2>/dev/null | grep -vE '^\s*#' | grep -c 'monitor\.py')
echo
echo "  monitor.py lines: $N"
if [ "$N" -gt 1 ]; then
  echo "  !! MORE THAN ONE monitor.py CRON LINE - this causes duplicate runs"
elif [ "$N" -eq 1 ]; then
  echo "  OK - exactly one"
else
  echo "  !! NONE - the bot is not scheduled at all"
fi
echo "  lines WITHOUT flock (unprotected, can overlap):"
crontab -l 2>/dev/null | grep 'monitor\.py' | grep -v 'flock' | sed 's/^/     /' || true
crontab -l 2>/dev/null | grep 'monitor\.py' | grep -v 'flock' -q || echo "     (none - all protected)"

echo
echo "=============================================================="
echo " 2. ROOT / SYSTEM CRON"
echo "=============================================================="
sudo -n crontab -l 2>/dev/null | grep -vE '^\s*(#|$)' | sed 's/^/  root: /' \
  || echo "  (root crontab not readable without password - check manually: sudo crontab -l)"
for f in /etc/crontab /etc/cron.d/*; do
  [ -f "$f" ] && grep -lE 'monitor|binaizleme' "$f" 2>/dev/null | sed 's/^/  found in: /'
done
for d in /etc/cron.hourly /etc/cron.daily /etc/cron.weekly; do
  ls "$d" 2>/dev/null | grep -iE 'monitor|bina' | sed "s|^|  $d/|"
done
echo "  (blank above = nothing system-wide)"

echo
echo "=============================================================="
echo " 3. SYSTEMD TIMERS"
echo "=============================================================="
systemctl list-timers --all 2>/dev/null | grep -iE 'monitor|bina' | sed 's/^/  /' \
  || echo "  none"
systemctl --user list-timers --all 2>/dev/null | grep -iE 'monitor|bina' | sed 's/^/  user: /'
echo "  (blank = no timers)"

echo
echo "=============================================================="
echo " 4. RUNNING RIGHT NOW"
echo "=============================================================="
# match only real interpreter invocations, not this script or its shell
ps -eo pid,etime,cmd | grep -E '(python[0-9.]*|\.venv/bin/python).*monitor\.py' \
  | grep -v grep | sed 's/^/  /'
C=$(ps -eo cmd | grep -E '(python[0-9.]*|\.venv/bin/python).*monitor\.py' | grep -vc grep)
[ "$C" -eq 0 ] && echo "  no monitor.py process running"
echo "  concurrent monitor.py processes: $C"
[ "$C" -gt 1 ] && echo "  !! MORE THAN ONE RUNNING AT THE SAME TIME"

echo
echo "=============================================================="
echo " 5. LOCK FILE"
echo "=============================================================="
if [ -e /tmp/binaizleme.lock ]; then
  echo "  lock exists: /tmp/binaizleme.lock"
  if command -v fuser >/dev/null 2>&1; then
    fuser -v /tmp/binaizleme.lock 2>&1 | sed 's/^/  /'
  elif command -v lsof >/dev/null 2>&1; then
    lsof /tmp/binaizleme.lock 2>/dev/null | sed 's/^/  /' || echo "  (held by nobody - idle)"
  else
    echo "  (install psmisc for holder details: sudo apt install -y psmisc)"
  fi
  if flock -n 9 9>>/tmp/binaizleme.lock 2>/dev/null; then
    echo "  lock is FREE -> no run in progress"
  else
    echo "  lock is HELD -> a run is in progress right now"
  fi
else
  echo "  no lock file yet (created on first flock run)"
fi

echo
echo "=============================================================="
echo " 6. OTHER COPIES OF monitor.py ON DISK"
echo "=============================================================="
find /home /opt /srv /root -maxdepth 4 -name 'monitor.py' 2>/dev/null | sed 's/^/  /'
echo
echo "=============================================================="
echo " 7. RUN CADENCE (are runs spaced as expected?)"
echo "=============================================================="
grep -c '^Done\.' run.log 2>/dev/null | sed 's/^/  completed runs in log: /'
echo "  last 10 run start times:"
grep -n 'starting' run.log 2>/dev/null | tail -10 | sed 's/^/     /'
echo
echo "  NOTE: also check GitHub -> Actions tab. .github/workflows/monitor.yml still"
echo "  exists with a cron schedule; if it is enabled it is a SECOND bot on a"
echo "  datacenter IP writing to the repo seen.json."
