#!/usr/bin/env python3
"""Naechtliche Analyse des Harness - SCHLAEGT VOR, aendert NICHTS.

Bewusste Entscheidung gegen selbsttaetiges Aendern (Phase F4 der Doku sah
das urspruenglich vor):
  1. Der vorgesehene Validator waere ein Klasse-3-Modell (Llama 3B) und
     wuerde Aenderungen eines 30B-Modells faktisch durchwinken.
  2. Der Optimizer wuerde den Code aendern, der ihn selbst ausfuehrt -
     ein Fehler kann den Rueckweg zerstoeren.
  3. Bei einem Lauf pro Woche fehlt jede statistische Grundlage fuer
     "besser oder schlechter".
  4. Das realistische Risiko ist schleichender Qualitaetsverlust ueber
     Wochen, nicht der offensichtliche Absturz.

Deshalb: Es rechnet die Telemetrie aus, schreibt einen kurzen Vorschlag,
und DU entscheidest morgens. Der Vorschlagstext wird nie ausgefuehrt.

Cron (Beispiel, taeglich 03:00) - richtet 12_MAC_harness_setup.sh NICHT
automatisch ein, das aktivierst du bewusst selbst:
    0 3 * * * cd /opt/ki-server/harness && python3 harness_optimizer.py
"""

import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from harness_telemetry import letzte_laeufe, LOG_FILE
from autonomie import killswitch_aktiv

BERICHT = Path.home() / "Desktop" / "M1_DEPLOYMENT" / "berichte" / "harness_vorschlaege.md"
MIN_LAEUFE_PRO_JOB = 3


def auswerten(laeufe: list) -> list:
    """Rein rechnerische Auswertung - kein Modell, keine Interpretation."""
    pro_job = defaultdict(list)
    for l in laeufe:
        pro_job[l.get("job", "?")].append(l)

    befunde = []
    for job, eintraege in sorted(pro_job.items()):
        n = len(eintraege)
        if n < MIN_LAEUFE_PRO_JOB:
            continue
        misserfolge = [e for e in eintraege if not e.get("ok")]
        versuche = [e.get("versuche", 1) for e in eintraege]
        dauer = [e.get("dauer_sek", 0) for e in eintraege]
        schnitt_versuche = sum(versuche) / n
        quote = len(misserfolge) / n

        if quote >= 0.5:
            befunde.append((
                "hoch", job,
                f"{len(misserfolge)} von {n} Laeufen fehlgeschlagen ({quote:.0%})",
                "Haeufigste Ursache ist ein nicht eingehaltenes Antwortformat. "
                "Pruefe die 'erwartete_ausgabe' im Job und den System-Prompt des Modells.",
            ))
        elif schnitt_versuche >= 2.5:
            befunde.append((
                "mittel", job,
                f"braucht im Schnitt {schnitt_versuche:.1f} Versuche",
                "Die Aufgabenbeschreibung ist vermutlich zu vage oder zu gross. "
                "Praeziser formulieren oder in zwei Tasks aufteilen.",
            ))
        if dauer and sum(dauer) / n > 600:
            befunde.append((
                "niedrig", job,
                f"laeuft im Schnitt {sum(dauer)/n/60:.0f} Minuten",
                "Pruefe, ob ein Review-Agent auf Klasse 3 (llama-fast) reicht.",
            ))
    return befunde


def bericht_schreiben(befunde: list, anzahl_laeufe: int):
    BERICHT.parent.mkdir(parents=True, exist_ok=True)
    stempel = datetime.now().strftime("%Y-%m-%d %H:%M")
    zeilen = [f"\n\n---\n\n## Analyse vom {stempel}", f"\nGrundlage: {anzahl_laeufe} protokollierte Laeufe.\n"]

    if not befunde:
        zeilen.append("Keine Auffaelligkeiten. Es gibt nichts zu tun.\n")
    else:
        rang = {"hoch": 0, "mittel": 1, "niedrig": 2}
        for schwere, job, beobachtung, vorschlag in sorted(befunde, key=lambda b: rang[b[0]]):
            zeilen.append(f"### [{schwere}] {job}\n")
            zeilen.append(f"- **Beobachtet:** {beobachtung}")
            zeilen.append(f"- **Vorschlag:** {vorschlag}")
            zeilen.append("- **Umgesetzt?** ( ) ja  ( ) nein - entscheidest du\n")
        zeilen.append("\n*Es wurde nichts geaendert. Diese Datei ist eine Empfehlung, kein Protokoll.*\n")

    with open(BERICHT, "a", encoding="utf-8") as f:
        f.write("\n".join(zeilen))
    return BERICHT


def main():
    stop = killswitch_aktiv()
    if stop:
        print(f"Kill-Switch aktiv ({stop}) - keine Analyse.")
        return

    laeufe = letzte_laeufe(1000)
    if not laeufe:
        print(f"Noch keine Telemetrie vorhanden ({LOG_FILE}) - nichts auszuwerten.")
        return

    befunde = auswerten(laeufe)
    pfad = bericht_schreiben(befunde, len(laeufe))

    if befunde:
        print(f"{len(befunde)} Vorschlag/Vorschlaege geschrieben nach: {pfad}")
        for schwere, job, beobachtung, _ in befunde:
            print(f"  [{schwere}] {job}: {beobachtung}")
    else:
        print(f"Keine Auffaelligkeiten ({len(laeufe)} Laeufe ausgewertet). Bericht: {pfad}")
    print("Es wurde NICHTS geaendert - du entscheidest.")


if __name__ == "__main__":
    # Selbsttest mit erfundenen Daten (schreibt keinen echten Bericht)
    if "--selbsttest" in sys.argv:
        testdaten = (
            [{"job": "kaputt", "ok": False, "versuche": 5, "dauer_sek": 100} for _ in range(4)]
            + [{"job": "zaeh", "ok": True, "versuche": 3, "dauer_sek": 900} for _ in range(4)]
            + [{"job": "gesund", "ok": True, "versuche": 1, "dauer_sek": 60} for _ in range(4)]
            + [{"job": "zu_wenig_daten", "ok": False, "versuche": 5, "dauer_sek": 10}]
        )
        ergebnis = auswerten(testdaten)
        gefunden = {b[1] for b in ergebnis}
        assert "kaputt" in gefunden, "hohe Fehlerquote nicht erkannt"
        assert "zaeh" in gefunden, "lange Laufzeit nicht erkannt"
        assert "gesund" not in gefunden, "gesunder Job faelschlich gemeldet"
        assert "zu_wenig_daten" not in gefunden, "zu duenne Datenlage haette ignoriert werden muessen"
        for schwere, job, beob, vor in ergebnis:
            print(f"  [{schwere}] {job}: {beob}")
        print("\nSelbsttest OK - erkennt Probleme, meldet Gesundes nicht, ignoriert duenne Daten.")
    else:
        main()
