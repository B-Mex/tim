#!/usr/bin/env python3
"""Vergleicht Whisper-Modelle auf ECHTEN Aufnahmen - nicht auf Werbeversprechen.

Warum es das gibt: Am 24.08.2026 sollte ggml-large-v3-turbo das bisherige
ggml-medium als Befehlsmodell abloesen, weil es "bei Deutsch deutlich
staerker" sei. Nachgemessen auf der einzigen vorhandenen echten Aufnahme
war es das NICHT: beide erkannten denselben Satz, medium war sogar
minimal schneller. Eine Behauptung aus einer Modellbeschreibung ist kein
Messwert - und eine einzelne Aufnahme ist noch keine Messung.

Dieses Werkzeug macht beides sichtbar: was die Modelle auf DEINEN
Aufnahmen wirklich verstehen, und wie duenn die Datenbasis dabei ist.

Nutzung:
    python3 sprach_vergleich.py                     # alle Modelle, alle Proben
    python3 sprach_vergleich.py --modelle medium large-v3-turbo
    python3 sprach_vergleich.py --ordner /pfad/zu/wavs

Datenschutz: Sprachaufnahmen sind ein personenbezogenes Datum. Der
Probenordner ist deshalb gitignoriert und wird von diesem Werkzeug nur
GELESEN. Es loescht nichts und legt nichts an.
"""

import argparse
import shutil
import subprocess
import sys
import time
import wave
from pathlib import Path

BASIS = Path(__file__).resolve().parent.parent
MODELL_ORDNER = BASIS / "whisper-models"
# Die Proben liegen bewusst NICHT im Repo: siehe Datenschutz oben.
PROBEN_ORDNER = BASIS / "logs" / "sprachproben"
# Die eine Aufnahme, die der Sprachassistent ohnehin schreibt. Sie wird
# bei jedem Zuruf ueberschrieben - deshalb ist sie ein Notbehelf, keine
# Sammlung.
EINZELNE = BASIS / "logs" / "letzte_aufnahme.wav"

WHISPER = (shutil.which("whisper-cli") or shutil.which("whisper-cpp")
           or "/opt/homebrew/bin/whisper-cli")


def modelle_finden(wunsch):
    """Vorhandene Modelldateien - nach Groesse aufsteigend."""
    gefunden = []
    for datei in sorted(MODELL_ORDNER.glob("ggml-*.bin")):
        name = datei.stem.replace("ggml-", "")
        if wunsch and name not in wunsch:
            continue
        gefunden.append((name, datei, datei.stat().st_size))
    gefunden.sort(key=lambda m: m[2])
    return gefunden


def proben_finden(ordner=None):
    """Alle Aufnahmen, die es gibt. Ehrlich auch dann, wenn es keine gibt."""
    quellen = []
    ziel = Path(ordner) if ordner else PROBEN_ORDNER
    if ziel.is_dir():
        quellen.extend(sorted(ziel.glob("*.wav")))
    if not ordner and EINZELNE.exists():
        quellen.append(EINZELNE)
    return quellen


def dauer(pfad):
    try:
        with wave.open(str(pfad)) as w:
            return w.getnframes() / w.getframerate()
    except (wave.Error, OSError):
        return 0.0


def erkennen(modell_datei, wav):
    """Einmal transkribieren. Gibt (text, sekunden) zurueck."""
    start = time.time()
    try:
        lauf = subprocess.run(
            [WHISPER, "-m", str(modell_datei), "-l", "de", "-f", str(wav),
             "-nt", "-np"],
            capture_output=True, text=True, timeout=600)
    except (FileNotFoundError, subprocess.TimeoutExpired) as fehler:
        return f"<{type(fehler).__name__}>", time.time() - start
    text = " ".join(lauf.stdout.split()).strip()
    return text, time.time() - start


