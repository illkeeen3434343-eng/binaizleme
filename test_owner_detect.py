# -*- coding: utf-8 -*-
"""Automated tests for Azerbaijani owner/makler detection.
Run: .venv/bin/python -m pytest test_owner_detect.py -q     (or plain: python test_owner_detect.py)
Phrases come from real listings; no listing ID is special-cased anywhere.
"""
import os, sys
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "t")
os.environ.setdefault("TELEGRAM_CHAT_ID", "c")
import monitor as m

OWNER_PHRASES = [
    # 1. explicit ownership (47283665 and variants)
    "Menzil özüme mexsusdur", "mənzil özümündü", "mənzil özümə məxsusdu",
    "mənzil mənə məxsusdu", "mənə məxsusdu", "mənə məxsusdur", "mənzil mənimdir",
    "mənzil mənimdi", "özümə məxsusdu", "özümə məxsusdur", "mənzilin sahibi özüməm",
    "ev özümündür", "ev özümə məxsusdur", "şəxsi evimdir", "şəxsi mənzilimdir",
    # 2. makler rejection (48628115, 48329892)
    "Maklerlər yazmasın", "Makler deyiləm", "makler deyil", "mən makler deyiləm",
    "maklerler yazmasin", "maklerlər narahat etməsin", "maklerler narahat etmesin",
    "vasitəçilər yazmasın", "vasitəçilər narahat etməsin", "agentlər yazmasın",
    "agentlər narahat etməsin", "maklerlərlə işləmirəm", "maklerlerle islemirem",
    "vasitəçilərlə işləmirəm",
    # 3. first-person ownership (48539900)
    "Şəxsi öz evimdi", "öz evimdir", "öz evimdi", "öz mənzilimdir", "öz mənzilimdi",
    "mənim öz evimdir", "mənim öz mənzilimdir", "özümün evidir", "özümün mənzilidir",
    # 4. direct-owner statements (48447019)
    "birbaşa özüm satıram", "birbaşa sahibindən", "sahibindən satılır",
    "mülkiyyətçiyəm", "mülkiyyətçi olaraq satıram", "evin sahibiyəm",
    "mənzilin sahibiyəm", "özüm satıram", "özüm tərəfindən satılır",
    "vasitəçisiz satılır", "vasitəçisiz", "vasitəçi yoxdur",
    "Vasitəçilərlə və maklerlərlə işləmirəm", "maklerlər zəng etməsin",
    # typo / casing / punctuation / no-diacritic robustness
    "MENZIL OZUME MEXSUSDUR", "menzil ozume mexsusdur!!!", "oz evimdi.",
    "sexsi oz evimdi", "makler deyilem", "maklerler yazmasin!!!",
]

AGENT_TEXTS = [
    "Ofis xidmət haqqı 1 % təşkil edir", "komissiya 2 faiz",
    "Ekskluziv olaraq bazamızda", "müştərilərimiz üçün",
]

def check(fn, label):
    bad = []
    for p in OWNER_PHRASES if fn == "owner" else AGENT_TEXTS:
        sc, why = m.owner_signal_score(p, "", None)
        ok = sc >= m.SCORE_OWNER_AT if fn == "owner" else sc <= m.SCORE_AGENT_AT
        if not ok:
            bad.append((p, sc, why))
    print(f"{label}: {'PASS' if not bad else 'FAIL'} "
          f"({len(OWNER_PHRASES if fn=='owner' else AGENT_TEXTS)-len(bad)} ok)")
    for p, sc, why in bad:
        print(f"   MISS score={sc:>3} {p!r} {why}")
    return not bad

def test_owner_phrases():   assert check("owner", "owner phrases")
def test_agency_phrases():  assert check("agent", "agency wording")

def test_makler_word_not_agent():
    """'makler deyiləm' must NOT count as an agent signal (masking works)."""
    sc, why = m.owner_signal_score("Mən makler deyiləm, ev özümündür", "", None)
    assert sc >= m.SCORE_OWNER_AT, (sc, why)
    assert not any(r.startswith("agency") for r in why), why
    print("makler-negation: PASS", sc, why)

def test_unrelated_listings_not_agent():
    """Many total ads but few APARTMENTS must stay owner (48628082/48407292/48625621)."""
    for cnt in (1, 2):
        sc, why = m.owner_signal_score("3 otaqli menzil satilir", "Ibrahim", cnt)
        assert sc > m.SCORE_AGENT_AT, (cnt, sc, why)
    sc3, _ = m.owner_signal_score("3 otaqli menzil satilir", "Ibrahim", 5)
    assert sc3 <= m.SCORE_AGENT_AT, sc3
    print("apartment-count rule: PASS (1,2 ok / 5 agent)")

def test_business_name():
    sc, why = m.owner_signal_score("menzil satilir", "Ashiq Invest", 1)
    assert sc <= m.SCORE_AGENT_AT, (sc, why)
    sc2, _ = m.owner_signal_score("menzil satilir", "İbrahim", 1)
    assert sc2 > m.SCORE_AGENT_AT
    print("business-name: PASS")

def test_owner_beats_business_noise():
    """A strong owner statement outweighs a weak/unknown profile."""
    sc, why = m.owner_signal_score("Menzil ozume mexsusdur, makler deyilem", "", None)
    assert sc >= m.SCORE_OWNER_AT, (sc, why)
    print("owner-overrides-unknown-profile: PASS", sc)

if __name__ == "__main__":
    fails = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
            except AssertionError as e:
                fails += 1; print(f"{name}: FAIL {e}")
    print("\nALL PASS" if not fails else f"\n{fails} TEST(S) FAILED")
    sys.exit(1 if fails else 0)
