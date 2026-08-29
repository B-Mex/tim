#!/usr/bin/env python3
"""Mikrofon-Waechter: hoert der Sprachassistent WIRKLICH noch?

Warum es das gibt (29.08.2026, Anlass in
docs/SPRACHASSISTENT_WAECHTER.md): Am 26.08. lief der Sprachassistent
laut Oberflaeche - und hoerte nichts. Im Protokoll stapelten sich
"Aufnahmestrom gestoert: ... PortAudio -9986". Am 29.08. waren es
10801 statt 1105. Der Dienst LEBT, aber er ARBEITET NICHT: Ein
Prozess-Check sagt gruen, waehrend monatelang niemand gehoert wird.

Dieser Waechter misst deshalb nicht "laeuft der Prozess", sondern
"kommt Arbeit heraus". Er ist NUR ein Messgeraet: standardmaessig
liest er, sonst nichts. Die Ursache von -9986 zu finden ist
ausdruecklich nicht seine Aufgabe - er liefert dafuer die Zahlen.

WAS ER NICHT NEU BAUT: Der Dienst setzt den Aufnahmestrom bereits
selbst neu auf. Die aeussere Schleife in scripts/sprachassistent.py
(Zeile ~1831) faengt jede Stoerung, baut den Strom neu und laedt nach
drei Fehlern in Folge die Geraeteliste neu. JEDE Zeile
"Aufnahmestrom gestoert:" IST also ein Neuaufsetzen. Was fehlte, war
nicht die Selbstheilung, sondern ihre BUCHFUEHRUNG - niemand wusste,
wie oft sie noetig war und ob sie half. Genau das zaehlt dieser
Waechter mit.

Aufruf:
    mikrofon_waechter.py              messen und urteilen (nur lesend)
    mikrofon_waechter.py --heilen     zusaetzlich: bei TAUB hoechstens
                                      EIN launchctl-Neustart je Sperrzeit
    mikrofon_waechter.py --zustand    den Zaehlerstand zeigen
    mikrofon_waechter.py --selbsttest Pruefungen (nur Fixtures)

Exit: 0 = gruen (auch "wackelt", siehe unten), 2 = Problem.
"""

from __future__ import annotations

import glob
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

BASIS = Path("/opt/ki-server")
LOGDATEI = BASIS / "logs" / "sprachassistent.log"
# Der Zaehlerstand liegt unter logs/ - dort, wo Laufzeitdaten
# hingehoeren und die .gitignore sie schon ausschliesst.
ZUSTANDSDATEI = BASIS / "logs" / "mikrofon_waechter.json"
DIENST = "com.ki-server.sprachassistent"
# Solange diese Datei liegt, laeuft eine Pruefung (Abitur oder
# Fuehrerschein). Derselbe Pfad wie in fuehrerschein.py.
PRUEFUNGSSCHALTER = BASIS / "config" / "PRUEFUNGSMODUS"


def pruefung_laeuft(schalter=None) -> bool:
    """Laeuft gerade eine Pruefung? Im Zweifel: ja.

    Ein verpasster Neustart kostet Minuten, ein zerschossener
    Pruefungslauf eine halbe Stunde - deshalb faellt der Zweifel hier
    zugunsten der Pruefung aus.
    """
    p = PRUEFUNGSSCHALTER if schalter is None else schalter
    try:
        return p.exists()
    except OSError:
        return True

# Dieselben Orte wie in scripts/sprachassistent.py und im Job-Server.
# Kopiert statt importiert, weil dieser Waechter im venv-Python laeuft
# und sprachassistent.py beim Import sounddevice zieht (nur im
# Homebrew-Python vorhanden) - ein Import waere ein Abhaengigkeits-
# Fallstrick fuer ein Werkzeug, das immer laufen koennen soll.
STOP_ORTE = (
    "/opt/ki-server/STOP",
    os.path.expanduser("~/Desktop/M1_DEPLOYMENT/STOP"),
    "/Volumes/M1_DEPLOYMENT/STOP",
    "/Volumes/Extreme SSD/M1_DEPLOYMENT/STOP",
    "/Volumes/SanDisk/M1_DEPLOYMENT/STOP",
    "/Volumes/SANDISK/M1_DEPLOYMENT/STOP",
)

