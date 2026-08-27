#!/usr/bin/env python3
"""Laeuft ein langer Testlauf noch, oder haengt er?

Warum es das gibt: Am 27.08.2026 hielt ich einen laufenden Finaldurchgang
sechs Stunden lang faelschlich fuer aufgehaengt. Ich hatte auf die
Pruefungschats geschaut - aber das Finale schreibt in den
Werkstatt-Sandkasten. Das Messgeraet zeigte Stillstand, wo Arbeit war.

Diese Abfrage schaut deshalb an ALLEN Orten nach, an denen ein Lauf
Spuren hinterlaesst, und meldet den juengsten Zeitstempel. Wer wissen
will, ob etwas lebt, fragt nicht einen Ort - er fragt alle.

    laeuft_noch.py
"""
import subprocess
import sys
import time
from pathlib import Path

ERGEBNIS_WURZEL = Path.home() / "Desktop" / "M1_DEPLOYMENT" / "docs"
PROZESSE = ("abitur_lauf.py", "abitur.py", "kettentest.py", "hardwaretest.py",
            "modell_benchmark.py")


def _juengster_abiturordner() -> Path | None:
    """Der Ergebnisordner traegt seit dem 27.08. einen Zeitstempel je
    Lauf (abitur_lauf._lauf_ordner) - hartkodiert waere er beim ersten
    neuen Lauf stumm falsch und meldete 'keine Spuren' fuer einen
    lebenden Durchgang."""
    kandidaten = [p for p in ERGEBNIS_WURZEL.glob("abitur_*") if p.is_dir()]
    if not kandidaten:
        return None
    return max(kandidaten, key=lambda p: p.stat().st_mtime)


def orte() -> list:
    liste = [
        ("Pruefungschats", Path("/opt/ki-server/memory/chats")),
        ("Werkstatt-Sandkasten", Path.home() / "Desktop" / "Tim-Werkstatt" / "sandkasten"),
        ("Livewerkstatt", Path.home() / "Desktop" / "Tim-Livewerkstatt" / "sandkasten"),
    ]
    o = _juengster_abiturordner()
    if o is not None:
        liste.insert(0, ("Fortschrittsdatei", o / "FORTSCHRITT.txt"))
        liste.append(("Ergebnisse", o))
    return liste


def juengste(pfad: Path) -> float:
    """Zeitstempel der juengsten Datei - 0, wenn nichts da ist."""
    if pfad.is_file():
        return pfad.stat().st_mtime
    if not pfad.is_dir():
        return 0.0
    neuste = 0.0
    for p in pfad.rglob("*"):
        try:
            if p.is_file():
                neuste = max(neuste, p.stat().st_mtime)
        except OSError:
            pass
    return neuste


def main() -> int:
    jetzt = time.time()
    print("Laufende Prozesse:")
    gefunden = False
    for name in PROZESSE:
        p = subprocess.run(["pgrep", "-f", name], capture_output=True, text=True)
        for pid in p.stdout.split():
            alter = subprocess.run(["ps", "-o", "etime=", "-p", pid],
                                   capture_output=True, text=True).stdout.strip()
            print("  %-22s PID %-7s seit %s" % (name, pid, alter))
            gefunden = True
    if not gefunden:
        print("  keiner")

    print("\nJuengste Spur je Ort:")
    neuste_gesamt = 0.0
    for name, pfad in orte():
        t = juengste(pfad)
        if not t:
            print("  %-22s (nichts)" % name)
            continue
        alter_s = jetzt - t
        neuste_gesamt = max(neuste_gesamt, t)
        print("  %-22s vor %s" % (name, _dauer(alter_s)))

    print()
    if not neuste_gesamt:
        print("URTEIL: keine Spuren gefunden.")
        return 1
    alter = jetzt - neuste_gesamt
    if not gefunden:
        print("URTEIL: kein Lauf aktiv (letzte Spur vor %s)." % _dauer(alter))
        return 0
    if alter < 600:
        print("URTEIL: LAEUFT - juengste Spur vor %s." % _dauer(alter))
        return 0
    print("URTEIL: VERDACHT AUF HAENGER - Prozess lebt, aber seit %s "
          "keine Spur mehr." % _dauer(alter))
    return 2


def _dauer(s: float) -> str:
    if s < 60:
        return "%.0f s" % s
    if s < 3600:
        return "%.0f min" % (s / 60)
    return "%.1f h" % (s / 3600)


if __name__ == "__main__":
    sys.exit(main())
