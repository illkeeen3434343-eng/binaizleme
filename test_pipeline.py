# -*- coding: utf-8 -*-
"""Regression tests for the 2026-10-18 review. Every case comes from a real listing
or a confirmed code path. Run on the VM:   .venv/bin/python test_pipeline.py
Needs no network and sends nothing (all fetchers and Telegram are replaced)."""
import contextlib
import importlib.util
import io
import json
import re
import os
import sys
import tempfile
import time

# Run with the bot's DEFAULT settings and dummy tokens, whatever the shell exported.
# After `set -a && . ./.env` the shell carries your live settings (11 Nov 2026:
# TAP_PROFILE_BROWSER=1 / TAP_COUNT_SCOPE=property made 7 checks fail and opened real
# tap.az profiles), and real tokens must never reach Telegram or GitHub from a test.
def _isolate_env(here):
    keys = {"GH_TOKEN", "GH_REPO", "GITHUB_TOKEN", "SEND", "SOURCES", "TG"}
    for fn in ("monitor.py", "recover_missed.py", "health.py", "collect_bina.py", "why.py",
               "lost_alerts.py"):
        try:
            src = open(os.path.join(here, fn), encoding="utf-8").read()
        except OSError:
            continue
        for pat in (r'os\.environ\.get\(\s*"([A-Z0-9_]+)"', r'os\.getenv\(\s*"([A-Z0-9_]+)"',
                    r'os\.environ\[\s*"([A-Z0-9_]+)"\s*\]'):
            keys |= set(re.findall(pat, src))
    for k in keys:
        os.environ.pop(k, None)
    os.environ["TELEGRAM_BOT_TOKEN"] = "test-token-not-real"
    os.environ["TELEGRAM_CHAT_ID"] = "0"


HERE = os.path.dirname(os.path.abspath(__file__))
_isolate_env(HERE)
sys.path.insert(0, HERE)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


m = load(os.path.join(HERE, "monitor.py"), "monitor")
V = {True: "OWNER", False: "AGENT", None: "DEFER"}
FAILS = []


def check(cond, label, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {label}" + (f"   [{detail}]" if detail else ""))
    if not cond:
        FAILS.append(label)


class R:
    def __init__(self, text, code=200):
        self.text, self.status_code = text, code


# --------------------------------------------------------------------------- #
# Fake tap pages. GENERAL_FEED is what tap really returns for
#   /elanlar/dasinmaz-emlak/menziller?user_id=<anything>   (user_id ignored):
# the first page of ALL Baku apartments - 28 cards (verified 18 Oct 2026).
# --------------------------------------------------------------------------- #
GENERAL_FEED = "<html>" + "".join(
    f'<a href="/elanlar/dasinmaz-emlak/menziller/{48900000 + i}">x</a>' for i in range(28)
) + "<div>6711 elan</div></html>" + "x" * 3000
JS_PROFILE = "<html><h1>İstifadəçinin elanları</h1><div id=app></div></html>" + "x" * 3000


def tap_ad(desc, seller="Rəşad", uid="22702915", multi=True):
    marker = "İstifadəçinin bütün elanları" if multi else ""
    return ('<html><nav>Ana səhifə / Daşınmaz əmlak</nav>'
            f'<meta property="og:description" content="{desc}">'
            f'<div class="author"><span>{seller}</span>'
            f'<a href="/elanlar?user_id={uid}">{marker}</a></div></html>' + "x" * 3000)


def install_tap(mod, ad_html):
    calls = []

    def fake(url, tries=3):
        calls.append(url)
        if "user_id=" in url and "/menziller?" in url:
            return R(GENERAL_FEED), GENERAL_FEED        # what tap actually serves
        if "user_id=" in url:
            return R(JS_PROFILE), JS_PROFILE            # JS-rendered, no cards
        return R(ad_html), ad_html
    mod._fetch_tap_html = fake
    mod._tap_blocked = lambda b: False
    return calls


AD_48628115 = tap_ad("Neftçilər metrosuna 5 dəqiqə. Maklerlər yazmasın. WhatsApp aktivdir")
OLD_PATH = os.path.join(HERE, "monitor.before.py")      # optional: the 2026-09-08-c file
old = load(OLD_PATH, "monitor_before") if os.path.exists(OLD_PATH) else None
OLD_V = getattr(old, "VERSION", "") if old is not None else ""
# Each "OLD code" proof only applies to the version that had that bug.
if old is not None and OLD_V < "2026-10-18-a":
    print("\n[C1] tap profile count - the production bug, reproduced on the OLD code")
    install_tap(old, AD_48628115)
    v_old, _, _ = old.tap_check_owner("https://tap.az/elanlar/dasinmaz-emlak/menziller/48628115", {}, {})
    check(v_old is False, "OLD code: 48628115 ('Maklerlər yazmasın') wrongly -> AGENT", V[v_old])

calls = install_tap(m, AD_48628115)
v, _, _ = m.tap_check_owner("https://tap.az/elanlar/dasinmaz-emlak/menziller/48628115", {}, {})
check(v is True, "NEW code: 48628115 -> OWNER", V[v])
check(not any("/menziller?user_id=" in c for c in calls),
      "NEW code never requests the user_id-ignoring category URL", str(calls[1:]))

print("\n[C1] acceptance cases with 'İstifadəçinin bütün elanları' (unrelated other ads)")
for lid, desc in [("48628082", "3 otaqlı mənzil satılır, təmirli"),
                  ("48407292", "Salam ev orta temirlidi Melhem hospitalın yanında Kupçası var"),
                  ("48625621", "Mənzil satılır, sənədləri qaydasındadır")]:
    install_tap(m, tap_ad(desc))
    v, _, _ = m.tap_check_owner(f"https://tap.az/elanlar/dasinmaz-emlak/menziller/{lid}", {}, {})
    check(v is True, f"{lid}: multi-listing seller, neutral text -> not AGENT", V[v])

print("\n[C1] agents are still caught")
install_tap(m, tap_ad("Təcili satılır. OFİS HAQQI 1% təşkil edir"))
v, _, _ = m.tap_check_owner("https://tap.az/elanlar/dasinmaz-emlak/menziller/48678538", {}, {})
check(v is False, "48678538 'OFİS HAQQI 1%' -> AGENT", V[v])
install_tap(m, tap_ad("Mənzil satılır", seller="Ashiq Invest"))
v, _, _ = m.tap_check_owner("https://tap.az/elanlar/dasinmaz-emlak/menziller/48364818", {}, {})
check(v is False, "48364818 seller name 'Invest' -> AGENT", V[v])
install_tap(m, tap_ad("Mənzil satılır", seller="Makler Elvin"))
v, _, _ = m.tap_check_owner("https://tap.az/elanlar/dasinmaz-emlak/menziller/1", {}, {})
check(v is False, "seller name 'Makler Elvin' -> AGENT", V[v])
history = {"6638901": {"2r-60m", "3r-80m"}}
install_tap(m, tap_ad("Mənzil satılır", uid="6638901"))
v, _, _ = m.tap_check_owner("https://tap.az/elanlar/dasinmaz-emlak/menziller/46970831",
                            history, {}, listing={"id": "46970831", "rooms": 4, "area": 105})
check(v is False, "seller with 3 different flats seen by the bot -> AGENT", V[v])
recent = "2026-10-17T10:00:00+00:00"
long_ago = "2026-06-01T10:00:00+00:00"
seen_h = {f"tap:{i}": {"source": "tap.az", "seller": "888", "first_seen": long_ago,
                       "rooms": i, "area": 50 + i} for i in (1, 2, 3)}
check(m._seller_apartments(seen_h).get("888") is None,
      "flats seen months ago do not count toward '3+ on sale'")
seen_h["tap:9"] = {"source": "tap.az", "seller": "888", "first_seen": recent, "rooms": 2, "area": 60}
check(len(m._seller_apartments(seen_h).get("888", ())) == 1, "recent flat counts")
history = {"777": {"3r-80m", "3r-80m"}}           # same flat re-posted
install_tap(m, tap_ad("Mənzil satılır", uid="777"))
v, _, _ = m.tap_check_owner("https://tap.az/elanlar/dasinmaz-emlak/menziller/2",
                            history, {}, listing={"id": "2", "rooms": 3, "area": 80})
check(v is True, "owner re-posting the SAME flat is not counted as 3 flats", V[v])

print("\n[C1] real description of 48619879 (fetched 18 Oct)")
install_tap(m, tap_ad("Öz evimdi her şerayti var sənədləri qaydasındadı kupcası var.",
                      seller="Namiq", multi=False))
v, _, _ = m.tap_check_owner("https://tap.az/elanlar/dasinmaz-emlak/menziller/48619879", {}, {})
check(v is True, "48619879 'Öz evimdi' -> OWNER", V[v])

print("\n[C1] browser count is optional and capped")
check(m.TAP_PROFILE_BROWSER is False, "TAP_PROFILE_BROWSER off by default")
check(m.tap_apartment_count("123", {}) is None, "count unknown without the browser")
m._RUNTIME["state"] = {"tap_profiles": {"555": {"n": 6, "t": "2099-01-01T00:00:00+00:00"}}}
check(m.tap_apartment_count("555", {}) == 6, "saved profile count is reused within TTL")
m._RUNTIME["state"] = None

# --------------------------------------------------------------------------- #
print("\n[C4] tap heading keywords on REAL headings from tap page 1 (18 Oct)")
for h, want in [("3-otaqlı mənzil, Xətai r., 75 m²", True),
                ("2-otaqlı mənzil, Neftçilər m., 44.3 m²", True),
                ("4-otaqlı yeni tikili, Əhmədli m., 105 m²", True),
                ("3-otaqlı mənzil, Qara Qarayev m., 70 m²", True),
                ("2-otaqlı mənzil, 8-ci kilometr qəs., 50 m²", True),
                ("2-otaqlı mənzil, Həzi Aslanov m., 55 m²", True),
                ("4-otaqlı yeni tikili, Nizami küç., 248.67 m²", False),
                ("3-otaqlı mənzil, Nəsimi m., 76 m²", False),
                ("2-otaqlı yeni tikili, Memar Əcəmi m., 65 m²", False),
                ("3-otaqlı mənzil, Şah İsmayıl Xətai m., 70 m²", False),
                ("2-otaqlı yeni tikili, Yeni Yasamal qəs., 80 m²", False)]:
    got = m._tap_location_ok(h)
    check(got == want, f"{'keep' if want else 'drop'}: {h}", str(got))

# --------------------------------------------------------------------------- #
print("\n[C5] bina seller label - description can no longer flip an owner")
OWNER_PAGE = ('<html><div class="owner"><div>Elvin</div><div>mülkiyyətçi</div></div>'
              '<div class="desc">Təcili satılır.<br>Vasitəçilər narahat etməsin<br></div>'
              '<a href="/agentlikler">Agentliklər</a></html>' + "x" * 2000)
AGENT_PAGE = ('<html><div class="owner"><div>Rauf</div><div>vasitəçi (agent)</div></div>'
              '<div class="desc">Ev sahibi ilə birbaşa görüş</div></html>' + "x" * 2000)
AGENCY_PAGE = ('<html><div class="owner"><div>Baku Homes</div></div>'
               '<div class="desc">3 otaqlı</div></html>' + "x" * 2000)
for page, want, label in [(OWNER_PAGE, True, "owner who writes 'Vasitəçilər narahat etməsin'"),
                          (AGENT_PAGE, False, "label 'vasitəçi (agent)'"),
                          (AGENCY_PAGE, None, "no seller label -> undecided")]:
    m.http_get = lambda url, headers=None, params=None, timeout=30, browser=False, _p=page: R(_p)
    v, _ = m.bina_is_owner("https://bina.az/items/1")
    check(v is want, label, f"{V[v]} via {m.bina_is_owner.last_reason}")
if old is not None and OLD_V < "2026-10-18-a":
    old.http_get = lambda url, headers=None, params=None, timeout=30, browser=False: R(
        '<html><div>Mülkiyyətçi</div><p>Vasitəçilər narahat etməsin</p></html>' + "x" * 2000)
    v_old, _ = old.bina_is_owner("https://bina.az/items/1")
    check(v_old is False, "OLD code: same owner page wrongly -> AGENT", V[v_old])
check(m._bina_owner_from_ldjson(
    '<script type="application/ld+json">{"author":{"@type":"Person"}}</script>') is None,
    "JSON-LD 'Person' no longer implies owner (realtors are persons too)")

# --------------------------------------------------------------------------- #
print("\n[C2] seeded records stay quiet (09-07-b regression)")
sent = []
m.notify_new_owner_msg = lambda l, n, ol="": sent.append(l["id"]) or True
m.PAGE_DELAY = 0
now_iso = "2026-10-18T10:00:00+00:00"
lsrc = {"name": "lalafo.az", "type": "lalafo", "prefix": "lala:", "mode": "owner_new",
        "owner_label": "Mülkiyyətçi", "prefiltered_owner": True,
        "url": "https://lalafo.az/baku/x"}
seen = {"lala:70795769": {"url": "u", "first_seen": "2026-09-03T10:00:00+00:00",
                          "source": "lalafo.az"}}                        # seeded, no owner key
flags = {"lalafo.az": True, "lalafo.az:url": m._url_fingerprint(lsrc["url"])}
item = {"id": "70795769", "url": "https://lalafo.az/baku/ads/baki-hmdli-5-otaqli-id-70795769",
        "location": "Bakı, Əhmədli", "_text": "5 otaqlı", "_agent": False, "created_ts": None}
m.process_owner_new([item], lsrc, seen, flags)
check(sent == [], "old seeded lalafo listing is NOT re-announced", str(sent))
seen2 = {"ye:1": {"url": "u", "first_seen": now_iso, "source": "yeniemlak.az",
                  "owner": None, "tries": 1}}
check(m._is_retryable(seen2["ye:1"]), "explicitly undecided record IS retried")
check(not m._is_retryable({"owner": None, "tries": 4}), "undecided after max tries is NOT")
check(not m._is_retryable({"url": "u"}), "record without owner key is NOT retried")

# --------------------------------------------------------------------------- #
print("\n[C10] age estimate (2026-11-07-a: labels only, never hides an ad)")
day = 86400.0
t_now = time.time()


def iso(ts):
    import datetime as dt
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).isoformat(timespec="seconds")


hist = {f"tap:{48000000 + i * 10000}": {"source": "tap.az", "first_seen": iso(t_now - (40 - i) * day)}
        for i in range(41)}          # ids grow 10k per day over the last 40 days
clock = m.IdClock.from_seen(hist, "tap.az", "tap:")
a_new = clock.age_days(48400000 + 5000, t_now)
a_old = clock.age_days(48290603, t_now)        # bumped ad seen on tap page 1 (18 Oct)
check(a_new is None or a_new <= 1.01, "id newer than our latest -> unknown/new", str(a_new))
check(a_old is not None and a_old > 3, "old bumped id -> clearly older than 3 days", f"{a_old:.1f}")
check(clock.age_days(99999999, t_now) is None, "id far above all history -> unknown (new)")
check(m.IdClock().age_days(5, t_now) is None, "no history -> unknown (never suppress)")
old_lalafo = {"created_ts": t_now - 300 * day}
check(m.listing_age_days(old_lalafo, None, t_now) > 299, "lalafo creation date wins")

sent.clear()
got = []
m.notify_new_owner_msg = lambda l, n, ol="": (sent.append(l["id"]), got.append(dict(l)))[0] or True
tsrc = {"name": "tap.az", "type": "tap", "prefix": "tap:", "mode": "owner_new",
        "owner_label": "Sahibindən", "url": "https://tap.az/x"}
seen3 = dict(hist)
flags3 = {"tap.az": True, "tap.az:url": m._url_fingerprint(tsrc["url"])}
install_tap(m, tap_ad("Öz evimdi", multi=False))
bumped = {"id": "48290603", "url": "https://tap.az/elanlar/dasinmaz-emlak/menziller/48290603",
          "rooms": 4, "area": 248}
fresh = {"id": "48405000", "url": "https://tap.az/elanlar/dasinmaz-emlak/menziller/48405000",
         "rooms": 3, "area": 75}
m.process_owner_new([bumped, fresh], tsrc, seen3, flags3)
check(sent == ["48290603", "48405000"], "bumped OLD owner ad never sent before -> sent too", str(sent))
check((got[0].get("_age_days") or 0) > 3 and got[1].get("_age_days") is None,
      "bumped one marked old, fresh one marked new",
      f"{got[0].get('_age_days')}, {got[1].get('_age_days')}")
check(seen3["tap:48290603"].get("owner") is True and "skip" not in seen3["tap:48290603"],
      "sent ad recorded as owner (never sent twice)")
check(seen3.get("tap:48405000", {}).get("seller") == "22702915"
      and seen3["tap:48405000"].get("rooms") == 3, "tap record keeps seller + rooms/area")
sent.clear()
m.process_owner_new([bumped, fresh], tsrc, seen3, flags3)
check(sent == [], "next run: nothing sent twice", str(sent))

# --------------------------------------------------------------------------- #
print("\n[URL] changing a search URL re-seeds silently")
sent.clear()
flags4 = {"tap.az": True}
seen4 = {"tap:1": {"source": "tap.az", "first_seen": now_iso, "owner": True}}
m.process_owner_new([], tsrc, seen4, flags4)
check(flags4.get("tap.az:url") == m._url_fingerprint(tsrc["url"]),
      "first run after deploy just stores the fingerprint")
tsrc2 = dict(tsrc, url="https://tap.az/narrowed")
m.process_owner_new([fresh], tsrc2, seen4, flags4)
check(sent == [] and "tap:48405000" in seen4, "new URL -> current listings recorded, none sent")

# --------------------------------------------------------------------------- #
print("\n[C7] bina undecided pages are retried a bounded number of times")
bsrc = {"name": "bina.az", "type": "bina", "prefix": "", "mode": "price_owner",
        "owner_label": "Mülkiyyətçi", "url": "https://bina.az/x"}
flags5 = {"bina.az:owner_seeded": True, "bina.az:url": m._url_fingerprint(bsrc["url"])}
tries = {}
items = [{"id": "6400001", "url": "https://bina.az/items/6400001", "price": 100}]


def nolabel(url):
    m.bina_is_owner.last_reason = "nolabel"
    return None, None


m.bina_is_owner = nolabel
seen5 = {}
for run in range(1, m.OWNER_MAX_TRIES + 1):
    n, deferred, decided = m.process_new_owner_checks(items, bsrc, seen5, flags5, tries_map=tries)
    m.process_source(items, bsrc, seen5, skip_ids=deferred, owner_map=decided)
check("6400001" in seen5, f"recorded after {m.OWNER_MAX_TRIES} unreadable reads (no endless re-fetch)")
check(tries == {}, "retry counter cleaned up")