# ---------------------------------------------------------------------
# Die Schwelle - und warum sie so und nicht anders ist
# ---------------------------------------------------------------------
# Gemessen am 29.08.2026 an der echten Logdatei (387816 Zeilen, rund
# sieben Tage Betrieb, mehrere Naechte darin):
#
#   * Das Protokoll traegt KEINE Zeitstempel. Abstaende sind deshalb
#     ueber die Zeilenfolge und die Wachfenster gemessen, nicht aus
#     dem Text gelesen.
#   * Live nachgemessen (drei Proben zu 30 s): 18, 20, 17 neue
#     "gehoert:"-Zeilen, also rund 0,6 pro Sekunde.
#   * Gegenprobe ueber einen langen Zeitraum: Zwischen dem Stand der
#     Doku (26.08., 247422 Zeilen) und dem 29.08. (387816) kamen in
#     59,4 Stunden 140394 Zeilen dazu. Nach Abzug der Stoerzeilen sind
#     das rund 0,61 "gehoert:"-Zeilen pro Sekunde - GLEICH der
#     Tagesrate, obwohl zwei volle Naechte darin liegen.
#
#     Das ist der Kern der Schreihals-Frage: Naechtliche Stille gibt
#     es in diesem Protokoll NICHT. Das Weckwort-Modell verschriftet
#     auch Umgebungsgeraeusche ("musik", "stimmengewirr"), rund um die
#     Uhr. Stille ist hier also kein Nachtzustand, sondern ein Befund.
#   * Laengste Luecke ohne "gehoert:", die KEINE Stoerung war: 14
#     Zeilen (Startbanner, Weckwort-Antwort) - wenige Sekunden. Ueber
#     123 solcher Luecken war keine laenger.
#   * Selbst gefangene Stoerungen (Dienst setzte den Strom neu auf und
#     hoerte danach weiter): 1,1,1,1,2,3,4,5,10 Fehler je Block, also
#     hoechstens rund 20 s taub (jeder Fehlversuch kostet time.sleep(2)).
#   * Echte Ausfaelle: 22 ... 5782 Fehler je Block, der groesste rund
#     3,2 Stunden am Stueck taub.
#
# 10 Minuten sind damit: 30-fache Luft ueber der laengsten harmlosen
# Luecke (20 s), gleichzeitig frueh genug, um die echten Ausfaelle ab
# rund 12 Minuten zu fangen. Es ist zugleich die Zahl, die die Doku
# vom 26.08. vorgeschlagen hat - eine eigene Zahl zu erfinden waere
# ohne Not gewesen.
#
# Wenn dieser Waechter je nachts faelschlich schreit, ist DIESE Zahl
# die Schraube - nicht eine Nachtabschaltung. Ein Waechter, der nachts
# schlaeft, verpasst genau die Ausfaelle, die bis zum Morgen niemand
# bemerkt.
STILLE_MINUTEN = 10.0

# Ab wie vielen Neuaufsetzungen die Stille als "taub" (Mikrofon weg)
# statt als "still" (Schleife liefert gar nichts) gilt. 3 ist keine
# freie Wahl: Genau bei drei Fehlern in Folge laedt der Dienst selbst
# die Geraeteliste neu (_fehler_folge >= 3). Waechter und Dienst
# sprechen so von demselben Ereignis.
FEHLER_FUER_TAUB = 3

# Hoechstens ein Neustartversuch je Sperrzeit. Die Doku haelt fest,
# dass ein launchctl-Neustart am 26.08. NICHT half (11 neue Fehler
# danach) - ein Waechter, der im Kreis neu startet, verdeckt den
# Fehler nur. Deshalb: einmal, dann melden und still sein.
HEILUNG_SPERRE_MINUTEN = 60.0

# Kennzeichen im Protokoll. Die Zeile "Aufnahmestrom gestoert:" ist
# das Neuaufsetzen des Stroms (siehe Kopf), "-9986" der PortAudio-
# Teilfall daraus, "Mikrofon:" das Neuladen der Geraeteliste.
MARKE_GEHOERT = "  gehoert:"
MARKE_AUFSETZEN = "Aufnahmestrom gestoert:"
MARKE_9986 = "-9986"
MARKE_GERAETELISTE = "Mikrofon:"


# ---------------------------------------------------------------------
# Messen
# ---------------------------------------------------------------------

def killswitch_aktiv(orte=STOP_ORTE) -> str | None:
    """Pfad der STOP-Datei, falls vorhanden - sonst None.

    Wichtig fuer die Schreihals-Regel: Bei gesetztem Kill-Switch RUHT
    der Dienst absichtlich (sprachassistent.py schlaeft dann in
    10-s-Schritten, der Strom ist zu). Stille ist dann der gewollte
    Zustand und kein Befund.
    """
    for p in list(orte) + glob.glob("/Users/*/Desktop/M1_DEPLOYMENT/STOP"):
        try:
            if os.path.exists(p) or os.path.islink(p):
                return p
        except OSError:
            continue
    return None


