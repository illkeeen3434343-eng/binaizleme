#!/usr/bin/env python3
# Assess all seen.json backups, then restore the newest VALID one atomically.
# Run under the monitor lock:
#   cd /home/vboxuser/binaizleme && set -a && . ./.env && set +a
#   /usr/bin/flock /tmp/binaizleme.lock .venv/bin/python restore_seen.py
import json, glob, gzip, os, shutil, datetime as dt

def count(path, gz=False):
    try:
        op = gzip.open(path, "rt", encoding="utf-8") if gz else open(path, encoding="utf-8")
        with op as fh:
            d = json.load(fh)
        return len(d.get("listings", {})) if isinstance(d, dict) else None
    except Exception:
        return None

# --- disk + big files (a truncated write usually means the disk filled up) ---
sv = os.statvfs(".")
print(f"disk: {sv.f_bavail*sv.f_frsize/1e6:.0f} MB free of {sv.f_blocks*sv.f_frsize/1e6:.0f} MB")
print("large files:")
for p in ["seen.json", "run.log"] + sorted(glob.glob("seen.json.bak-*")) \
         + sorted(glob.glob("seen.json.CORRUPT-*")) + sorted(glob.glob("backups/*")):
    if os.path.exists(p):
        print(f"   {os.path.getsize(p)/1e6:8.2f} MB  {p}")

cur_sz = os.path.getsize("seen.json") if os.path.exists("seen.json") else -1
print(f"\ncurrent seen.json: size={cur_sz} bytes, records={count('seen.json')}")

cands  = [(os.path.getmtime(p), "plain", p) for p in glob.glob("seen.json.bak-*")]
cands += [(os.path.getmtime(p), "gz",    p) for p in glob.glob("backups/seen-*.json.gz")]
cands.sort(reverse=True)

print("\ncandidate backups (newest first):")
best = None
for mtime, kind, p in cands:
    n = count(p, gz=(kind == "gz"))
    ts = dt.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
    tag = ""
    if best is None and isinstance(n, int) and n > 0:
        best = (kind, p, n); tag = "   <== newest valid, will restore"
    print(f"   {ts}  {kind:5} records={n}  {p}{tag}")

if not best:
    print("\nNO VALID BACKUP FOUND. Leave cron stopped and do not let the bot re-seed.")
    raise SystemExit(1)

kind, p, n = best
if cur_sz >= 0:
    shutil.copy("seen.json", "seen.json.CORRUPT-" + dt.datetime.now().strftime("%Y%m%d-%H%M%S"))
tmp = "seen.json.restore.tmp"
if kind == "gz":
    with gzip.open(p, "rt", encoding="utf-8") as fin, open(tmp, "w", encoding="utf-8") as fo:
        shutil.copyfileobj(fin, fo)
else:
    shutil.copy(p, tmp)

if count(tmp) and count(tmp) > 0:
    os.replace(tmp, "seen.json")     # atomic swap
    print(f"\nRESTORED seen.json from {p}  ({n} records).")
    print("The corrupt file is kept as seen.json.CORRUPT-* for forensics.")
else:
    os.path.exists(tmp) and os.remove(tmp)
    print("\nRestore candidate failed validation; seen.json left untouched.")
    raise SystemExit(1)