def fetchfail(url):
    m.bina_is_owner.last_reason = "fetch"
    return None, None


m.bina_is_owner = fetchfail
calls = []
_orig = m.bina_is_owner
m.bina_is_owner = lambda url: (calls.append(url), fetchfail(url))[1]
many = [{"id": str(6500000 + i), "url": f"https://bina.az/items/{6500000 + i}", "price": 1}
        for i in range(20)]
n, deferred, decided = m.process_new_owner_checks(many, bsrc, {}, flags5, tries_map={})
check(len(calls) == m.OWNER_FETCH_FAIL_STREAK and len(deferred) == 20,
      "site refusing pages -> stop after a short streak, everything kept for next run",
      f"fetched={len(calls)} deferred={len(deferred)}")

# --------------------------------------------------------------------------- #
print("\n[C8/C9] lalafo location + agent text")
check(not m.lalafo_ok({"url": "https://lalafo.az/baku/ads/baki-2-otaqli-kohn-tikili-40-kv-m-id-42756259",
                       "location": "Bakı, 2 otaqlı, Köhnə tikili, m. Nizami, 40 kv. m"}),
      "42756259 'm. Nizami' (Yasamal metro) -> dropped")
check(m.lalafo_ok({"url": "https://lalafo.az/baku/ads/x-id-1", "location": "Bakı, Nizami r., 3 otaqlı"}),
      "'Nizami r.' -> kept")
check(not m.lalafo_ok({"url": "https://lalafo.az/baku/ads/baki-koroglu-2-otaqli-id-2", "location": ""}),
      "Koroğlu -> dropped (consistent with bina)")
sent.clear()
seen6 = {"lala:1": {"url": "u", "first_seen": now_iso, "source": "lalafo.az", "owner": True}}
flags6 = {"lalafo.az": True, "lalafo.az:url": m._url_fingerprint(lsrc["url"])}
agent_item = {"id": "900001", "url": "https://lalafo.az/baku/ads/baki-hmdli-2-otaqli-id-900001",
              "location": "Bakı, Əhmədli", "_agent": False, "created_ts": t_now,
              "_text": "EKSKLUZIV VARIYANT TECILI SATILIR"}
owner_item = {"id": "900002", "url": "https://lalafo.az/baku/ads/baki-hmdli-3-otaqli-id-900002",
              "location": "Bakı, Əhmədli", "_agent": False, "created_ts": t_now,
              "_text": "Makler deyiləm, ev özümündür"}
got.clear()
m.process_owner_new([agent_item, owner_item, dict(owner_item, id="900003",
                    created_ts=t_now - 300 * day)], lsrc, seen6, flags6)
check(sent == ["900002", "900003"], "EKSKLUZIV agent skipped; fresh owner and 300-day-old owner sent",
      str(sent))
check(got[-1].get("_age_kind") == "exact" and got[-1]["_age_days"] > 299,
      "300-day-old one marked old with lalafo's real creation date")

# --------------------------------------------------------------------------- #
print("\n[C12] phrases restored + percent handling")
for phrase, want_owner in [("Mənim evimdir, təcili", True), ("Sahibi mənəm", True),
                           ("Vasitəçi olmadan satılır", True), ("Birbaşa mülkiyyətçidən", True),
                           ("Xidmət haqqı alınmır? Yox, haqqı 1% olacaq", False)]:
    sc, why = m.owner_signal_score(phrase, "", None)
    ok = sc >= m.SCORE_OWNER_AT if want_owner else sc <= m.SCORE_AGENT_AT
    check(ok, f"{'owner' if want_owner else 'agent'}: {phrase}", f"score={sc} {why}")

print("\n[ORDER] page-1 sources run before the long bina scan")
check([s_["name"] for s_ in m.SOURCES] == ["tap.az", "lalafo.az", "yeniemlak.az", "bina.az"],
      "source order", str([s_["name"] for s_ in m.SOURCES]))

# --------------------------------------------------------------------------- #
print("\n[NOV-1] yeniemlak runs two id series: new ads must not look old (ad 234096)")
import datetime as _dt
BAKU = _dt.timezone(_dt.timedelta(hours=4))


def baku_date(ts):
    return _dt.datetime.fromtimestamp(ts, BAKU).strftime("%d.%m.%Y")


def ye_history():
    h = {}
    for i in range(300):          # first seed 80 days ago: ads of every age, BOTH series
        h[f"ye:{100000 + i * 260}"] = {"source": "yeniemlak.az", "first_seen": iso(t_now - 80 * day)}
    for i in range(60):
        h[f"ye:{900000 + i * 1600}"] = {"source": "yeniemlak.az", "first_seen": iso(t_now - 80 * day)}
    nid = 178600                  # then ~40 genuinely new arrivals a day, sequential ids
    for d_ in range(79, 0, -1):
        for k_ in range(40):
            nid += 20
            h[f"ye:{nid}"] = {"source": "yeniemlak.az",
                              "first_seen": iso(t_now - d_ * day + k_ * 2000)}
    return h, nid


ye_hist, ye_top = ye_history()
NEW_ID = str(ye_top + 20)                      # posted right now
if old is not None and OLD_V == "2026-10-18-a":
    oc = old.IdClock.from_seen(ye_hist, "yeniemlak.az", "ye:")
    a_bug = oc.age_days(NEW_ID, t_now)
    check(a_bug is not None and a_bug > 3, "OLD code: brand-new yeniemlak ad looked old -> hidden",
          f"{a_bug:.0f} days")
nc = m.IdClock.from_seen(ye_hist, "yeniemlak.az", "ye:")
a_fix = nc.age_days(NEW_ID, t_now)
check(a_fix is None or a_fix <= 1, "NEW code: brand-new yeniemlak ad -> new", str(a_fix))
check((nc.age_days("152232", t_now) or 0) > 70, "old id from the first-seed era -> old")
check((nc.age_days("230000", t_now) or 0) > 10, "id first seen ~15 days ago -> old")

print("\n[NOV-2] yeniemlak: new / hidden-by-bug / renewed / unchanged / gave-up / agent")
yesrc = {"name": "yeniemlak.az", "type": "yeniemlak", "prefix": "ye:", "mode": "owner_new",
         "owner_label": "Əmlak sahibi", "url": "https://yeniemlak.az/x"}
yflags = {"yeniemlak.az": True, "yeniemlak.az:url": m._url_fingerprint(yesrc["url"])}
m.check_is_owner = lambda url, label="": (setattr(m.check_is_owner, "last_reason", "ok"), True)[1:] + (None,)
m.PAGE_DELAY = 0
ys = dict(ye_hist)
ys["ye:230000"] = dict(ys["ye:230000"], skip="old ~75d")              # hidden by 10-18-a
ys["ye:152232"] = {"source": "yeniemlak.az", "first_seen": iso(t_now - 75 * day)}   # seeded
ys["ye:152400"] = {"source": "yeniemlak.az", "first_seen": iso(t_now - 75 * day)}   # seeded
ys["ye:200000"] = dict(ys["ye:200000"], owner=None, tries=4)            # gave up
ys["ye:210000"] = dict(ys["ye:210000"], owner=False)                    # agent


def ye_item(iid, tarix_ts):
    return {"id": iid, "url": f"https://yeniemlak.az/elan/x-{iid}", "price": 100000,
            "location": "Bakı Xətai rayonu", "updated_at": baku_date(tarix_ts)}


feed = [ye_item(NEW_ID, t_now), ye_item("230000", t_now - 15 * day),
        ye_item("152232", t_now - 3600), ye_item("152400", t_now - 75 * day),
        ye_item("200000", t_now - 30 * day), ye_item("210000", t_now - 20 * day)]
sent.clear()
got.clear()
m.process_owner_new(feed, yesrc, ys, yflags)
check(sent == [NEW_ID, "230000", "152232"], "sent: new ad, the one hidden by the bug, the renewed one",
      str(sent))
check(got and got[0].get("_age_days") is None, "new ad -> '‼️ Yeni elan'")
check(len(got) > 1 and got[1].get("_age_days", 0) > 10 and not got[1].get("_note"),
      "hidden one -> marked old (no extra note)")
check(len(got) > 2 and got[2].get("_age_days", 0) > 70, "renewed 152232 -> marked old (~75 days)")
check(all(ys[f"ye:{i}"].get("owner") is True for i in (NEW_ID, "230000", "152232")),
      "all three recorded as sent")
sent.clear()
m.process_owner_new(feed, yesrc, ys, yflags)
check(sent == [], "next run: nothing sent twice", str(sent))

a_tap, _ = m._age_estimate({"id": "47325885"}, None, t_now,
                           {"source": "tap.az", "first_seen": iso(t_now - 2 * day), "skip": "old ~100d"})
a_ye, _ = m._age_estimate({"id": NEW_ID}, None, t_now,
                          {"source": "yeniemlak.az", "first_seen": iso(t_now - 2 * day), "skip": "old ~75d"})
check(a_tap > 100, "hidden tap ad keeps its real age (one id series)", f"{a_tap:.0f}")
check(a_ye < 3, "hidden yeniemlak ad ignores the bug's '75 days' -> new", f"{a_ye:.1f}")

print("\n[NOV-3] old backlog is capped per run so new ads are never delayed")
ys2 = dict(ye_hist)
backlog = [str(231000 + 20 * i) for i in range(20)]
for i in backlog:
    ys2[f"ye:{i}"] = dict(ys2[f"ye:{i}"], skip="old ~75d")
feed2 = [ye_item(NEW_ID, t_now)] + [ye_item(i, t_now - 12 * day) for i in backlog]
sent.clear()
m.process_owner_new(feed2, yesrc, ys2, yflags)
check(len(sent) == 1 + m.OLD_MAX_PER_RUN and sent[0] == NEW_ID,
      f"run 1: the new ad + {m.OLD_MAX_PER_RUN} old ones", str(len(sent)))
sent.clear()
m.process_owner_new(feed2, yesrc, ys2, yflags)
check(len(sent) == 20 - m.OLD_MAX_PER_RUN, "run 2: the rest of the backlog", str(len(sent)))
sent.clear()
m.process_owner_new(feed2, yesrc, ys2, yflags)
check(sent == [], "run 3: nothing left", str(sent))

print("\n[NOV-4] lalafo: silently recorded ad renewed by its owner -> once, marked old")
ls = {"lala:1": {"url": "u", "first_seen": now_iso, "source": "lalafo.az", "owner": True},
      "lala:800001": {"url": "u", "first_seen": iso(t_now - 30 * day), "source": "lalafo.az"},
      "lala:800002": {"url": "u", "first_seen": iso(t_now - 30 * day), "source": "lalafo.az"}}
lfeed = [{"id": "800001", "url": "https://lalafo.az/baku/ads/baki-hmdli-3-otaqli-id-800001",
          "location": "Bakı, Əhmədli", "_agent": False, "_text": "3 otaqlı",
          "created_ts": t_now - 200 * day, "updated_ts": t_now - 3600},
         {"id": "800002", "url": "https://lalafo.az/baku/ads/baki-hmdli-2-otaqli-id-800002",
          "location": "Bakı, Əhmədli", "_agent": False, "_text": "2 otaqlı",
          "created_ts": t_now - 200 * day, "updated_ts": t_now - 31 * day}]
sent.clear()
got.clear()
m.process_owner_new(lfeed, lsrc, ls, flags6)
check(sent == ["800001"], "renewed one sent; untouched one stays quiet", str(sent))
check(got and got[0].get("_age_kind") == "exact" and got[0]["_age_days"] > 199,
      "labelled with lalafo's real creation date (~200 days)")
sent.clear()
m.process_owner_new(lfeed, lsrc, ls, flags6)
check(sent == [], "next run: nothing sent twice", str(sent))

print("\n[NOV-5] bina: old owner ads are labelled, not hidden; never-sent VIP/Premium -> once")
bsrc2 = dict(bsrc)
bflags = {"bina.az:owner_seeded": True, "bina.az:url": m._url_fingerprint(bsrc2["url"])}
m.bina_is_owner = lambda url: (setattr(m.bina_is_owner, "last_reason", "ok"), True)[1:] + (None,)
m._BINA_MAX_ID["v"] = 6470000
bs = {"6400100": {"url": "u", "first_seen": iso(t_now - 40 * day), "source": "bina.az",
                  "price": 1, "price_history": []},                         # seeded, never sent
      "6400101": {"url": "u", "first_seen": iso(t_now - 40 * day), "source": "bina.az",
                  "price": 1, "price_history": []},                         # seeded, not promoted
      "6400102": {"url": "u", "first_seen": iso(t_now - 40 * day), "source": "bina.az",
                  "price": 1, "price_history": [], "owner": False}}         # agent
bfeed = [{"id": "6400200", "url": "https://bina.az/items/6400200", "price": 150000, "_promoted": True},
         {"id": "6400100", "url": "https://bina.az/items/6400100", "price": 150000, "_promoted": True},
         {"id": "6400101", "url": "https://bina.az/items/6400101", "price": 150000, "_promoted": False},
         {"id": "6400102", "url": "https://bina.az/items/6400102", "price": 150000, "_promoted": True}]
sent.clear()
got.clear()
n_, deferred_, decided_ = m.process_new_owner_checks(bfeed, bsrc2, bs, bflags, tries_map={})
check(sent == ["6400200", "6400100"],
      "unseen promoted 25-day-old owner ad sent (was hidden); never-sent VIP owner ad sent",
      str(sent))
check(all(g.get("_age_days", 0) > 3 for g in got), "both marked old")
check(bs["6400100"].get("owner") is True and "owner" not in bs["6400101"],
      "VIP one recorded as sent; non-promoted known one untouched")
if old is not None and OLD_V == "2026-10-18-a":
    old.bina_is_owner = m.bina_is_owner
    old._BINA_MAX_ID["v"] = 6470000
    old.notify_new_owner_msg = lambda l, n, ol="": True
    n_old, _, _ = old.process_new_owner_checks([dict(bfeed[0])], bsrc2, {}, dict(bflags), tries_map={})
    check(n_old == 0, "OLD code: the same promoted owner ad was hidden")
sent.clear()
bs["6400200"] = {"url": "u", "first_seen": now_iso, "source": "bina.az", "owner": True}
m.process_new_owner_checks(bfeed, bsrc2, bs, bflags, tries_map={})
check(sent == [], "next run: nothing sent twice", str(sent))

print("\n[NOV-6] message header (2026-11-08-a: a date instead of 'N gün əvvəl', no note)")
_pt = _dt.datetime(2026, 8, 24, 15, 0, tzinfo=BAKU).timestamp()
t_old = m.format_new_owner({"url": "https://x", "price": 100000, "_age_days": 75.2,
                            "_age_kind": "min", "_posted_ts": _pt,
                            "_note": "Bu elan əvvəl göndərilməmişdi"},
                           "yeniemlak.az", "Əmlak sahibi")
check("♻️ <b>Yenilənmiş elan</b>" in t_old and "🕓 <b>Yerləşdirilib:</b> 24.08.2026\n" in t_old,
      "old ad: ♻️ header, 'Yerləşdirilib: 24.08.2026' (plain date)", t_old.splitlines()[2])
check("gün əvvəl" not in t_old and "ən azı" not in t_old and "ən gec" not in t_old
      and "göndərilməmişdi" not in t_old and "ℹ️" not in t_old,
      "no 'N gün əvvəl', no 'ən azı', no 'ən gec', no note line")
_pc = _dt.datetime(2026, 4, 12, 9, 30, tzinfo=BAKU).timestamp()
t_ex = m.format_new_owner({"url": "https://x", "_age_days": 200.4, "_age_kind": "exact",
                           "_posted_ts": _pc}, "lalafo.az", "Mülkiyyətçi")
check("Yerləşdirilib:</b> 12.04.2026" in t_ex, "site's own creation date: '12.04.2026'")
t_ap = m.format_new_owner({"url": "https://x", "_age_days": 40, "_age_kind": "approx",
                           "_posted_ts": _pc}, "bina.az", "Mülkiyyətçi")
check("Yerləşdirilib:</b> ~12.04.2026" in t_ap, "estimate: '~12.04.2026'")
t_new = m.format_new_owner({"url": "https://x", "price": 1, "_age_days": 1.5}, "tap.az", "Sahibindən")
check("‼️ <b>Yeni elan</b>" in t_new and "🕓" not in t_new, "new ad: ‼️ header, no age line")

