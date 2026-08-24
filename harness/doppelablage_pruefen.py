#!/usr/bin/env python3
"""Doppelablage-Waechter - vergleicht Quelle und Betrieb, nur lesend.

Warum es dieses Werkzeug gibt: Das Projekt lebt in zwei Ablagen -
~/Desktop/M1_DEPLOYMENT ist die QUELLE, /opt/ki-server ist der BETRIEB.
Am 23.08.2026 hat diese Bauweise zweimal zugeschlagen:

  1. sprachassistent.py lag NUR im Betrieb, nicht in der Quelle - der
     Mutationstest fand seine Suchtexte nicht und lief rot, obwohl der
     Code in Ordnung war (die "Doppelablage-Falle").
  2. Nach einem cp nach /opt lief ein Dienst weiter mit der ALTEN
     Fassung im Speicher - Aenderungen "griffen nicht", bis jemand an
     den Neustart dachte (die "Dienst-Neustart-Falle").

Beides sind keine Programmierfehler, sondern Zustaende, die man SEHEN
muss. Genau das tut dieses Werkzeug: Es meldet Abweichungen und sagt,
welche Seite neuer ist - es kopiert NICHTS, repariert NICHTS und
loescht NICHTS. Was zu tun ist, entscheidet Mexla.

Drei Blicke:

  * Baumvergleich: Code-Dateien (py/sh/html/json), die in beiden
    Ablagen liegen, muessen byte-gleich sein. Eine Datei, die NUR im
    Betrieb liegt, ist die klassische Doppelablage-Falle.
  * Dienst-Frische: Laeuft ein launchd-Dienst laenger, als seine
    Programmdatei alt ist, faehrt er eine veraltete Fassung.
  * LaunchAgents: Die installierten plists gegen die im Repo.

Aufruf:
    python3 doppelablage_pruefen.py               # echter Vergleich
    python3 doppelablage_pruefen.py --selbsttest

Exit: 0 = deckungsgleich (Hinweise erlaubt), 1 = Abweichungen gefunden.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import time
from pathlib import Path

QUELLE = Path.home() / "Desktop" / "M1_DEPLOYMENT"
BETRIEB = Path("/opt/ki-server")
# Nur diese Paare sind als synchron vereinbart (Quell-Ordner, Betriebs-
# Ordner - die Kamera wohnt in der Quelle unter hardware/). config/
# fehlt bewusst (Geheimnisse und *.example duerfen abweichen), ebenso
# alles, was zur Laufzeit entsteht (logs, memory, venv).
SYNCHRONE_PAARE = (("harness", "harness"), ("oberflaeche", "oberflaeche"),
                   ("hardware/kamera", "kamera"), ("scripts", "scripts"))
CODE_ENDUNGEN = (".py", ".sh", ".html", ".json")
AUSGEBLENDET = ("__pycache__", "_archiv", ".DS_Store",
                # Laufzeitzustand der Kamera - legt der Dienst selbst an,
                # darf also je Seite verschieden sein (steht auch in der
                # .gitignore des Betriebs-Repos).
                "auge.json", "anzeigen.json", "messfeld.json")

DIENSTE = {
    "com.ki-server.zentrale": BETRIEB / "oberflaeche" / "m1_zentrale.py",
    "com.ki-server.jobserver": BETRIEB / "oberflaeche" / "m1_job_server.py",
    "com.ki-server.sprachassistent": BETRIEB / "scripts" / "sprachassistent.py",
    "com.ki-server.kamera": BETRIEB / "kamera" / "kamera_dienst.py",
}
LAUNCHAGENTS_REPO = BETRIEB / "launchagents"
LAUNCHAGENTS_INSTALLIERT = Path.home() / "Library" / "LaunchAgents"


def _dateien(basis: Path, unterordner: str) -> dict:
    """Relative Pfade (ab Unterordner) -> volle Pfade der Code-Dateien."""
    wurzel = basis / unterordner
    gefunden = {}
    if not wurzel.is_dir():
        return gefunden
    for pfad in sorted(wurzel.rglob("*")):
        if not pfad.is_file() or pfad.suffix not in CODE_ENDUNGEN:
            continue
        if any(teil in AUSGEBLENDET for teil in pfad.parts) \
                or pfad.name in AUSGEBLENDET:
            continue
        gefunden[str(pfad.relative_to(wurzel))] = pfad
    return gefunden


def _pruefsumme(pfad: Path) -> str:
    return hashlib.sha256(pfad.read_bytes()).hexdigest()


def vergleiche_baeume(quelle: Path, betrieb: Path,
                      paare=SYNCHRONE_PAARE) -> dict:
    """Der reine Vergleich - ohne Ausgabe, damit pruefbar.

    Rueckgabe: {"abweichend": [(pfad, neuere_seite)],
                "nur_betrieb": [pfad], "nur_quelle": [pfad],
                "gleich": anzahl} - Pfade aus Betriebssicht.
    """
    ergebnis = {"abweichend": [], "nur_betrieb": [], "nur_quelle": [],
                "gleich": 0}
    for q_ordner, b_ordner in paare:
        q = _dateien(quelle, q_ordner)
        b = _dateien(betrieb, b_ordner)
        for rel in sorted(set(q) | set(b)):
            anzeige = "%s/%s" % (b_ordner, rel)
            if rel in q and rel in b:
                if _pruefsumme(q[rel]) == _pruefsumme(b[rel]):
                    ergebnis["gleich"] += 1
                else:
                    neuer = ("Quelle" if q[rel].stat().st_mtime
                             > b[rel].stat().st_mtime else "Betrieb")
                    ergebnis["abweichend"].append((anzeige, neuer))
            elif rel in b:
                ergebnis["nur_betrieb"].append(anzeige)
            else:
                ergebnis["nur_quelle"].append(anzeige)
    return ergebnis


# ----------------------------------------------------------------------
# Dienst-Frische
# ----------------------------------------------------------------------
def _pid_aus_print(text: str) -> int | None:
    """Die pid-Zeile aus 'launchctl print' - oder None."""
    for zeile in text.splitlines():
        zeile = zeile.strip()
        if zeile.startswith("pid = "):
            try:
                return int(zeile.split("=", 1)[1].strip())
            except ValueError:
                return None
    return None


def _start_aus_lstart(text: str) -> float | None:
    """'ps -o lstart=' ('Sun Aug 24 03:10:00 2026') -> Unix-Zeit."""
    text = text.strip()
    if not text:
        return None
    try:
        return time.mktime(time.strptime(text, "%a %b %d %H:%M:%S %Y"))
    except ValueError:
        return None


def dienst_frische() -> tuple[list, list]:
    """(probleme, zeilen) - laeuft jeder Dienst mit der aktuellen Datei?

    Genau genommen: Neustart noetig macht eine INHALTSAENDERUNG, nicht
    jedes frische mtime. Der Vergleich hier sieht nur den Zeitstempel -
    schreibt etwas die Datei inhaltsgleich neu (z.B. 'git checkout --'
    in einer Gegenprobe, so geschehen am 24.08.2026), meldet er
    VERALTET, obwohl der Prozess denselben Stand faehrt. Das ist
    hingenommen: Der empfohlene Neustart schadet dann nicht, waehrend
    die Gegenrichtung (echte Aenderung nicht gemeldet) teuer waere -
    und was der laufende Prozess WIRKLICH geladen hat, laesst sich von
    aussen nicht nachlesen.
    """
    probleme, zeilen = [], []
    uid = os.getuid()
    for label, datei in DIENSTE.items():
        lauf = subprocess.run(["launchctl", "print", "gui/%d/%s" % (uid, label)],
                              capture_output=True, text=True, timeout=15)
        pid = _pid_aus_print(lauf.stdout) if lauf.returncode == 0 else None
        if pid is None:
            zeilen.append("  HINWEIS Dienst %s laeuft nicht." % label)
            continue
        ps = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                            capture_output=True, text=True, timeout=15)
        start = _start_aus_lstart(ps.stdout)
        if start is None:
            zeilen.append("  HINWEIS Startzeit von %s nicht lesbar." % label)
            continue
        if datei.is_file() and datei.stat().st_mtime > start:
            probleme.append(
                "%s laeuft seit %s, aber %s wurde danach geaendert - der "
                "Dienst faehrt eine VERALTETE Fassung. Neustart: "
                "launchctl kickstart -k gui/%d/%s"
                % (label, time.strftime("%H:%M", time.localtime(start)),
                   datei.name, uid, label))
        else:
            zeilen.append("  ok      %s laeuft mit aktueller Fassung "
                          "(seit %s)" % (label, time.strftime(
                              "%d.%m. %H:%M", time.localtime(start))))
    return probleme, zeilen


def launchagents_vergleich() -> list:
    """Hinweise, wenn installierte plists vom Repo-Stand abweichen."""
    hinweise = []
    if not LAUNCHAGENTS_REPO.is_dir():
        return hinweise
    for repo_plist in sorted(LAUNCHAGENTS_REPO.glob("*.plist")):
        installiert = LAUNCHAGENTS_INSTALLIERT / repo_plist.name
        if not installiert.is_file():
            hinweise.append("plist %s ist nicht installiert." % repo_plist.name)
        elif _pruefsumme(repo_plist) != _pruefsumme(installiert):
            hinweise.append("plist %s weicht vom Repo-Stand ab (bekannte "
                            "Falle: brew regeneriert plists und loescht "
                            "Env-Eintraege)." % repo_plist.name)
    return hinweise


def bericht() -> int:
    print("Doppelablage-Waechter: %s (Quelle) gegen %s (Betrieb)"
          % (QUELLE, BETRIEB))
    print("Nur lesend - kopiert nichts, repariert nichts.\n")

    if not QUELLE.is_dir():
        print("HINWEIS Keine Quell-Ablage unter %s - der Baumvergleich "
              "entfaellt. (Wer nur das Betriebs-Repo nutzt, braucht ihn "
              "nicht.)\n" % QUELLE)
        lage = {"abweichend": [], "nur_betrieb": [], "nur_quelle": [],
                "gleich": 0}
    else:
        lage = vergleiche_baeume(QUELLE, BETRIEB)
    print("Baumvergleich (%s):"
          % ", ".join(b for _q, b in SYNCHRONE_PAARE))
    print("  ok      %d Dateien byte-gleich" % lage["gleich"])
    for rel, neuer in lage["abweichend"]:
        print("  ABWEICHUNG %s - die Fassung in der %s ist neuer."
              % (rel, neuer))
    for rel in lage["nur_betrieb"]:
        print("  DOPPELABLAGE %s liegt NUR im Betrieb - genau die Falle "
              "vom 23.08.2026: Mutationstest und Quelle sehen die Datei "
              "nicht." % rel)
    if lage["nur_quelle"]:
        print("  (Nur in der Quelle, meist Installations-Skripte: %d "
              "Dateien - kein Fehler.)" % len(lage["nur_quelle"]))

    print("\nDienst-Frische:")
    dienst_probleme, zeilen = dienst_frische()
    for z in zeilen:
        print(z)
    for p in dienst_probleme:
        print("  VERALTET %s" % p)

    la = launchagents_vergleich()
    if la:
        print("\nLaunchAgents:")
        for h in la:
            print("  HINWEIS %s" % h)

    schlecht = (len(lage["abweichend"]) + len(lage["nur_betrieb"])
                + len(dienst_probleme))
    if schlecht:
        print("\nErgebnis: %d Befund(e). Beheben heisst: Ablagen BEWUSST "
              "angleichen - seit dem 24.08.2026 ist /opt/ki-server das "
              "Git-Repo mit Datenschutz-Bereinigung, gespiegelt wird von "
              "dort in die Quelle, nie umgekehrt ohne Datenschutz-"
              "Pruefung. Danach Dienste neu starten und HIER gegenpruefen."
              % schlecht)
        return 1
    print("\nErgebnis: Quelle und Betrieb sind deckungsgleich.")
    return 0


# ----------------------------------------------------------------------
# Selbsttest
# ----------------------------------------------------------------------
def _selbsttest() -> int:
    import shutil
    import tempfile

    fehler = 0

    def pruefe(bedingung, text, zusatz=""):
        nonlocal fehler
        if bedingung:
            print("  ok      %s" % text)
        else:
            print("  FEHLER  %s%s" % (text, ("  [%s]" % zusatz) if zusatz else ""))
            fehler += 1

    print("doppelablage_pruefen Selbsttest:")

    with tempfile.TemporaryDirectory() as wurzel:
        q = Path(wurzel) / "quelle"
        b = Path(wurzel) / "betrieb"
        for basis in (q, b):
            (basis / "harness").mkdir(parents=True)
            (basis / "harness" / "gleich.py").write_text("print(1)\n")

        nur_harness = (("harness", "harness"),)
        lage = vergleiche_baeume(q, b, nur_harness)
        pruefe(lage["gleich"] == 1 and not lage["abweichend"]
               and not lage["nur_betrieb"],
               "identische Baeume: keine Befunde", str(lage))

        # Abweichung anlegen - die Betriebsseite ist die neuere.
        time.sleep(0.05)
        (b / "harness" / "gleich.py").write_text("print(2)\n")
        lage = vergleiche_baeume(q, b, nur_harness)
        pruefe(lage["abweichend"] == [("harness/gleich.py", "Betrieb")],
               "Abweichung wird erkannt und die neuere Seite benannt",
               str(lage["abweichend"]))

        # Die Doppelablage-Falle: Datei nur im Betrieb.
        (b / "harness" / "nur_hier.py").write_text("x = 1\n")
        lage = vergleiche_baeume(q, b, nur_harness)
        pruefe(lage["nur_betrieb"] == ["harness/nur_hier.py"],
               "Datei nur im Betrieb wird als Doppelablage gemeldet")

        # Nicht-Code-Dateien, __pycache__ und Laufzeitdateien bleiben
        # aussen vor.
        (b / "harness" / "__pycache__").mkdir()
        (b / "harness" / "__pycache__" / "muell.py").write_text("x")
        (b / "harness" / "notiz.txt").write_text("x")
        (b / "harness" / "auge.json").write_text("{}")
        lage = vergleiche_baeume(q, b, nur_harness)
        pruefe(lage["nur_betrieb"] == ["harness/nur_hier.py"],
               "__pycache__, Nicht-Code und Laufzeitdateien werden "
               "uebergangen", str(lage["nur_betrieb"]))

        # Verschieden benannte Paar-Ordner (hardware/kamera <-> kamera).
        (q / "hardware" / "kamera").mkdir(parents=True)
        (b / "kamera").mkdir()
        (q / "hardware" / "kamera" / "dienst.py").write_text("k = 1\n")
        (b / "kamera" / "dienst.py").write_text("k = 1\n")
        lage = vergleiche_baeume(q, b, (("hardware/kamera", "kamera"),))
        pruefe(lage["gleich"] == 1 and not lage["nur_betrieb"],
               "Paar-Ordner mit verschiedenen Namen werden gepaart",
               str(lage))

        # Nur-lesend-Beweis: Der Vergleich laesst beide Baeume unberuehrt.
        def baum_stand(basis):
            return sorted((str(p.relative_to(basis)), p.read_bytes())
                          for p in basis.rglob("*") if p.is_file())
        vorher = (baum_stand(q), baum_stand(b))
        vergleiche_baeume(q, b, nur_harness)
        pruefe((baum_stand(q), baum_stand(b)) == vorher,
               "der Vergleich veraendert keine einzige Datei")

    # --- Die Zerleger fuer die Dienst-Frische ---
    probe = "system info\n\tpid = 4711\n\tstate = running\n"
    pruefe(_pid_aus_print(probe) == 4711, "pid wird aus launchctl print gelesen")
    pruefe(_pid_aus_print("state = not running\n") is None,
           "ohne pid-Zeile kommt None")
    stempel = _start_aus_lstart("Sun Aug 24 03:10:00 2026\n")
    pruefe(stempel is not None
           and time.localtime(stempel)[:5] == (2026, 8, 24, 3, 10),
           "lstart wird korrekt in Unix-Zeit uebersetzt")
    pruefe(_start_aus_lstart("") is None, "leere lstart-Antwort ergibt None")
    pruefe(_start_aus_lstart("kaputt") is None, "kaputte lstart ergibt None")

    # Die Dienstliste muss auf die echten Betriebspfade zeigen - ein
    # Tippfehler hier hiesse: der Waechter prueft eine Datei, die es
    # nicht gibt, und meldet nie VERALTET.
    for label, datei in DIENSTE.items():
        pruefe(str(datei).startswith(str(BETRIEB)),
               "Dienst %s zeigt in den Betrieb" % label, str(datei))

    if fehler:
        print("\n%d Fehler." % fehler)
    else:
        print("\nAlle Pruefungen bestanden.")
    return fehler


def main(argumente: list[str]) -> int:
    if "--selbsttest" in argumente:
        return _selbsttest()
    return bericht()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
