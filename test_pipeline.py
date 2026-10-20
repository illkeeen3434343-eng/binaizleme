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
if old is not None:
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
if old is not None:
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
print("\n[C10] really-new guard")
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
check(a_new is not None and a_new <= 1.01, "id newer than our latest -> age ~0", f"{a_new:.2f}")
check(a_old is not None and a_old > 3, "old bumped id -> clearly older than 3 days", f"{a_old:.1f}")
check(clock.age_days(99999999, t_now) == 0.0, "brand-new id above all history -> 0")
check(m.IdClock().age_days(5, t_now) is None, "no history -> unknown (never suppress)")
old_lalafo = {"created_ts": t_now - 300 * day}
check(m.listing_age_days(old_lalafo, None, t_now) > 299, "lalafo creation date wins")

sent.clear()
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
check(sent == ["48405000"], "bumped old owner ad suppressed, fresh owner ad announced", str(sent))
check(str(seen3.get("tap:48290603", {}).get("skip", "")).startswith("old"),
      "suppressed ad is recorded with skip=old (never re-checked)")
check(seen3.get("tap:48405000", {}).get("seller") == "22702915"
      and seen3["tap:48405000"].get("rooms") == 3, "tap record keeps seller + rooms/area")

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
m.process_owner_new([agent_item, owner_item, dict(owner_item, id="900003",
                    created_ts=t_now - 300 * day)], lsrc, seen6, flags6)
check(sent == ["900002"], "EKSKLUZIV agent skipped, fresh owner sent, 300-day-old owner skipped",
      str(sent))

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
mm.fetch_for_source = lambda src, plan=None: [dict(x) for x in feeds[src["name"]]]
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
print("\n[2026-10-20] Telegram retry + outbox")
mq = load(os.path.join(HERE, "monitor.py"), "monitor_q")
mq.time.sleep = lambda s: None                     # no real waiting in tests
mq.PAGE_DELAY = 0


class TR:
    def __init__(self, code, body=None):
        self.status_code, self._b, self.text = code, body or {}, json.dumps(body or {})

    def json(self):
        return self._b


posts = []


def tg_flaky(url, timeout=30, **kw):
    posts.append(url)
    return TR(429, {"parameters": {"retry_after": 3}}) if len(posts) == 1 else TR(200)


mq.requests.post = tg_flaky
check(mq.tg_send_message("x") and len(posts) == 2, "429 -> waits retry_after, then delivered",
      f"posts={len(posts)}")
mq._RUNTIME["state"] = st_q = {"listings": {}}
mq.SEND_PHOTOS = False
mq.requests.post = lambda url, timeout=30, **kw: TR(400, {"description": "can't parse entities"})
check(mq.notify_new_owner_msg({"id": "0", "url": "https://tap.az/0", "price": 5}, "tap.az") is False
      and not st_q.get("outbox"), "permanent rejection (400) is NOT queued (would never pass)")
mq.requests.post = lambda url, timeout=30, **kw: TR(502)
check(mq.notify_new_owner_msg({"id": "1", "url": "https://tap.az/1", "price": 5}, "tap.az") is False
      and len(st_q.get("outbox", [])) == 1, "Telegram down (502) -> alert queued in state['outbox']")
st_q["outbox"].append({"text": "ancient", "photo": None, "t": "2020-01-01T00:00:00+00:00"})
delivered = []
mq.requests.post = lambda url, timeout=30, **kw: delivered.append(kw["json"]["text"]) or TR(200)
n_sent = mq.flush_outbox(st_q)
check(n_sent == 1 and st_q["outbox"] == [] and "ancient" not in delivered,
      "next run re-sends the queued alert, drops ones older than OUTBOX_MAX_AGE_H",
      f"sent={n_sent} left={st_q['outbox']}")
st_q["outbox"] = [{"text": "a", "t": iso(t_now)}, {"text": "b", "t": iso(t_now)}]
mq.requests.post = lambda url, timeout=30, **kw: TR(502)
mq.flush_outbox(st_q)
check(len(st_q["outbox"]) == 2, "Telegram still down -> whole queue kept for later")
st_q["outbox"] = [{"text": "poison", "t": iso(t_now)}, {"text": "good", "t": iso(t_now)}]
delivered.clear()
mq.requests.post = lambda url, timeout=30, **kw: (
    TR(400) if kw["json"]["text"] == "poison" else delivered.append("good") or TR(200))
mq.flush_outbox(st_q)
check(delivered == ["good"] and st_q["outbox"] == [],
      "a message Telegram rejects for good does not block the rest of the queue")

print("\n[2026-10-20] bina page retry")
calls_b = []


def bina_get(url, params=None, headers=None, timeout=30):
    calls_b.append(1)
    if len(calls_b) < 3:
        return TR(502)
    return TR(200, {"data": {"itemsConnection": {"edges": [], "pageInfo": {}}}})


mq.requests.get = bina_get
check(mq._bina_page({}, None) is not None and len(calls_b) == 3,
      "two failed responses then success -> page returned, scan continues")
mq.requests.get = lambda url, params=None, headers=None, timeout=30: TR(
    200, {"errors": [{"message": "PersistedQueryNotFound"}]})
try:
    mq._bina_page({}, None)
    check(False, "expired persisted query raises PersistedQueryError")
