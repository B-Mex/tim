#!/usr/bin/env python3
"""M1 Job-Server - die Bruecke zwischen Bedienoberflaeche und Ablaeufen.

Warum es diesen Dienst gibt: Open WebUI laeuft in einem Container und kann
auf dem Mac nichts ausfuehren. Der Sprachassistent kann es, aber nur von
seinem eigenen Prozess aus. Damit beide - Tippen im Browser und Zuruf per
Sprache - dieselben Ablaeufe ausloesen, gibt es genau eine Stelle, die sie
kennt: diesen Dienst.

Die Sicherheitslinie des Projekts bleibt dabei erhalten:

  * Es gibt eine **feste Liste** erlaubter Aktionen (AKTIONEN). Was nicht
    darin steht, laesst sich nicht ausloesen. Kein freier Shell-Aufruf,
    keine vom Modell zusammengesetzten Kommandos.
  * Argumente werden gegen eine Positivliste geprueft, nicht gefiltert.
    Ein Ablaufname muss in harness/jobs/ existieren, sonst wird abgelehnt.
  * Der Kill-Switch wird VOR jeder Ausfuehrung geprueft - derselbe
    Mechanismus und dieselben Orte wie in autonomie.py.
  * Der Not-Aus ist ausloesbar, das Wiedereinschalten NICHT. Etwas
    abschalten darf eine Fehlbedienung, wieder scharf schalten nicht.
  * Jeder Aufruf braucht ein Token (Datei ~/.m1_job_token, 0600).

Starten:
    python3 m1_job_server.py                # Vordergrund, zum Ausprobieren
    m1-server-start                         # als Hintergrunddienst

Pruefen:
    curl -H "X-M1-Token: $(cat ~/.m1_job_token)" http://127.0.0.1:8765/aktionen
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import stat
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# launchd startet Dienste mit minimalem PATH (/usr/bin:/bin:...). ollama
# liegt unter /opt/homebrew/bin - ohne diese Zeile scheitert "modelle"
# genau dann, wenn der Server als Dienst laeuft, waehrend es im Terminal
# funktioniert. (/usr/local/bin bleibt drin: dort landen Homebrew-Formeln
# auf Intel-Macs und spaeter nachinstallierte Werkzeuge.)
os.environ["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + os.environ.get("PATH", "")

# ----------------------------------------------------------------------
# Orte
# ----------------------------------------------------------------------
HOME = Path.home()
DEPLOY_DIR = HOME / "Desktop" / "M1_DEPLOYMENT"
HARNESS_DIR = Path("/opt/ki-server/harness")
VENV_PY = Path("/opt/ki-server/venv/bin/python")
JOBS_DIR = HARNESS_DIR / "jobs"
TOKEN_DATEI = HOME / ".m1_job_token"
# Hardware-Anbindung: die Funkbruecke auf dem Pico W und der Kameradienst.
# In der Positivliste stehen bewusst nur die LESENDEN Aufrufe - Status,
# Scannen, Schauen. Rundrufe senden gehoert zur Lampensteuerung und kommt
# erst, wenn die belegt funktioniert; siehe die Notiz bei den Aktionen.
HARDWARE_DIR = DEPLOY_DIR / "hardware"

# Dieselben Orte, die autonomie.py prueft. Weicht diese Liste ab, meldet
# der Dienst "kein Kill-Switch", waehrend der Harness laengst gestoppt ist.
STOP_ORTE = [
    Path("/opt/ki-server"),
    HOME / "Desktop" / "M1_DEPLOYMENT",
    Path("/Volumes/M1_DEPLOYMENT"),
    Path("/Volumes/Extreme SSD/M1_DEPLOYMENT"),
    Path("/Volumes/SanDisk/M1_DEPLOYMENT"),
    Path("/Volumes/SANDISK/M1_DEPLOYMENT"),
]

# Wie lange eine Aktion laufen darf, bevor sie abgebrochen wird (Sekunden).
# Ein Ablauf mit dem 30B-Modell braucht Minuten - deshalb grosszuegig.
ZEITGRENZE = 1800


def _py() -> str:
    """Der Interpreter, in dem crewai installiert ist."""
    return str(VENV_PY) if VENV_PY.exists() else "python3"


# ----------------------------------------------------------------------
# Die Positivliste. Alles, was die Oberflaeche ausloesen darf.
# ----------------------------------------------------------------------
#   schluessel -> (beschreibung, befehl_bauen, braucht_argument)
# befehl_bauen bekommt das gepruefte Argument und gibt eine Argumentliste
# zurueck - nie einen String, damit nichts von einer Shell interpretiert wird.
AKTIONEN = {
    "status": (
        "Systemzustand pruefen (Healthcheck)",
        lambda arg: ["bash", str(DEPLOY_DIR / "scripts" / "10_MAC_healthcheck.sh")],
        False,
    ),
    "ablaeufe": (
        "Verfuegbare Ablaeufe auflisten",
        lambda arg: [_py(), str(HARNESS_DIR / "crew_generic.py")],
        False,
    ),
    "ablauf_starten": (
        "Einen Ablauf aus harness/jobs starten (Argument: Name)",
        lambda arg: [_py(), str(HARNESS_DIR / "crew_generic.py"), arg],
        True,
    ),
    "autonomie": (
        "Zeigen, welche autonomen Aktionen erlaubt sind",
        lambda arg: [_py(), str(HARNESS_DIR / "autonomie.py")],
        False,
    ),
    "selbsttests": (
        "Alle Selbsttests fahren (dauert einige Minuten)",
        lambda arg: ["bash", str(DEPLOY_DIR / "scripts" / "14_MAC_selbsttests.sh")],
        False,
    ),
    "berichte": (
        "Vorhandene Berichte auflisten",
        lambda arg: ["bash", "-c",
                     "ls -t ~/Desktop/M1_DEPLOYMENT/berichte/*.md 2>/dev/null "
                     "| head -20 || echo 'Noch keine Berichte.'"],
        False,
    ),
    "bericht_lesen": (
        "Einen Bericht im Wortlaut zurueckgeben (Argument: Dateiname)",
        None,  # Sonderfall, siehe _bericht_lesen()
        True,
    ),
    "modelle": (
        "Installierte Modelle auflisten",
        lambda arg: ["ollama", "list"],
        False,
    ),
    "notaus": (
        "Kill-Switch setzen - stoppt alle autonomen Ablaeufe",
        None,  # Sonderfall, siehe _notaus()
        False,
    ),
    # Der Sprachassistent laeuft als launchd-Dienst. Die Befehle sind
    # feste Zeichenketten ohne Argumente - bash dient hier nur dazu,
    # "war schon aus" von echten Fehlern zu unterscheiden.
    "sprachassistent_stoppen": (
        "Sprachassistent anhalten - Mikrofon aus bis zum naechsten Start",
        lambda arg: ["/bin/bash", "-c",
                     "/bin/launchctl bootout gui/$(id -u)/com.ki-server.sprachassistent"
                     " 2>/dev/null && echo 'Sprachassistent angehalten.'"
                     " || echo 'Sprachassistent war schon aus.'"],
        False,
    ),
    "sprachassistent_starten": (
        "Sprachassistent starten - hoert danach auf 'Hey Tim'",
        lambda arg: ["/bin/bash", "-c",
                     "/bin/launchctl bootstrap gui/$(id -u)"
                     " \"$HOME/Library/LaunchAgents/com.ki-server.sprachassistent.plist\""
                     " 2>/dev/null;"
                     " /bin/launchctl kickstart gui/$(id -u)/com.ki-server.sprachassistent"
                     " && echo 'Sprachassistent laeuft.'"],
        False,
    ),
    # Der Kameradienst laeuft wie der Sprachassistent als launchd-Dienst
    # (com.ki-server.kamera). So kann Tim ihn auch von unterwegs starten
    # und stoppen - ohne Terminal und ohne die Sicherheitslinie zu
    # verschieben: Es sind zwei feste Befehle in der Positivliste, kein
    # freier Terminalzugriff. Die macOS-Kameraerlaubnis muss einmalig am
    # Mac selbst bestaetigt werden, wenn macOS beim ersten Start fragt.
    "kamera_starten": (
        "Kameradienst (Tims Auge) starten oder neu starten",
        lambda arg: ["/bin/bash", "-c",
                     "/bin/launchctl bootstrap gui/$(id -u)"
                     " \"$HOME/Library/LaunchAgents/com.ki-server.kamera.plist\""
                     " 2>/dev/null;"
                     " /bin/launchctl kickstart -k gui/$(id -u)/com.ki-server.kamera"
                     " && echo 'Kameradienst laeuft.'"],
        False,
    ),
    "kamera_stoppen": (
        "Kameradienst anhalten - Kamera aus bis zum naechsten Start",
        lambda arg: ["/bin/bash", "-c",
                     "/bin/launchctl bootout gui/$(id -u)/com.ki-server.kamera"
                     " 2>/dev/null && echo 'Kameradienst angehalten.'"
                     " || echo 'Kameradienst war schon aus.'"],
        False,
    ),
    # Seit dem 23.08.2026 haengt der Pico eigenstaendig im WLAN. Der
    # USB-Weg hier ist der WARTUNGSWEG: Er haelt die WLAN-Bruecke fuer
    # ein paar Sekunden an (sie faehrt danach von selbst wieder hoch).
    # Fuer die blosse Frage "laeuft sie?" gibt es darunter
    # "funkbruecke_wlan" - der stoert nichts.
    "funkbruecke": (
        "Wartungsweg: Pico ueber USB ansprechen (unterbricht die WLAN-Bruecke kurz)",
        lambda arg: [_py(), str(HARDWARE_DIR / "pico_bruecke" / "bruecke_cli.py"),
                     "status"],
        False,
    ),
    # Die Dauer steht hier fest und kommt nicht als Argument herein. Zwei
    # Gruende: Die Oberflaeche zeigt nur Aktionen ohne Argument als Knopf,
    # und die Bruecke haelt den Scan ohnehin bei 30 Sekunden an - eine
    # frei waehlbare Zahl brauchte eine zweite Pruefung ohne Nutzen.
    "funk_scannen": (
        "Hoeren, wer in der Naehe funkt - Lampen sind mit LAMPE markiert",
        lambda arg: [_py(), str(HARDWARE_DIR / "pico_bruecke" / "bruecke_cli.py"),
                     "scannen", "4000"],
        False,
    ),
    "kamera_schauen": (
        "Die aktuelle Farbmessung des Kameradienstes holen",
        lambda arg: [_py(), str(HARDWARE_DIR / "kamera" / "kamera_cli.py"),
                     "messung"],
        False,
    ),
    # Die Lampensteuerung - am 22.08.2026 scharf geschaltet.
    #
    # Die Bedingung, die hier frueher als Sperre stand, ist erfuellt: Der
    # Mesh-Schluessel ist mitgeschnitten (aus den Fuellbytes der eigenen
    # Pakete), und der Weg Senden -> Lampe -> Kamera ist nachgemessen -
    # am Flur sprang die Helligkeit von 0.039 auf 0.680, Blau traf auf
    # 239.6 Grad bei Soll 240.
    #
    # Scharf ist bewusst NUR dieser Weg, nicht das rohe Senden: Die
    # Aktion nimmt Raumnamen aus der angelernten Liste entgegen, keine
    # Hex-Pakete. Damit kann ueber Tim nichts anderes gefunkt werden als
    # die Raeume, die Mexla selbst angelernt hat. Der Selbsttest weiter
    # unten haelt genau diese Grenze fest.
    "lampen": (
        "Raum schalten (Argument: '<raum>.<befehl>', z.B. 'wohnzimmer.rot')",
        # Punkt statt Leerzeichen als Trenner: Die Zentrale laesst in
        # Argumenten nur [A-Za-z0-9_.-] durch - ein Riegel gegen
        # eingeschleuste Befehle, den ich nicht aufweichen will. Der Punkt
        # ist dort erlaubt und taugt genauso: "wohnzimmer.rot",
        # "flur.hell.40", "kueche.255.128.0".
        lambda arg: [_py(), str(HARDWARE_DIR / "pico_bruecke" /
                     "lampen_steuern.py")] + str(arg or "").replace(".", " ").split(),
        True,
    ),
    "funkbruecke_wlan": (
        "Stand der WLAN-Funkbruecke abfragen (stoert den Betrieb nicht)",
        lambda arg: [_py(), str(HARDWARE_DIR / "pico_bruecke" /
                     "lampen_steuern.py"), "bruecke"],
        False,
    ),
    "lampen_raeume": (
        "Zeigen, welche Raeume und Farben bekannt sind",
        lambda arg: [_py(), str(HARDWARE_DIR / "pico_bruecke" /
                     "lampen_steuern.py"), "raeume"],
        False,
    ),
    # Der Modell-Benchmark - die 14 Pruefungen vom 23.08.2026 (Tempo,
    # Ehrlichkeit, Mathe, JSON, Werkzeuge, Code, ...), jetzt per Knopf
    # und aus dem Chat. Der Starter kehrt sofort zurueck und laesst den
    # eigentlichen Lauf im Hintergrund weiterarbeiten - ein Benchmark
    # dauert laenger als die ZEITGRENZE dieses Servers erlaubt.
    "modell_benchmark_neue": (
        "Modelltest/Benchmark starten: alle installierten, noch nie "
        "gemessenen Modelle testen (laeuft im Hintergrund)",
        lambda arg: [_py(), str(HARNESS_DIR / "benchmark_starter.py"),
                     "--neue"],
        False,
    ),
    "modell_benchmark_modell": (
        "Ein bestimmtes installiertes Modell testen/messen (Argument: "
        "Name, Punkt statt Doppelpunkt erlaubt, z.B. qwen3.5.9b)",
        lambda arg: [_py(), str(HARNESS_DIR / "benchmark_starter.py"),
                     "--modell", str(arg or "")],
        True,
    ),
    "modell_benchmark_vergleich": (
        "Mehrere Modelle gegeneinander testen (Argument: Namen mit zwei "
        "Unterstrichen getrennt, z.B. qwen3.5.9b__gpt-oss.20b; ein Lauf, "
        "ein Vergleichsbericht)",
        lambda arg: [_py(), str(HARNESS_DIR / "benchmark_starter.py"),
                     "--vergleich", str(arg or "")],
        True,
    ),
    "modell_abitur": (
        "Vollpruefung eines Modells: schriftlicher Teil (Benchmark) UND "
        "praktischer Teil in der Werkstatt - dauert Stunden (Argument: "
        "Modellname, Punkt statt Doppelpunkt)",
        lambda arg: [_py(), str(HARNESS_DIR / "benchmark_starter.py"),
                     "--abitur", str(arg or "")],
        True,
    ),
    # Der Pruefungsausschuss - aber nur die VORSCHLAGENDE Haelfte.
    # Eintragen (--pruefung-uebernehmen) steht bewusst NICHT in dieser
    # Liste: Was Massstab fuer alle kuenftigen Modelle wird, entscheidet
    # Mexla an der Tastatur. Ein Pruefling, der seine eigenen Pruefungen
    # eintraegt, prueft am Ende nur noch das, was er ohnehin kann.
    "pruefung_vorschlagen": (
        "Aus einer BESTANDENEN Werkstattarbeit einen Pruefungs-Entwurf "
        "ableiten (Argument: dateiname.py aus dem Sandkasten) - schlaegt "
        "nur vor, traegt nichts ein",
        lambda arg: [_py(), str(HARNESS_DIR / "abitur.py"),
                     "--pruefung-vorschlagen",
                     str(arg or "").rsplit(".py", 1)[0], str(arg or "")],
        True,
    ),
    "modell_abitur_neue": (
        "Vollpruefung fuer ALLE noch nie gemessenen Modelle - "
        "schriftlich und praktisch, nacheinander (dauert Stunden)",
        lambda arg: [_py(), str(HARNESS_DIR / "benchmark_starter.py"),
                     "--abitur"],
        False,
    ),
    "modell_benchmark_stoppen": (
        "Laufenden Benchmark- oder Abiturlauf abbrechen (gibt die GPU frei)",
        lambda arg: [_py(), str(HARNESS_DIR / "benchmark_starter.py"), "--stoppen"],
        False,
    ),
    "modell_benchmark_status": (
        "Stand des laufenden Modelltests/Benchmarks zeigen "
        "(letzte Logzeilen, juengster Bericht)",
        lambda arg: [_py(), str(HARNESS_DIR / "benchmark_starter.py"),
                     "--status"],
        False,
    ),
    # Tims Weg, den Benchmark selbst zu erweitern: Der Ablauf modell_scan
    # recherchiert Testfall-Vorschlaege, DIESE Aktion prueft und
    # uebernimmt sie als DATEN (nie als Code) - jeder Fall muss seine
    # eigene Gut/Schlecht-Gegenprobe bestehen.
    "benchmark_faelle_uebernehmen": (
        "Recherchierte Benchmark-Testfaelle aus dem juengsten "
        "modell_scan-Bericht pruefen und uebernehmen",
        lambda arg: [_py(), str(HARNESS_DIR / "benchmark_faelle_pflegen.py")],
        False,
    ),
    # Die Diagnose-Trias vom 24.08.2026 - Tim uebernimmt, was in den
    # Nacht-Sitzungen von Hand geprueft wurde. Alle drei sind NUR
    # LESEND: Sie melden Befunde und veraendern nichts. Beheben bleibt
    # Handarbeit an der Tastatur - genau die Grenze, die auch sonst
    # zwischen "nachsehen" und "veraendern" gilt.
    "ha_diagnose": (
        "Home Assistant pruefen (nur lesend): Kacheln, BRMesh-Lampen, "
        "Fehlermeldungen, Geister-Entitaeten",
        lambda arg: [_py(), str(HARNESS_DIR / "ha_diagnose.py")],
        False,
    ),
    "doppelablage_pruefen": (
        "Quelle gegen Betrieb abgleichen: abweichende Dateien und "
        "Dienste mit veralteter Fassung (nur lesend)",
        lambda arg: [_py(), str(HARNESS_DIR / "doppelablage_pruefen.py")],
        False,
    ),
    "datenschutz_pruefen": (
        "Repos vor dem Veroeffentlichen auf private Angaben pruefen: "
        "Arbeitskopie, Historie, Identitaeten (nur lesend)",
        lambda arg: [_py(), str(HARNESS_DIR / "datenschutz_pruefen.py")],
        False,
    ),
    # Tims Werkstatt (24.08.2026) - der einzige Weg, auf dem Tim
    # SCHREIBEN darf. Die Grenze steckt nicht in dieser Liste, sondern
    # in werkstatt.py selbst (pfad_erlaubt): geschrieben wird nur
    # innerhalb von ~/Desktop/Tim-Werkstatt/sandkasten, jeder Ausbruch
    # ueber .., ~, / oder Symlink wird abgewiesen. Ausrollen kann die
    # Werkstatt nicht - kein Kopieren nach /opt, kein SSH, kein
    # Dienst-Neustart. Das bleibt Handarbeit.
    "werkstatt_aufgabe": (
        "Eine Uebungsaufgabe der Werkstatt lesen (Argument: Name, z.B. "
        "lampen_zeitplan)",
        lambda arg: [_py(), str(HARNESS_DIR / "werkstatt.py"), "neu",
                     str(arg or "")],
        True,
    ),
    "werkstatt_liste": (
        "Zeigen, was im Werkstatt-Sandkasten liegt",
        lambda arg: [_py(), str(HARNESS_DIR / "werkstatt.py"), "liste"],
        False,
    ),
    "werkstatt_lesen": (
        "Eine Datei aus dem Werkstatt-Sandkasten lesen (Argument: Pfad)",
        lambda arg: [_py(), str(HARNESS_DIR / "werkstatt.py"), "lesen",
                     str(arg or "")],
        True,
    ),
    "werkstatt_gelernt": (
        "Tims Lernprotokoll aus der Werkstatt lesen - was er bei "
        "frueheren Uebungen gelernt hat",
        lambda arg: [_py(), str(HARNESS_DIR / "werkstatt.py"), "gelernt"],
        False,
    ),
    "werkstatt_testen": (
        "Eine Python-Datei im Sandkasten kompilieren und ihren "
        "--selbsttest fahren (Argument: Pfad)",
        lambda arg: [_py(), str(HARNESS_DIR / "werkstatt.py"), "testen",
                     str(arg or "")],
        True,
    ),
    # Schreiben laeuft NICHT ueber diese Liste: Der Inhalt einer Datei
    # passt weder durch den Argument-Riegel der Zentrale
    # ([A-Za-z0-9_.-]) noch waere er in einer Kommandozeile gut
    # aufgehoben. Der Chat der Zentrale ruft dafuer werkstatt.schreiben
    # direkt auf (Werkzeug "werkstatt_schreiben") - dieselbe Pfadsperre,
    # nur ohne Umweg ueber die Shell.
    "autonomie_modus": (
        "Autonomie-Modus setzen (Argument: safe, assist oder autonom)",
        None,  # Sonderfall, siehe aktion_ausfuehren()
        True,
    ),
    "autonomie_normal": (
        "Alles zurueck auf sicher: Modus safe, alle Schalter nein",
        lambda arg: [_py(), str(HARNESS_DIR / "autonomie.py"), "normal"],
        False,
    ),
    "autonomie_setzen": (
        "Einen erlaube-Schalter umlegen (Argument: SCHALTER.ja / SCHALTER.nein)",
        None,  # Sonderfall, siehe aktion_ausfuehren()
        True,
    ),
}

# Bewusst NICHT in der Liste: das Aufheben des Kill-Switch, das Aendern von
# autonomie.conf, das Installieren von Software und jede Form von freiem
# Befehl. Diese Dinge gehoeren an die Tastatur, nicht in einen Chat.


# ----------------------------------------------------------------------
# Hilfen
# ----------------------------------------------------------------------
def token_holen() -> str:
    """Token lesen, beim ersten Start erzeugen (nur fuer den Besitzer lesbar)."""
    if TOKEN_DATEI.exists():
        wert = TOKEN_DATEI.read_text(encoding="utf-8").strip()
        if wert:
            return wert
    wert = secrets.token_urlsafe(32)
    TOKEN_DATEI.write_text(wert + "\n", encoding="utf-8")
    TOKEN_DATEI.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600
    return wert


def killswitch_aktiv() -> str | None:
    """Gibt den Pfad der STOP-Datei zurueck, wenn eine existiert."""
    for ordner in STOP_ORTE:
        ziel = ordner / "STOP"
        try:
            if ziel.exists() or ziel.is_symlink():
                return str(ziel)
        except OSError:
            continue
    return None


def bekannte_ablaeufe() -> list[str]:
    """Namen aus harness/jobs/*.json - die einzige erlaubte Argumentmenge."""
    if not JOBS_DIR.is_dir():
        return []
    return sorted(p.stem for p in JOBS_DIR.glob("*.json"))


def _notaus() -> tuple[int, str]:
    gesetzt = []
    for ordner in (Path("/opt/ki-server"), DEPLOY_DIR):
        try:
            ordner.mkdir(parents=True, exist_ok=True)
            (ordner / "STOP").touch()
            gesetzt.append(str(ordner / "STOP"))
        except OSError:
            continue
    if gesetzt:
        return 0, "Kill-Switch gesetzt:\n" + "\n".join(gesetzt)
    return 1, "FEHLER: Der Kill-Switch konnte nirgends gesetzt werden."


def _bericht_lesen(name: str) -> tuple[int, str]:
    """Nur Dateien direkt aus berichte/, keine Pfadangaben."""
    if "/" in name or "\\" in name or name.startswith("."):
        return 1, "Ungueltiger Name - nur Dateinamen aus berichte/ sind erlaubt."
    if not name.endswith(".md"):
        name += ".md"
    ziel = (DEPLOY_DIR / "berichte" / name).resolve()
    basis = (DEPLOY_DIR / "berichte").resolve()
    if basis not in ziel.parents:
        return 1, "Der Pfad zeigt aus berichte/ heraus - abgelehnt."
    if not ziel.is_file():
        return 1, f"Kein Bericht namens {name}."
    text = ziel.read_text(encoding="utf-8", errors="replace")
    return 0, text[:20000]


def aktion_ausfuehren(schluessel: str, argument: str | None) -> dict:
    """Fuehrt eine Aktion aus der Positivliste aus."""
    if schluessel not in AKTIONEN:
        return {"ok": False, "fehler": f"Unbekannte Aktion: {schluessel}",
                "erlaubt": sorted(AKTIONEN)}

    beschreibung, bauen, braucht_arg = AKTIONEN[schluessel]

    # Kill-Switch vor allem anderen - ausser beim Not-Aus selbst und beim
    # Anhalten des Sprachassistenten: Abschalten muss auch im Notfall
    # noch gehen, nur das Wieder-Einschalten bleibt gesperrt.
    # "autonomie_normal" gehoert dazu: Zurueck auf sicher ist ein
    # Abschalt-Vorgang. Wer bei gesetztem Kill-Switch aufraeumen will,
    # soll das koennen - gesperrt ist nur das Hochstufen (weiter unten).
    if schluessel not in ("notaus", "sprachassistent_stoppen",
                          "modell_benchmark_stoppen", "autonomie_normal"):
        stop = killswitch_aktiv()
        if stop:
            return {"ok": False, "aktion": schluessel,
                    "fehler": f"Kill-Switch aktiv ({stop}) - es wird nichts ausgefuehrt."}

    if braucht_arg and not argument:
        return {"ok": False, "aktion": schluessel,
                "fehler": f"'{schluessel}' braucht ein Argument."}

    # Sonderfaelle
    if schluessel == "notaus":
        rc, text = _notaus()
        return {"ok": rc == 0, "aktion": schluessel, "ausgabe": text}
    if schluessel == "bericht_lesen":
        rc, text = _bericht_lesen(argument or "")
        return {"ok": rc == 0, "aktion": schluessel, "ausgabe": text}

    # Argument gegen Positivliste pruefen, nicht filtern.
    if schluessel == "ablauf_starten":
        erlaubt = bekannte_ablaeufe()
        if argument not in erlaubt:
            return {"ok": False, "aktion": schluessel,
                    "fehler": f"Unbekannter Ablauf: {argument}",
                    "erlaubt": erlaubt}

    if schluessel == "autonomie_modus":
        gewuenscht = (argument or "").strip().lower()
        if gewuenscht not in ("safe", "assist", "autonom"):
            return {"ok": False, "aktion": schluessel,
                    "fehler": "Modus muss safe, assist oder autonom sein."}
        # Herunterstufen (Richtung safe) ist ein Abschalt-Vorgang und
        # deshalb immer erlaubt - genau wie der Not-Aus. Hochstufen bei
        # gesetztem Kill-Switch waere das Gegenteil und bleibt gesperrt;
        # die allgemeine Pruefung oben hat das bereits abgefangen.
        befehl = [_py(), str(HARNESS_DIR / "autonomie.py"), "modus", gewuenscht]
        try:
            lauf = subprocess.run(befehl, capture_output=True, text=True,
                                  timeout=60)
        except (subprocess.TimeoutExpired, FileNotFoundError) as fehler:
            return {"ok": False, "aktion": schluessel, "fehler": str(fehler)}
        ausgabe = (lauf.stdout or "") + (("\n" + lauf.stderr) if lauf.stderr else "")
        return {"ok": lauf.returncode == 0, "aktion": schluessel,
                "exitcode": lauf.returncode, "ausgabe": ausgabe.strip()[:20000]}

    if schluessel == "autonomie_setzen":
        # Hier nur die FORM. Welche Schalter setzbar sind, entscheidet
        # allein autonomie.py (SETZBARE_SCHALTER) - eine zweite Liste
        # hier wuerde frueher oder spaeter auseinanderlaufen.
        passt = re.fullmatch(r"([A-Z][A-Z_]{2,40})\.(ja|nein)", argument or "")
        if not passt:
            return {"ok": False, "aktion": schluessel,
                    "fehler": "Argument muss SCHALTER.ja oder SCHALTER.nein "
                              "sein, z.B. ERLAUBE_SHELL.nein"}
        befehl = [_py(), str(HARNESS_DIR / "autonomie.py"), "setzen",
                  passt.group(1), passt.group(2)]
    else:
        befehl = bauen(argument)
    try:
        lauf = subprocess.run(befehl, capture_output=True, text=True,
                              timeout=ZEITGRENZE, cwd=str(HARNESS_DIR)
                              if HARNESS_DIR.is_dir() else None)
    except subprocess.TimeoutExpired:
        return {"ok": False, "aktion": schluessel,
                "fehler": f"Abgebrochen nach {ZEITGRENZE} Sekunden."}
    except FileNotFoundError as fehler:
        return {"ok": False, "aktion": schluessel,
                "fehler": f"Befehl nicht gefunden: {fehler}"}

    ausgabe = (lauf.stdout or "") + (("\n" + lauf.stderr) if lauf.stderr else "")
    return {"ok": lauf.returncode == 0, "aktion": schluessel,
            "exitcode": lauf.returncode, "ausgabe": ausgabe.strip()[:20000]}


# ----------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    server_version = "M1JobServer/1.0"

    def _antwort(self, code: int, nutzlast: dict) -> None:
        roh = json.dumps(nutzlast, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(roh)))
        self.end_headers()
        self.wfile.write(roh)

    def _token_ok(self) -> bool:
        mitgeschickt = self.headers.get("X-M1-Token", "")
        return secrets.compare_digest(mitgeschickt, TOKEN)

    def do_GET(self) -> None:  # noqa: N802
        if self.path.rstrip("/") in ("/aktionen", ""):
            if not self._token_ok():
                self._antwort(401, {"ok": False, "fehler": "Token fehlt oder falsch."})
                return
            self._antwort(200, {
                "ok": True,
                "kill_switch": killswitch_aktiv(),
                "aktionen": {k: {"beschreibung": v[0], "braucht_argument": v[2]}
                             for k, v in AKTIONEN.items()},
                "ablaeufe": bekannte_ablaeufe(),
            })
            return
        self._antwort(404, {"ok": False, "fehler": "Unbekannter Pfad."})

    def do_POST(self) -> None:  # noqa: N802
        if self.path.rstrip("/") != "/start":
            self._antwort(404, {"ok": False, "fehler": "Unbekannter Pfad."})
            return
        if not self._token_ok():
            self._antwort(401, {"ok": False, "fehler": "Token fehlt oder falsch."})
            return
        try:
            laenge = int(self.headers.get("Content-Length", "0"))
            daten = json.loads(self.rfile.read(laenge) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._antwort(400, {"ok": False, "fehler": "Ungueltiges JSON."})
            return

        schluessel = str(daten.get("aktion", "")).strip()
        argument = daten.get("argument")
        argument = str(argument).strip() if argument else None

        ergebnis = aktion_ausfuehren(schluessel, argument)
        self._antwort(200 if ergebnis.get("ok") else 400, ergebnis)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        # Eine Zeile pro Aufruf, damit im Dienstprotokoll nachvollziehbar
        # bleibt, was ausgeloest wurde.
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))


TOKEN = ""


def _selbsttest() -> int:
    """Prueft die Sicherheitsgrenzen des Job-Servers gegen einen echten Server.

    Wie beim Selbsttest der Zentrale bewusst ueber HTTP statt gegen die
    Funktionen: geprueft wird die Verdrahtung, nicht die Absicht. Es wird
    dabei KEINE echte Aktion ausgefuehrt - gerade die Stopp-Befehle
    duerfen im Test keinen laufenden Dienst anfassen.
    """
    fehler = 0

    def pruefe(bedingung, text, zusatz=""):
        nonlocal fehler
        if bedingung:
            print(f"  ok      {text}")
        else:
            print(f"  FEHLER  {text}" + (f"  [{zusatz}]" if zusatz else ""))
            fehler += 1

    print("m1_job_server Selbsttest:")

    # Port 0: das Betriebssystem sucht einen freien - so kollidiert der
    # Test nie mit einem laufenden Job-Server.
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    basis = f"http://127.0.0.1:{port}"

    def anfrage(pfad, token=TOKEN, methode="GET", koerper=None):
        daten = json.dumps(koerper).encode("utf-8") if koerper is not None else None
        a = urllib.request.Request(basis + pfad, data=daten, method=methode)
        if token is not None:
            a.add_header("X-M1-Token", token)
        a.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(a, timeout=10) as antwort:
                return antwort.status, antwort.read().decode("utf-8")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8")
        except (urllib.error.URLError, OSError) as e:
            return -1, str(e)

    try:
        # --- Token ---
        code, _ = anfrage("/aktionen", token=None)
        pruefe(code == 401, "ohne Token abgewiesen", f"HTTP {code}")
        code, _ = anfrage("/aktionen", token="falsch")
        pruefe(code == 401, "mit falschem Token abgewiesen", f"HTTP {code}")
        code, text = anfrage("/aktionen")
        pruefe(code == 200, "mit richtigem Token durchgelassen", f"HTTP {code}")

        # --- Positivliste vollstaendig gemeldet ---
        # Faellt eine Aktion still aus der Liste, verschwindet ihr Knopf in
        # Tim kommentarlos - das soll hier auffliegen, nicht beim Bedienen.
        try:
            liste = json.loads(text).get("aktionen", {})
        except ValueError:
            liste = {}
        for erwartet in ("status", "ablaeufe", "ablauf_starten", "notaus",
                         "modelle",
                         "sprachassistent_stoppen", "sprachassistent_starten",
                         "autonomie_setzen", "funkbruecke", "funk_scannen",
                         "kamera_schauen", "kamera_starten", "kamera_stoppen",
                         "lampen", "lampen_raeume",
                         "autonomie_modus", "autonomie_normal",
                         "modell_benchmark_neue", "modell_benchmark_modell",
                         "modell_benchmark_status", "modell_benchmark_stoppen",
                         "modell_benchmark_vergleich",
                         "benchmark_faelle_uebernehmen", "modell_abitur",
                         "modell_abitur_neue", "pruefung_vorschlagen",
                         "ha_diagnose", "doppelablage_pruefen",
                         "datenschutz_pruefen",
                         "werkstatt_aufgabe", "werkstatt_liste",
                         "werkstatt_lesen", "werkstatt_testen",
                         "werkstatt_gelernt"):
            pruefe(erwartet in liste, f"Aktion gemeldet: {erwartet}")

        # --- Die Werkstatt-Grenze (24.08.2026) ---
        # Tim darf hier schreiben - aber nur im Sandkasten. Zwei Dinge
        # muessen deshalb gelten und werden hier festgehalten:
        # 1. Alle Werkstatt-Aktionen laufen ueber werkstatt.py, das die
        #    Pfadsperre traegt. Eine Aktion, die am Modul vorbei
        #    schreibt (cp, mv, tee), waere ein zweiter Weg ohne Sperre.
        # 2. Kein Ausrollen: nichts in der Positivliste darf aus dem
        #    Sandkasten heraus kopieren oder ihn per SSH verlassen.
        for _name in ("werkstatt_aufgabe", "werkstatt_liste",
                      "werkstatt_lesen", "werkstatt_testen",
                      "werkstatt_gelernt"):
            _befehl = [str(t) for t in AKTIONEN[_name][1]("probe")]
            pruefe(any(t.endswith("werkstatt.py") for t in _befehl),
                   f"'{_name}' laeuft ueber werkstatt.py", str(_befehl))
            pruefe(not any(";" in t or "&&" in t or "|" in t
                           for t in _befehl),
                   f"'{_name}' enthaelt keine Shell-Verkettung")
        # Eintragen von Pruefungen gehoert NICHT in die Positivliste.
        _eintragend = []
        for _name, (_besch, _bauen, _arg) in AKTIONEN.items():
            if _bauen is None:
                continue
            try:
                _befehl = [str(x) for x in _bauen("PRUEFWERT")]
            except Exception:
                continue
            if any("--pruefung-uebernehmen" in x for x in _befehl):
                _eintragend.append(_name)
        pruefe(not _eintragend,
               "keine Aktion traegt Pruefungen selbst ein",
               ", ".join(_eintragend))

        _ausrollend = []
        for _name, (_besch, _bauen, _arg) in AKTIONEN.items():
            if _bauen is None:
                continue
            try:
                _befehl = [str(t) for t in _bauen("PRUEFWERT")]
            except Exception:
                continue
            if any("Tim-Werkstatt" in t for t in _befehl) and \
                    any(t in ("cp", "mv", "rsync", "scp", "ssh")
                        or t.endswith("/cp") or t.endswith("/scp")
                        for t in _befehl):
                _ausrollend.append(_name)
        pruefe(not _ausrollend,
               "keine Aktion rollt aus der Werkstatt aus",
               ", ".join(_ausrollend))
        # Und die Gegenprobe zur Pfadsperre selbst: sie muss im Modul
        # wirklich greifen, nicht nur im Kommentar stehen.
        import importlib.util as _ilu
        _spec = _ilu.spec_from_file_location(
            "_werkstatt_probe", str(HARNESS_DIR / "werkstatt.py"))
        if _spec and _spec.loader:
            _w = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_w)
            for _boese in ("../raus.py", "/etc/passwd", "~/.ssh/id_rsa"):
                _ziel, _grund = _w.pfad_erlaubt(_boese)
                pruefe(_ziel is None,
                       f"werkstatt.py weist '{_boese}' ab", str(_grund))
            _ziel, _ = _w.pfad_erlaubt("uebung/datei.py")
            pruefe(_ziel is not None,
                   "werkstatt.py laesst den Sandkasten-Pfad zu")
        else:
            pruefe(False, "werkstatt.py ladbar")

        # --- Die Diagnose-Trias (24.08.2026): nur lesend, nur sie selbst ---
        # Die drei Aktionen versprechen in ihrer Beschreibung "nur
        # lesend". Das Versprechen wohnt in den Skripten (deren
        # Selbsttests belegen es: Baum-Stand vorher/nachher, git status,
        # GET-Zaehler) - hier wird festgehalten, dass wirklich NUR diese
        # Skripte laufen, ohne Zusatzargumente und ohne Shell.
        for _name in ("ha_diagnose", "doppelablage_pruefen",
                      "datenschutz_pruefen"):
            _befehl = [str(t) for t in AKTIONEN[_name][1](None)]
            pruefe(_befehl[-1].endswith(_name + ".py") and len(_befehl) == 2,
                   f"'{_name}' ruft genau das eigene Pruefskript",
                   str(_befehl))
            pruefe(not any(";" in t or "&&" in t or "|" in t
                           for t in _befehl),
                   f"'{_name}' enthaelt keine Shell-Verkettung")

        # --- Benchmark-Aktionen: nur ueber den Starter, nie direkt ---
        # Der Starter haelt das Lock (kein Doppellauf) und den Kill-Switch-
        # Riegel. Eine Aktion, die modell_benchmark.py direkt anwirft,
        # wuerde beides umgehen und nach 30 Minuten mitten in der Messung
        # abgebrochen (ZEITGRENZE) - halbe JSONs, kein Bericht.
        for _name in ("modell_benchmark_neue", "modell_benchmark_modell",
                      "modell_benchmark_status", "modell_benchmark_vergleich",
                      "modell_abitur", "modell_abitur_neue"):
            _befehl = [str(t) for t in AKTIONEN[_name][1]("x")]
            pruefe(any("benchmark_starter.py" in t for t in _befehl),
                   f"'{_name}' laeuft ueber den Starter", str(_befehl))
            pruefe(not any("modell_benchmark.py" in t for t in _befehl),
                   f"'{_name}' ruft den Benchmark nicht direkt")

        # --- Die Grenze zur Lampensteuerung ---
        # Lampen schalten darf Tim seit dem 22.08.2026 - aber NUR ueber
        # benannte Raeume. Rohe Hex-Pakete bleiben gesperrt: Damit liesse
        # sich alles funken, auch an fremde Geraete in Reichweite, und
        # niemand koennte hinterher sagen, was gesendet wurde.
        # Eine Entscheidung, die nur als Kommentar dasteht, haelt nicht -
        # deshalb faellt hier auf, wenn jemand das rohe Senden einbaut.
        _funkend = []
        for _name, (_besch, _bauen, _arg) in AKTIONEN.items():
            if _bauen is None:
                continue
            try:
                _befehl = [str(t) for t in _bauen("PRUEFWERT")]
            except Exception:
                continue
            if any("bruecke_cli.py" in t for t in _befehl) and \
                    any(t in ("senden", "stoppen") for t in _befehl):
                _funkend.append(_name)
        pruefe(not _funkend,
               "keine Aktion funkt rohe Hex-Pakete", ", ".join(_funkend))

        # Und die Gegenprobe: Der erlaubte Weg muss ueber lampen_steuern.py
        # laufen, das nur angelernte Raumnamen annimmt.
        _lampen = AKTIONEN.get("lampen")
        pruefe(_lampen is not None, "die Lampenaktion ist vorhanden")
        if _lampen:
            _befehl = [str(x) for x in _lampen[1]("wohnzimmer.rot")]
            pruefe(any("lampen_steuern.py" in x for x in _befehl),
                   "die Lampenaktion geht ueber lampen_steuern.py")
            pruefe("wohnzimmer" in _befehl and "rot" in _befehl,
                   "Raum und Befehl kommen als getrennte Worte an", str(_befehl))
            pruefe(not any(";" in x or "&&" in x for x in _befehl),
                   "im Befehl steckt keine Shell-Verkettung")
            # Mehrteilige Befehle muessen ebenso ankommen.
            _mehr = [str(x) for x in _lampen[1]("kueche.255.128.0")]
            pruefe(_mehr[-3:] == ["255", "128", "0"],
                   "auch Farbwerte kommen als einzelne Worte an", str(_mehr[-4:]))
            # Und das Argument der Zentrale muss durch deren Riegel passen.
            import re as _re
            _riegel = _re.compile(r"^[A-Za-z0-9_.-]{1,80}$")
            for _probe in ("wohnzimmer.rot", "flur.hell.40", "kueche.aus"):
                pruefe(bool(_riegel.match(_probe)),
                       "Argument '%s' passiert den Riegel der Zentrale" % _probe)

        # --- Autonomie-Schalter: Form und Positivliste ---
        # Die beiden ersten scheitern schon an der Form (keine Ausfuehrung),
        # der dritte laeuft bis autonomie.py und wird DORT abgelehnt -
        # NIEMALS_* ist bewusst nicht setzbar.
        for kaputt in ("kaputt", "ERLAUBE_SHELL.vielleicht"):
            code, _ = anfrage("/start", methode="POST",
                              koerper={"aktion": "autonomie_setzen",
                                       "argument": kaputt})
            pruefe(code == 400, f"Schalter-Argument abgewiesen: {kaputt}",
                   f"HTTP {code}")
        code, _ = anfrage("/start", methode="POST",
                          koerper={"aktion": "autonomie_setzen",
                                   "argument": "NIEMALS_KAEUFE.nein"})
        pruefe(code == 400, "NIEMALS-Grenze nicht per Schalter setzbar",
               f"HTTP {code}")

        # --- Autonomie-Modus: nur die drei bekannten Werte ---
        # Der Modus ist seit 22.08.2026 ueber die Oberflaeche setzbar.
        # Ohne diese Pruefung landete jeder Tippfehler in autonomie.conf.
        for kaputt in ("root", "autonom; rm -rf /", "ja", ""):
            code, _ = anfrage("/start", methode="POST",
                              koerper={"aktion": "autonomie_modus",
                                       "argument": kaputt})
            pruefe(code == 400, f"Modus abgewiesen: {kaputt[:22] or '(leer)'}",
                   f"HTTP {code}")

        # --- Nur die Positivliste ---
        code, _ = anfrage("/start", methode="POST",
                          koerper={"aktion": "rm -rf /", "argument": ""})
        pruefe(code == 400, "unbekannte Aktion abgewiesen", f"HTTP {code}")
        code, _ = anfrage("/start", methode="POST",
                          koerper={"aktion": "ablauf_starten",
                                   "argument": "gibtsnicht"})
        pruefe(code == 400, "unbekannter Ablauf abgewiesen", f"HTTP {code}")
        code, _ = anfrage("/start", token=None, methode="POST",
                          koerper={"aktion": "status", "argument": ""})
        pruefe(code == 401, "Start ohne Token abgewiesen", f"HTTP {code}")
        code, _ = anfrage("/gibtsnicht")
        pruefe(code == 404, "unbekannter Pfad abgewiesen", f"HTTP {code}")

        # --- Befehle auffindbar ---
        # Faengt den Fall, dass ollama im Dienst-PATH fehlt:
        # unter launchd ist der PATH minimal, im Terminal nicht. Ohne diese
        # Pruefung scheitert die Aktion erst beim Klick in Tim.
        for name in sorted(AKTIONEN):
            bauen = AKTIONEN[name][1]
            if bauen is None:
                continue
            programm = bauen("x")[0]
            pruefe(shutil.which(programm) is not None,
                   f"Programm auffindbar fuer '{name}'", programm)

        # --- Kill-Switch ---
        # killswitch_aktiv wird ersetzt statt eine echte STOP-Datei
        # anzulegen: der Test darf den laufenden Betrieb nicht stoppen.
        # sprachassistent_stoppen bekommt fuer den Test einen harmlosen
        # Befehl, damit die Ausnahme geprueft wird, ohne den laufenden
        # Dienst anzufassen.
        global killswitch_aktiv
        echt = killswitch_aktiv
        echter_eintrag = AKTIONEN.get("sprachassistent_stoppen")
        killswitch_aktiv = lambda: "/opt/ki-server/STOP"
        try:
            code, _ = anfrage("/start", methode="POST",
                              koerper={"aktion": "sprachassistent_starten",
                                       "argument": ""})
            pruefe(code == 400, "bei Kill-Switch kein Sprachassistent-Start",
                   f"HTTP {code}")
            code, _ = anfrage("/start", methode="POST",
                              koerper={"aktion": "status", "argument": ""})
            pruefe(code == 400, "bei Kill-Switch kein Ablauf-Start",
                   f"HTTP {code}")
            # Hochstufen der Autonomie ist das Gegenteil von Abschalten -
            # bei gesetztem Kill-Switch muss es gesperrt sein.
            for hoch in ("assist", "autonom"):
                code, _ = anfrage("/start", methode="POST",
                                  koerper={"aktion": "autonomie_modus",
                                           "argument": hoch})
                pruefe(code == 400,
                       f"bei Kill-Switch kein Hochstufen auf {hoch}",
                       f"HTTP {code}")
            if echter_eintrag:
                AKTIONEN["sprachassistent_stoppen"] = (
                    echter_eintrag[0], lambda arg: ["/usr/bin/true"], False)
                code, _ = anfrage("/start", methode="POST",
                                  koerper={"aktion": "sprachassistent_stoppen",
                                           "argument": ""})
                pruefe(code == 200, "Anhalten trotz Kill-Switch erlaubt",
                       f"HTTP {code}")
            else:
                pruefe(False, "sprachassistent_stoppen in der Positivliste")
        finally:
            killswitch_aktiv = echt
            if echter_eintrag:
                AKTIONEN["sprachassistent_stoppen"] = echter_eintrag
    finally:
        server.shutdown()
        server.server_close()

    if fehler:
        print(f"\n{fehler} Fehler.")
    return fehler


def main() -> int:
    global TOKEN
    TOKEN = token_holen()

    if "--selbsttest" in sys.argv[1:]:
        return _selbsttest()

    # 0.0.0.0 ist noetig, weil Open WebUI im Container laeuft und den Host
    # nur ueber host.docker.internal erreicht - 127.0.0.1 waere von dort
    # unerreichbar. Der Zugriff ist deshalb ueber das Token abgesichert,
    # nicht ueber die Bindung.
    adresse = os.environ.get("M1_SERVER_HOST", "0.0.0.0")
    port = int(os.environ.get("M1_SERVER_PORT", "8765"))

    server = ThreadingHTTPServer((adresse, port), Handler)
    print(f"M1 Job-Server laeuft auf {adresse}:{port}")
    print(f"Token: {TOKEN_DATEI}  (nur fuer dich lesbar)")
    print(f"Erlaubte Aktionen: {', '.join(sorted(AKTIONEN))}")
    stop = killswitch_aktiv()
    if stop:
        print(f"HINWEIS: Kill-Switch ist aktiv ({stop}) - Ablaeufe werden abgelehnt.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBeendet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
