#!/usr/bin/env python3
"""Telemetrie fuer den Harness: protokolliert jeden Crew-Lauf in eine JSONL-Datei.

Siehe VOLLAUSBAU_SYSTEM.md Phase F3. Grundlage fuer harness_optimizer.py
(Self-Improvement) - der darf aber erst handeln, wenn hier genug Historie
vorhanden ist (siehe dortige Sicherheitsregel).
"""

import json
from datetime import datetime
from pathlib import Path

LOG_FILE = Path("/opt/ki-server/memory/harness_log.jsonl")

# Grobe Stromkosten-Schaetzung (Mac Studio M1 Max unter Last)
WATT_UNDER_LOAD = 45
STROMPREIS_EUR_KWH = 0.30


def kosten_cent(dauer_sek: float) -> float:
    kwh = (WATT_UNDER_LOAD * dauer_sek / 3600) / 1000
    return round(kwh * STROMPREIS_EUR_KWH * 100, 3)


# Rotation: ohne Begrenzung waechst die Datei ueber Jahre unbegrenzt.
# 5000 Zeilen reichen fuer die Auswertung weit mehr als aus.
MAX_ZEILEN = 5000


def _rotieren():
    """Kuerzt die Logdatei auf die letzten MAX_ZEILEN Eintraege."""
    try:
        if not LOG_FILE.exists():
            return
        zeilen = LOG_FILE.read_text(encoding="utf-8").splitlines()
        if len(zeilen) > MAX_ZEILEN * 2:  # erst bei deutlichem Ueberschreiten anfassen
            behalten = zeilen[-MAX_ZEILEN:]
            LOG_FILE.write_text("\n".join(behalten) + "\n", encoding="utf-8")
    except Exception:
        pass  # Telemetrie darf den eigentlichen Lauf nie stoppen


def log_run(job_name: str, versuche: int, dauer_sek: float, erfolgreich: bool, tokens_geschaetzt: int = 0) -> dict:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    _rotieren()
    eintrag = {
        "ts": datetime.now().isoformat(),
        "job": job_name,
        "versuche": versuche,
        "dauer_sek": round(dauer_sek, 1),
        "kosten_cent": kosten_cent(dauer_sek),
        "tokens_geschaetzt": tokens_geschaetzt,
        "ok": erfolgreich,
    }
    # encoding ausdruecklich: ohne Angabe haengt es von der Locale ab.
    # Ein Job-Name mit Umlaut schriebe dann je nach System andere Bytes,
    # als letzte_laeufe() spaeter erwartet.
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(eintrag, ensure_ascii=False) + "\n")
    return eintrag


def letzte_laeufe(n: int = 20) -> list:
    """Die letzten n Eintraege. Kaputte Zeilen werden UEBERSPRUNGEN.

    Warum das wichtig ist: Bricht der Rechner mitten in log_run ab,
    bleibt eine halbe JSON-Zeile stehen. Vorher lief json.loads darauf
    ungeschuetzt - die Funktion starb, und mit ihr der naechtliche
    harness_optimizer. Der raeumt nichts auf, also scheiterte er ab da
    JEDE Nacht, bis jemand die Datei von Hand reparierte. Eine
    Protokolldatei darf die Auswertung nie zum Absturz bringen.
    """
    if not LOG_FILE.exists():
        return []
    try:
        roh = LOG_FILE.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    eintraege = []
    kaputt = 0
    for z in roh.splitlines():
        if not z.strip():
            continue
        try:
            wert = json.loads(z)
        except ValueError:
            kaputt += 1
            continue
        # Nur Objekte auswerten - auswerten() greift mit .get() zu.
        if isinstance(wert, dict):
            eintraege.append(wert)
        else:
            kaputt += 1
    if kaputt:
        print(f"Telemetrie: {kaputt} unlesbare Zeile(n) uebersprungen "
              f"({LOG_FILE}).")
    return eintraege[-n:]


if __name__ == "__main__":
    # Selbsttest - schreibt einen Testeintrag in eine temporaere Datei,
    # ruehrt die echte harness_log.jsonl NICHT an.
    import tempfile

    original_log_file = LOG_FILE
    globals()["LOG_FILE"] = Path(tempfile.gettempdir()) / "harness_telemetry_selbsttest.jsonl"
    if LOG_FILE.exists():
        LOG_FILE.unlink()

    eintrag = log_run("selbsttest", versuche=1, dauer_sek=42.0, erfolgreich=True, tokens_geschaetzt=500)
    print("Test-Eintrag geschrieben:", eintrag)
    geladen = letzte_laeufe(5)
    print("Zurueckgelesen:", geladen)
    LOG_FILE.unlink()

    assert len(geladen) == 1 and geladen[0]["job"] == "selbsttest", "Selbsttest fehlgeschlagen"

    # Runde 3: eine abgeschnittene Zeile (Absturz waehrend log_run) darf
    # die Auswertung nicht toeten - sonst stirbt jede Nacht der Optimizer.
    LOG_FILE.write_text(
        '{"ts":"2026-08-18T01:00:00","job":"gut","versuche":1,"dauer_sek":10,"ok":true}\n'
        '{"ts":"2026-08-18T02:00:00","job":"halb","versu',
        encoding="utf-8")
    ueberlebt = letzte_laeufe(10)
    assert len(ueberlebt) == 1 and ueberlebt[0]["job"] == "gut", \
        "halbe Zeile bringt letzte_laeufe zum Absturz oder verschluckt gute Zeilen"
    print("Selbsttest OK: abgeschnittene Zeile wird uebersprungen,"
          " die gute Zeile bleibt erhalten.")

    # Und eine Zeile, die zwar gueltiges JSON ist, aber kein Objekt.
    LOG_FILE.write_text('"nur ein string"\n42\n', encoding="utf-8")
    assert letzte_laeufe(10) == [], "Nicht-Objekte muessen aussortiert werden"
    print("Selbsttest OK: JSON-Zeilen ohne Objektform werden aussortiert.")
    LOG_FILE.unlink(missing_ok=True)

    print("\nSelbsttest OK. (Produktiv-Pfad:", original_log_file, ")")
