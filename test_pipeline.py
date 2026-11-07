# -*- coding: utf-8 -*-
"""Regression tests for the 2026-10-18 review. Every case comes from a real listing
or a confirmed code path. Run on the VM:   .venv/bin/python test_pipeline.py
Needs no network and sends nothing (all fetchers and Telegram are replaced)."""
import importlib.util
import json
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
check(len(got) > 1 and got[1].get("_age_days", 0) > 10 and got[1].get("_note"),
      "hidden one -> marked old + 'not sent before' note")
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

print("\n[NOV-6] message header")
t_old = m.format_new_owner({"url": "https://x", "price": 100000, "_age_days": 75.2,
                            "_age_kind": "min", "_note": "Bu elan əvvəl göndərilməmişdi"},
                           "yeniemlak.az", "Əmlak sahibi")
check("♻️ <b>Yenilənmiş elan</b>" in t_old and "ən azı 75 gün əvvəl" in t_old
      and "göndərilməmişdi" in t_old, "old ad: ♻️ header, 'at least 75 days ago', note")
t_ex = m.format_new_owner({"url": "https://x", "_age_days": 200.4, "_age_kind": "exact"},
                          "lalafo.az", "Mülkiyyətçi")
check("~200 gün əvvəl" in t_ex, "exact creation date: '~200 days ago'")
t_new = m.format_new_owner({"url": "https://x", "price": 1, "_age_days": 1.5}, "tap.az", "Sahibindən")
check("‼️ <b>Yeni elan</b>" in t_new and "🕓" not in t_new, "new ad: ‼️ header, no age line")

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
