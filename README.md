# binaizleme

Apartment monitor for Baku that sends Telegram alerts.

| Source | What it announces |
|---|---|
| tap.az, lalafo.az, yeniemlak.az | new posts by the **owner** (agents/makler filtered out) |
| bina.az | new owner posts **and** price changes (up or down) on every matching listing |

## How it runs

The bot runs on the VM from cron. GitHub only carries the code to the VM and
runs the offline tests (`.github/workflows/tests.yml`); it does not run the bot.

```bash
cd /home/vboxuser/binaizleme
git pull                                  # get the new code
set -a && . ./.env && set +a              # TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, overrides
/usr/bin/flock -n /tmp/binaizleme.lock .venv/bin/python monitor.py >> run.log 2>&1
```

Example crontab line (every 5 minutes; `flock -n` skips a tick while a run is
still going):

```
*/5 * * * * cd /home/vboxuser/binaizleme && set -a && . ./.env && set +a && /usr/bin/flock -n /tmp/binaizleme.lock .venv/bin/python monitor.py >> run.log 2>&1
```

Memory is the local `seen.json` (written atomically). The copy of `seen.json`
in the repository is an old snapshot. Do not overwrite the VM's file with it.

## Run time: quick and full scans

Reading all ~830 bina pages takes about 10 minutes. bina sorts by
`BUMPED_AT_DESC`, so new and bumped listings are at the top. Most runs therefore
read pages only until a page has nothing unknown on it (at least
`QUICK_MIN_PAGES`). A full pass runs every `FULL_SCAN_EVERY_MIN` for price
changes on older listings. Runs are short, so tap.az (which only shows page 1)
is read far more often.

## Settings (env, all optional)

| Variable | Default | Meaning |
|---|---|---|
| `FULL_SCAN_EVERY_MIN` | `60` | minutes between full passes; `0` = always full (old behaviour) |
| `QUICK_MIN_PAGES` | `3` | pages a quick scan always reads |
| `QUICK_SCAN_SOURCES` | `bina.az` | sources allowed to quick-scan (add `yeniemlak.az` / `lalafo.az` only if their feed is newest-first) |
| `BINA_PAGE_TRIES` | `3` | attempts per bina page before the run gives up on bina |
| `TG_TRIES` | `3` | Telegram attempts (429 `retry_after` is honoured) |
| `OUTBOX_MAX_AGE_H` | `48` | undelivered alerts are re-sent on later runs for this long |
| `SCAN_PAGES` | `1000` | cap on bina pages per full pass (one Telegram warning if reached) |
| `ENABLED_SOURCES` | all | e.g. `bina.az,tap.az` |
| `BINA_PERSISTED_HASH` | built in | refresh from the browser Network tab when bina reports "signature expired" |

The detection rules (keywords, owner/agent phrases, age limits) are described in
the comments of `monitor.py` next to each setting.

## Tests

```bash
.venv/bin/python test_pipeline.py      # whole pipeline, every site faked
.venv/bin/python test_owner_detect.py  # Azerbaijani owner/agent wording
```

Neither test needs network access or sends anything.

## Diagnostics (read-only unless noted)

`health.py` (is it alive), `why.py <id|url>...` (why a listing was or was not
announced), `check_sources.py`, `why_quiet.py`, `check_crons.sh`.
Repair scripts are dry-run by default: `catchup_bina.py`, `recheck_unowned.py`,
`resweep_tap.py`, `seed_visible.py` (`SEND=1` / `APPLY=1` to apply),
`restore_seen.py`.