except mq.PersistedQueryError:
    check(True, "expired persisted query raises PersistedQueryError (not retried)")

print("\n[2026-10-20] quick vs full bina scans")


def bina_pages(n_pages, per=16, start=6500000):
    pages = []
    for p in range(n_pages):
        edges = [{"node": {"id": start - p * per - j, "path": f"/items/{start - p * per - j}",
                           "price": {"total": 100000}, "rooms": 2}} for j in range(per)]
        pages.append({"totalCount": n_pages * per, "edges": edges,
                      "pageInfo": {"hasNextPage": p + 1 < n_pages, "endCursor": f"c{p + 1}"}})
    return pages


PAGES_B = bina_pages(10)
read = []


def fake_page(fv, cursor):
    i = int(cursor[1:]) if cursor else 0
    read.append(i)
    return PAGES_B[i]


mq._bina_page = fake_page
known = {str(e["node"]["id"]): {"price": 100000} for pg in PAGES_B for e in pg["edges"]}
new_id = str(PAGES_B[0]["edges"][0]["node"]["id"])
known.pop(new_id)
quick = mq.ScanPlan(False, known, "", owner_new=False)
got = mq.fetch_bina("https://bina.az/x", quick)
check(read == [0, 1, 2] and any(l["id"] == new_id for l in got),
      "quick scan stops after the first all-known page (min 3 pages), new listing kept",
      f"pages read={read}")
read.clear()
got = mq.fetch_bina("https://bina.az/x", mq.ScanPlan(True, known))
check(len(read) == 10 and len(got) == 160, "full scan reads every page", f"pages read={len(read)}")
read.clear()
known.pop(str(PAGES_B[2]["edges"][5]["node"]["id"]))
known.pop(str(PAGES_B[3]["edges"][5]["node"]["id"]))
mq.fetch_bina("https://bina.az/x", mq.ScanPlan(False, known))
check(read == [0, 1, 2, 3, 4], "quick scan keeps going while pages hold unknown listings",
      f"pages read={read}")

bsrc_q = {"name": "bina.az", "type": "bina", "prefix": "", "mode": "price_owner",
          "url": "https://bina.az/x"}
fp_b = mq._url_fingerprint(bsrc_q["url"])
base = {"listings": {"1": {}}, "seeded": {"bina.az:owner_seeded": True, "bina.az:url": fp_b}}
check(mq.plan_scan(bsrc_q, dict(base)).full, "no full pass recorded yet -> full")
check(not mq.plan_scan(bsrc_q, dict(base, last_full_scan={"bina.az": iso(t_now - 600)})).full,
      "full pass 10 min ago -> quick")
check(mq.plan_scan(bsrc_q, dict(base, last_full_scan={"bina.az": iso(t_now - 7200)})).full,
      "full pass 2 h ago -> full")
check(mq.plan_scan(bsrc_q, dict(base, last_full_scan={"bina.az": iso(t_now - 600)},
                                seeded={"bina.az:owner_seeded": True, "bina.az:url": "old"})).full,
      "search URL changed -> full (the silent seed needs every page)")
check(mq.plan_scan(dict(bsrc_q, name="yeniemlak.az", mode="owner_new", prefix="ye:"),
                   dict(base, last_full_scan={"yeniemlak.az": iso(t_now - 600)})).full,
      "sources not in QUICK_SCAN_SOURCES always scan fully")

tries_q = {"6400001": 2}
mq.bina_is_owner = lambda url: (setattr(mq.bina_is_owner, "last_reason", "ok") or True, None)
mq.process_new_owner_checks([], bsrc_q, {}, {"bina.az:owner_seeded": True, "bina.az:url": fp_b},
                            tries_map=tries_q, full_scan=False)
check(tries_q == {"6400001": 2}, "quick scan does not reset retry counters of deeper listings")

print("\n[2026-10-20] coverage warning sent once")
warned = []
mq.tg_send_message = lambda text: warned.append(text) or True
mq._RUNTIME["state"] = st_c = {"listings": {}}
mq._coverage_warn(100, 20000, True)
mq._coverage_warn(100, 20000, True)
check(len(warned) == 1, "SCAN_PAGES hit -> one Telegram warning, not one per run", str(len(warned)))
mq._coverage_warn(100, 100, False)
check("bina.az:coverage_warned" not in st_c["seeded"], "flag cleared once the scan fits again")

print("\n[2026-10-20] empty first seed no longer swallows the first real posts")
sent_q = []
mq.notify_new_owner_msg = lambda l, n, ol="": sent_q.append(l["id"]) or True
mq.tap_check_owner = lambda url, *a, **k: (True, None, None)
tsrc_q = {"name": "tap.az", "type": "tap", "prefix": "tap:", "mode": "owner_new",
          "owner_label": "Sahibindən", "url": "https://tap.az/x"}
seen_q, flags_q = {}, {}
mq.process_owner_new([], tsrc_q, seen_q, flags_q)            # first run: nothing matched
mq.process_owner_new([{"id": "48900009", "url": "https://tap.az/9"}], tsrc_q, seen_q, flags_q)
check(sent_q == ["48900009"], "first listing after an empty seed is announced", str(sent_q))
mq._RUNTIME["state"] = None

print("\n" + ("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}"))
sys.exit(1 if FAILS else 0)
