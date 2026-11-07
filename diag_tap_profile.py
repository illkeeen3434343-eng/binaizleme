#!/usr/bin/env python3
"""Why did the browser count of a tap.az seller's apartments look wrong? READ-ONLY:
sends nothing, changes nothing, does not touch seen.json or .env.

  cd /home/vboxuser/binaizleme && set -a && . ./.env && set +a
  .venv/bin/python diag_tap_profile.py 6638901 22702915 > tapdiag.txt 2>&1; cat tapdiag.txt

For each seller it opens the profile in the hidden browser, waits until the page has
finished loading (not just until the first link appears - the old check waited for
ANY "/elanlar/" link, which the site's menu has, so it counted too early), scrolls
down so lazy-loaded ads appear, then prints:
  * what the page is (title, size, blocked or not)
  * the ad links on the page, grouped by category, with a bit of text for each flat
  * the data requests the page itself made (if the list comes from a data address,
    the bot may be able to read it directly, without a browser)
The page is also saved as tapdiag_<seller>.html next to this script.
"""
import os
import re
import sys

import monitor as m

URL = os.environ.get("TAP_PROFILE_URL", "https://tap.az/elanlar?user_id={uid}")
LINK = re.compile(r'href="(?:https?://(?:www\.)?tap\.az)?(/elanlar/[a-z0-9-]+/[a-z0-9-]+/(\d+))[^"]*"')


def flat(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def run(uid):
    from playwright.sync_api import sync_playwright
    print("=" * 72)
    print("seller", uid, "->", URL.format(uid=uid))
    calls, html, title, status, final = [], "", "", None, ""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=m.UA, locale="az-AZ",
                                    viewport={"width": 1280, "height": 2200})

            def on_response(r):
                try:
                    ct = (r.headers or {}).get("content-type", "")
                except Exception:
                    ct = ""
                u = r.url
                if "json" in ct or "/api/" in u or "graphql" in u:
                    try:
                        body = r.text()[:160]
                    except Exception:
                        body = ""
                    calls.append((r.status, r.request.method, u, ct, body))

            page.on("response", on_response)
            resp = page.goto(URL.format(uid=uid), wait_until="domcontentloaded", timeout=45000)
            status, final = (resp.status if resp else None), page.url
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                print("   (page still loading after 20 s)")
            for _ in range(6):                     # lazy-loaded lists appear on scroll
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(1200)
            try:
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
            html, title = page.content(), page.title()
        finally:
            browser.close()

    print(f"HTTP {status} | final url: {final}")
    print(f"title: {title!r} | page size: {len(html)} | blocked page: {m._tap_blocked(html)}")
    with open(f"tapdiag_{uid}.html", "w", encoding="utf-8") as fh:
        fh.write(html)

    by_cat = {}
    for path, iid in LINK.findall(html):
        by_cat.setdefault(path.rsplit("/", 1)[0], set()).add(iid)
    print("ad links on the page, by category:")
    if not by_cat:
        print("   (none)")
    for cat, ids in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        print(f"   {len(ids):3d}  {cat}")

    apts = sorted(by_cat.get("/elanlar/dasinmaz-emlak/menziller", set()))
    print(f"apartment ads found: {len(apts)}")
    for iid in apts[:40]:
        mm = re.search(r'menziller/%s[^"]*"[^>]*>(.{0,600}?)</a>' % iid, html, re.S)
        print(f"   {iid}  {flat(mm.group(1))[:90] if mm else ''}")

    text = flat(html)
    for label, pat in (("ad counts", r"\b\d+\s+elan\b"),
                       ("profile header", r"İstifadəçinin[^.|]{0,60}"),
                       ("VIP / premium blocks", r"(?:VIP|Premium)[^.|]{0,40}"),
                       ("more button", r"(?:Daha çox|Hamısına bax|Növbəti)[^.|]{0,30}")):
        hits = sorted(set(re.findall(pat, text)))[:6]
        if hits:
            print(f"text - {label}: {hits}")

    print(f"data requests made by the page: {len(calls)}")
    for st, meth, u, ct, body in calls[:25]:
        print(f"   {st} {meth} {u[:170]}")
        print(f"        {ct[:45]} | {body[:150]!r}")


if __name__ == "__main__":
    print("VERSION:", m.VERSION)
    for uid in (sys.argv[1:] or ["6638901", "22702915"]):
        try:
            run(uid)
        except Exception as e:
            print(f"seller {uid}: ERROR {e.__class__.__name__}: {e}")
