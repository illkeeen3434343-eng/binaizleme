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

os.environ.setdefault("TELEGRAM_BOT_TOKEN", "t")
os.environ.setdefault("TELEGRAM_CHAT_ID", "c")
HERE = os.path.dirname(os.path.abspath(__file__))
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

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