def dienst_zustand(dienst: str = DIENST, laufer=subprocess.run) -> str:
    """'laeuft', 'geladen' oder 'aus' - aus launchctl gelesen.

    Warum ueberhaupt: Ein absichtlich angehaltener Dienst (Mexla hat
    das Mikrofon ueber die Zentrale ausgeschaltet) ist still - aber in
    Ordnung. Ohne diese Unterscheidung wuerde der Waechter jedes
    bewusste Ausschalten als Ausfall melden.
    """
    try:
        lauf = laufer(["/bin/launchctl", "print",
                       f"gui/{os.getuid()}/{dienst}"],
                      capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return "aus"
    if lauf.returncode != 0:
        return "aus"
    text = lauf.stdout or ""
    if "state = running" in text:
        return "laeuft"
    return "geladen"


def zaehlen(zeilen) -> dict:
    """Die vier Kennzahlen aus einem Strom von Protokollzeilen."""
    z = {"gehoert": 0, "aufsetzungen": 0, "portaudio_9986": 0,
         "geraeteliste": 0}
    for zeile in zeilen:
        if zeile.startswith(MARKE_GEHOERT):
            z["gehoert"] += 1
        elif MARKE_AUFSETZEN in zeile:
            z["aufsetzungen"] += 1
            if MARKE_9986 in zeile:
                z["portaudio_9986"] += 1
        elif zeile.lstrip().startswith(MARKE_GERAETELISTE):
            z["geraeteliste"] += 1
    return z


def zuwachs_zaehlen(logdatei: Path, offset: int, inode: int) -> tuple:
    """Nur die seit der letzten Wache angehaengten Zeilen zaehlen.

    Rueckgabe: (zaehlung, neuer_offset, neuer_inode, gedreht).

    Zeilenweise gelesen statt am Stueck: Nach einer langen Pause kann
    der Zuwachs viele Megabyte gross sein - der Waechter soll deshalb
    laufen koennen, ohne die Datei in den Speicher zu heben.
    """
    try:
        stat = logdatei.stat()
    except OSError:
        return zaehlen([]), offset, inode, False
    gedreht = (inode and stat.st_ino != inode) or stat.st_size < offset
    if gedreht:
        offset = 0
    with logdatei.open("r", errors="replace") as datei:
        datei.seek(offset)
        zaehlung = zaehlen(datei)
        neuer_offset = datei.tell()
    return zaehlung, neuer_offset, stat.st_ino, bool(gedreht)


# ---------------------------------------------------------------------
# Urteilen - reine Funktion, damit sie ohne Betriebsdaten pruefbar ist
# ---------------------------------------------------------------------

def urteil(*, dienst: str, killswitch: str | None, neu_gehoert: int,
           stumm_minuten: float, fehler_seit_stumm: int,
           erstlauf: bool = False) -> tuple[str, bool, str]:
    """(stufe, ist_problem, satz).

    Die Stufen in der Reihenfolge, in der sie geprueft werden - die
    harmlosen Erklaerungen zuerst, damit der Waechter nicht schreit,
    wo eine Erklaerung vorliegt (Schreihals-Regel).
    """
    if erstlauf:
        return ("ERSTLAUF", False,
                "erste Wache - Zaehlerstand angelegt, ab jetzt wird gemessen.")
    if killswitch:
        return ("RUHT", False,
                f"Kill-Switch liegt ({killswitch}) - der Dienst ruht "
                "absichtlich. Stille ist hier der gewollte Zustand.")
    if dienst == "aus":
        return ("AUS", False,
                "der Dienst ist nicht geladen - abgeschaltet, nicht taub. "
                "Wieder an ueber die Aktion 'sprachassistent_starten'.")
    if neu_gehoert > 0:
        if fehler_seit_stumm > 0:
            return ("WACKELT", False,
                    f"der Dienst hoert, hat den Aufnahmestrom aber "
                    f"{fehler_seit_stumm}x neu aufsetzen muessen und sich "
                    "selbst gefangen. Gezaehlt, kein Alarm.")
        return ("GRUEN", False, "der Dienst hoert.")
    if stumm_minuten < STILLE_MINUTEN:
        # Noch keine Aussage: kurze Luecken gibt es im Normalbetrieb
        # (Weckwort-Antwort, Neustart), und ein Wachfenster kann
        # zufaellig genau in so eine Luecke fallen.
        return ("KURZ_STILL", False,
                f"seit {stumm_minuten:.1f} min nichts gehoert - unter der "
                f"Schwelle von {STILLE_MINUTEN:.0f} min, noch kein Befund.")
    if fehler_seit_stumm >= FEHLER_FUER_TAUB:
        return ("TAUB", True,
                f"seit {stumm_minuten:.1f} min keine einzige gehoert-Zeile, "
                f"dabei {fehler_seit_stumm} Neuaufsetzungen des "
                "Aufnahmestroms. Der Dienst lebt, aber er arbeitet nicht - "
                "das Mikrofon laesst sich nicht mehr oeffnen.")
    return ("STILL", True,
            f"seit {stumm_minuten:.1f} min keine einzige gehoert-Zeile - und "
            "auch keine Stoerung dazu. Die Hoerschleife liefert gar nichts "
            "mehr, ohne sich zu beschweren.")


# ---------------------------------------------------------------------
# Zaehlerstand
# ---------------------------------------------------------------------

LEERER_ZUSTAND = {
    "version": 1,
    "wache_seit": 0.0,
    "letzte_wache": 0.0,
    "offset": 0,
    "inode": 0,
    "stumm_seit": 0.0,
    "fehler_seit_stumm": 0,
    "summe_aufsetzungen": 0,
    "summe_9986": 0,
    "summe_geraeteliste": 0,
    "heilungen": 0,
    "letzte_heilung": 0.0,
}


def zustand_lesen(pfad: Path) -> dict:
    z = dict(LEERER_ZUSTAND)
    try:
        gelesen = json.loads(pfad.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return z
    if isinstance(gelesen, dict):
        z.update({k: v for k, v in gelesen.items() if k in LEERER_ZUSTAND})
    return z


def zustand_schreiben(pfad: Path, zustand: dict) -> None:
    pfad.parent.mkdir(parents=True, exist_ok=True)
    # Erst daneben schreiben, dann umbenennen: Ein abgebrochener Lauf
    # soll keinen halben Zaehlerstand hinterlassen, sonst faengt die
    # Buchfuehrung stumm bei null an.
    neben = pfad.with_suffix(pfad.suffix + ".neu")
    neben.write_text(json.dumps(zustand, indent=2), encoding="utf-8")
    os.replace(neben, pfad)


# ---------------------------------------------------------------------
# Heilen - nur mit --heilen, hoechstens einmal je Sperrzeit
# ---------------------------------------------------------------------

def heilen(dienst: str = DIENST, laufer=subprocess.run) -> tuple[bool, str]:
    """Den Dienst einmal neu anstossen. Bewusst KEIN Stromneuaufbau von
    aussen: den macht der Dienst selbst schon (siehe Kopf). Hier bleibt
    nur die groebere Stufe, wenn die eigene Heilung nachweislich nicht
    mehr greift."""
    try:
        lauf = laufer(["/bin/launchctl", "kickstart", "-k",
                       f"gui/{os.getuid()}/{dienst}"],
                      capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as fehler:
        return False, f"Neustart nicht moeglich: {fehler}"
    if lauf.returncode != 0:
        return False, f"Neustart abgewiesen (Code {lauf.returncode}): " \
                      f"{(lauf.stderr or '').strip()[:200]}"
    return True, "Dienst neu angestossen (launchctl kickstart -k)."


# ---------------------------------------------------------------------
# Bericht
# ---------------------------------------------------------------------

def _dauer(minuten: float) -> str:
    if minuten < 60:
        return f"{minuten:.1f} min"
    if minuten < 1440:
        return f"{minuten / 60:.1f} h"
    return f"{minuten / 1440:.1f} Tage"


def bericht(zustand: dict, zaehlung: dict, stufe: str, satz: str,
            fenster_minuten: float, stumm_minuten: float, dienst: str,
            gedreht: bool, heilnotiz: str = "") -> str:
    zeilen = [
        f"Mikrofon-Waechter - {DIENST}, "
        f"{datetime.now().strftime('%d.%m.%Y %H:%M')}",
        f"Dienst: {dienst}",
        f"Wachfenster: {_dauer(fenster_minuten)}",
        f"  gehoert-Zeilen neu:          {zaehlung['gehoert']}",
        f"  Aufnahmestrom neu gesetzt:   {zaehlung['aufsetzungen']}"
        f"  (davon PortAudio -9986: {zaehlung['portaudio_9986']})",
        f"  Geraeteliste neu geladen:    {zaehlung['geraeteliste']}",
        f"Stumm seit: {_dauer(stumm_minuten)} "
        f"(Schwelle {STILLE_MINUTEN:.0f} min)",
    ]
    if gedreht:
        zeilen.append("Hinweis: Das Protokoll wurde gedreht oder geleert - "
                      "ab Dateianfang weitergezaehlt.")
    seit = zustand.get("wache_seit") or time.time()
    zeilen.append(
        "Buchfuehrung seit "
        f"{datetime.fromtimestamp(seit).strftime('%d.%m.%Y %H:%M')}: "
        f"{zustand['summe_aufsetzungen']} Neuaufsetzungen des Stroms "
        f"(davon {zustand['summe_9986']} mit PortAudio -9986), "
        f"{zustand['summe_geraeteliste']} Neuladungen der Geraeteliste, "
        f"{zustand['heilungen']} Neustarts durch den Waechter.")
    if heilnotiz:
        zeilen.append(f"Heilung: {heilnotiz}")
    kopf = "PROBLEM" if stufe in ("TAUB", "STILL") else "gruen"
    zeilen.append(f"URTEIL: {kopf} ({stufe}) - {satz}")
    return "\n".join(zeilen)


# ---------------------------------------------------------------------
# Ablauf
# ---------------------------------------------------------------------

def wache(logdatei: Path = LOGDATEI, zustandsdatei: Path = ZUSTANDSDATEI,
          heilen_erlaubt: bool = False, jetzt: float | None = None,
          dienst_pruefer=dienst_zustand, heiler=heilen,
          stop_pruefer=killswitch_aktiv) -> tuple[int, str]:
    jetzt = time.time() if jetzt is None else jetzt
    z = zustand_lesen(zustandsdatei)
    erstlauf = not z["letzte_wache"]

    zaehlung, offset, inode, gedreht = zuwachs_zaehlen(
        logdatei, int(z["offset"]), int(z["inode"]))

    if erstlauf:
        # Beim ersten Lauf steht der Zeiger noch am Dateianfang: Der
        # gesamte Altbestand (am 29.08. rund 10801 Stoerungen) wuerde
        # sonst als "gerade eben passiert" gezaehlt und der Waechter
        # schluege sofort Alarm ueber einen Ausfall von vorgestern.
        try:
            offset = logdatei.stat().st_size
            inode = logdatei.stat().st_ino
        except OSError:
            offset, inode = 0, 0
        zaehlung = zaehlen([])
        z["wache_seit"] = jetzt
        z["stumm_seit"] = jetzt

    if zaehlung["gehoert"] > 0:
        z["stumm_seit"] = jetzt
        z["fehler_seit_stumm"] = 0
    else:
        if not z["stumm_seit"]:
            z["stumm_seit"] = jetzt
        z["fehler_seit_stumm"] = int(z["fehler_seit_stumm"]) + \
            zaehlung["aufsetzungen"]

    z["summe_aufsetzungen"] += zaehlung["aufsetzungen"]
    z["summe_9986"] += zaehlung["portaudio_9986"]
    z["summe_geraeteliste"] += zaehlung["geraeteliste"]

    stumm_minuten = max(0.0, (jetzt - z["stumm_seit"]) / 60.0)
    fenster_minuten = max(0.0, (jetzt - z["letzte_wache"]) / 60.0) \
        if z["letzte_wache"] else 0.0

    dienst = dienst_pruefer()
    stop = stop_pruefer()
    stufe, ist_problem, satz = urteil(
        dienst=dienst, killswitch=stop, neu_gehoert=zaehlung["gehoert"],
        stumm_minuten=stumm_minuten,
        fehler_seit_stumm=int(z["fehler_seit_stumm"]), erstlauf=erstlauf)

    heilnotiz = ""
    # Waehrend einer Pruefung wird NIE geheilt (29.08.2026, teuer
    # gelernt): Ein Fuehrerschein-Lauf verlor T3 komplett - fuenf Runden
    # "Connection refused" -, weil mitten in der Pruefung ein Dienst neu
    # startete. T1 und T2 standen sauber auf 5/5, der ganze Lauf wurde
    # trotzdem ungueltig. Damals war es Handarbeit; automatisch waere es
    # schlimmer, denn dieser Waechter laeuft alle zehn Minuten.
    if ist_problem and heilen_erlaubt and pruefung_laeuft():
        heilen_erlaubt = False
        heilnotiz = ("kein Neustart - es laeuft gerade eine Pruefung. "
                     "Ein Dienst, der mitten in einer Pruefung neu "
                     "startet, macht sie ungueltig.")
    if ist_problem and heilen_erlaubt:
        seit_heilung = (jetzt - z["letzte_heilung"]) / 60.0 \
            if z["letzte_heilung"] else None
        if seit_heilung is not None and seit_heilung < HEILUNG_SPERRE_MINUTEN:
            heilnotiz = (
                f"kein Neustart - der letzte war vor "
                f"{_dauer(seit_heilung)}, die Sperrzeit betraegt "
                f"{HEILUNG_SPERRE_MINUTEN:.0f} min. Ein Waechter, der im "
                "Kreis neu startet, verdeckt den Fehler nur.")
        else:
            ok, notiz = heiler()
            heilnotiz = notiz
            z["letzte_heilung"] = jetzt
            if ok:
                z["heilungen"] = int(z["heilungen"]) + 1
                # Nach einem Neustart neu ansetzen: sonst gilt die alte
                # Stille weiter und der naechste Lauf heilt sofort wieder.
                z["stumm_seit"] = jetzt
                z["fehler_seit_stumm"] = 0
    elif ist_problem:
        heilnotiz = ("nicht versucht - der Waechter laeuft nur lesend "
                     "(--heilen wuerde einen Neustart erlauben).")

    z["offset"], z["inode"], z["letzte_wache"] = offset, inode, jetzt
    zustand_schreiben(zustandsdatei, z)

    text = bericht(z, zaehlung, stufe, satz, fenster_minuten, stumm_minuten,
                   dienst, gedreht, heilnotiz)
    return (2 if ist_problem else 0), text


def main(argv) -> int:
    if "--zustand" in argv:
        print(json.dumps(zustand_lesen(ZUSTANDSDATEI), indent=2))
        return 0
    code, text = wache(heilen_erlaubt="--heilen" in argv)
    print(text)
    return code


# ====================== Selbsttest ==========================
# Kein Test fasst Betriebsdaten an, auch nicht lesend: Protokoll und
# Zaehlerstand sind Fixtures in einem Temp-Ordner, launchctl und der
# Kill-Switch sind Doppelgaenger. Zwei-Seiten-Beweis ueberall - der
# gesunde Fall besteht, der kranke faellt durch.

def _selbsttest() -> int:
    import io
    import tempfile
    fehler = [0]

    def pruefe(bedingung, text, zusatz=""):
        stand = "ok     " if bedingung else "FEHLER "
        print(f"  {stand} {text}"
              + (f"  [{zusatz}]" if zusatz and not bedingung else ""))
        if not bedingung:
            fehler[0] += 1

    print("Mikrofon-Waechter - Selbsttest")

    # --- Keine Betriebsdaten: die echten Pfade duerfen nicht vorkommen
    print("\n[Grenze] Der Test fasst nichts Echtes an")
    echt_log_da = LOGDATEI.exists()
    echt_groesse = LOGDATEI.stat().st_size if echt_log_da else -1

    # --- Zaehlen
    print("\n[Zaehlen] Die vier Kennzahlen")
    probe = io.StringIO(
        "  gehoert: musik\n"
        "  gehoert: stimmengewirr\n"
        "Aufnahmestrom gestoert: Error starting stream: Internal PortAudio "
        "error [PaErrorCode -9986]\n"
        "||PaMacCore (AUHAL)|| Error on line 2744\n"
        "Aufnahmestrom gestoert: irgendetwas anderes\n"
        "Mikrofon: WEB CAM\n"
        "Sprachassistent gestartet. Sage 'Hey Tim' um zu sprechen...\n")
    z = zaehlen(probe)
    pruefe(z["gehoert"] == 2, "gehoert-Zeilen gezaehlt", str(z))
    pruefe(z["aufsetzungen"] == 2, "Neuaufsetzungen gezaehlt", str(z))
    pruefe(z["portaudio_9986"] == 1, "nur der -9986-Teilfall gezaehlt", str(z))
    pruefe(z["geraeteliste"] == 1, "Neuladen der Geraeteliste gezaehlt", str(z))
    # Gegenprobe: eine Zeile, die nur zufaellig 'gehoert' enthaelt, darf
    # nicht mitzaehlen - sonst waere jede Fehlermeldung mit dem Wort
    # darin ein Lebenszeichen.
    pruefe(zaehlen(io.StringIO("Fehler: nichts gehoert: seit Stunden\n"))
           ["gehoert"] == 0,
           "nur die echte Protokollform zaehlt als gehoert")

    # --- Urteil: der gesunde Fall
    print("\n[Urteil] gruen, wackelt, taub, still")
    stufe, problem, _ = urteil(dienst="laeuft", killswitch=None,
                               neu_gehoert=300, stumm_minuten=0.0,
                               fehler_seit_stumm=0)
    pruefe(stufe == "GRUEN" and not problem, "hoert -> gruen", stufe)

    stufe, problem, _ = urteil(dienst="laeuft", killswitch=None,
                               neu_gehoert=300, stumm_minuten=0.0,
                               fehler_seit_stumm=12)
    pruefe(stufe == "WACKELT" and not problem,
           "hoert trotz Stoerungen -> gezaehlt, kein Alarm", stufe)

    stufe, problem, satz = urteil(dienst="laeuft", killswitch=None,
                                  neu_gehoert=0, stumm_minuten=23.0,
                                  fehler_seit_stumm=512)
    pruefe(stufe == "TAUB" and problem, "stumm mit Stoerungen -> taub", stufe)
    pruefe("lebt, aber er arbeitet nicht" in satz,
           "der Befund nennt den Kern des Vorfalls")

    stufe, problem, _ = urteil(dienst="laeuft", killswitch=None,
                               neu_gehoert=0, stumm_minuten=23.0,
                               fehler_seit_stumm=0)
    pruefe(stufe == "STILL" and problem,
           "stumm ohne Stoerung -> still (Schleife liefert nichts)", stufe)

    # --- Schreihals-Regel: die harmlosen Faelle duerfen NICHT schreien
    print("\n[Schreihals] Was harmlos ist, darf keinen Alarm ausloesen")
    for minuten in (0.0, 5.0, STILLE_MINUTEN - 0.1):
        stufe, problem, _ = urteil(dienst="laeuft", killswitch=None,
                                   neu_gehoert=0, stumm_minuten=minuten,
                                   fehler_seit_stumm=9)
        pruefe(not problem,
               f"kurze Stille ({minuten} min) ist kein Alarm", stufe)
    stufe, problem, _ = urteil(dienst="laeuft", killswitch="/opt/ki-server/STOP",
                               neu_gehoert=0, stumm_minuten=600.0,
                               fehler_seit_stumm=0)
    pruefe(stufe == "RUHT" and not problem,
           "Kill-Switch: Stille ist gewollt, kein Alarm", stufe)
    stufe, problem, _ = urteil(dienst="aus", killswitch=None, neu_gehoert=0,
                               stumm_minuten=600.0, fehler_seit_stumm=0)
    pruefe(stufe == "AUS" and not problem,
           "abgeschalteter Dienst: kein Alarm", stufe)
    stufe, problem, _ = urteil(dienst="laeuft", killswitch=None, neu_gehoert=0,
                               stumm_minuten=999.0, fehler_seit_stumm=999,
                               erstlauf=True)
    pruefe(stufe == "ERSTLAUF" and not problem,
           "erste Wache urteilt noch nicht")
    # Und die Gegenprobe zur Schreihals-Regel: knapp UEBER der Schwelle
    # muss es schreien, sonst waere der Waechter nur stumm.
    stufe, problem, _ = urteil(dienst="laeuft", killswitch=None, neu_gehoert=0,
                               stumm_minuten=STILLE_MINUTEN + 0.1,
                               fehler_seit_stumm=9)
    pruefe(problem, "knapp ueber der Schwelle schlaegt er an", stufe)

    # --- Meldeweg: die Marker von routine.py
    print("\n[Meldeweg] Rohbefund passt zu den Problem-Markern der Routine")
    marker = ("FUND", "VERALTET", "ABWEICHUNG", "FEHLER",
              "NICHT veroeffentlichen", "PROBLEM")
    leer = {"gehoert": 0, "aufsetzungen": 0, "portaudio_9986": 0,
            "geraeteliste": 0}
    gruen_text = bericht(dict(LEERER_ZUSTAND), dict(leer, gehoert=300),
                         "GRUEN", "der Dienst hoert.", 10.0, 0.0, "laeuft",
                         False)
    getroffen = [m for m in marker if m in gruen_text]
    pruefe(not getroffen,
           "gruener Bericht traegt KEINEN Problem-Marker", str(getroffen))
    rot_text = bericht(dict(LEERER_ZUSTAND), dict(leer, aufsetzungen=512),
                       "TAUB", "taub.", 10.0, 23.0, "laeuft", False)
    pruefe("PROBLEM" in rot_text, "roter Bericht traegt den Marker PROBLEM")

    # --- Zuwachs und Zaehlerstand an Fixtures
    print("\n[Zuwachs] Nur das Neue wird gezaehlt")
    with tempfile.TemporaryDirectory() as ordner:
        log = Path(ordner) / "sprachassistent.log"
        zst = Path(ordner) / "waechter.json"
        log.write_text("  gehoert: musik\n" * 5, encoding="utf-8")
        z1, off1, ino1, gedreht1 = zuwachs_zaehlen(log, 0, 0)
        pruefe(z1["gehoert"] == 5 and not gedreht1,
               "erster Blick zaehlt alles", str(z1))
        with log.open("a", encoding="utf-8") as f:
            f.write("  gehoert: musik\n" * 2)
        z2, off2, ino2, _ = zuwachs_zaehlen(log, off1, ino1)
        pruefe(z2["gehoert"] == 2,
               "zweiter Blick zaehlt nur den Zuwachs", str(z2))
        z3, off3, _, _ = zuwachs_zaehlen(log, off2, ino2)
        pruefe(z3["gehoert"] == 0, "ohne Zuwachs wird nichts gezaehlt", str(z3))
        # Gegenprobe: geleertes Protokoll darf nicht ins Leere zeigen
        log.write_text("  gehoert: musik\n", encoding="utf-8")
        z4, _, _, gedreht4 = zuwachs_zaehlen(log, off3, ino2)
        pruefe(gedreht4 and z4["gehoert"] == 1,
               "geleertes Protokoll wird erkannt und neu angesetzt", str(z4))

        # Zaehlerstand haelt ueber Laeufe hinweg
        zustand_schreiben(zst, dict(LEERER_ZUSTAND, summe_9986=7))
        pruefe(zustand_lesen(zst)["summe_9986"] == 7,
               "Zaehlerstand ueberlebt das Schreiben und Lesen")
        zst.write_text("{kaputt", encoding="utf-8")
        pruefe(zustand_lesen(zst) == LEERER_ZUSTAND,
               "kaputter Zaehlerstand faellt auf null zurueck, ohne Absturz")

    # --- Der ganze Ablauf, mit Doppelgaengern statt echtem System
    print("\n[Ablauf] Wache von Anfang bis Urteil")
    with tempfile.TemporaryDirectory() as ordner:
        log = Path(ordner) / "sprachassistent.log"
        zst = Path(ordner) / "waechter.json"
        log.write_text("  gehoert: alt\n" * 9000, encoding="utf-8")
        t0 = 1_700_000_000.0

        code, text = wache(log, zst, jetzt=t0,
                           dienst_pruefer=lambda: "laeuft",
                           stop_pruefer=lambda: None)
        pruefe(code == 0 and "ERSTLAUF" in text,
               "erste Wache legt nur den Stand an", text.splitlines()[-1])
        pruefe(zustand_lesen(zst)["offset"] == log.stat().st_size,
               "erste Wache setzt den Zeiger ans Ende - der Altbestand "
               "zaehlt nicht als frischer Ausfall")

        # Der Dienst hoert weiter -> gruen
        with log.open("a", encoding="utf-8") as f:
            f.write("  gehoert: musik\n" * 400)
        code, text = wache(log, zst, jetzt=t0 + 600,
                           dienst_pruefer=lambda: "laeuft",
                           stop_pruefer=lambda: None)
        pruefe(code == 0 and "GRUEN" in text, "hoerender Dienst -> gruen",
               text.splitlines()[-1])

        # Jetzt der Vorfall: nur noch Stoerungen, keine gehoert-Zeile
        with log.open("a", encoding="utf-8") as f:
            f.write("Aufnahmestrom gestoert: Error starting stream: Internal "
                    "PortAudio error [PaErrorCode -9986]\n" * 300)
        code, text = wache(log, zst, jetzt=t0 + 1200,
                           dienst_pruefer=lambda: "laeuft",
                           stop_pruefer=lambda: None)
        pruefe(code == 2 and "TAUB" in text and "PROBLEM" in text,
               "Stoerungen ohne gehoert -> Problem gemeldet",
               text.splitlines()[-1])
        stand = zustand_lesen(zst)
        pruefe(stand["summe_9986"] == 300 and stand["summe_aufsetzungen"] == 300,
               "die Neuaufsetzungen sind gebucht - Datengrundlage fuer die "
               "spaetere Ursachensuche", str(stand["summe_9986"]))

        # Heilung: hoechstens einmal je Sperrzeit
        gerufen = []
        heiler = lambda: (gerufen.append(1), (True, "neu angestossen"))[1]
        with log.open("a", encoding="utf-8") as f:
            f.write("Aufnahmestrom gestoert: Error starting stream: Internal "
                    "PortAudio error [PaErrorCode -9986]\n" * 300)
        wache(log, zst, heilen_erlaubt=True, jetzt=t0 + 1800,
              dienst_pruefer=lambda: "laeuft", stop_pruefer=lambda: None,
              heiler=heiler)
        pruefe(len(gerufen) == 1, "erkannte Taubheit loest EINEN Neustart aus",
               str(len(gerufen)))
        with log.open("a", encoding="utf-8") as f:
            f.write("Aufnahmestrom gestoert: Error starting stream: Internal "
                    "PortAudio error [PaErrorCode -9986]\n" * 900)
        wache(log, zst, heilen_erlaubt=True, jetzt=t0 + 1800 + 3000,
              dienst_pruefer=lambda: "laeuft", stop_pruefer=lambda: None,
              heiler=heiler)
        pruefe(len(gerufen) == 1,
               "innerhalb der Sperrzeit wird NICHT nochmal neu gestartet",
               str(len(gerufen)))
        pruefe(zustand_lesen(zst)["heilungen"] == 1,
               "die Zahl der Neustarts steht im Zaehlerstand")
        # Gegenprobe: nach der Sperrzeit darf er wieder
        with log.open("a", encoding="utf-8") as f:
            f.write("Aufnahmestrom gestoert: Error starting stream: Internal "
                    "PortAudio error [PaErrorCode -9986]\n" * 900)
        wache(log, zst, heilen_erlaubt=True,
              jetzt=t0 + 1800 + 60 * (HEILUNG_SPERRE_MINUTEN + 1),
              dienst_pruefer=lambda: "laeuft", stop_pruefer=lambda: None,
              heiler=heiler)
        pruefe(len(gerufen) == 2, "nach der Sperrzeit ist ein Versuch erlaubt",
               str(len(gerufen)))
        # Und ohne --heilen wird NIE neu gestartet
        gerufen2 = []
        with log.open("a", encoding="utf-8") as f:
            f.write("Aufnahmestrom gestoert: Error starting stream: Internal "
                    "PortAudio error [PaErrorCode -9986]\n" * 900)
        code, text = wache(log, zst, heilen_erlaubt=False,
                           jetzt=t0 + 60 * 600,
                           dienst_pruefer=lambda: "laeuft",
                           stop_pruefer=lambda: None,
                           heiler=lambda: (gerufen2.append(1), (True, "x"))[1])
        pruefe(not gerufen2 and code == 2,
               "ohne --heilen bleibt es beim Melden")

        # Kill-Switch: dieselbe Stille, aber gewollt -> kein Alarm
        code, text = wache(log, zst, jetzt=t0 + 60 * 900,
                           dienst_pruefer=lambda: "laeuft",
                           stop_pruefer=lambda: "/opt/ki-server/STOP")
        pruefe(code == 0 and "RUHT" in text,
               "bei gesetztem Kill-Switch schweigt der Waechter",
               text.splitlines()[-1])

    # --- Grenze: das echte Protokoll ist unangetastet
    print("\n[Grenze] Nachgemessen")
    pruefe(LOGDATEI.exists() == echt_log_da
           and (not echt_log_da or LOGDATEI.stat().st_size >= echt_groesse),
           "das Betriebsprotokoll wurde vom Test nicht veraendert")
    pruefe(not (Path(ZUSTANDSDATEI).exists()
                and Path(ZUSTANDSDATEI).stat().st_mtime > time.time() - 5),
           "der Betriebs-Zaehlerstand wurde vom Test nicht geschrieben")

    print(f"\n{'Alles gruen.' if not fehler[0] else str(fehler[0]) + ' Pruefung(en) rot.'}")
    return 1 if fehler[0] else 0


if __name__ == "__main__":
    if "--selbsttest" in sys.argv:
        sys.exit(_selbsttest())
    sys.exit(main(sys.argv[1:]))
