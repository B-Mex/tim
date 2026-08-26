#!/usr/bin/env python3
"""Tims Livewerkstatt: eigener Code gegen echte Hardware.

Die Werkstatt (werkstatt.py) laesst Tim Code schreiben und testen - aber
eingesperrt ohne Netz und ohne Geraete. Fuer alles, was man nur durch
AUSPROBIEREN lernt, reicht das nicht: Ein Funkprotokoll erarbeitet man,
indem man ein Byte aendert, sendet und schaut, was die Lampe tut. Genau
dieser Kreislauf fehlte.

Hier bekommt er ihn - mit zwei Riegeln statt eines Versprechens:

**Riegel 1: die Sandbox.** macOS kann Netzwerk nicht auf einzelne
Adressen einschraenken (`host must be * or localhost`), deshalb ist das
Netz KOMPLETT zu. Erlaubt ist allein der serielle Draht zum Pico am USB.
Die echte Funkbruecke haengt am Netzteil, nicht am Mac - sie ist damit
ausser Reichweite, gemessen: PermissionError.

**Riegel 2: die Chip-ID.** Vor jedem Lauf wird gefragt, WER am USB
haengt. Ist es nicht der Dummy, laeuft nichts. Das faengt den Fall ab,
dass jemand die echte Bruecke ansteckt.

Was hier BEWUSST fehlt: fertige Bausteine. Kein fastcon.py, keine
Paketlogik, keine Brueckensoftware. Der Sinn der Uebung ist, dass Tim
sich das selbst erarbeitet - so wie es beim ersten Mal auch niemand
vorgekaut bekommen hat.

Aufrufe:

    livewerkstatt.py schreiben <datei>      (Inhalt ueber stdin)
    livewerkstatt.py fahren <datei> [arg]
    livewerkstatt.py liste
    livewerkstatt.py lesen <datei>
    livewerkstatt.py --selbsttest
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

LIVEWERKSTATT = Path.home() / "Desktop" / "Tim-Livewerkstatt"
SANDKASTEN = LIVEWERKSTATT / "sandkasten"
PROTOKOLL = Path("/opt/ki-server/memory/livewerkstatt_log.jsonl")

DUMMY_ID = "28cdc106c5be"
ERLAUBTE_ENDUNGEN = (".py", ".json", ".txt", ".md")
ZEITGRENZE_S = 120

# Netz komplett zu, serieller Draht offen. Die Regex deckt beide
# Schreibweisen ab, die macOS fuer denselben Anschluss anlegt.
SANDBOX_PROFIL = """(version 1)
(deny default)
(allow process-exec)
(allow process-fork)
(allow signal (target self))
(allow sysctl-read)
(allow mach-lookup)
(allow file-read*)
(allow file-write* (subpath "%s"))
(allow file-write* (subpath "/private/var/folders"))
(allow file-write-data (literal "/dev/null") (literal "/dev/stdout")
                       (literal "/dev/stderr"))
