#!/usr/bin/env python3
"""Quality Gate fuer den Harness: Canary-Check (Drift-Erkennung) + Max-Versuche-Limit.

Laeuft bewusst OHNE KI - rein deterministische String-Pruefung, siehe
VOLLAUSBAU_SYSTEM.md Phase F2/F3 ("Quality Gates ohne KI!"). Wird von
crew_generic.py nach jedem Crew-Lauf aufgerufen.
"""

MAX_VERSUCHE = 5


def check_response(response: str, versuch: int, job_name: str) -> dict:
    """Prueft eine Modell-Antwort, bevor sie weiterverarbeitet wird.

    Rueckgabe: {"ok": bool, "reason": str, "action": str (optional bei ok=False)}
    """
    # Reihenfolge ist wichtig: Eine GUTE Antwort ist gut, egal im wievielten
    # Versuch sie kam. Vorher wurde erst die Ankerphrase und danach die
    # Versuchszahl geprueft - eine einwandfreie Antwort im letzten Versuch
    # wurde deshalb verworfen und der Lauf als gescheitert gemeldet, obwohl
    # das Ergebnis vorlag (und ein kompletter Modell-Lauf dafuer draufging).
    if response and response.strip().startswith("Mexla,"):
        return {"ok": True, "reason": "ok"}

    # Ab hier: die Antwort taugt nicht. War es der letzte erlaubte Versuch,
    # ist Weiterprobieren sinnlos - dann Aufgabe aufteilen statt wiederholen.
    if versuch >= MAX_VERSUCHE:
        return {
            "ok": False,
            "reason": "max_attempts",
            "action": "split_ticket",
            "hinweis": f"[{job_name}] {MAX_VERSUCHE} Versuche ohne gueltige Antwort - Aufgabe aufteilen",
        }

    return {
        "ok": False,
        "reason": "canary_failed",
        "hinweis": f"[{job_name}] Ankerphrase 'Mexla,' fehlt (Versuch {versuch}/{MAX_VERSUCHE})",
    }


if __name__ == "__main__":
    # Selbsttest - laeuft ohne Ollama/CrewAI, nur Standardbibliothek.
    # (text, versuch, soll_ok, erwarteter reason)
    # Der reason wird mitgeprueft, damit ein Test nicht zufaellig aus dem
    # falschen Grund besteht - "abgelehnt" allein sagt noch nicht, ob die
    # Ankerphrase fehlte oder die Versuche aufgebraucht waren.
    tests = [
        ("Mexla, alles klar.", 1, True, "ok"),
        ("Alles klar, kein Canary davor.", 1, False, "canary_failed"),
        ("", 1, False, "canary_failed"),
        ("   Mexla, mit Leerzeichen davor.", 1, True, "ok"),
        ("mexla, kleingeschrieben.", 1, False, "canary_failed"),
        # Gueltige Antwort im letzten erlaubten Versuch: muss zaehlen.
        ("Mexla, ok.", MAX_VERSUCHE, True, "ok"),
        # Ungueltige Antwort im letzten Versuch: aufteilen statt wiederholen.
        ("keine Ankerphrase", MAX_VERSUCHE, False, "max_attempts"),
        ("keine Ankerphrase", MAX_VERSUCHE - 1, False, "canary_failed"),
    ]
    fehler = 0
    for text, versuch, erwartet_ok, erwartet_grund in tests:
        ergebnis = check_response(text, versuch, "selbsttest")
        passt = (ergebnis["ok"] == erwartet_ok
                 and ergebnis["reason"] == erwartet_grund)
        status = "OK    " if passt else "FEHLER"
        if not passt:
            fehler += 1
        print(f"{status} versuch={versuch} text={text!r} -> {ergebnis['reason']}"
              f" (erwartet: {erwartet_grund})")

    # Zusaetzlich: nach einem max_attempts MUSS die Engine abbrechen koennen.
    letzter = check_response("nix", MAX_VERSUCHE, "selbsttest")
    if letzter.get("action") != "split_ticket":
        print("FEHLER: kein 'split_ticket' - crew_generic wuerde endlos weiterprobieren")
        fehler += 1
    else:
        print("OK     letzter Versuch liefert das Abbruch-Signal 'split_ticket'")

    print(f"\n{len(tests) + 1 - fehler}/{len(tests) + 1} Tests bestanden.")
    if fehler:
        raise SystemExit(1)