def vergleichbar(text):
    """Fuer den Vergleich auf das Wesentliche reduzieren.

    Satzzeichen und Gross-/Kleinschreibung sind fuer die Befehlserkennung
    egal - der Sprachassistent normalisiert ohnehin. Sie als Unterschied
    zu zaehlen wuerde Unterschiede vortaeuschen, wo keine sind (gemessen:
    large-v3-turbo haengt gern einen Punkt an).
    """
    behalten = [z for z in text.lower() if z.isalnum() or z.isspace() or z == "%"]
    return " ".join("".join(behalten).split())


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--modelle", nargs="*", default=None,
                   help="nur diese Modelle (ohne 'ggml-' und '.bin')")
    p.add_argument("--ordner", default=None, help="eigener Probenordner")
    p.add_argument("--laeufe", type=int, default=2,
                   help="Laeufe je Paar; der erste enthaelt die Ladezeit")
    args = p.parse_args()

    modelle = modelle_finden(args.modelle)
    proben = proben_finden(args.ordner)

    if not modelle:
        print("LUECKE: keine Modelldateien in", MODELL_ORDNER)
        return 1

    print(f"Modelle ({len(modelle)}):")
    for name, datei, groesse in modelle:
        print(f"   {name:22} {groesse / 1e9:5.2f} GB")

    if not proben:
        # Ehrlich melden statt still ein leeres 'bestanden' zu liefern.
        print()
        print("LUECKE: keine Aufnahmen gefunden.")
        print(f"  Erwartet in: {PROBEN_ORDNER}")
        print("  Ohne echte Aufnahmen ist kein Vergleich moeglich. Sag Tim")
        print("  ein paar Saetze zu (verschiedene Raeume, Abstaende,")
        print("  Nebengeraeusche) und lege die Mitschnitte dort ab.")
        print("  EIN Beispiel ist keine Messung - dieses Werkzeug wird")
        print("  aussagekraeftig ab etwa zehn Aufnahmen.")
        return 2

    print(f"\nAufnahmen ({len(proben)}):")
    for wav in proben:
        print(f"   {wav.name:34} {dauer(wav):5.1f}s")
    if len(proben) < 10:
        print(f"\n  ACHTUNG: {len(proben)} Aufnahme(n) sind zu wenig fuer eine")
        print("  belastbare Aussage. Das Ergebnis unten ist ein Hinweis,")
        print("  kein Beweis.")

    unterschiede = 0
    tempo = {name: [] for name, _, _ in modelle}

    for wav in proben:
        print(f"\n--- {wav.name} ({dauer(wav):.1f}s) " + "-" * 34)
        texte = {}
        for name, datei, _ in modelle:
            zeiten = []
            text = ""
            for lauf in range(max(1, args.laeufe)):
                text, sek = erkennen(datei, wav)
                zeiten.append(sek)
            # Der erste Lauf enthaelt die Ladezeit; gewertet wird der beste.
            warm = min(zeiten)
            tempo[name].append(warm)
            texte[name] = text
            print(f"  {name:22} {warm:5.2f}s  {text[:74]}")
        eindeutig = {vergleichbar(t) for t in texte.values()}
        if len(eindeutig) > 1:
            unterschiede += 1
            print("  -> UNTERSCHIED: die Modelle verstehen NICHT dasselbe.")

    print("\n" + "=" * 62)
    print("Tempo (warm, Mittel ueber alle Aufnahmen):")
    for name, _, _ in modelle:
        werte = tempo[name]
        if werte:
            print(f"   {name:22} {sum(werte) / len(werte):5.2f}s")
    print(f"\nAufnahmen mit abweichendem Ergebnis: {unterschiede} von {len(proben)}")
    if unterschiede == 0:
        print("Kein Modell versteht etwas, das ein anderes nicht auch versteht.")
        print("Ein Wechsel ist auf dieser Datenbasis NICHT begruendet -")
        print("das kleinere und schnellere Modell bleibt die richtige Wahl.")
    else:
        print("Sieh dir die Unterschiede oben einzeln an: Welches Modell hat")
        print("recht? Nur das entscheidet, nicht die Modellgroesse.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
