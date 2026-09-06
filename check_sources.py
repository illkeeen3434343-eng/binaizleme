#!/usr/bin/env python3
# Why are yeniemlak / tap / lalafo silent? Tests each stage of each source.
#   cd /home/vboxuser/binaizleme && set -a && . ./.env && set +a
#   /usr/bin/flock /tmp/binaizleme.lock .venv/bin/python check_sources.py
import re, html
import monitor as m

st, _ = m.load_state(); L = st["listings"]
print("VERSION:", m.VERSION)

print("\n=== TELEGRAM DELIVERY ===")
tok, chat = (m.BOT_TOKEN or ""), (m.CHAT_ID or "")
if tok != tok.strip() or chat != chat.strip() or "\r" in tok or "\r" in chat:
    print("  !! stray whitespace/\\r in .env -> run: sed -i 's/\\r$//' .env")
print("  test message sent:", m.tg_send_message("health probe - delivery works"))

# ---------------- tap.az ----------------
print("\n=== TAP.AZ ===")
try:
    r, body = m._fetch_tap_html(m._with_page(m.TAP_SEARCH_URL, 1))
    print("  page1 HTTP", getattr(r, "status_code", "?"), "| blocked:", m._tap_blocked(body),
          "| bytes:", len(body))
    orig_kw, orig_oo = m.TAP_KEYWORDS, m.TAP_OWNER_ONLY
    m.TAP_KEYWORDS, m.TAP_OWNER_ONLY = [], False
    raw_cards, total = m._parse_tap_page(body)
    m.TAP_KEYWORDS, m.TAP_OWNER_ONLY = [], orig_oo
    no_kw, _ = m._parse_tap_page(body)
    m.TAP_KEYWORDS = orig_kw
    filtered, _ = m._parse_tap_page(body)
    print(f"  site says ~{total} ads | cards on page1: {len(raw_cards)}")
    print(f"  after Magaza filter: {len(no_kw)}  ->  after TAP_KEYWORDS: {len(filtered)}")
    print("  TAP_KEYWORDS:", orig_kw)
    print("  sample headings the keyword filter sees:")
    for l in raw_cards[:8]:
        head = f"{l.get('rooms')}-otaqlı ... {l.get('location')}"
        hit = [k for k in orig_kw if l.get("location") and k in l["location"]]
        print(f"     {'KEEP' if hit else 'DROP'}  {head[:66]}")
except Exception as e:
    print("  tap error:", e)

# ---------------- yeniemlak.az ----------------
print("\n=== YENIEMLAK.AZ ===")
try:
    items = m.fetch_yeniemlak(m.YENIEMLAK_SEARCH_URL)
    unseen = [l for l in items if "ye:" + str(l["id"]) not in L]
    print(f"  fetched {len(items)} | not in seen.json: {len(unseen)}")
    print("  owner_label used:", "Əmlak sahibi")
    for l in unseen[:5]:
        rr = m.http_get(l["url"], headers=m.HTML_HEADERS, timeout=30, browser=True)
        raw = rr.text or ""
        txt = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw)))
        labels = {w: (w in txt) for w in
                  ("\u018fmlak sahibi", "M\u00fclkiyy\u0259t\u00e7i", "Vasit\u0259\u00e7i",
                   "Sahibi", "Agentlik", "Rieltor")}
        v, _p = m.check_is_owner(l["url"], "\u018fmlak sahibi")
        print(f"   {l['id']} HTTP {getattr(rr,'status_code','?')} verdict={v} "
              f"present={[k for k,ok in labels.items() if ok]}")
except Exception as e:
    print("  yeniemlak error:", e)

# ---------------- lalafo.az ----------------
print("\n=== LALAFO.AZ ===")
try:
    items = m.fetch_lalafo(m.LALAFO_SEARCH_URL)
    unseen = [l for l in items if "lala:" + str(l["id"]) not in L]
    print(f"  fetched {len(items)} | not in seen.json: {len(unseen)}")
    okloc = [l for l in items if m.lalafo_ok(l)]
    agents = [l for l in items if l.get("_agent")]
    print(f"  pass /baku/ location check: {len(okloc)}/{len(items)} | flagged agent: {len(agents)}")
    for l in items[:5]:
        print(f"     ok={m.lalafo_ok(l)} agent={l.get('_agent')} {l['url'][:72]}")
except Exception as e:
    print("  lalafo error:", e)