(allow file-write* (regex #"^/dev/(cu|tty)\\.usbmodem.*"))
(allow file-ioctl (regex #"^/dev/(cu|tty)\\.usbmodem.*"))
"""

# Im Pruefungsmodus kommt das hier dazu. Grund: Ohne diese Zeilen
# koennte Tims Code die fertige Bruecke einfach LESEN und abschreiben -
# der Lesezugriff des Chats ist dann zwar umgebogen, der seines Codes
# aber nicht. Am 25.08.2026 genau so aufgefallen.
PRUEFUNGSSCHALTER = Path("/opt/ki-server/config/PRUEFUNGSMODUS")
PRUEFUNGS_SPERREN = """
(deny file-read* (subpath "%s"))
(deny file-read* (subpath "%s"))
""" % (Path.home() / "Desktop" / "M1_DEPLOYMENT" / "hardware",
       Path.home() / "Desktop" / "brmesh-bridge")


class KeinDummy(Exception):
    """Am USB haengt nicht der Dummy - es wird nichts gefahren."""


def pfad_erlaubt(relpfad: str):
    """Nur innerhalb des Sandkastens, nur erlaubte Endungen."""
    if not relpfad or relpfad.startswith("/") or ".." in relpfad:
        return None, "Pfad muss innerhalb des Sandkastens liegen."
    ziel = (SANDKASTEN / relpfad).resolve()
    if SANDKASTEN.resolve() not in ziel.parents and ziel != SANDKASTEN.resolve():
        return None, "Der Pfad zeigt aus dem Sandkasten heraus."
    if ziel.suffix not in ERLAUBTE_ENDUNGEN:
        return None, "Endung nicht erlaubt (%s)" % ", ".join(ERLAUBTE_ENDUNGEN)
    return ziel, ""


def usb_id_lesen() -> str | None:
    """Wer haengt am USB? Gibt die WLAN-MAC zurueck (wie in /status)."""
    sys.path.insert(0, str(Path.home() / "Desktop" / "brmesh-bridge" / "tools"))
    try:
        import pico_draht
    except ImportError:
        return None
    if not pico_draht.anschluss_finden():
        return None
    try:
        with pico_draht.Draht() as draht:
            draht.abbrechen()
            draht.schreiben(
                "import network; print('MAC', "
                "network.WLAN(network.STA_IF).config('mac').hex())")
            roh = draht.lesen_bis("MAC ", 8.0)
    except Exception:
        return None
    for zeile in roh.splitlines():
        zeile = zeile.strip()
        if zeile.startswith("MAC ") and len(zeile) > 4:
            return zeile[4:].strip().lower()
    return None


def dummy_bestaetigen() -> str:
    am_usb = usb_id_lesen()
    if am_usb is None:
        raise KeinDummy(
            "Am USB haengt kein ansprechbarer Pico. Ohne Draht kein Lauf.")
    if am_usb != DUMMY_ID:
        raise KeinDummy(
            "Am USB haengt NICHT der Dummy: gemeldete ID %r, erwartet %r. "
            "Abgebrochen - es wird nichts gefahren." % (am_usb, DUMMY_ID))
    return am_usb


def _sandbox_befehl(befehl: list) -> list:
    if not Path("/usr/bin/sandbox-exec").exists():
        print("WARNUNG sandbox-exec fehlt - der Lauf ist UNGESCHUETZT.")
        return befehl
    SANDKASTEN.mkdir(parents=True, exist_ok=True)
    profil = SANDBOX_PROFIL % SANDKASTEN.resolve()
    if PRUEFUNGSSCHALTER.exists():
        profil += PRUEFUNGS_SPERREN
    fd, datei = tempfile.mkstemp(suffix=".sb")
    os.write(fd, profil.encode("utf-8"))
    os.close(fd)
    return ["/usr/bin/sandbox-exec", "-f", datei] + befehl


def schreiben(relpfad: str, inhalt: str) -> dict:
    """Eine Datei im Sandkasten anlegen - der Weg fuer den Chat.

    Wie bei der Werkstatt bewusst nicht ueber den Job-Server: Ein
    Dateiinhalt passt weder durch dessen Argument-Riegel noch in eine
    Kommandozeile. Die Grenze bleibt dieselbe - sie steckt in
    pfad_erlaubt(), nicht im Aufrufweg.
    """
    ziel, grund = pfad_erlaubt(relpfad)
    if ziel is None:
        return {"ok": False, "fehler": grund}
    if not inhalt.strip():
        return {"ok": False, "fehler": "kein Inhalt"}
    ziel.parent.mkdir(parents=True, exist_ok=True)
    ziel.write_text(inhalt, encoding="utf-8")
    return {"ok": True, "pfad": str(ziel), "bytes": len(inhalt.encode())}


def befehl_schreiben(relpfad: str) -> int:
    ziel, grund = pfad_erlaubt(relpfad)
    if ziel is None:
        print("FEHLER %s" % grund)
        return 1
    inhalt = sys.stdin.read()
    ziel.parent.mkdir(parents=True, exist_ok=True)
    ziel.write_text(inhalt, encoding="utf-8")
    print("Geschrieben: %s (%d Zeichen)" % (relpfad, len(inhalt)))
    return 0


def befehl_fahren(relpfad: str, arg: str | None = None) -> int:
    """Tims Code laufen lassen - eingesperrt, aber am echten Draht."""
    ziel, grund = pfad_erlaubt(relpfad)
    if ziel is None:
        print("FEHLER %s" % grund)
        return 1
    if not ziel.is_file():
        print("FEHLER Datei gibt es nicht: %s" % relpfad)
        return 1
    kennung = dummy_bestaetigen()
    print("Am USB bestaetigt: %s (der Dummy). Netz ist zu." % kennung)

    py = "/opt/ki-server/venv/bin/python"
    befehl = _sandbox_befehl([py, str(ziel)] + ([arg] if arg else []))
    start = time.time()
    try:
        lauf = subprocess.run(befehl, capture_output=True, text=True,
                              timeout=ZEITGRENZE_S)
    except subprocess.TimeoutExpired:
        print("ABGEBROCHEN nach %d s Zeitgrenze." % ZEITGRENZE_S)
        return 1
    dauer = time.time() - start
    if lauf.stdout:
        print(lauf.stdout[-6000:])
    if lauf.stderr.strip():
        print("--- Fehlerausgabe ---")
        print(lauf.stderr[-3000:])
    print("(Ende nach %.1f s, Rueckgabewert %d)" % (dauer, lauf.returncode))
    return lauf.returncode


def befehl_liste() -> int:
    SANDKASTEN.mkdir(parents=True, exist_ok=True)
    dateien = sorted(p for p in SANDKASTEN.rglob("*") if p.is_file())
    if not dateien:
        print("Der Sandkasten ist leer. Hier liegt nichts Vorgebautes - "
              "das ist Absicht.")
        return 0
    for p in dateien:
        print("  %-40s %6d Bytes" % (p.relative_to(SANDKASTEN), p.stat().st_size))
    return 0


def befehl_lesen(relpfad: str) -> int:
    ziel, grund = pfad_erlaubt(relpfad)
    if ziel is None:
        print("FEHLER %s" % grund)
        return 1
    if not ziel.is_file():
        print("FEHLER Datei gibt es nicht: %s" % relpfad)
        return 1
    print(ziel.read_text(encoding="utf-8")[:12000])
    return 0


def selbsttest() -> int:
    fehler = []

    def pruefe(bedingung, text, zusatz=""):
        print("  %-7s %s%s" % ("ok" if bedingung else "FEHLER", text,
                               "" if bedingung else "   <- " + str(zusatz)))
        if not bedingung:
            fehler.append(text)

    print("livewerkstatt Selbsttest:")

    # Die Pfadsperre
    for boese in ("../raus.py", "/etc/passwd", "~/.ssh/id_rsa",
                  "unter/../../weg.py", ""):
        ziel, _ = pfad_erlaubt(boese)
        pruefe(ziel is None, "Pfad abgewiesen: %r" % boese)
    ziel, _ = pfad_erlaubt("uebung/versuch.py")
    pruefe(ziel is not None, "Sandkasten-Pfad zugelassen (Gegenprobe)")
    ziel, _ = pfad_erlaubt("uebung/schadhaft.sh")
    pruefe(ziel is None, "fremde Endung abgewiesen")

    # Der ID-Riegel
    echt = globals()["usb_id_lesen"]
    try:
        globals()["usb_id_lesen"] = lambda: "28cdc106c5c0"   # echte Bruecke
        try:
            dummy_bestaetigen()
            pruefe(False, "ECHTE Bruecke am USB wird abgewiesen",
                   "durchgelassen!")
        except KeinDummy as k:
            pruefe("NICHT der Dummy" in str(k),
                   "ECHTE Bruecke am USB wird abgewiesen")
        globals()["usb_id_lesen"] = lambda: None
        try:
            dummy_bestaetigen()
            pruefe(False, "kein Pico am USB wird abgewiesen", "durchgelassen!")
        except KeinDummy:
            pruefe(True, "kein Pico am USB wird abgewiesen")
        globals()["usb_id_lesen"] = lambda: DUMMY_ID
        try:
            dummy_bestaetigen()
            pruefe(True, "der Dummy wird durchgelassen (Gegenprobe)")
        except KeinDummy as k:
            pruefe(False, "der Dummy wird durchgelassen (Gegenprobe)", k)
    finally:
        globals()["usb_id_lesen"] = echt

    # schreiben() muss dieselbe Sperre tragen wie der Kommandozeilenweg
    pruefe(schreiben("../raus.py", "x")["ok"] is False,
           "schreiben() weist Pfad ausserhalb ab")
    pruefe(schreiben("probe.py", "")["ok"] is False,
           "schreiben() weist leeren Inhalt ab")
    pruefe(schreiben("selbsttest_probe.py", "# ok\n")["ok"] is True,
           "schreiben() legt im Sandkasten an (Gegenprobe)")

    # Das Sandbox-Profil muss das Netz wirklich zumachen
    profil = SANDBOX_PROFIL % SANDKASTEN.resolve()
    pruefe("(deny default)" in profil, "Profil verbietet standardmaessig alles")
    pruefe("network" not in profil.replace("network.WLAN", ""),
           "Profil erlaubt KEIN Netzwerk")
    pruefe("usbmodem" in profil, "Profil erlaubt den seriellen Draht")

    pruefe("deny file-read*" in PRUEFUNGS_SPERREN,
           "Pruefungsmodus sperrt das Lesen der Loesung")
    pruefe("pico_bruecke" not in SANDBOX_PROFIL,
           "das Grundprofil nennt die Loesung nicht")

    print("\n%s" % ("Alle Pruefungen bestanden." if not fehler
                    else "%d FEHLER." % len(fehler)))
    return 1 if fehler else 0


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("--hilfe", "-h"):
        print(__doc__)
        return 0
    if args[0] == "--selbsttest":
        return selbsttest()
    try:
        if args[0] == "schreiben" and len(args) > 1:
            return befehl_schreiben(args[1])
        if args[0] == "fahren" and len(args) > 1:
            return befehl_fahren(args[1], args[2] if len(args) > 2 else None)
        if args[0] == "liste":
            return befehl_liste()
        if args[0] == "lesen" and len(args) > 1:
            return befehl_lesen(args[1])
    except KeinDummy as f:
        print("ABGEBROCHEN %s" % f)
        return 1
    print("FEHLER Unbekannter Befehl: %s" % args[0])
    return 1


if __name__ == "__main__":
    sys.exit(main())