# --------------------------------------------------------------------------- #
# 2026-11-07-b: yeniemlak rate limit (HTTP 429 after ~30 quick requests, 7 Nov 2026)
# --------------------------------------------------------------------------- #
class Clock:
    """Virtual time for a module: sleep() only moves the clock (tests stay fast)."""
    def __init__(self, t0=2_000_000_000.0):
        self.t, self.slept = t0, []

    def time(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += max(0.0, s)


class RH:
    def __init__(self, code=200, text="", headers=None):
        self.status_code, self.text, self.headers = code, text, headers or {}
        self.content = text.encode("utf-8")


OWNER_PAGE = "<html><div>Əmlak sahibi</div>" + "x" * 3000 + "</html>"
print("\n[NOV-7] yeniemlak requests are spaced out; a 429 is waited out once, then left alone")
mt = load(os.path.join(HERE, "monitor.py"), "monitor_net")
ck = Clock()
mt.time = ck
mt.HOST_MIN_INTERVAL = {"yeniemlak.az": 2.5}
mt.RATE_LIMIT_WAIT, mt.RATE_LIMIT_MAX_WAIT = 45.0, 90.0
raw_calls, replies = [], []


def fake_raw(url, headers=None, params=None, timeout=30, browser=False):
    raw_calls.append((url, ck.t))
    return replies.pop(0) if replies else RH(200, OWNER_PAGE)


mt._raw_get = fake_raw
for _ in range(3):
    mt.http_get("https://yeniemlak.az/elan/axtar?page=1")
gaps = [b[1] - a[1] for a, b in zip(raw_calls, raw_calls[1:])]
check(all(g >= 2.5 - 1e-9 for g in gaps), "yeniemlak requests >= 2.5 s apart", str(gaps))
raw_calls.clear()
mt.http_get("https://tap.az/elanlar/1")
mt.http_get("https://tap.az/elanlar/2")
check(len(raw_calls) == 2 and raw_calls[1][1] - raw_calls[0][1] == 0, "other sites are not slowed down")
raw_calls.clear()
ck.slept.clear()
replies[:] = [RH(429, "too many", {"Retry-After": "20"}), RH(200, OWNER_PAGE)]
r_ = mt.http_get("https://yeniemlak.az/elan/x-1")
check(r_.status_code == 200 and len(raw_calls) == 2 and 20 in ck.slept and not mt.RATE_LIMITED,
      "429 -> waits Retry-After (20 s) -> retry succeeds", str(ck.slept))
raw_calls.clear()
ck.slept.clear()
replies[:] = [RH(429, "", {"Retry-After": "600"}), RH(429, "")]
r_ = mt.http_get("https://yeniemlak.az/elan/x-2")
check(r_.status_code == 429 and mt.RATE_LIMITED.get("yeniemlak.az") and max(ck.slept) == 90.0,
      "still 429 after a capped wait (90 s, not 600) -> host marked rate-limited", str(ck.slept))
n_before = len(raw_calls)
r_ = mt.http_get("https://yeniemlak.az/elan/x-3")
check(r_.status_code == 429 and len(raw_calls) == n_before,
      "after that: no more requests to yeniemlak this run")
v_, p_ = mt.check_is_owner("https://yeniemlak.az/elan/x-4", "Əmlak sahibi")
check(v_ is None and mt.check_is_owner.last_reason == "ratelimit" and len(raw_calls) == n_before,
      "owner check -> 'ratelimit' (not a failed read), no request made")
check(mt._retry_after_seconds(RH(429, "", {"Retry-After": "7"})) == 7.0
      and mt._retry_after_seconds(RH(429, "", {"Retry-After": "soon"})) is None
      and mt._retry_after_seconds(RH(429)) is None, "Retry-After parsing")
mt.RATE_LIMITED.clear()
raw_calls.clear()
replies[:] = [RH(429, ""), RH(429, "")]
v_, p_ = mt.check_is_owner("https://yeniemlak.az/elan/x-5", "Əmlak sahibi")
check(v_ is None and mt.check_is_owner.last_reason == "ratelimit" and len(raw_calls) == 2,
      "429 during an owner check: 2 requests in total, then stop (not 2 x 2)", str(len(raw_calls)))
if old is not None and OLD_V == "2026-11-07-a":
    oc_calls = []
    old.time = Clock()
    old.requests = type("Q", (), {"get": staticmethod(lambda *a, **k: oc_calls.append(1) or RH(200, "x"))})
    old.HAS_CURL_CFFI = False
    t0_ = old.time.t
    for _ in range(3):
        old.http_get("https://yeniemlak.az/elan/axtar?page=1")
    check(old.time.t == t0_ and len(oc_calls) == 3, "OLD code: yeniemlak requests fired back to back")

print("\n[NOV-8] rate-limited run: nothing recorded, no try used; next run checks them")
mt.RATE_LIMITED.clear()
mt.PAGE_DELAY = 0
ye_src = {"name": "yeniemlak.az", "type": "yeniemlak", "prefix": "ye:", "mode": "owner_new",
          "owner_label": "Əmlak sahibi", "url": "https://yeniemlak.az/x"}
ye_flags = {"yeniemlak.az": True, "yeniemlak.az:url": mt._url_fingerprint(ye_src["url"])}
ye_seen = {"ye:234000": {"url": "u", "first_seen": iso(ck.t - day), "source": "yeniemlak.az",
                         "owner": True}}
sent8 = []
mt.notify_new_owner_msg = lambda l, n, ol="": sent8.append(l["id"]) or True
feed8 = [{"id": str(234100 + i), "url": f"https://yeniemlak.az/elan/x-{234100 + i}",
          "price": 100000, "location": "Bakı Xətai rayonu", "updated_at": baku_date(ck.t)}
         for i in range(3)]
replies[:] = [RH(429, ""), RH(429, "")]
raw_calls.clear()
mt.process_owner_new([dict(x) for x in feed8], ye_src, ye_seen, ye_flags)
check(sent8 == [] and len(ye_seen) == 1, "run 1 (429): nothing sent, nothing recorded")
check(len(raw_calls) == 2, "run 1: stopped after the refused page (no hammering)", str(len(raw_calls)))
mt.RATE_LIMITED.clear()                       # a new run starts with a clean slate
mt.process_owner_new([dict(x) for x in feed8], ye_src, ye_seen, ye_flags)
check(sent8 == ["234100", "234101", "234102"], "run 2: all three checked and sent", str(sent8))
check(all("tries" not in ye_seen[f"ye:{234100 + i}"] for i in range(3)), "no try was used up")

print("\n[NOV-9] yeniemlak search: newest pages only, full read every few hours")


def ye_page(cards):
    out = []
    for iid, tarix, loc in cards:
        out.append(f'<div class="ad"><a href="/elan/satilir-2-otaqli-x-{iid}">x</a>'
                   f"Satılır 150 000 Baxış 12 Elan: {iid} 2 otaq 60 m2 3/9 Mərtəbə "
                   f"Tarix: {tarix} Ünvan: {loc}</div>")
    return "<html>Nəticə: 250 " + "".join(out) + "</html>"


XETAI, YAS = "Bakı Xətai rayonu, metro. Əhmədli", "Bakı Yasamal rayonu, metro. İnşaatçılar"
TODAY, YDAY, OLD_D = baku_date(ck.t), baku_date(ck.t - day), baku_date(ck.t - 6 * day)


def build_site(pages):
    def fake_get(url, headers=None, params=None, timeout=30, browser=False):
        n = int(mt.parse_qs(mt.urlparse(url).query).get("page", ["1"])[0])
        fetched.append(n)
        return RH(200, ye_page(pages[n - 1]) if n <= len(pages) else "<html>Nəticə: 250</html>")
    return fake_get


def ids(a, b):
    return list(range(a, b))


site = [[(i, TODAY, XETAI) for i in ids(240000, 240025)],
        [(i, YDAY, XETAI) for i in ids(239900, 239925)]]
site += [[(i, OLD_D, XETAI) for i in ids(239000 - 100 * p, 239025 - 100 * p)] for p in range(8)]
known = {f"ye:{c[0]}": {"url": "u", "first_seen": iso(ck.t - 7 * day), "source": "yeniemlak.az",
                        "owner": False} for pg in site for c in pg}
fetched = []
mt.http_get = build_site(site)
act_known = (lambda l: mt._candidate_kind("yeniemlak", l, known.get("ye:" + str(l["id"])),
                                          ck.t) is not None)
got9 = mt.fetch_yeniemlak("https://yeniemlak.az/x")
check(fetched == list(range(1, 12)) and len(got9) == 250, "full read: all 10 pages", str(fetched))
fetched.clear()
got9 = mt.fetch_yeniemlak("https://yeniemlak.az/x", actionable=act_known)
check(fetched == [1, 2, 3, 4], "nothing new: today + yesterday + 2 quiet pages, then stop", str(fetched))
fetched.clear()
k2 = dict(known)
k2.pop("ye:238910")                                   # unrecorded old-dated ad on page 4
k2.pop("ye:238710")                                   # and on page 6
act2 = (lambda l: mt._candidate_kind("yeniemlak", l, k2.get("ye:" + str(l["id"])), ck.t) is not None)
got9 = mt.fetch_yeniemlak("https://yeniemlak.az/x", actionable=act2)
check(fetched == [1, 2, 3, 4, 5, 6, 7, 8],
      "unrecorded listings keep it reading (pages 4 and 6), stops 2 quiet pages later", str(fetched))
fetched.clear()
site3 = [list(pg) for pg in site]
for p in range(2, 10):
    site3[p] = [(c[0] + 5_000_000, OLD_D, YAS) for c in site3[p]]       # leaked out-of-area ads
mt.http_get = build_site(site3)
got9 = mt.fetch_yeniemlak("https://yeniemlak.az/x", actionable=act_known)
check(fetched == [1, 2, 3, 4], "out-of-area ads (never recorded) do not keep it reading", str(fetched))
fetched.clear()
site4 = [list(pg) for pg in site]
site4[2] = [(c[0], "??", XETAI) for c in site4[2]]
mt.http_get = build_site(site4)
mt.fetch_yeniemlak("https://yeniemlak.az/x", actionable=act_known)
check(fetched == [1, 2, 3, 4, 5], "unreadable date counts as recent (keeps reading)", str(fetched))

st9 = {"listings": dict(known), "seeded": dict(ye_flags)}
mt._RUNTIME["state"] = None
check(mt._ye_crawl_plan(ye_src, ck.t)[0] is None, "plan: no state -> full read")
mt._RUNTIME["state"] = {"listings": dict(known), "seeded": {}}
check(mt._ye_crawl_plan(ye_src, ck.t)[0] is None, "plan: first run -> full read")
mt._RUNTIME["state"] = {"listings": dict(known), "seeded": {"yeniemlak.az": True,
                                                           "yeniemlak.az:url": "different"}}
check(mt._ye_crawl_plan(ye_src, ck.t)[1] == "search URL changed", "plan: URL changed -> full read")
mt._RUNTIME["state"] = st9
a1, w1 = mt._ye_crawl_plan(ye_src, ck.t)
a2, w2 = mt._ye_crawl_plan(ye_src, ck.t + 600)
a3, w3 = mt._ye_crawl_plan(ye_src, ck.t + mt.YE_FULL_CRAWL_HOURS * 3600 + 1)
check(a1 is None and callable(a2) and a3 is None, "plan: full read, then newest pages, then full "
      f"again after {mt.YE_FULL_CRAWL_HOURS:g} h", f"{w1} | {w2} | {w3}")
st9["listings"]["ye:300001"] = {"url": "u", "first_seen": iso(ck.t), "source": "yeniemlak.az",
                                "owner": True}
st9["listings"]["ye:300002"] = {"url": "u", "first_seen": iso(ck.t - 30 * day), "source": "yeniemlak.az",
                                "skip": "old ~75d"}
st9["listings"]["ye:300003"] = {"url": "u", "first_seen": iso(ck.t - 30 * day), "source": "yeniemlak.az"}
probe9 = [{"id": "300001", "updated_at": TODAY}, {"id": "300002", "updated_at": OLD_D},
          {"id": "300003", "updated_at": TODAY}, {"id": "300003", "updated_at": OLD_D},
          {"id": "399999", "updated_at": OLD_D}]
check([a2(x) for x in probe9] == [False, True, True, False, True],
      "worth a look: sent=no, hidden=yes, renewed=yes, quiet=no, unrecorded=yes")
mt._RUNTIME["state"] = None
if old is not None and OLD_V == "2026-11-07-a":
    fetched.clear()
    old.http_get = build_site(site)
    old.PAGE_DELAY = 0
    old.fetch_yeniemlak("https://yeniemlak.az/x")
    check(len(fetched) == 11, "OLD code: every run read every page (what triggered the 429)")

print("\n[NOV-12] main() end to end: first run seeds, then a full read, then newest pages only")
me = load(os.path.join(HERE, "monitor.py"), "monitor_ye_e2e")
tmpe = tempfile.mkdtemp()
me.STATE_FILE = os.path.join(tmpe, "seen.json")
me.USE_API = False
me.PAGE_DELAY = 0
me.SOURCES = [dict(s_) for s_ in me.SOURCES if s_["name"] == "yeniemlak.az"]
out_e = []
me.tg_send_message = lambda text: out_e.append(text) or True
me.tg_send_photo = lambda photo, cap: out_e.append(cap) or True
me.check_is_owner = lambda url, label="": (setattr(me.check_is_owner, "last_reason", "ok") or True, None)
_tr = time.time()                                  # main() runs on the real clock
_dates = {TODAY: baku_date(_tr), YDAY: baku_date(_tr - day), OLD_D: baku_date(_tr - 6 * day)}
site_e = [[(c[0], _dates[c[1]], c[2]) for c in pg] for pg in site]
fetched = []
me.http_get = build_site(site_e)
with contextlib.redirect_stdout(io.StringIO()):
    me.main()                                      # run 1: first run -> full read, all recorded silently
r1 = list(fetched)
fetched.clear()
with contextlib.redirect_stdout(io.StringIO()):
    me.main()                                      # run 2: no full-read stamp yet -> full read
r2 = list(fetched)
fetched.clear()
site_e[0] = [(240500, _dates[TODAY], XETAI)] + site_e[0][:-1]   # a brand-new ad appears on top
with contextlib.redirect_stdout(io.StringIO()):
    me.main()                                      # run 3: newest pages only
r3 = list(fetched)
st_e = json.load(open(me.STATE_FILE, encoding="utf-8"))
check(len(r1) == 11 and len(r2) == 11, "runs 1-2: full reads", f"{len(r1)}, {len(r2)}")
check(r3 == [1, 2, 3, 4], "run 3: only the newest pages", str(r3))
check(len(out_e) == 1 and "240500" in out_e[0], "run 3: the brand-new ad is sent (and nothing else)",
      str(len(out_e)))
check("yeniemlak.az" in st_e.get("full_crawl", {}), "time of the last full read is saved in state")

print("\n[NOV-13] yeniemlak: only SALE ads (rental 235014 'kiraye-1-otaqli' got through, 7 Nov)")


def ye_card(iid, deal, slug, price="150 000", extra=""):
    return (f'<div class="ad"><a href="/elan/{slug}-1-otaqli-bina-evi-menzil-xetai-rayonu-{iid}">x</a>'
            f"{deal} {price} {extra}Baxış 12 Elan: {iid} 1 otaq 45 m2 3/9 Mərtəbə "
            f"Tarix: {TODAY} Ünvan: Bakı Xətai rayonu H.Aslanov qəs. metro. Həzi Aslanov</div>")


page13 = ("<html>Nəticə: 4 " + ye_card(235001, "Satılır", "satilir")
          + ye_card(235014, "Kirayə", "kiraye", price="600")
          + ye_card(235020, "Girov", "girov", price="20 000")
          + ye_card(235030, "Satılır", "satilir", price="98 000", extra="Kreditlə ") + "</html>")
p13, _t = mt._parse_yeniemlak_page(page13)
check([x["deal"] for x in p13] == ["Satılır", "Kirayə", "Girov", "Satılır"]
      and [x["price"] for x in p13] == [150000, 600, 20000, 98000], "deal type + price parsed",
      str([(x["deal"], x["price"]) for x in p13]))
check([mt.ye_passes(x) for x in p13] == [True, False, False, True], "rent and girov dropped, sales kept")
check(not mt.ye_passes({"url": "https://yeniemlak.az/elan/kiraye-1-otaqli-bina-evi-menzil-xetai-"
                               "rayonu-h-aslanov-qes-vunq-tau-235014", "location": "Bakı Xətai rayonu"}),
      "the real 235014 link alone is enough to drop it")
check(mt.ye_passes({"url": "https://yeniemlak.az/elan/235099", "location": "Bakı Xətai rayonu"}),
      "unknown deal type is kept (never lose a sale ad over parsing)")
if old is not None and OLD_V == "2026-11-07-b":
    po, _t = old._parse_yeniemlak_page(page13)
    check(sum(1 for x in po if old.ye_passes(x)) == 4, "OLD code: rent and girov ads were kept")
fetched = []
mt.http_get = lambda url, **k: (fetched.append(1), RH(200, page13 if len(fetched) == 1 else "<html></html>"))[1]
got13 = mt.fetch_yeniemlak("https://yeniemlak.az/x")
check([x["id"] for x in got13] == ["235001", "235030"], "the search result keeps only the 2 sale ads")

print("\n[NOV-14] yeniemlak pages 31-74 (read for the first time): old ads recorded, not sent")
ys14 = dict(ye_hist)                           # bot's first yeniemlak record: 80 days ago
NEW14 = str(ye_top + 40)
pre_bot = [str(140000 + i) for i in range(20)]  # dated before the bot ever read yeniemlak
feed14 = ([ye_item(NEW14, t_now)] + [ye_item(i, t_now - 100 * day) for i in pre_bot]
          + [ye_item("205555", t_now - 40 * day)])   # dated after bot start, but never recorded
sent.clear()
got.clear()
m.process_owner_new(feed14, yesrc, ys14, yflags)
check(sent == [NEW14, "205555"], "sent: the new ad + the one the bot missed while running", str(sent))
check(len(got) == 2 and got[0].get("_age_days") is None and (got[1].get("_age_days") or 0) > 38,
      "missed one is marked old (at least ~39 days, from its Tarix)")
check(all(f"ye:{i}" in ys14 and "owner" not in ys14[f"ye:{i}"] for i in pre_bot),
      "all 20 pre-bot ads recorded silently (not capped, nothing sent)")
sent.clear()
m.process_owner_new(feed14, yesrc, ys14, yflags)
check(sent == [], "next run: nothing", str(sent))
ys14["ye:140003"]["first_seen"] = iso(t_now - 5 * day)
feed14b = [ye_item("140003", t_now)]               # its owner renews it -> today's date
m.process_owner_new(feed14b, yesrc, ys14, yflags)
check(sent == ["140003"], "if a pre-bot ad is renewed later, it is sent once", str(sent))
ys15 = dict(ye_hist)
ys15[f"ye:{NEW14}"] = {"url": "u", "first_seen": iso(t_now - 3600), "source": "yeniemlak.az",
                       "skip": "old ~75d"}
got.clear()
m.process_owner_new([ye_item(NEW14, t_now)], yesrc, ys15, yflags)
check(got and not got[0].get("_note") and got[0].get("_age_days") is None,
      "hidden but only hours old -> plain '‼️ Yeni elan', no 'not sent before' note")

print("\n[NOV-15] yeniemlak address line: clean, no description or half street names")
ADDR_CASES = [   # text after "Ünvan:" on real cards -> address line in the message
    ("Bakı Xətai rayonu Əhmədli metro. Əhmədli Xudu Məmmədov küçəsi Təcili satılır Ətraflı >>",
     "Bakı, Xətai rayonu, Əhmədli m."),
    ("Bakı Xətai rayonu metro Mənzil təcili olaraq satılır qiymətdə cüzi endirim Ətraflı >>",
     "Bakı, Xətai rayonu"),
    ("Bakı Xətai rayonu Ağ şəhər metro. Xətai 1 ci yaşıl ada küç Təcili satilir",
     "Bakı, Xətai rayonu, Ağ şəhər, Xətai m."),
    ("Bakı Nizami rayonu 8-ci kilometr metro. Qara Qarayev Behruz Nuriyev küç",
     "Bakı, Nizami rayonu, 8-ci kilometr, Qara Qarayev m."),
    ("Bakı Xətai rayonu H.Aslanov qəs. metro. Həzi Aslanov Gənclər parkının qarşısı",
     "Bakı, Xətai rayonu, H.Aslanov qəs., Həzi Aslanov m."),
    ("Bakı Nərimanov rayonu metro. Nəriman Nərimanov Təbriz küçəsi",
     "Bakı, Nərimanov rayonu, Nəriman Nərimanov m."),
    ("Bakı Nəsimi rayonu metro. 28 may Azadlıq pr", "Bakı, Nəsimi rayonu, 28 May m."),
    ("Bakı Xətai rayonu metro. Naməlum stansiya küç", "Bakı, Xətai rayonu"),
    ("Bakı Xətai rayonu H.Aslanov qəs. metro", "Bakı, Xətai rayonu, H.Aslanov qəs."),
]
bad_addr = [(a, m._ye_address(a), b) for a, b in ADDR_CASES if m._ye_address(a) != b]
check(not bad_addr, f"{len(ADDR_CASES)} real address shapes -> clean address", str(bad_addr))
card15 = ("<html>Nəticə: 1 <div>Satılır 185 000 Baxış sayı: 12| Tarix: 27.10.2026| Elan: 227514 "
          "Bina evi menzil / Köhnə tikili 2 otaq 50 m2 5 / 3 Mərtəbə Ünvan: <b>Bakı</b><b>Xətai rayonu"
          "</b><b>Əhmədli</b> metro. <b>Əhmədli</b> Xudu Məmmədov küçəsi</div><div>Təcili satılır, "
          "Əhmədli metrosuna 5 dəqiqə</div><a href='/elan/satilir-2-otaqli-bina-evi-menzil-xetai-rayonu-"
          "ehmedli-xudu-memmedov-kucesi-227514'>Ətraflı >></a></html>")
it15 = m._parse_yeniemlak_page(card15)[0][0]
check(it15["address"] == "Bakı, Xətai rayonu, Əhmədli m." and m.ye_passes(it15),
      "parsed card: clean address; area filter unchanged", f"{it15['address']!r} | {it15['location']!r}")
msg15 = m.format_new_owner(dict(it15), "yeniemlak.az", "Əmlak sahibi")
check("📍 <b>Ünvan:</b> Bakı, Xətai rayonu, Əhmədli m.\n" in msg15 and "Xudu" not in msg15
      and "Təcili" not in msg15, "message shows the clean address only")
check("Ünvan" not in m.format_new_owner({"url": "u", "address": "", "location": "junk text"},
                                         "yeniemlak.az"), "nothing usable -> no address line (not junk)")
_now15 = _dt.datetime(2026, 11, 8, 1, 30, tzinfo=BAKU).timestamp()      # just after midnight
a15, k15 = m._age_estimate(it15, None, _now15, None)
got15 = dict(it15, _age_days=a15, _age_kind=k15, _posted_ts=_now15 - a15 * 86400)
check(k15 == "min" and "Yerləşdirilib:</b> 27.10.2026\n" in m.format_new_owner(got15, "yeniemlak.az"),
      "date comes from the card's Tarix (27.10.2026), not the day after")
ysrc15 = dict(yesrc)
ys16 = dict(ye_hist)
ys16["ye:227514"] = {"url": "u", "first_seen": iso(t_now - 11 * day), "source": "yeniemlak.az",
                     "skip": "old ~75d"}
got.clear()
m.process_owner_new([dict(it15, updated_at=baku_date(t_now - 11 * day))], ysrc15, ys16, yflags)
msg16 = m.format_new_owner(got[0], "yeniemlak.az", "Əmlak sahibi") if got else ""
_dm = re.search(r"Yerləşdirilib:</b> (\d\d)\.(\d\d)\.(\d{4})\n", msg16)
_shown = _dt.datetime(int(_dm.group(3)), int(_dm.group(2)), int(_dm.group(1)), tzinfo=BAKU) if _dm else None
_tarix = _dt.datetime.fromtimestamp(t_now - 11 * day, BAKU).replace(hour=0, minute=0, second=0, microsecond=0)
check("♻️" in msg16 and _shown is not None and _shown <= _tarix and "ℹ️" not in msg16,
      "real run: ♻️ + plain date (on or before its Tarix) + no note",
      msg16.splitlines()[2] if msg16 else "")
mt3 = load(os.path.join(HERE, "monitor.py"), "monitor_taploc")
ad_t = tap_ad("Öz evimdi", multi=False).replace(
    "<html>", '<html><head><meta property="og:title" content="2-otaqlı mənzil, Əhmədli m., 60 m² - Tap.az">'
              '<title>x</title></head>', 1)
install_tap(mt3, ad_t)
mt3.tap_check_owner("https://tap.az/elanlar/dasinmaz-emlak/menziller/48500099", {}, {},
                    listing={"id": "48500099"})
check(mt3.tap_check_owner.last_location == "Əhmədli m.", "tap detail page -> location for the message",
      str(mt3.tap_check_owner.last_location))

print("\n[NOV-16] tap messages have the same lines as the others (Qiymət, Otaq sayı, Sahə, Ünvan)")
REAL_TAP_HEAD = ('<meta property="og:title" content="2-otaqlı mənzil, Neftçilər m., 44.3 m²">'
                 '<meta content="2026-09-02T15:39:29+04:00" property="og:updated_time">'
                 '<meta property="product:price:amount" content="118000.00">'
                 '<meta property="product:price:currency" content="AZN">')
d16 = m._tap_details("<html><head>" + REAL_TAP_HEAD + "</head></html>")
check(d16 == {"rooms": 2, "area": 44.3, "location": "Neftçilər m.", "price": 118000, "currency": "AZN"},
      "real tap page meta -> price, rooms, area, address", str(d16))
check(m._tap_details("<html><title>Tap.Az</title></html>") == {}, "nothing matching -> nothing added")
mt4 = load(os.path.join(HERE, "monitor.py"), "monitor_tapfill")
install_tap(mt4, tap_ad("Öz evimdi, makler deyiləm", multi=False).replace("<html>", "<html>" + REAL_TAP_HEAD, 1))
mt4.PAGE_DELAY = 0
msgs16 = []
mt4.notify_new_owner_msg = lambda l, n, ol="": msgs16.append(mt4.format_new_owner(l, n, ol)) or True
tsrc16 = {"name": "tap.az", "type": "tap", "prefix": "tap:", "mode": "owner_new",
          "owner_label": "Sahibindən", "url": "https://tap.az/x"}
tflags16 = {"tap.az": True, "tap.az:url": mt4._url_fingerprint(tsrc16["url"])}
tseen16 = {"tap:48900000": {"url": "u", "first_seen": iso(time.time() - day), "source": "tap.az",
                            "owner": True}}
card16 = {"id": "48900050", "url": "https://tap.az/elanlar/dasinmaz-emlak/menziller/48900050",
          "rooms": None, "area": None, "price": None, "location": None}       # card gave nothing
with contextlib.redirect_stdout(io.StringIO()):
    mt4.process_owner_new([card16], tsrc16, tseen16, tflags16)
t16 = msgs16[0] if msgs16 else ""
check("💰 <b>Qiymət:</b> 118 000 AZN" in t16 and "🛏 <b>Otaq sayı:</b> 2" in t16
      and "📐 <b>Sahə:</b> 44.3 m²" in t16 and "📍 <b>Ünvan:</b> Neftçilər m." in t16,
      "live tap: missing card fields filled from the ad's page", " | ".join(t16.splitlines()[2:7]))
check(tseen16["tap:48900050"].get("rooms") is None,
      "flat counting still uses only the card's own values (no change to the 3-flats rule)")
card16b = dict(card16, id="48900051", url=card16["url"][:-2] + "51", price=99000, location="Əhmədli m.")
msgs16.clear()
with contextlib.redirect_stdout(io.StringIO()):
    mt4.process_owner_new([card16b], tsrc16, tseen16, tflags16)
check(msgs16 and "99 000 AZN" in msgs16[0] and "Əhmədli m." in msgs16[0],
      "values the card already had are kept (page only fills gaps)")

print("\n[NOV-17] tap seller profile count (browser page) - what is trusted")


def tap_profile(total, links, extra=""):
    nav = '<nav><a href="/elanlar">Elanlar</a><a href="/elanlar/dasinmaz-emlak">Daşınmaz</a></nav>'
    cards = "".join(f'<a href="/elanlar/{c}/{s_}/{i}"><img></a><a href="/elanlar/{c}/{s_}/{i}">ad</a>'
                    for c, s_, i in links)
    return f"<html>{nav}<h1>İstifadəçinin elanları</h1><div>{total} elan</div>{cards}{extra}</html>"


D_ = "dasinmaz-emlak"
deniz = tap_profile(6, [(D_, "obyektler-ve-ofisler", 1), (D_, "obyektler-ve-ofisler", 2),
                        (D_, "heyet-evleri", 3), (D_, "heyet-evleri", 4), (D_, "torpaq-sahesi", 5),
                        (D_, "menziller", 48826143)])
mp = load(os.path.join(HERE, "monitor.py"), "monitor_profile")
n_, d_ = mp._tap_profile_count_from_html(deniz, "apartments")
check(n_ == 1 and d_["total"] == 6 and d_["links"] == 6, "6638901 today: 6 ads, 1 flat -> 1 (flats only)",
      str(d_))
check(mp._tap_profile_count_from_html(deniz, "property")[0] == 6,
      "same seller with TAP_COUNT_SCOPE=property -> 6 -> agent")
check(mp._tap_profile_count_from_html(tap_profile(0, []), "apartments")[0] == 0,
      "22702915 today: '0 elan', nothing listed -> 0 (exact)")
part = tap_profile(40, [(D_, "menziller", 10 + i) for i in range(2)]
                   + [("elektronika", "telefonlar", 50 + i) for i in range(22)])
check(mp._tap_profile_count_from_html(part, "apartments")[0] is None,
      "list only partly loaded and below the limit -> not trusted")
big = tap_profile(40, [(D_, "menziller", 100 + i) for i in range(10)])
check(mp._tap_profile_count_from_html(big, "apartments")[0] == 10,
      "partly loaded but already 10 flats -> enough to call it an agent")
vip = tap_profile(2, [(D_, "menziller", 700 + i) for i in range(5)])
check(mp._tap_profile_count_from_html(vip, "apartments")[0] is None,
      "more ads on the page than the seller has (someone else's block) -> not trusted")
check(mp._tap_profile_count_from_html("<html>Just a moment... cf-chl</html>", "apartments")[0] is None,
      "blocked page -> not trusted")
phones = tap_profile(3, [(D_, "menziller", 900), ("elektronika", "telefonlar", 901),
                         ("neqliyyat", "avtomobiller", 902)])
check(mp._tap_profile_count_from_html(phones, "property")[0] == 1,
      "phones and cars never count, even with TAP_COUNT_SCOPE=property")
mp._RUNTIME["state"] = {"tap_profiles": {"555": {"n": 1, "t": iso(time.time())},
                                         "556": {"n": 6, "scope": "property", "t": iso(time.time())}}}
mp.TAP_PROFILE_BROWSER = False
check(mp.tap_apartment_count("555", {}) == 1 and mp.tap_apartment_count("556", {}) is None,
      "saved counts are only reused for the same rule (flats vs all property)")
mp.TAP_PROFILE_BROWSER = True
mp._RUNTIME["state"] = {"tap_profiles": {}}
browser_n = {"v": 6}
mp.tap_profile_count_browser = lambda uid: browser_n["v"]
install_tap(mp, tap_ad("Təmirli mənzil, sənədləri qaydasındadır", seller="Dəniz Xanim", uid="6638901"))
v17, _p, _s = mp.tap_check_owner("https://tap.az/elanlar/dasinmaz-emlak/menziller/48826143", {}, {},
                                 listing={"id": "48826143"})
check(v17 is False, "browser on: profile shows 6 -> AGENT (was 'unknown -> owner')", V[v17])
mp._RUNTIME["state"] = {"tap_profiles": {}}
browser_n["v"] = 1
v17b, _p, _s = mp.tap_check_owner("https://tap.az/elanlar/dasinmaz-emlak/menziller/48826144", {}, {},
                                  listing={"id": "48826144"})
check(v17b is True and "count:1-apartment(s)-ok" in mp.tap_check_owner.last_reasons,
      "browser on: profile shows 1 -> owner (now verified)", str(mp.tap_check_owner.last_reasons))
mp._RUNTIME["state"] = {"tap_profiles": {}}
browser_n["v"] = None
v17c, _p, _s = mp.tap_check_owner("https://tap.az/elanlar/dasinmaz-emlak/menziller/48826145", {}, {},
                                  listing={"id": "48826145"})
check(v17c is True and "count-unknown:defaulted-to-owner" in mp.tap_check_owner.last_reasons,
      "browser could not tell -> same as today (owner by default)")

print("\n[NOV-18] tap 49172190: owner wording with 'öz', seller id from the page's data")
OZ_OWNER = ["Men evin oz sahibiyem. xais edirem real almaq isteyenler narahat etsin.",
            "öz sahibiyəm", "oz sahibiyem", "öz evim", "oz evim", "öz evimdi", "oz evimdi",
            "Mənzilin öz sahibi mənəm", "Mən evin öz sahibiyəm"]
bad_oz = [t for t in OZ_OWNER if m.owner_signal_score(t, "", None)[0] < m.SCORE_OWNER_AT]
check(not bad_oz, f"{len(OZ_OWNER)} owner sentences with 'öz' (user, 11 Nov) are owner wording", str(bad_oz))
NOT_OWNER = ["öz eviniz kimi rahat yaşayın", "ev sahibi ilə birbaşa əlaqə", "sahibkar", "Təmirli mənzil satılır"]
bad_no = [t for t in NOT_OWNER if m.owner_signal_score(t, "", None)[0] != 0]
check(not bad_no, "look-alikes are not owner wording ('öz eviniz kimi', 'ev sahibi ilə')", str(bad_no))
DATA_USER = ('"currentUser":{"id":null,"legacyId":null},{"__typename":"User",'
             '"id":"Z2lkOi8vdGFwL1VzZXIvMjM5NTk4NzU","legacyId":"23959875","name":"S"},'
             '"adDetails:{\\"legacyId\\":\\"49172190\\"}')
check(m._tap_seller_id("<html>" + DATA_USER + "</html>") == ("23959875", "data"),
      "real page data of 49172190 -> seller 23959875 (base64 'gid://tap/User/23959875')")
check(m._tap_seller_id('<a href="/elanlar?user_id=6638901">x</a>') == ("6638901", "link"),
      "older pages: the profile link still works")
check(m._tap_seller_id("<html>" + DATA_USER + '{"__typename":"User","id":"Z2lkOi8vdGFwL1VzZXIvMTIz",'
                       '"legacyId":"123"}</html>') == (None, None), "two different users -> unknown")


def tap_new_page(desc):
    """Newer tap page: no profile link and no 'bütün elanları' text, seller only in data."""
    return ('<html><nav>Ana səhifə / Daşınmaz əmlak</nav>'
            f'<meta property="og:description" content="{desc}">'
            '<meta property="og:title" content="2-otaqlı mənzil, Əhmədli m., 65 m²">'
            f'<script>window.__DATA__={{{DATA_USER}}}</script></html>' + "x" * 3000)


m18 = load(os.path.join(HERE, "monitor.py"), "monitor_tapdata")
m18.TAP_PROFILE_BROWSER = True
prof18 = {"v": 6, "asked": []}
m18.tap_profile_count_browser = lambda uid: (prof18["asked"].append(uid), prof18["v"])[1]
U18 = "https://tap.az/elanlar/dasinmaz-emlak/menziller/49172190"
TXT18 = OZ_OWNER[0]
for desc, pv, want, label in [
        ("Təmirli mənzil, sənədləri qaydasındadır", 6, False, "neutral text + profile shows 6 -> AGENT"),
        (TXT18, 6, False, "'evin öz sahibiyəm' + profile shows 6 -> AGENT (3 or more always wins)"),
        (TXT18, 1, True, "'evin öz sahibiyəm' + profile shows 1 -> owner"),
        ("Təmirli mənzil, sənədləri qaydasındadır", None, True,
         "profile unreadable -> same as before (owner by default)")]:
    m18._RUNTIME["state"] = {"tap_profiles": {}}
    prof18["v"], prof18["asked"] = pv, []
    install_tap(m18, tap_new_page(desc))
    v18, _p, s18 = m18.tap_check_owner(U18, {}, {}, listing={"id": "49172190"})
    check(v18 is want and s18 == "23959875" and prof18["asked"] == ["23959875"], label,
          f"{V[v18]} seller={s18} {m18.tap_check_owner.last_reasons}")
m18._RUNTIME["state"] = {"tap_profiles": {}}
install_tap(m18, tap_new_page("Təmirli mənzil"))
v18h, _p, _s = m18.tap_check_owner(U18, {"23959875": {"2r-60m", "3r-80m"}}, {}, listing={"id": "49172190"})
check(v18h is False and "history:3-flats-seen" in m18.tap_check_owner.last_reasons,
      "seller now known -> the bot's own history counts too (2 seen + this one = 3)")
if old is not None and OLD_V == "2026-11-08-c":
    old.TAP_PROFILE_BROWSER = True
    old.tap_profile_count_browser = lambda uid: 6
    old._RUNTIME["state"] = {"tap_profiles": {}}
    install_tap(old, tap_new_page(TXT18))
    vo, _p, so = old.tap_check_owner(U18, {}, {}, listing={"id": "49172190"})
    check(vo is True and so is None, "OLD code: seller None -> sent as owner by default", f"{V[vo]} {so}")

print("\n[NOV-19] bina: 'mülkiyyətçi' label + agency wording in the ad's own text -> agent")
import html as html_mod
OG19 = "AZE🇦🇿 ENG🇬🇧 RUS🇷🇺\n\n🇦🇿 Əhmədlidə Neapol dairəsindən 2dəqiqə məsafədə yerləşən Leninqrad layihəli, 9/9, 3 otaqlı, tam yeni..."
FULL19 = OG19[:-3] + " təmirli mənzil satılır. Çıxarış var. 🇬🇧 ENG: 3-room flat near Neapol. 🇷🇺 RUS: квартира."


def bina_page(full, label="mülkiyyətçi", og=OG19, where="ld+json", extra=""):
    data = {"ld+json": '<script type="application/ld+json">' + json.dumps(
                {"@type": "Product", "name": "3 otaqlı", "description": full}, ensure_ascii=False) + "</script>",
            "data": "<script>window.__S__=" + json.dumps({"item": {"description": full}}) + "</script>",
            "html": '<div class="product-description__content">' + full.replace(". ", ".<br>") + "</div>",
            "none": ""}[where]
    return ("<html><head>" + f'<meta property="og:description" content="{html_mod.escape(og)}">' + "</head>"
            + f'<div class="owner"><div>Elvin</div><div>{label}</div></div>' + data + extra
            + '<a href="/agentlikler">Agentliklər</a></html>' + "x" * 2000)


mb = load(os.path.join(HERE, "monitor.py"), "monitor_binatext")
for where in ("ld+json", "data", "html"):
    d19, s19 = mb._bina_description(bina_page(FULL19 + " Ofis haqqı 1%", where=where))
    check(s19 == where and d19.rstrip().endswith("Ofis haqqı 1%"),
          f"full description found in the page ({where}), not just the cut og text", f"{s19} {len(d19)}")
other = ('<script>x={"description":"Başqa elan: Xətai, ofis haqqı 1%, zəng edin indi"}</script>')
d19, s19 = mb._bina_description(bina_page(FULL19, where="none", extra=other))
check(s19 == "og" and "ofis" not in mb.az_normalize(d19),
      "another ad's description on the page is never used (must start like og:description)")


def bina_check(page):
    mb.http_get = lambda url, headers=None, params=None, timeout=30, browser=False: R(page)
    return mb.bina_is_owner("https://bina.az/items/6058940")[0]


check(bina_check(bina_page(FULL19 + " Ofis haqqı 1% alınır.")) is False
      and "agency:ofis haqqi" in mb.bina_is_owner.last_text["reasons"],
      "label 'mülkiyyətçi' + 'Ofis haqqı 1%' at the END of the text -> AGENT",
      str(mb.bina_is_owner.last_text))
check(bina_check(bina_page(FULL19)) is True,
      "6058940-style AZE/ENG/RUS text without agency words -> stays owner (no 3-language rule)")
check(bina_check(bina_page("Komissiya yoxdur, sahibindən satılır. Əhmədli, 3 otaqlı, təmirli mənzil",
                           og="Komissiya yoxdur, sahibindən satılır. Əhmədli, 3 otaqlı, təmirli...")) is True,
      "'Komissiya yoxdur' (no fee) does not make an owner an agent")
check(bina_check(bina_page("Agentliyimiz sizə ən yaxşı mənzilləri təklif edir. Əhmədli 3 otaqlı",
                           og="Agentliyimiz sizə ən yaxşı mənzilləri təklif edir. Əhmədli 3...")) is False,
      "'Agentliyimiz ...' -> AGENT")
check(bina_check(bina_page(FULL19 + " Ofis haqqı 1%", label="vasitəçi (agent)")) is False
      and bina_check(bina_page(FULL19 + " Ofis haqqı 1%", label="")) is None,
      "agent label and no label behave as before")
check(m.owner_signal_score("Komissiyasız. Vasitəçilik haqqı yoxdur. Ofis haqqı alınmır.", "", None)[0] == 0
      and m.owner_signal_score("OFİS HAQQI 1%", "", None)[0] <= m.SCORE_AGENT_AT,
      "tap too: 'komissiyasız / haqqı yoxdur / alınmır' are no longer agent words; 'ofis haqqı 1%' still is")
if old is not None and OLD_V == "2026-11-11-a":
    old.http_get = lambda url, headers=None, params=None, timeout=30, browser=False: R(
        bina_page(FULL19 + " Ofis haqqı 1% alınır."))
    check(old.bina_is_owner("https://bina.az/items/6058940")[0] is True,
          "OLD code: 'mülkiyyətçi' label alone -> sent as owner")

print("\n[NOV-20] bina: owner label is only overruled by STRONG agency proof (12 Nov)")
OWNER_TEXTS = ["Eksklüziv təmirli mənzil, Əhmədli metrosuna yaxın, kupça var, təcili satılır",
               "Xahiş edirəm agentliklər zəng vurmasın. Əhmədli, 3 otaqlı, təmirli mənzil satılır",
               "Kupça var. Agentliklərə satılmır. Əhmədli metrosu yaxınlığında 2 otaqlı mənzil",
               "Komissiya ödəmirəm, makler haqqı vermirəm. Təmirli 3 otaqlı mənzil satılır",
               "Agentliklər və maklerlər zəng etməsin. Təmirli 2 otaqlı mənzil, kupça var"]
for t in OWNER_TEXTS:
    check(bina_check(bina_page(t, og=t[:60] + "...")) is True, "stays OWNER: " + t[:48],
          str(mb.bina_is_owner.last_text))
AGENT_TEXTS = ["Əhmədli, 3 otaqlı təmirli mənzil satılır. Kupça var. Ofis haqqı 1%",
               "Agentliyimiz sizə ən yaxşı mənzilləri təklif edir. Əhmədli 3 otaqlı",
               "Təmirli mənzil satılır, komissiya 1%, baxış üçün zəng edin, Əhmədli metrosu"]
for t in AGENT_TEXTS:
    check(bina_check(bina_page(t, og=t[:60] + "...")) is False, "AGENT: " + t[:48],
          str(mb.bina_is_owner.last_text))
if old is not None and OLD_V == "2026-11-11-b":
    old.http_get = lambda url, headers=None, params=None, timeout=30, browser=False: R(
        bina_page(OWNER_TEXTS[1], og=OWNER_TEXTS[1][:60] + "..."))
    check(old.bina_is_owner("https://bina.az/items/1")[0] is False,
          "OLD code: owner who wrote 'agentliklər zəng vurmasın' was filed as AGENT")

print("\n[NOV-21] bina 6618907: shown in Koroğlu, tagged Qara Qarayev -> wanted")


def tag_links(tags, suffix="/kohne-tikili"):
    return "<ul>" + "".join(f'<li><a class="t" href="https://bina.az/baki/{slug}/alqi-satqi/menziller{suffix}">'
                            f"{text}</a></li>\n" for slug, text in tags) + "</ul>"


ALL_METROS = [("koroglu", "Koroğlu m."), ("qara-qarayev", "Qara Qarayev m."), ("neftciler", "Neftçilər m."),
              ("nizami", "Nizami m."), ("28-may", "28 May m."), ("genclik", "Gənclik m."), ("sahil", "Sahil m."),
              ("ulduz", "Ulduz m."), ("bakmil", "Bakmil m."), ("xetai", "Xətai m.")]
FOOTER = "<footer><h3>Populyar</h3>" + tag_links(ALL_METROS, "") + "</footer>"


def tag_page(tags, label="mülkiyyətçi"):
    return ('<html><nav><a href="/baki/alqi-satqi/menziller">Mənzillər</a></nav>'
            f'<div class="owner"><div>Elvin</div><div>{label}</div></div>'
            '<div class="desc">Təmirli mənzil</div>' + tag_links(tags) + FOOTER + "x" * 2000 + "</html>")


T_BOTH = [("nizami", "Nizami r."), ("koroglu", "Koroğlu m."), ("qara-qarayev", "Qara Qarayev m.")]
T_ONLY = [("nizami", "Nizami r."), ("koroglu", "Koroğlu m.")]
mg = load(os.path.join(HERE, "monitor.py"), "monitor_tags")
check(mg._bina_tags_from_html(tag_page(T_BOTH), "Koroğlu") == ["Nizami r.", "Koroğlu m.", "Qara Qarayev m."],
      "tags read from the ad page (the footer list of all metros is not taken)")
check(mg._bina_wanted_tag(["Nizami r.", "Koroğlu m.", "Qara Qarayev m."]) == "Qara Qarayev m."
      and mg._bina_wanted_tag(["Nizami r.", "Koroğlu m."]) is None,
      "Qara Qarayev tag = wanted; a district tag alone (Nizami r.) is not")
check(mg._bina_tags_from_html("<html>" + FOOTER + "</html>", "Koroğlu") is None,
      "page with only the big footer list -> tags unknown (never guessed)")
mg.PAGE_DELAY = 0
mg._BINA_MAX_ID["v"] = 6619000
pages_g = {}
fetch_g = []


def http_g(url, headers=None, params=None, timeout=30, browser=False):
    fetch_g.append(url)
    return R(pages_g[url]) if url in pages_g else RH(404, "")


mg.http_get = http_g
sent_g = []
mg.notify_new_owner_msg = lambda l, n, ol="": sent_g.append((l["id"], l.get("location"))) or True
bsrc_g = {"name": "bina.az", "type": "bina", "prefix": "", "mode": "price_owner",
          "owner_label": "Mülkiyyətçi", "url": "https://bina.az/x"}


def bl(iid, loc="Koroğlu m."):
    l = {"id": str(iid), "url": f"https://bina.az/items/{iid}", "price": 150000, "rooms": 2,
         "area": 60.0, "location": loc}
    return l


def bina_run(st, plain, excluded):
    """One bot run for bina: `excluded` = ads whose shown location is excluded."""
    for l in excluded:
        l["_excluded_by"] = "Koroğlu"
    mg._BINA_TAG_CANDS["items"] = list(excluded)
    seen_ = st["listings"]
    items = list(plain) + mg.bina_rescue_by_tags(bsrc_g, seen_, st)
    n1, deferred, decided = mg.process_new_owner_checks(items, bsrc_g, seen_, st["seeded"],
                                                        tries_map=st.setdefault("owner_tries", {}))
    mg.process_source(items, bsrc_g, seen_, skip_ids=deferred, owner_map=decided)
    mg._bina_stamp_tags(items, bsrc_g, seen_)
    mg._BINA_PAGE_CACHE.clear()


st_g = {"listings": {"6000001": {"url": "u", "price": 1, "first_seen": iso(time.time() - 30 * day),
                                 "source": "bina.az", "owner": False}},
        "seeded": {"bina.az:owner_seeded": True, "bina.az:url": mg._url_fingerprint(bsrc_g["url"])}}
pages_g.update({"https://bina.az/items/6618907": tag_page(T_BOTH),
                "https://bina.az/items/6618800": tag_page(T_ONLY),
                "https://bina.az/items/6618700": tag_page(T_BOTH, label="vasitəçi (agent)")})
old_ads = [bl(6100000 + i) for i in range(40)]            # months old by id
with contextlib.redirect_stdout(io.StringIO()):
    bina_run(st_g, [], [bl(6618907), bl(6618800), bl(6618700)] + old_ads)
L_g = st_g["listings"]
check(sent_g == [("6618907", "Nizami r., Koroğlu m., Qara Qarayev m.")],
      "first run: 6618907 is sent, with all its tags as the address", str(sent_g))
check(L_g["6618907"].get("owner") is True and L_g["6618907"].get("tags_ok") is True,
      "it is recorded as sent + 'wanted by tag'")
check(L_g.get("6618800", {}).get("skip") == "area" and "owner" not in L_g["6618800"],
      "pure Koroğlu ad (no wanted tag): not sent, never looked at again")
check(L_g.get("6618700", {}).get("owner") is False, "wanted by tag but an agent: not sent")
check(all(L_g.get(str(6100000 + i), {}).get("skip") == "area-old" for i in range(40))
      and len(fetch_g) == 3, "first run: 40 old excluded ads recorded silently, no page read for them",
      str(len(fetch_g)))
check(fetch_g.count("https://bina.az/items/6618907") == 1,
      "the ad's page is read once (tags and owner check share it)")
fetch_g.clear()
sent_g.clear()
with contextlib.redirect_stdout(io.StringIO()):
    bina_run(st_g, [], [dict(bl(6618907), price=140000), bl(6618800), bl(6618700)] + old_ads)
check(sent_g == [] and fetch_g == [], "next run: nothing sent again, no page read again",
      f"{sent_g} {fetch_g}")
check(L_g["6618907"]["price"] == 140000, "the wanted-by-tag ad is price-tracked like any other")
pages_g["https://bina.az/items/6618950"] = "<html>" + FOOTER + "x" * 2000 + "</html>"   # tags not found
for _ in range(mg.OWNER_MAX_TRIES + 1):
    with contextlib.redirect_stdout(io.StringIO()):
        bina_run(st_g, [], [bl(6618950)])
check(L_g.get("6618950", {}).get("skip") == "area-unknown"
      and fetch_g.count("https://bina.az/items/6618950") == mg.OWNER_MAX_TRIES and sent_g == [],
      f"tags unreadable: tried {mg.OWNER_MAX_TRIES} times, then left alone (as before: not sent)")
fetch_g.clear()
with contextlib.redirect_stdout(io.StringIO()):
    bina_run(st_g, [], [bl(6618960)])                      # page cannot be fetched at all
check("6618960" not in L_g, "page not fetched: nothing recorded, tried again next run")
many = [bl(6618000 + i) for i in range(50)]
for l_ in many:
    pages_g[l_["url"]] = tag_page(T_ONLY)
fetch_g.clear()
with contextlib.redirect_stdout(io.StringIO()):
    bina_run(st_g, [], many)
check(len(fetch_g) <= mg.BINA_TAG_MAX_PER_RUN + mg.OWNER_FETCH_FAIL_STREAK,
      f"at most {mg.BINA_TAG_MAX_PER_RUN} tag pages per run", str(len(fetch_g)))
c_g = mg.bina_check("https://bina.az/x")
check(mg.bina_passes(bl(1, "Koroğlu m."), c_g) is False and mg.bina_passes(bl(2, "Əhmədli q."), c_g) is True,
      "bina_passes itself is unchanged (helper scripts keep working)")

print("\n[NOV-22] bina search: one bad page no longer throws the whole run away")
mf = load(os.path.join(HERE, "monitor.py"), "monitor_binafetch")
mf.PAGE_DELAY = 0
mf.time = Clock()


def gql_page(ids, nxt, loc="Əhmədli q."):
    return {"data": {"itemsConnection": {"totalCount": 4, "edges": [
        {"node": {"id": str(i), "path": f"/items/{i}", "rooms": 2, "price": {"total": 100000},
                  "area": {"value": 60}, "location": {"id": "1", "fullName": loc if i != 6618907 else "Koroğlu m."}}}
        for i in ids], "pageInfo": {"hasNextPage": bool(nxt), "endCursor": nxt}}}}


class Sess:
    def __init__(self, script):
        self.script, self.calls, self.closed = script, 0, False

    def get(self, url, params=None, headers=None, timeout=30):
        self.calls += 1
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step

        class Resp:
            status_code = step[0]

            def json(self_inner):
                if step[1] is None:
                    raise ValueError("not json")
                return step[1]
        return Resp()

    def close(self):
        self.closed = True


sess = Sess([(200, gql_page([6618901, 6618902], "c1")), mf.requests.ConnectionError("reset"),
             (502, None), (200, gql_page([6618907, 6618903], None))])
mf.requests.Session = lambda: sess
got_f = mf.fetch_bina("https://bina.az/baki/alqi-satqi/menziller?location_ids%5B%5D=51")
check([x["id"] for x in got_f] == ["6618901", "6618902", "6618903"] and sess.calls == 4 and sess.closed,
      "network error + HTTP 502 on page 2 are retried; the scan finishes", str(sess.calls))
check([x["id"] for x in mf._BINA_TAG_CANDS["items"]] == ["6618907"],
      "the Koroğlu ad is not in the normal list; it waits for the tag check")
sess = Sess([(200, gql_page([1], "c1"))] + [(503, None)] * 3)
try:
    mf.fetch_bina("https://bina.az/baki/alqi-satqi/menziller")
    failed_f = False
except RuntimeError as e_:
    failed_f = "HTTP 503" in str(e_)
check(failed_f and sess.calls == 4, "a page that keeps failing still stops the run with a clear reason")

print("\n[NOV-23] collect_bina.py: table of ads placed since 01.01.2026 (read-only)")
if os.path.exists(os.path.join(HERE, "collect_bina.py")):
    mc = load(os.path.join(HERE, "monitor.py"), "monitor")
    sys.modules["monitor"] = mc
    tmpc = tempfile.mkdtemp()
    mc.STATE_FILE = os.path.join(tmpc, "seen.json")
    mc.USE_API = False
    mc.PAGE_DELAY = 0
    _noon = _dt.datetime.now(_dt.timezone.utc).replace(hour=8, minute=0, second=0, microsecond=0)
    seen_c, TOP_C = {}, 6_620_000
    for d in range(80):                                # 80 days of bot history, 2800 ids a day
        for j in range(4):
            iid = TOP_C - d * 2800 - j * 500
            seen_c[str(iid)] = {"url": f"https://bina.az/items/{iid}", "price": 1, "source": "bina.az",
                                "first_seen": (_noon - _dt.timedelta(days=d)).isoformat(timespec="seconds"),
                                "owner": (j == 0)}
    with open(mc.STATE_FILE, "w", encoding="utf-8") as fh_:
        json.dump({"listings": seen_c}, fh_)
    before_c = open(mc.STATE_FILE, encoding="utf-8").read()
    days_to_jan1 = (_noon - _dt.datetime(2026, 1, 1, tzinfo=_dt.timezone.utc)).days


    def ad_c(iid, **kw):
        base = {"id": str(iid), "url": f"https://bina.az/items/{iid}", "price": 150000, "currency": "AZN",
                "rooms": 3, "area": 80.0, "floor": 4, "floors": 9, "location": "Əhmədli q.",
                "has_bill_of_sale": True, "has_mortgage": False, "has_repair": True}
        base.update(kw)
        return base


    ID_DEC = TOP_C - (days_to_jan1 + 20) * 2800           # ~20 days before 1 Jan 2026
    ID_FEB = TOP_C - (days_to_jan1 - 40) * 2800           # ~10 Feb 2026
    live_c = [ad_c(TOP_C), ad_c(TOP_C - 500), ad_c(ID_FEB), ad_c(ID_DEC),
              ad_c(5_000_000, created_at="2026-03-05T10:00:00+04:00"),      # old number, bina's own date
              ad_c(TOP_C - 77, location='Nizami r., "Koroğlu" m.')]
    kor_c = [dict(ad_c(TOP_C - 900 - i, location="Koroğlu m."), _excluded_by="Koroğlu") for i in range(4)]
    seen_c[str(TOP_C - 900)] = {"url": "u", "first_seen": _noon.isoformat(timespec="seconds"),
                                "source": "bina.az", "skip": "area", "tags": "Nizami r., Koroğlu m."}
    with open(mc.STATE_FILE, "w", encoding="utf-8") as fh_:
        json.dump({"listings": seen_c}, fh_)
    before_c = open(mc.STATE_FILE, encoding="utf-8").read()
    tag_reads = []


    def tags_c(l):
        tag_reads.append(l["id"])
        mc.bina_location_tags.last_reason = "ok"
        return (["Nizami r.", "Koroğlu m.", "Qara Qarayev m."] if l["id"] == str(TOP_C - 901)
                else ["Nizami r.", "Koroğlu m."])


    mc.bina_location_tags = tags_c
    mc.fetch_bina = lambda url: (mc._BINA_TAG_CANDS.__setitem__("items", [dict(x) for x in kor_c]),
                                 list(live_c))[1]
    mc._BINA_MAX_ID["v"] = TOP_C
    owner_reads = []
    mc.bina_is_owner = lambda url: (owner_reads.append(url),
                                    setattr(mc.bina_is_owner, "last_reason", "ok"), (True, None))[2]
    sent_c = []
    mc.tg_send_message = lambda text: sent_c.append(text) or True
    os.environ["OUT"] = os.path.join(tmpc, "out.csv")
    os.environ["COLLECT_CACHE"] = os.path.join(tmpc, "cache.json")
    cb = load(os.path.join(HERE, "collect_bina.py"), "collect_bina")
    cb.time = Clock()
    with contextlib.redirect_stdout(io.StringIO()) as buf_c:
        cb.main()
    first_tag_reads = list(tag_reads)
    import csv as _csv
    rows_c = list(_csv.DictReader(open(os.environ["OUT"], encoding="utf-8-sig")))
    by_id = {r_["id"]: r_ for r_ in rows_c}
    check(sorted(first_tag_reads) == sorted(str(TOP_C - 900 - i) for i in (1, 2, 3))
          and by_id.get(str(TOP_C - 901), {}).get("location") == "Nizami r., Koroğlu m., Qara Qarayev m."
          and str(TOP_C - 902) not in by_id and str(TOP_C - 900) not in by_id,
          "Koroğlu ads are opened once: the one tagged Qara Qarayev is in the table, the others are not",
          str(first_tag_reads))
    by_id.pop(str(TOP_C - 901), None)
    check(set(by_id) == {str(TOP_C), str(TOP_C - 500), str(ID_FEB), "5000000", str(TOP_C - 77)},
          "ads since 01.01.2026 are in the table; the December 2025 one is not", str(sorted(by_id)))
    check(by_id["5000000"]["placed"] == "05.03.2026" and by_id["5000000"]["placed_how"] == "bina",
          "bina's own date is used when the search gives one")
    est_c = _dt.datetime.strptime(by_id[str(ID_FEB)]["placed"], "%d.%m.%Y")
    want_c = (_noon - _dt.timedelta(days=days_to_jan1 - 40)).replace(tzinfo=None)
    check(abs((est_c - want_c).days) <= 2 and by_id[str(ID_FEB)]["placed_how"] == "estimate",
          "otherwise the date is worked out from the ad number (within 2 days here)",
          f"{by_id[str(ID_FEB)]['placed']} vs {want_c:%d.%m.%Y}")
    check(by_id[str(TOP_C)]["seller"] == "owner" and by_id[str(TOP_C - 500)]["seller"] == "agent"
          and by_id[str(ID_FEB)]["seller"] == "?", "seller column comes from the bot's history")
    check(by_id[str(TOP_C)]["link"] == f"https://bina.az/items/{TOP_C}" and by_id[str(TOP_C)]["floor"] == "4/9"
          and by_id[str(TOP_C)]["cixaris"] == "var" and by_id[str(TOP_C - 77)]["location"] == 'Nizami r., "Koroğlu" m.',
          "link, floor, çıxarış and a location with commas/quotes are written correctly")
    check(rows_c[0]["id"] == str(TOP_C) and owner_reads == [] and sent_c == []
          and open(mc.STATE_FILE, encoding="utf-8").read() == before_c,
          "newest first; no page read, nothing sent, seen.json untouched")
    cb.OWNERS, cb.LIMIT = True, 1
    cb.time = Clock()
    with contextlib.redirect_stdout(io.StringIO()):
        cb.main()
    rows_c = list(_csv.DictReader(open(os.environ["OUT"], encoding="utf-8-sig")))
    unknown_c = [r_["id"] for r_ in rows_c if r_["seller"] == "?"]
    check(len(owner_reads) == 1 and len(unknown_c) == 3
          and open(mc.STATE_FILE, encoding="utf-8").read() == before_c,
          "OWNERS=1 LIMIT=1: one page read, answer kept in the side file, seen.json untouched",
          f"{len(owner_reads)} {unknown_c}")
    with contextlib.redirect_stdout(io.StringIO()):
        cb.main()
    check(len(owner_reads) == 2 and len(set(owner_reads)) == 2, "running again continues with the next ad")
    check(tag_reads == first_tag_reads, "tags are remembered: no Koroğlu ad is opened a second time")
    os.environ.pop("OUT", None)
    os.environ.pop("COLLECT_CACHE", None)
    sys.modules.pop("monitor", None)

print("\n[NOV-24] lalafo 118119487: three searches merged, 'Orta təmir' and no-owner-filter ads kept")
ml = load(os.path.join(HERE, "monitor.py"), "monitor_lalafo")
ml.PAGE_DELAY = 0
ml.time = Clock(time.time())
urls24 = ml._lalafo_urls(ml.LALAFO_SEARCH_URL)
check(len(urls24) == 3 and all("/owner" not in u and "fresh-renovation" not in u and u.endswith("/kupchaya")
                               for u in urls24)
      and "hatainskij" in urls24[0] and "azi-aslanov" in urls24[1] and "pos-azi-aslanov" in urls24[2]
      and "azi-aslanov/kupchaya" in urls24[1] and "hatainskij" not in urls24[1],
      "default: districts | metros | settlements, each with Kupça only", str(urls24))


def la_item(iid, slug="hmdli-2-otaqli", ttype="Mülkiyyətçi", created=None, title=None, repair="Orta təmir"):
    c = int(ml.time.t - 10 * day) if created is None else int(created)
    params = [{"name": "Təmir", "value": repair, "id": 352}, {"name": "Sənədlər", "value": "Kupça (Çıxarış)", "id": 888}]
    if ttype:
        params.append({"name": "Təklifin növü", "value": ttype, "id": 256})
    return {"id": int(iid), "price": 150000, "symbol": "AZN", "url": f"/baku/ads/baki-{slug}-id-{iid}",
            "title": title or "Bakı, 2 otaqlı, Köhnə tikili, m. Əhmədli, 60 kv. m", "description": "Təmirli mənzil",
            "created_time": c, "updated_time": c, "params": params, "images": []}


def la_page(items_, cur, pages):
    nd = {"props": {"initialState": {"listing": {"listingFeed": {
        "items": items_, "_meta": {"currentPage": cur, "pageCount": pages, "totalCount": 40 * pages},
        "_links": ({"next": {"href": "x"}} if cur < pages else {})}}}}}
    return ('<html><script id="__NEXT_DATA__" type="application/json">' + json.dumps(nd) + "</script>"
            + "x" * 60000 + "</html>")


AD24 = la_item(118119487, slug="3-otaqli-kohn-tikili-m-hzi-aslanov-61-kv-m", ttype="Maklerlər narahat etməsin",
               created=ml.time.t - 3.6 * day, title="Bakı, 3 otaqlı, Köhnə tikili, m. Həzi Aslanov, 61 kv. m")
site24 = {0: [[la_item(1001), AD24], [la_item(1002)]],                 # districts
          1: [[AD24, la_item(1003)]],                                  # metros
          2: [[la_item(1004)]]}                                        # settlements
la_reads = []


def la_get(purl, tries=4):
    idx = next(i for i, u in enumerate(urls24) if purl.split("?")[0] == u)
    n = int(ml.parse_qs(ml.urlparse(purl).query).get("page", ["1"])[0])
    la_reads.append((idx, n))
    pages_ = site24[idx]
    body = la_page(pages_[n - 1] if n <= len(pages_) else [], n, len(pages_))
    return RH(200, body), body


ml._fetch_lalafo_html = la_get
got24 = ml.fetch_lalafo(ml.LALAFO_SEARCH_URL)
check(sorted(x["id"] for x in got24) == ["1001", "1002", "1003", "1004", "118119487"]
      and la_reads == [(0, 1), (0, 2), (1, 1), (2, 1)],
      "all three searches are read and merged; the ad in two of them appears once", str(la_reads))
ad24 = next(x for x in got24 if x["id"] == "118119487")
check(ml.lalafo_ok(ad24) and ad24["_agent"] is False
      and ml._lalafo_seller_type(AD24) == "owner" and ml._lalafo_seller_type(la_item(5, ttype="Vasitəçi")) == "agent"
      and ml._lalafo_seller_type(la_item(6, ttype=None)) is None,
      "'Maklerlər narahat etməsin' = owner type; 'Vasitəçi' = agent; none chosen = unknown")
lsrc24 = {"name": "lalafo.az", "type": "lalafo", "prefix": "lala:", "mode": "owner_new",
          "owner_label": "Mülkiyyətçi", "prefiltered_owner": True, "url": ml.LALAFO_SEARCH_URL}
OLD_LINK = "https://lalafo.az/baku/kvartiry/prodazha-kvartir/owner/nizaminskij"
seen24 = {"lala:1": {"url": "u", "first_seen": iso(ml.time.t - 60 * day), "source": "lalafo.az", "owner": True}}
flags24 = {"lalafo.az": True, "lalafo.az:url": ml._url_fingerprint(OLD_LINK)}
sent24 = []
ml.notify_new_owner_msg = lambda l, n, ol="": sent24.append((l["id"], bool(l.get("_age_days")))) or True
feed24 = [dict(x) for x in got24]
feed24.append(ml._parse_lalafo_page(la_page([la_item(2001, ttype="Vasitəçi", created=ml.time.t - day)], 1, 1))[0][0])
feed24.append(ml._parse_lalafo_page(la_page([la_item(2002, ttype=None, created=ml.time.t - 3600)], 1, 1))[0][0])
with contextlib.redirect_stdout(io.StringIO()):
    ml.process_owner_new([dict(x) for x in feed24], lsrc24, seen24, flags24)
check(sorted(sent24) == [("118119487", True), ("2002", False)],
      "links changed: the ad placed 3.6 days ago is sent (marked old), the 1-hour-old one as new", str(sent24))
check(all("owner" not in seen24[f"lala:{i}"] for i in (1001, 1002, 1003, 1004)),
      "the 10-day-old ads are recorded silently (no flood)")
check(seen24["lala:2001"].get("owner") is False and seen24["lala:2001"].get("skip") == "agent",
      "'Vasitəçi' ad in the wider search is filed as agent, not sent")
sent24.clear()
with contextlib.redirect_stdout(io.StringIO()):
    ml.process_owner_new([dict(x) for x in feed24], lsrc24, seen24, flags24)
check(sent24 == [], "next run: nothing is sent twice")

big24 = [[la_item(3000 + p * 40 + i, created=ml.time.t - 20 * day) for i in range(40)] for p in range(10)]
site24.update({0: big24, 1: [[la_item(3000)]], 2: [[la_item(3001)]]})
known24 = {f"lala:{it_['id']}": {"url": "u", "first_seen": "x", "source": "lalafo.az", "owner": False}
           for pg in big24 for it_ in pg}
act24 = (lambda l: ml._candidate_kind("lalafo", l, known24.get("lala:" + str(l["id"])), ml.time.t) is not None)
la_reads.clear()
ml.fetch_lalafo(ml.LALAFO_SEARCH_URL, actionable=act24)
check([n for i, n in la_reads if i == 0] == [1, 2, 3],
      f"normal run: stops after {ml.LALAFO_QUIET_PAGES} pages with nothing new", str(la_reads))
known24.pop("lala:%d" % (3000 + 4 * 40 + 7))                           # unknown ad on page 5
big24[1][3]["updated_time"] = int(ml.time.t - 3600)                    # pushed up an hour ago, page 2
la_reads.clear()
got_b = ml.fetch_lalafo(ml.LALAFO_SEARCH_URL, actionable=act24)
check([n for i, n in la_reads if i == 0] == [1, 2, 3, 4, 5, 6, 7, 8],
      "a recently updated ad and an unknown ad keep it reading", str(la_reads))
la_reads.clear()
ml.fetch_lalafo(ml.LALAFO_SEARCH_URL)
check(len([1 for i, n in la_reads if i == 0]) == 10, "full read: every page")
site24[0] = [[la_item(4001)], [la_item(4001)], [la_item(4001)]]


def la_get_loop(purl, tries=4):
    la_reads.append(purl)
    body = la_page([la_item(4001)], 1, 99)                             # site ignores ?page
    return RH(200, body), body


ml._fetch_lalafo_html = la_get_loop
la_reads.clear()
ml.fetch_lalafo(urls24[0])
check(len(la_reads) == 2, "a site that repeats the same page does not loop forever", str(len(la_reads)))
if old is not None and OLD_V == "2026-11-12-a":
    check("/owner/" in old.LALAFO_SEARCH_URL and "fresh-renovation" in old.LALAFO_SEARCH_URL
          and "|" not in old.LALAFO_SEARCH_URL, "OLD code: one link with every filter joined by AND")

print("\n[NOV-25] yeniemlak timeout (17 Nov): retried; one failed run sends no alert")
my = load(os.path.join(HERE, "monitor.py"), "monitor_yetimeout")
my.PAGE_DELAY = 0
my.time = Clock(time.time())
TODAY_Y = baku_date(my.time.t)
ye_script, ye_calls = [], []


def ye_get(url, headers=None, params=None, timeout=30, browser=False):
    ye_calls.append(url)
    step = ye_script.pop(0) if ye_script else "empty"
    if step == "timeout":
        raise RuntimeError("Failed to perform, curl: (28) Operation timed out after 30007 milliseconds")
    if step == "empty":
        return RH(200, "<html>Nəticə: 2</html>")
    return RH(200, ye_page(step))


my.http_get = ye_get
ye_script[:] = ["timeout", "timeout", [(240001, TODAY_Y, XETAI)], [(240002, TODAY_Y, XETAI)], "empty"]
got_y = my.fetch_yeniemlak("https://yeniemlak.az/x")
check([x["id"] for x in got_y] == ["240001", "240002"], "page 1 timed out twice, third try worked: nothing lost",
      str(len(ye_calls)))
ye_calls.clear()
ye_script[:] = [[(240001, TODAY_Y, XETAI)], "timeout", "timeout", "timeout"]
got_y = my.fetch_yeniemlak("https://yeniemlak.az/x")
check([x["id"] for x in got_y] == ["240001"] and len(ye_calls) == 4,
      "page 2 never answers: the ads already read are kept (not thrown away)")
ye_script[:] = ["timeout"] * 3
try:
    my.fetch_yeniemlak("https://yeniemlak.az/x")
    raised_y = False
except RuntimeError as e_:
    raised_y = "timed out" in str(e_)
check(raised_y, "page 1 never answers: the run reports it")

ma = load(os.path.join(HERE, "monitor.py"), "monitor_alerts")
tmpa = tempfile.mkdtemp()
ma.STATE_FILE = os.path.join(tmpa, "seen.json")
ma.USE_API = False
ma.SOURCES = [dict(s_) for s_ in ma.SOURCES if s_["name"] == "yeniemlak.az"]
tg_a = []
ma.tg_send_message = lambda text: tg_a.append(text) or True
mode_a = {"fail": True, "n": 0}


def fetch_a(src):
    mode_a["n"] += 1
    if mode_a["fail"]:
        raise RuntimeError(f"Failed to perform, curl: (28) Operation timed out after {30000 + mode_a['n']} milliseconds")
    return []


ma.fetch_for_source = fetch_a


def run_a():
    with contextlib.redirect_stdout(io.StringIO()):
        ma.main()
    return len(tg_a)


check(run_a() == 0 and run_a() == 0, "1st and 2nd failed run: no message")
check(run_a() == 1 and "3 runs in a row" in tg_a[-1], "3rd failed run in a row: one alert", str(tg_a[-1:])[:90])
check(run_a() == 1 and run_a() == 1, "still failing (different timeout numbers): no repeated alerts")
mode_a["fail"] = False
check(run_a() == 2 and "working again" in tg_a[-1], "back again: one 'working again' message")
mode_a["fail"] = True
run_a()
mode_a["fail"] = False
check(run_a() == 2, "a single failed run later: no alert and no 'working again'")
st_a = json.load(open(ma.STATE_FILE, encoding="utf-8"))
st_a["source_status"]["yeniemlak.az"] = "old error text"          # state written by an older version
st_a.pop("source_fails", None)
json.dump(st_a, open(ma.STATE_FILE, "w", encoding="utf-8"))
check(run_a() == 3 and "working again" in tg_a[-1], "outage alerted by the old version still gets its 'working again'")
if old is not None and OLD_V == "2026-11-15-a":
    old.http_get = ye_get
    old.PAGE_DELAY = 0
    ye_script[:] = ["timeout", [(240001, TODAY_Y, XETAI)], "empty"]
    try:
        old.fetch_yeniemlak("https://yeniemlak.az/x")
        old_ok = True
    except Exception:
        old_ok = False
    check(not old_ok, "OLD code: one timeout on page 1 lost the whole yeniemlak run")

print("\n[NOV-10] re-checking a recorded tap ad does not count it twice")
mt2 = load(os.path.join(HERE, "monitor.py"), "monitor_tapcount")
install_tap(mt2, tap_ad("Təmirli mənzil, sənədləri qaydasındadır", seller="Aynur", uid="777"))
seen10 = {"tap:48100001": {"url": "u", "first_seen": iso(time.time() - day), "source": "tap.az",
                           "owner": False, "seller": "777"},
          "tap:48100002": {"url": "u", "first_seen": iso(time.time() - day), "source": "tap.az",
                           "owner": False, "seller": "777"}}
v10, _p, _s = mt2.tap_check_owner("https://tap.az/elanlar/dasinmaz-emlak/menziller/48100001",
                                  mt2._seller_apartments(seen10), {}, listing={"id": "48100001"})
check(v10 is not False, "seller with 2 ads (this one + 1) is NOT made a 3-flat agent", V[v10])
check("count-unknown:defaulted-to-owner" in mt2.tap_check_owner.last_reasons,
      "reasons are exposed for the recovery script", str(mt2.tap_check_owner.last_reasons))

print("\n[NOV-11] health report: Telegram delivery + what 'source ok' did not show")
mh = load(os.path.join(HERE, "monitor.py"), "monitor")
sys.modules["monitor"] = mh
hh = load(os.path.join(HERE, "health.py"), "health")
now_h = _dt.datetime.now(_dt.timezone.utc)
log_lines = []
for i in range(24):
    t_ = (now_h - _dt.timedelta(minutes=15 * (24 - i))).isoformat(timespec="seconds")
    log_lines += [f"monitor.py 2026-11-07-b starting {t_}\n",
                  "tap.az: checked 2 new, announced 1 owner posts\n",
                  "yeniemlak.az: RATE-LIMITED - HTTP 429 again after waiting; no more\n",
                  "yeniemlak.az: checked 0 new, announced 0 owner posts\n",
                  "bina.az: owner-checked 3 new, announced 0, deferred 0\n",
                  "Done. notified=1 saved=True(local)\n"]
old_t = (now_h - _dt.timedelta(hours=30)).isoformat(timespec="seconds")
log_lines = [f"monitor.py 2026-11-07-a starting {old_t}\n",
             "yeniemlak.az: checked 9 new, announced 4 owner posts\n"] + log_lines
act = hh.scan_runs(log_lines, now_h)
check(act["runs"] == 24 and act["announced"].get("tap.az") == 24
      and act["checked"].get("yeniemlak.az", 0) == 0, "last-24h totals per source (older run ignored)",
      str(act))
iss = hh.activity_issues(act)
check(any("yeniemlak.az refused the bot (HTTP 429" in x for x in iss)
      and any("yeniemlak.az: not a single listing" in x for x in iss)
      and not any(x.startswith("tap.az") for x in iss), "yeniemlak silence is reported", str(iss))
tmpd = tempfile.mkdtemp()
with open(os.path.join(tmpd, "run.log"), "w", encoding="utf-8") as fh_:
    fh_.writelines(log_lines)
with open(os.path.join(tmpd, "seen.json"), "w", encoding="utf-8") as fh_:
    json.dump({"listings": {"a": {}}}, fh_)
hh.LOG = os.path.join(tmpd, "run.log")
mh.STATE_FILE = os.path.join(tmpd, "seen.json")
tg_out = []
mh.tg_send_message = lambda text: tg_out.append(text) or True
os.environ["TG"] = "1"
try:
    with contextlib.redirect_stdout(io.StringIO()):
        rc_ = hh.main()
finally:
    os.environ.pop("TG", None)
check(len(tg_out) == 1 and "PROBLEM" in tg_out[0] and "HTTP 429" in tg_out[0] and rc_ == 1,
      "TG=1 really sends the report (it used to call a function that does not exist)")
check(not hasattr(mh, "tg_send"), "monitor has no tg_send (so health must not use it)")

print("\n[REC] recover_missed.py sends only PROVEN tap owners by default")
mr = load(os.path.join(HERE, "monitor.py"), "monitor")
sys.modules["monitor"] = mr
tmpr = tempfile.mkdtemp()
mr.STATE_FILE = os.path.join(tmpr, "seen.json")
mr.USE_API = False
mr.PAGE_DELAY = 0.0001
TAPU = "https://tap.az/elanlar/dasinmaz-emlak/menziller/"
pages_r = {
    "48500001": tap_ad("Mənzil öz evimdi, makler deyiləm", seller="Elvin", uid="1001").replace(
        "<html>", '<html><meta property="og:title" content="3-otaqlı mənzil, Neftçilər m., 75 m²">'
                  '<meta content="165000.00" property="product:price:amount">'
                  '<meta property="product:price:currency" content="AZN">', 1),
    "48500002": tap_ad("Təmirli 2 otaqlı mənzil, sənədləri qaydasındadır", seller="Nigar", uid="1002"),
    "48500003": tap_ad("Təmirli mənzil", seller="Kamran", uid="1003"),
    "48500004": tap_ad("Təmirli mənzil", seller="Kamran", uid="1003"),
    "48500005": tap_ad("Təmirli mənzil", seller="Kamran", uid="1003"),
    "48500006": tap_ad("OFİS HAQQI 1% təmirli mənzil", seller="Ruslan", uid="1006"),
    "48500007": tap_ad("Sahibiyəm, öz evimdir, vasitəçilər narahat etməsin", seller="Leyla", uid="1007"),
}


def fake_tap_r(url, tries=3):
    iid = url.rstrip("/").rsplit("/", 1)[-1]
    body = pages_r[iid]
    return R(body), body


mr._fetch_tap_html = fake_tap_r
mr._tap_blocked = lambda b: False
rooms_area = {"48500003": (1, 40), "48500004": (2, 60), "48500005": (3, 80)}
seen_r = {}
for iid in ("48500001", "48500002", "48500003", "48500004", "48500005", "48500006"):
    rec_ = {"url": TAPU + iid, "first_seen": "2026-10-10T10:00:00+00:00", "source": "tap.az",
            "owner": False}
    if iid in rooms_area:
        rec_["rooms"], rec_["area"] = rooms_area[iid]
    seen_r["tap:" + iid] = rec_
seen_r["tap:48500007"] = {"url": TAPU + "48500007", "first_seen": "2026-10-25T10:00:00+00:00",
                          "source": "tap.az", "skip": "old ~30d"}


def write_state():
    with open(mr.STATE_FILE, "w", encoding="utf-8") as fh_:
        json.dump({"listings": json.loads(json.dumps(seen_r)), "seeded": {}}, fh_)


os.environ["SOURCES"] = "tap"
rr = load(os.path.join(HERE, "recover_missed.py"), "recover_missed")
sent_r = []
msgs_r = {}
mr.notify_new_owner_msg = lambda l, n, ol="": (sent_r.append(l["id"]),
                                               msgs_r.__setitem__(l["id"], mr.format_new_owner(l, n, ol)))[0] or True
write_state()
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rr.main()
out_r = buf.getvalue()
send_list = out_r.split("owner listing(s) to send")[-1]
check(TAPU + "48500001" in send_list and TAPU + "48500007" in send_list,
      "proven owners (owner wording) are in the send list")
check(TAPU + "48500002" not in send_list and "UNVERIFIED" in out_r and TAPU + "48500002" in out_r,
      "count-unknown seller is listed as UNVERIFIED, not sent")
check(all(TAPU + i not in send_list for i in ("48500003", "48500004", "48500005", "48500006")),
      "same seller with 3 flats in the batch -> agent; 'ofis haqqi' -> agent")
check("seller 1003 has 3 different flats" in out_r, "batch count is shown")
check(sent_r == [] and json.load(open(mr.STATE_FILE))["listings"] == seen_r,
      "dry run: nothing sent, seen.json untouched")
rr.SEND = True
write_state()
with contextlib.redirect_stdout(io.StringIO()):
    rr.main()
st_r = json.load(open(mr.STATE_FILE, encoding="utf-8"))["listings"]
check(sorted(sent_r) == ["48500001", "48500007"], "SEND=1 sends exactly the 2 proven owners", str(sent_r))
m01 = msgs_r.get("48500001", "")
check("♻️" in m01 and "Yerləşdirilib:</b> 10.10.2026\n" in m01 and "səhvən" not in m01
      and "ℹ️" not in m01 and "ən gec" not in m01,
      "recovered message: ♻️ + '10.10.2026' (first seen), no note",
      m01.splitlines()[2] if m01 else "")
check("📍 <b>Ünvan:</b> Neftçilər m." in m01, "recovered tap message has its address")
check("💰 <b>Qiymət:</b> 165 000 AZN" in m01 and "🛏 <b>Otaq sayı:</b> 3" in m01
      and "📐 <b>Sahə:</b> 75 m²" in m01, "recovered tap message has Qiymət / Otaq sayı / Sahə",
      " | ".join(m01.splitlines()[2:7]))
check(st_r["tap:48500001"]["owner"] is True and st_r["tap:48500007"]["owner"] is True
      and "skip" not in st_r["tap:48500007"], "they are recorded as sent (never twice)")
check(st_r["tap:48500002"]["owner"] is False and "rechecked" not in st_r["tap:48500002"],
      "unverified one is left as it was (can be verified later)")
check(all("rechecked" in st_r[f"tap:{i}"] for i in ("48500003", "48500004", "48500005", "48500006")),
      "re-confirmed agents are marked rechecked")
sent_r.clear()
with contextlib.redirect_stdout(io.StringIO()):
    rr.main()
check(sent_r == [], "running it again sends nothing twice", str(sent_r))
os.environ.pop("SOURCES", None)
sys.modules.pop("monitor", None)

# --------------------------------------------------------------------------- #
print("\n[E2E] two full runs of main() with every site faked")
mm = load(os.path.join(HERE, "monitor.py"), "monitor_e2e")
tmp = tempfile.mkdtemp()
mm.STATE_FILE = os.path.join(tmp, "seen.json")
mm.USE_API = False
mm.PAGE_DELAY = 0
mm.SOURCES = [dict(s_) for s_ in mm.SOURCES]
outbox = []
mm.tg_send_message = lambda text: outbox.append(text) or True
mm.tg_send_photo = lambda photo, cap: outbox.append(cap) or True
feeds = {"tap.az": [], "lalafo.az": [], "yeniemlak.az": [], "bina.az": []}
mm.fetch_for_source = lambda src: [dict(x) for x in feeds[src["name"]]]
install_tap(mm, tap_ad("Öz evimdi", multi=False))
mm.check_is_owner = lambda url, label="": (setattr(mm.check_is_owner, "last_reason", "ok") or True, None)
mm.bina_is_owner = lambda url: (setattr(mm.bina_is_owner, "last_reason", "ok") or True, None)
feeds["tap.az"] = [{"id": "48900001", "url": "https://tap.az/elanlar/dasinmaz-emlak/menziller/48900001",
                    "rooms": 2, "area": 60, "price": 100000}]
feeds["bina.az"] = [{"id": "6470001", "url": "https://bina.az/items/6470001", "price": 150000,
                     "rooms": 3, "area": 80, "_promoted": False}]
mm.main()                                         # run 1: everything seeded silently
run1 = len(outbox)
feeds["tap.az"].append({"id": "48900002", "url": "https://tap.az/elanlar/dasinmaz-emlak/menziller/48900002",
                        "rooms": 3, "area": 75, "price": 120000})
feeds["bina.az"].append({"id": "6470002", "url": "https://bina.az/items/6470002", "price": 160000,
                         "rooms": 2, "area": 55, "_promoted": False})
mm.main()                                         # run 2: only the two new ones
run2 = outbox[run1:]
check(run1 == 0, "run 1 (fresh state) sends nothing", f"sent={run1}")
check(len(run2) == 2 and any("48900002" in t for t in run2) and any("6470002" in t for t in run2),
      "run 2 announces exactly the 2 new listings", f"sent={len(run2)}")
mm.main()
check(len(outbox) == run1 + 2, "run 3 sends nothing (no duplicates)", f"total={len(outbox)}")
st = json.load(open(mm.STATE_FILE, encoding="utf-8"))
check(st["seeded"].get("tap.az:url") and st["seeded"].get("bina.az:url"),
      "URL fingerprints saved in state")

# --------------------------------------------------------------------------- #
print("\n[NOV-26] not announced: tap 49259520, bina 6637490, bina 6639973")
mn = load(os.path.join(HERE, "monitor.py"), "monitor_nov23")
TXT23 = ("Təcili satış! Real alıcı ilə qiymətdə razılaşmaq mümkündür.\nVaisteciler narahat etmesinler!")
sc23, rs23 = mn.owner_signal_score(TXT23, "", None)
check(sc23 >= mn.SCORE_OWNER_AT, "49259520 'Vaisteciler narahat etmesinler!' is owner wording",
      f"{sc23} {rs23}")
VARIANTS23 = ["Vasitəçilər narahat etməsin", "Vasteciler narahat elemesin", "Vasitçilər zəng etməsin",
              "Maklerlər narahat etməyin", "Vasitechiler yazmasin", "vasitəçisiz satılır",
              "Vasitçi yoxdur", "Agentliklər zəng vurmasın"]
bad23 = [t for t in VARIANTS23 if mn.owner_signal_score(t, "", None)[0] < mn.SCORE_OWNER_AT]
check(not bad23, "how people really spell 'vasitəçilər narahat etməsin' - all owner wording", str(bad23))
check(mn.owner_signal_score("Ofis haqqı 1%", "", None)[0] <= mn.SCORE_AGENT_AT
      and mn.owner_signal_score("Vasitəçilik haqqı yoxdur", "", None)[0] == 0
      and mn.owner_signal_score("metroya vasitə ilə 5 dəqiqə", "", None)[0] == 0,
      "fees are still agent wording; 'haqqı yoxdur' and 'vasitə' alone count for nobody")

# tap: the ad page says how many ads the seller has in total (real 49259520 data)
USER23 = ('"User:{\\"id\\":\\"Z2lkOi8vdGFwL1VzZXIvMTQyMDM3OTg\\"}":{"__typename":"User",'
          '"id":"Z2lkOi8vdGFwL1VzZXIvMTQyMDM3OTg","legacyId":"14203798","publishedAdsCount":%d,'
          '"isBlockedMe":false,"isBlockedByMe":false}')


def tap23(desc, total):
    return ('<html><nav>Ana səhifə / Daşınmaz əmlak</nav>'
            f'<meta property="og:description" content="{html_mod.escape(desc)}">'
            '<meta property="og:title" content="2-otaqlı mənzil, Xalqlar Dostluğu m., 46.7 m²">'
            '<script id="__NEXT_DATA__">{' + (USER23 % total) + '}</script></html>' + "x" * 3000)


check(mn._tap_seller_ads_total(tap23("x", 1), "14203798") == 1
      and mn._tap_seller_ads_total(tap23("x", 1), "1420379") is None,
      "seller's ad total read from THIS seller's data only")
mn.TAP_PROFILE_BROWSER = True
asked23 = []
mn.tap_profile_count_browser = lambda uid: (asked23.append(uid), 6)[1]
U23 = "https://tap.az/elanlar/dasinmaz-emlak/menziller/49259520"
for desc, total, want, opened, label in [
        ("Təmirli mənzil, kupçalı", 1, True, False,
         "seller has 1 ad in total -> owner, profile not even opened"),
        (TXT23, 1, True, False, "the real 49259520 text + 1 ad -> owner"),
        ("Təmirli mənzil, kupçalı", 7, False, True,
         "seller has 7 ads -> profile opened as before (6 flats -> agent)")]:
    mn._RUNTIME["state"] = {"tap_profiles": {}}
    asked23.clear()
    install_tap(mn, tap23(desc, total))
    v23, _p, s23 = mn.tap_check_owner(U23, {}, {}, listing={"id": "49259520"})
    check(v23 is want and s23 == "14203798" and bool(asked23) == opened, label,
          f"{V[v23]} opened={asked23} {mn.tap_check_owner.last_reasons}")

# bina: seller type straight from the page data of THIS ad (real 6637490 layout)


def bina23(iid, ctype=None, og="Təcili satılır! Nefçilər və Xalqlar dostluğuna piyada məsafə",
           other=None, with_id=True):
    item = {"__typename": "Item", "id": iid, "additionalParameters": [],
            "address": "Cəmşid Naxçıvanski küç. 38", "buildingTypeName": None,
            "price": {"__typename": "Price", "currency": "AZN", "total": 149000}}
    if ctype is not None:
        item["contactTypeName"] = ctype
    data = {"props": {"apolloState": {f"Item:{iid}": item}}}
    if other:
        data["props"]["apolloState"]["Item:6600001"] = {"__typename": "Item", "id": "6600001",
                                                        "contactTypeName": other}
    body = json.dumps(data, ensure_ascii=False)
    if not with_id:
        body = '{"props":{}}'
    canon = f'<link href="https://bina.az/items/{iid}" rel="canonical">' if with_id else ""
    return ("<html><head>" + canon + f'<meta property="og:description" content="{html_mod.escape(og)}">'
            + '</head><script id="__NEXT_DATA__" type="application/json">' + body
            + "</script></html>" + "x" * 2000)


def bina23_check(page, iid="6637490"):
    mn.http_get = lambda url, headers=None, params=None, timeout=30, browser=False: R(page)
    return mn.bina_is_owner(f"https://bina.az/items/{iid}")[0], mn.bina_is_owner.last_reason


check(bina23_check(bina23("6637490", "mülkiyyətçi")) == (True, "ok"),
      "6637490: page data 'contactTypeName: mülkiyyətçi' -> owner (no visible label needed)")
check(bina23_check(bina23("6637490", "vasitəçi (agent)")) == (False, "ok"),
      "page data 'vasitəçi (agent)' -> agent")
check(bina23_check(bina23("6637490", None, other="mülkiyyətçi")) == (None, "nolabel"),
      "another ad's seller type on the same page is never used")
check(bina23_check(bina23("6637490", "mülkiyyətçi", with_id=False)) == (None, "fetch"),
      "a page that is not this ad (error / wait page) counts as NOT READ - no try used up")
check(bina23_check(bina23("6637490", "mülkiyyətçi", og="Təmirli mənzil, ofis haqqı 1% alınır"))[0] is False,
      "owner by page data + 'ofis haqqı 1%' in the text -> still agent")

# Telegram: wait out 429, keep undelivered messages, send them next run
mt23 = load(os.path.join(HERE, "monitor.py"), "monitor_tg23")


class TR:
    def __init__(self, code, js=None):
        self.status_code, self._js = code, (js or {})
        self.text = json.dumps(self._js)

    def json(self):
        return self._js


import requests as _rq
_orig_post, _orig_sleep = _rq.post, time.sleep
posted23, slept23, script23 = [], [], []


def _fake_post(url, **kw):
    posted23.append((url.rsplit("/", 1)[-1], kw.get("json") or kw.get("data")))
    nxt = script23.pop(0) if script23 else TR(200, {"ok": True})
    if isinstance(nxt, Exception):
        raise nxt
    return nxt


_rq.post = _fake_post
time.sleep = lambda s_: slept23.append(s_)
mt23.TG_MIN_GAP = 0
try:
    script23[:] = [TR(429, {"ok": False, "parameters": {"retry_after": 3}}), TR(200, {"ok": True})]
    ok23 = mt23.tg_send_message("x")
    check(ok23 and len(posted23) == 2 and slept23 == [3.5],
          "Telegram 429 'retry after 3' -> waits 3 s, sends again, delivered", f"{posted23} {slept23}")
    posted23.clear(); slept23.clear()
    script23[:] = [TR(429, {"ok": False, "parameters": {"retry_after": 999}})]
    check(mt23.tg_send_message("x") is False and len(posted23) == 1 and not slept23,
          "an absurd wait (999 s) is not waited out")
    posted23.clear()
    script23[:] = [TR(400, {"ok": False, "description": "Bad Request: can't parse entities"}),
                   TR(200, {"ok": True})]
    check(mt23.tg_send_message("<b>a & b</b>") is True and "parse_mode" not in posted23[-1][1],
          "HTML Telegram cannot read -> the same text is sent without formatting")
    st23 = {"listings": {}}
    mt23._RUNTIME["state"] = st23
    l23 = {"id": "6637490", "url": "https://bina.az/items/6637490", "price": 149000, "rooms": 3,
           "area": 61, "location": "Xalqlar Dostluğu m."}
    script23[:] = [TR(500)] * 3
    check(mt23.notify_new_owner_msg(l23, "bina.az", "Mülkiyyətçi") is False
          and len(st23.get("outbox", [])) == 1 and st23["outbox"][0]["key"] == l23["url"],
          "Telegram refused the alert -> kept in the outbox (was: lost, record said 'sent')")
    posted23.clear()
    script23[:] = [TR(200, {"ok": True})]
    with contextlib.redirect_stdout(io.StringIO()):
        n23 = mt23.flush_outbox()
    check(n23 == 1 and st23["outbox"] == [] and "6637490" in json.dumps(posted23, ensure_ascii=False),
          "next run: the waiting alert is delivered and leaves the outbox")
    script23[:] = [_rq.exceptions.ReadTimeout("slow")]
    posted23.clear()
    with contextlib.redirect_stdout(io.StringIO()):
        mt23.notify_new_owner_msg(l23, "bina.az", "Mülkiyyətçi")
    check(len(st23["outbox"]) == 1 and len(posted23) == 1,
          "Telegram timed out (log line 100830, 19 Nov) -> no more tries this run, kept for "
          "the next run (was: lost)", f"posts={len(posted23)}")
    mt23.TG_RESEND_ON_TIMEOUT = False
    st23["outbox"] = []
    script23[:] = [_rq.exceptions.ReadTimeout("slow")]
    with contextlib.redirect_stdout(io.StringIO()):
        mt23.notify_new_owner_msg(l23, "bina.az", "Mülkiyyətçi")
    check(st23["outbox"] == [], "TG_RESEND_ON_TIMEOUT=0 -> a timed-out message is never repeated")
    mt23.TG_RESEND_ON_TIMEOUT = True
    st23["outbox"] = [{"text": "x", "photo": None, "key": "k3", "n": mt23.OUTBOX_MAX_TRIES + 1,
                       "t": iso(time.time() - 3600)}]
    posted23.clear()
    with contextlib.redirect_stdout(io.StringIO()):
        mt23.flush_outbox()
    check(st23["outbox"] == [] and not posted23, "a message that failed OUTBOX_MAX_TRIES times is dropped (logged)")
    mt23.TG_MIN_GAP = 3.1
    mt23._TG_LAST["t"] = 0.0
    slept23.clear(); posted23.clear()
    for _i in range(3):
        mt23.tg_send_message(f"m{_i}")
    check(len(posted23) == 3 and len(slept23) == 2 and all(2.5 < x <= 3.1 for x in slept23),
          "messages are paced ~3 s apart (Telegram allows ~20 a minute in a group; 23 Oct lost 25 to 429)",
          str(slept23))
    mt23.TG_MIN_GAP = 0
    st23["outbox"] = [{"text": "old", "photo": None, "key": "k", "n": 9,
                       "t": iso(time.time() - 8 * 86400)}]
    posted23.clear()
    with contextlib.redirect_stdout(io.StringIO()):
        mt23.flush_outbox()
    check(st23["outbox"] == [] and not posted23, "alerts older than OUTBOX_MAX_DAYS are dropped (logged)")
    st23["outbox"] = [{"text": "now", "photo": None, "key": "k2", "n": 1, "t": iso(time.time())}]
    with contextlib.redirect_stdout(io.StringIO()):
        mt23.flush_outbox(run_started=time.time() - 60)
    check(len(st23["outbox"]) == 1 and not posted23,
          "an alert that failed during THIS run waits for the next run (no hammering)")
    # a burst of price changes: at most TG_PRICE_PER_RUN go out now, the rest wait
    st23 = {"listings": {}}
    mt23._RUNTIME["state"] = st23
    mt23.TG_PRICE_PER_RUN = 2
    mt23._TG_RUN["price"] = 0
    posted23.clear(); script23[:] = []
    with contextlib.redirect_stdout(io.StringIO()):
        r_pc = [mt23.notify_change(dict(l23, id=f"66{_i}", url=f"https://bina.az/items/66{_i}"),
                                   "bina.az", "drop", 150000, 140000, 2) for _i in range(3)]
        r_ow = mt23.notify_new_owner_msg(l23, "bina.az", "Mülkiyyətçi")
    check(r_pc == [True, True, False] and r_ow is True and len(posted23) == 3
          and [e.get("kind") for e in st23["outbox"]] == ["price"],
          "price-change burst: 2 sent, the 3rd waits in the outbox; owner alerts are never held back",
          f"{r_pc} {r_ow} posts={len(posted23)} box={st23.get('outbox')}")
    st23["outbox"] = [{"text": "PRICE", "photo": None, "key": "p", "n": 0, "kind": "price",
                       "t": iso(time.time() - 7200)},
                      {"text": "OWNER", "photo": None, "key": "o", "n": 1, "kind": "owner",
                       "t": iso(time.time() - 3600)}]
    posted23.clear()
    with contextlib.redirect_stdout(io.StringIO()):
        mt23.flush_outbox()
    check([p_[1]["text"] for p_ in posted23] == ["OWNER", "PRICE"] and st23["outbox"] == [],
          "outbox: waiting owner-ad alerts go out before waiting price changes", str(posted23))
finally:
    _rq.post, time.sleep = _orig_post, _orig_sleep

# the 23 Oct 17:00 burst replayed against a Telegram that allows 20 messages a minute
_orig_time = time.time
clock = {"t": 1_000_000.0}
accepted, refused = [], []


def _tg_limited(url, **kw):
    now_ = clock["t"]
    recent = [x for x in accepted if now_ - x < 60]
    if len(recent) >= 20:
        refused.append(now_)
        return TR(429, {"ok": False, "parameters": {"retry_after": int(60 - (now_ - recent[0])) + 1}})
    accepted.append(now_)
    clock["t"] += 0.4                       # Telegram answers in 0.4 s
    return TR(200, {"ok": True})


def _vsleep(sec):
    clock["t"] += sec


_rq.post, time.sleep, time.time = _tg_limited, _vsleep, lambda: clock["t"]
try:
    for gap, label in [(3.1, "with 3.1 s spacing"), (0, "spacing turned off (the 429 wait does the work)")]:
        mt23.TG_MIN_GAP, mt23.TG_PRICE_PER_RUN = gap, 40
        mt23._TG_RUN["price"] = 0
        mt23._TG_LAST["t"] = 0.0
        st23 = {"listings": {}}
        mt23._RUNTIME["state"] = st23
        accepted.clear(); refused.clear()
        t_start = clock["t"]
        with contextlib.redirect_stdout(io.StringIO()):
            res = [mt23.notify_change(dict(l23, id=f"65{_i}", url=f"https://bina.az/items/65{_i}"),
                                      "bina.az", "drop", 205000, 195000, 2) for _i in range(56)]
        waiting = len(st23.get("outbox", []))
        check(sum(res) + waiting == 56 and sum(res) == 40 and waiting == 16
              and (gap == 0 or not refused),
              f"23 Oct burst of 56 price changes {label}: 40 delivered, 16 wait for the next run, "
              f"0 lost (was 25 lost)",
              f"sent={sum(res)} waiting={waiting} 429s={len(refused)} took={clock['t'] - t_start:.0f}s")
finally:
    _rq.post, time.sleep, time.time = _orig_post, _orig_sleep, _orig_time
    mt23.TG_MIN_GAP = 0

# tap: an ad seen on page 1 but not decided is checked next run, even off page 1
mpd = load(os.path.join(HERE, "monitor.py"), "monitor_pend23")
mpd.PAGE_DELAY = 0
st_p = {"listings": {}}
mpd._RUNTIME["state"] = st_p
src_p = {"name": "tap.az", "type": "tap", "url": "https://tap.az/x", "prefix": "tap:",
         "mode": "owner_new", "owner_label": "Sahibindən"}
flags_p = {"tap.az": True, "tap.az:url": mpd._url_fingerprint(src_p["url"])}
seen_p = {"tap:48000001": {"url": TAPU + "48000001", "first_seen": iso(time.time() - 3600),
                           "source": "tap.az", "owner": True}}
beh = {"reason": "fetch", "calls": []}


def fake_tco(url, seller_counts=None, profile_cache=None, listing=None):
    beh["calls"].append(url)
    mpd.tap_check_owner.last_reason = beh["reason"]
    mpd.tap_check_owner.last_details = {"price": 167000, "rooms": 2, "area": 46.7,
                                        "location": "Xalqlar Dostluğu m."}
    if beh["reason"] == "ok":
        return True, None, "14203798"
    return None, None, None


mpd.tap_check_owner = fake_tco
sent_p = []
mpd.notify_new_owner_msg = lambda l, n, ol="": sent_p.append(l["id"]) or True
A23 = {"id": "49259520", "url": TAPU + "49259520", "rooms": 2, "area": 46.7, "price": 167000,
       "location": "Xalqlar Dostluğu m."}
with contextlib.redirect_stdout(io.StringIO()):
    mpd.process_owner_new([dict(A23)], src_p, seen_p, flags_p)     # page did not load
check("tap:49259520" not in seen_p and "49259520" in st_p.get("tap_pending", {}),
      "run 1: page 1 had the ad, its page did not load -> remembered as pending")
beh["reason"] = "ok"
with contextlib.redirect_stdout(io.StringIO()):
    mpd.process_owner_new([], src_p, seen_p, flags_p)              # ad is off page 1 now
check(sent_p == ["49259520"] and seen_p["tap:49259520"]["owner"] is True
      and st_p["tap_pending"] == {},
      "run 2: no longer on page 1, still checked and announced once", f"sent={sent_p}")
with contextlib.redirect_stdout(io.StringIO()):
    mpd.process_owner_new([dict(A23)], src_p, seen_p, flags_p)
check(sent_p == ["49259520"], "run 3: never sent twice")
st_p["tap_pending"] = {"49000001": {"id": "49000001", "url": TAPU + "49000001",
                                    "_since": iso(time.time() - 3 * 86400)},
                       "49000002": {"id": "49000002", "url": TAPU + "49000002",
                                    "_since": iso(time.time() - 3600)}}
beh["reason"], beh["calls"] = "gone", []
with contextlib.redirect_stdout(io.StringIO()):
    mpd.process_owner_new([], src_p, seen_p, flags_p)
check(beh["calls"] == [TAPU + "49000002"] and st_p["tap_pending"] == {}
      and "tap:49000002" not in seen_p,
      "pending older than TAP_PENDING_DAYS dropped unread; a deleted ad is dropped, not recorded")

# main(): tap page 1 is read again at the end of a long run
m2r = load(os.path.join(HERE, "monitor.py"), "monitor_second23")
tmp2 = tempfile.mkdtemp()
m2r.STATE_FILE = os.path.join(tmp2, "seen.json")
m2r.USE_API = False
m2r.PAGE_DELAY = 0
m2r.SOURCES = [dict(s_) for s_ in m2r.SOURCES]
m2r.tg_send_message = lambda text: True
m2r.tg_send_photo = lambda photo, cap: True
reads23 = []
m2r.fetch_for_source = lambda src: reads23.append(src["name"]) or []
m2r.TAP_SECOND_READ_AFTER = 0
with contextlib.redirect_stdout(io.StringIO()):
    m2r.main()
check(reads23.count("tap.az") == 2 and reads23[-1] == "tap.az" and reads23.count("bina.az") == 1,
      "tap page 1 is read at the start AND at the end of a run", str(reads23))
reads23.clear()
m2r.TAP_SECOND_READ_AFTER = 10 ** 6
with contextlib.redirect_stdout(io.StringIO()):
    m2r.main()
check(reads23.count("tap.az") == 1, "short run -> no second read")

# --------------------------------------------------------------------------- #
print("\n[NOV-27] lost_alerts.py: alerts Telegram did not deliver (18-19 Nov) are found and re-sent")
la_ = load(os.path.join(HERE, "lost_alerts.py"), "lost_alerts_t")
B502 = '{"ok":false,"error_code":502,"description":"Bad Gateway"}'
T0 = time.time() - 4 * 86400
LOG27 = "\n".join([
    f"monitor.py 2026-11-17-a starting {iso(T0)}",
    "tap.az: 30 listings",
    f"tap.az: {TAPU}49259520 score=1 -> OWNER ['count:1-apartment(s)-ok']",
    f"sendPhoto upload failed: 502 {B502}", f"sendPhoto by-url failed: 502 {B502}",
    f"Telegram sendMessage failed: 502 {B502}",
    f"tap.az: {TAPU}49259521 score=4 -> OWNER ['owner:oz evimdi']",
    "tap.az: checked 2 new, announced 1 owner posts",
    "lalafo.az: 5 listings (1 page(s), newest pages)",
    "yeniemlak: read 2 page(s) [newest; stopped: nothing new], kept 3 of 3 parsed",
    "bina: scanned 70 pages, 1100 matching listings of ~1100 total",
    "sendPhoto upload error: HTTPSConnectionPool(host='api.telegram.org', port=443): Read timed out.",
    "sendPhoto by-url error: HTTPSConnectionPool(host='api.telegram.org', port=443): Read timed out.",
    "Telegram error: HTTPSConnectionPool(host='api.telegram.org', port=443): Read timed out.",
    "bina.az: owner-checked 2 new, announced 1, deferred 0",
    "price change 6600009: 120000 -> 110000 (-10000)",
    "price change 6600010: 90000 -> 85000 (-5000)",
    f"Telegram sendMessage failed: 429 {B502}",
    "Done. notified=2 saved=True(local) records_before=1 records_after=6 elapsed=400s status={}",
    f"monitor.py 2026-11-17-a starting {iso(T0 + 900)}",
    "tap.az: 30 listings", "bina: scanned 70 pages, 1100 matching listings",
    f"Telegram sendMessage failed: 502 {B502}",
    "Done. notified=0 saved=True(local) records_before=6 records_after=7 elapsed=390s status={}",
    f"monitor.py 2026-11-17-a starting {iso(T0 + 1800)}",
    "Done. notified=0 saved=True(local) records_before=7 records_after=7 elapsed=380s status={}",
]) + "\n"
L27 = {"tap:49259520": {"url": TAPU + "49259520", "first_seen": iso(T0 + 4), "source": "tap.az",
                        "owner": True, "seller": "14203798", "rooms": 2, "area": 46.7},
       "tap:49259521": {"url": TAPU + "49259521", "first_seen": iso(T0 + 5), "source": "tap.az", "owner": True},
       "6637490": {"url": "https://bina.az/items/6637490", "price": 167000, "first_seen": iso(T0 + 380),
                   "source": "bina.az", "owner": True},
       "6637491": {"url": "https://bina.az/items/6637491", "price": 150000, "first_seen": iso(T0 + 380),
                   "source": "bina.az", "owner": True},
       "6600009": {"url": "https://bina.az/items/6600009", "price": 110000, "first_seen": iso(T0 - 9e5),
                   "source": "bina.az"},
       "6637600": {"url": "https://bina.az/items/6637600", "price": 99000, "first_seen": iso(T0 + 1250),
                   "source": "bina.az", "owner": True},
       "6637700": {"url": "https://bina.az/items/6637700", "price": 99000, "first_seen": iso(T0 + 2000),
                   "source": "bina.az", "owner": True}}
tmp27 = tempfile.mkdtemp()
la_.LOG = os.path.join(tmp27, "run.log")
with open(la_.LOG, "w", encoding="utf-8") as fh_:
    fh_.write(LOG27)
buf27 = io.StringIO()
with contextlib.redirect_stdout(buf27):
    out27 = la_.list_lost({"listings": L27})
txt27 = buf27.getvalue()
v27 = {i["key"]: i["verdict"] for i in out27}
check(v27.get("tap:49259520") == "LOST", "tap 49259520: Telegram failed right after its check -> LOST",
      str(v27))
check("tap:49259521" not in v27, "the next tap ad (no failure after it) counts as delivered")
check(v27.get("6637490", "").startswith("maybe") and v27.get("6637491", "").startswith("maybe"),
      "bina: 1 owner-ad failure, 2 owner ads in that run -> both 'maybe' (Telegram does not say which)")
check("1 of 2 bina price-change alert(s) lost" in txt27,
      "bina: a failure AFTER 'owner-checked ... announced' is a price change, not an owner ad",
      [ln for ln in txt27.splitlines() if "price-change" in ln][:1])
check(v27.get("6637600") == "LOST" and "6637700" not in v27,
      "bina: 1 failure, 1 owner ad in that run -> LOST; a run without failures is not listed")
check("6600009" not in v27 and "6600010" not in v27,
      "price-change alerts are counted but not offered for re-sending (only owner ads)")
check("Owner ads whose alert was lost: 49259520 6637600" in txt27, "summary line with ids to re-send",
      txt27.strip().splitlines()[-3] if txt27.strip() else "")

# RESEND puts the messages into the outbox; the page fills price / rooms / address
mr27 = load(os.path.join(HERE, "monitor.py"), "monitor")
mr27.STATE_FILE = os.path.join(tmp27, "seen.json")
mr27.USE_API = False
with open(mr27.STATE_FILE, "w", encoding="utf-8") as fh_:
    json.dump({"listings": json.loads(json.dumps(L27))}, fh_)
BINA27 = ('<html><head><link href="https://bina.az/items/6637490" rel="canonical">'
          '<meta property="og:image" content="https://bina.azstatic.com/x.jpg">'
          '<script type="application/ld+json">' + json.dumps(
              {"@type": "Product", "offers": {"price": "149000", "priceCurrency": "AZN", "itemOffered": {
                  "numberOfRooms": 3, "floorSize": {"value": 61}}}}) + '</script></head>'
          '<script>{"Item:6637490":{"__typename":"Item","id":"6637490","floor":1,"floors":9,'
          '"location":{"__typename":"Location","fullName":"Xalqlar Dostluğu m.","path":"/baki/x"}}}'
          '</script></html>')
TAP27 = tap23(TXT23, 1).replace("<html>", '<html><meta content="167000.00" property="product:price:amount">'
                                 '<meta property="product:price:currency" content="AZN">', 1)
mr27.http_get = lambda url, headers=None, params=None, timeout=30, browser=False: R(BINA27)
mr27._fetch_tap_html = lambda url, tries=4: (R(TAP27), TAP27)
sys.modules["monitor"] = mr27
os.environ["RESEND"] = "49259520 https://bina.az/items/6637490 6600009 999"
buf27 = io.StringIO()
with contextlib.redirect_stdout(buf27):
    la_.main()
st27 = json.load(open(mr27.STATE_FILE, encoding="utf-8"))
box27 = st27.get("outbox", [])
tb = next((e["text"] for e in box27 if "6637490" in e["key"]), "")
tt = next((e["text"] for e in box27 if "49259520" in e["key"]), "")
check(len(box27) == 2 and st27["listings"]["6637490"].get("resent") and st27["listings"]["tap:49259520"].get("resent"),
      "RESEND: the 2 owner ads are queued in the outbox and stamped 'resent'", str([e["key"] for e in box27]))
check("♻️" in tb and "149 000 AZN" in tb and "Otaq sayı:</b> 3" in tb and "Sahə:</b> 61 m²" in tb
      and "1/9" in tb and "Xalqlar Dostluğu m." in tb
      and next(e["photo"] for e in box27 if "6637490" in e["key"]) == "https://bina.azstatic.com/x.jpg",
      "bina message: ♻️ (4 days old), today's price, rooms, area, floor, address, photo",
      " | ".join(tb.splitlines()[:8]))
check("167 000 AZN" in tt and "Otaq sayı:</b> 2" in tt and "Xalqlar Dostluğu m." in tt,
      "tap message: price / rooms / address from the ad's page", " | ".join(tt.splitlines()[:8]))
check("6600009: the bot did not judge it an owner ad" in buf27.getvalue()
      and "999: not in the bot's records" in buf27.getvalue(),
      "non-owner and unknown ids are refused")
with contextlib.redirect_stdout(io.StringIO()):
    la_.main()
check(len(json.load(open(mr27.STATE_FILE, encoding="utf-8"))["outbox"]) == 2,
      "running RESEND again queues nothing twice")
os.environ.pop("RESEND", None)
sys.modules.pop("monitor", None)

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
