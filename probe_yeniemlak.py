#!/usr/bin/env python3
"""Can this VM read yeniemlak.az detail pages the way the bot does? Read-only.

  cd /home/vboxuser/binaizleme && set -a && . ./.env && set +a
  .venv/bin/python probe_yeniemlak.py

Reads one detail page cold, then again right after reading 3 search pages (the bot
reads ~30 search pages first, which can trigger a rate limit). Paste the output.
"""
import re
import sys
import time
import html as H

import monitor as m

URL = sys.argv[1] if len(sys.argv) > 1 else (
    "https://yeniemlak.az/elan/satilir-2-otaqli-bina-evi-menzil-xetai-rayonu-xetai-rayon-seyidezim-sirva-234096")
LABEL = "Əmlak sahibi"


def probe(tag):
    try:
        r = m.http_get(URL, headers=m.HTML_HEADERS, timeout=30, browser=True)
    except Exception as e:
        print(f"[{tag}] request error: {e}")
        return
    body = r.text or ""
    flat = re.sub(r"\s+", " ", H.unescape(re.sub(r"<[^>]+>", " ", body)))
    try:
        utf = r.content.decode("utf-8")
        utf_flag = LABEL in utf
    except Exception as e:
        utf_flag = f"not valid utf-8 ({e.__class__.__name__})"
    low = body.lower()
    print(f"[{tag}] HTTP {r.status_code}  bytes={len(body)}  "
          f"content-type={r.headers.get('content-type')!r}")
    print(f"[{tag}] owner label in page text: {LABEL in flat}   in raw utf-8: {utf_flag}")
    print(f"[{tag}] looks like a block page: "
          f"{any(x in low for x in ('just a moment', 'cf-chl', 'captcha', 'access denied', 'too many'))}")
    print(f"[{tag}] first 160 chars of text: {flat[:160]!r}")


print("VERSION:", m.VERSION, "| curl_cffi available:", m.HAS_CURL_CFFI)
probe("cold")
v, _p = m.check_is_owner(URL, LABEL)
print(f"[cold] check_is_owner -> {v} (reason: {m.check_is_owner.last_reason})\n")
for p in (1, 2, 3):
    try:
        m.http_get(m._with_page(m.YENIEMLAK_SEARCH_URL, p), headers=m.HTML_HEADERS,
                   timeout=30, browser=True)
    except Exception as e:
        print("search page error:", e)
    time.sleep(m.PAGE_DELAY)
probe("after search")
v, _p = m.check_is_owner(URL, LABEL)
print(f"[after search] check_is_owner -> {v} (reason: {m.check_is_owner.last_reason})")
