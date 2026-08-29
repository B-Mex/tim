#!/usr/bin/env python3
"""Kamera-Waechter: sieht Tims Auge WIRKLICH noch etwas?

Anlass (29.08.2026, im Betrieb passiert): Mexla hat die Webcam kurz
ab- und wieder angesteckt. Der Kameradienst lief danach weiter -
launchctl sagte "running", die Oberflaeche zeigte "Auge an", und
/auge meldete kamera_fehler = "" (also KEIN Fehler). Nur das Bild war
eingefroren: bildalter_s wuchs von 43 auf 61 Sekunden und weiter,
waehrend alles andere gruen aussah. Ein launchctl kickstart hat es
sofort behoben.

Das ist genau dieselbe Krankheit wie beim Sprachassistenten (siehe
mikrofon_waechter.py): **Der Dienst LEBT, aber er ARBEITET NICHT** -
und er meldet es nicht einmal. Wer nur auf den Prozess schaut, sieht
gruen. Beim Auge ist das besonders teuer, weil niemand danebensitzt:
Es misst die Deckenlampen, es ist Pruefgegenstand im Hardwaretest,
und ein eingefrorenes Bild sieht auf einem Standbild genauso aus wie
ein frisches.

Dieser Waechter fragt deshalb nicht "laeuft der Dienst", sondern
"kommt ein FRISCHES Bild heraus". Er ist ein Messgeraet: standard-
maessig liest er, sonst nichts.

Aufruf:
    kamera_waechter.py              messen und urteilen (nur lesend)
    kamera_waechter.py --heilen     zusaetzlich: bei EINGEFROREN
                                    hoechstens EIN Neustart je Sperrzeit
    kamera_waechter.py --selbsttest Pruefungen (nur Fixtures)

Exit: 0 = in Ordnung, 2 = Problem.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BASIS = Path("/opt/ki-server")
ZUSTANDSDATEI = BASIS / "logs" / "kamera_waechter.json"
DIENST = "com.ki-server.kamera"
AUGE = "http://127.0.0.1:8781/auge"

# Dieselben Orte wie ueberall sonst im Haus.
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
# Gemessen am 29.08.2026 am laufenden Dienst, 12 Proben ueber 50
# Sekunden: bildalter_s lag durchgehend bei 0,00 bis 0,10 s. Median
# 0,10. Kein einziger Ausreisser.
#
# Im Ausfall derselben Stunde wuchs es unbegrenzt weiter (43 -> 53 ->
# 57 -> 61 s und mehr), weil gar kein neues Bild mehr kam.
#
# Der Abstand ist also rund das Sechshundertfache - eine Schwelle
# festzulegen ist hier ausnahmsweise einfach. 30 Sekunden geben
# 300-fache Luft ueber dem Normalbetrieb.
#
# Zweiter Anker: Die Oberflaeche warnt schon ab 10 s ("Das letzte Bild
# ist X s alt - die Kamera liefert gerade nicht", zentrale.html). Wer
# erst bei 30 s Alarm schlaegt, ist also bewusst zurueckhaltender als
# die Anzeige, die Mexla ohnehin sieht.
BILDALTER_GRENZE_S = 30.0

# Wie lange nach einem Heilversuch nicht erneut geheilt wird. Ein
# Waechter, der im Minutentakt Dienste neu startet, richtet mehr
# Schaden an als das Einfrieren.
HEILUNG_SPERRE_S = 3600

# Zeitgrenze fuer die Abfrage. Grosszuegig, weil der Dienst beim
# Hochfahren das Modell laedt und dann kurz nicht antwortet - das ist
# kein Ausfall, sondern ein Start.
ABFRAGE_ZEITGRENZE_S = 10


def stop_aktiv() -> str:
    """Pfad der STOP-Datei, falls vorhanden - sonst leer."""
    for p in STOP_ORTE:
        try:
            if os.path.exists(p) or os.path.islink(p):
                return p
        except OSError:
            continue
    return ""


def auge_lesen(adresse: str = AUGE) -> dict:
    """Was meldet der Kameradienst? {} heisst: antwortet nicht."""
    try:
        with urllib.request.urlopen(adresse,
                                    timeout=ABFRAGE_ZEITGRENZE_S) as antwort:
            return json.loads(antwort.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return {}


def urteilen(auge: dict, stop: str = "",
             grenze: float = BILDALTER_GRENZE_S) -> tuple:
    """(stufe, text) - reine Funktion, damit sie pruefbar ist.

    Stufen:
      OK          alles in Ordnung (oder bewusst aus)
      HINWEIS     nicht messbar, aber kein Grund zum Schreien
      EINGEFROREN der Dienst lebt und liefert trotzdem kein Bild

    Die Schreihals-Regel steckt in der Reihenfolge: Alles, was einen
    harmlosen Grund hat, wird VOR der Altersgrenze abgefangen. Ein
    Waechter, der bei ausgeschaltetem Auge Alarm schlaegt, wird
    abgeschaltet - und meldet dann auch das echte Einfrieren nicht.
    """
    if stop:
        return "OK", "Kill-Switch aktiv (%s) - das Auge ruht mit Absicht" % stop
    if not auge:
        # Kein Alarm: Der Dienst kann gerade neu starten, und ein
        # Waechter, der jeden Neustart als Ausfall meldet, meldet bei
        # jedem Neustart. Ob der Dienst laeuft, sagt launchctl - das
        # ist die Frage der Trias, nicht die dieses Waechters.
        return "HINWEIS", "Kameradienst antwortet nicht (laeuft er?)"
    # "da" gibt es nur an /api/auge der ZENTRALE - die setzt das Feld
    # selbst, um zu sagen, ob der Kameradienst ueberhaupt laeuft. Der
    # Dienst auf 8781 kennt es nicht: Wer antwortet, ist da.
    #
    # Beim ersten Lauf gegen den echten Dienst meldete dieser Waechter
    # deshalb "hat aber keine Kamera", obwohl das Auge einwandfrei
    # lief - meine Fixtures hatten das Feld erfunden. Gruene Tests
    # gegen selbst ausgedachte Antworten beweisen nichts ueber die
    # echte Schnittstelle; deshalb steht unten ein Testfall mit der
    # WOERTLICHEN Antwort von 8781.
    if "da" in auge and not auge["da"]:
        return "HINWEIS", "Kameradienst meldet sich, hat aber keine Kamera"
    if not auge.get("an"):
        return "OK", "Auge ist ausgeschaltet - kein Bild ist hier richtig"
    alter = auge.get("bildalter_s")
    if alter is None:
        return "HINWEIS", "Dienst meldet kein Bildalter"
    try:
        alter = float(alter)
    except (TypeError, ValueError):
        return "HINWEIS", "Bildalter unlesbar: %r" % (auge.get("bildalter_s"),)
    fehler = str(auge.get("kamera_fehler") or "").strip()
    if alter > grenze:
        # Der Fehlertext wird MITGENOMMEN, aber er ist keine Bedingung:
        # Am 29.08.2026 war er leer, waehrend das Bild einfror. Wer auf
        # eine Fehlermeldung wartet, wartet in genau diesem Fall ewig.
        return "EINGEFROREN", (
            "Auge ist an, aber das letzte Bild ist %.1f s alt "
            "(Grenze %.0f s)%s - der Dienst lebt und arbeitet nicht. "
            "Neustart: launchctl kickstart -k gui/%d/%s"
            % (alter, grenze,
               " - Dienst meldet: %s" % fehler if fehler else
               " und meldet KEINEN Fehler",
               os.getuid(), DIENST))
    if fehler:
        return "HINWEIS", "Bild ist frisch (%.1f s), Dienst meldet: %s" % (
            alter, fehler)
    return "OK", "Auge liefert frische Bilder (%.1f s alt)" % alter


def zustand_lesen(pfad: Path = None) -> dict:
    p = ZUSTANDSDATEI if pfad is None else pfad
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def zustand_schreiben(daten: dict, pfad: Path = None) -> None:
    p = ZUSTANDSDATEI if pfad is None else pfad
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(daten, ensure_ascii=False, indent=2) + "\n",
                     encoding="utf-8")
    except OSError:
        pass


# Solange diese Datei liegt, laeuft eine Pruefung (Abitur oder
# Fuehrerschein). Derselbe Pfad wie in fuehrerschein.py.
PRUEFUNGSSCHALTER = BASIS / "config" / "PRUEFUNGSMODUS"


def pruefung_laeuft(schalter: Path = None) -> bool:
    """Laeuft gerade eine Pruefung?

    Warum das hier steht (29.08.2026, teuer gelernt): Waehrend eines
    Fuehrerschein-Laufs wurde die Zentrale neu gestartet. Ergebnis:
    T3 fiel in allen fuenf Runden mit "Connection refused" aus - fuenf
    Umgebungsfehler, der ganze Lauf ungueltig, und das nach zwei
    sauberen Teilen (T1 5/5, T2 5/5). Ein halbstuendiger Pruefungslauf
    war weg, weil ein Dienst zur Unzeit neu startete.

    Damals war es Handarbeit. Automatisch waere es schlimmer: Dieser
    Waechter laeuft alle zehn Minuten, und mit --heilen wuerde er
    fruehr oder spaeter genau in eine Pruefung hineinstarten.
    """
    p = PRUEFUNGSSCHALTER if schalter is None else schalter
    try:
        return p.exists()
    except OSError:
        # Im Zweifel NICHT heilen: Ein verpasster Neustart kostet
        # Minuten, ein zerschossener Pruefungslauf eine halbe Stunde.
        return True


def darf_heilen(zustand: dict, jetzt: float = None,
                sperre: float = HEILUNG_SPERRE_S,
                pruefung: bool = None) -> bool:
    """Ist die Sperrzeit abgelaufen - und laeuft gerade keine Pruefung?"""
    if pruefung_laeuft() if pruefung is None else pruefung:
        return False
    jetzt = time.time() if jetzt is None else jetzt
    letzte = zustand.get("letzte_heilung")
    if letzte is None:
        return True
    try:
        return (jetzt - float(letzte)) >= sperre
    except (TypeError, ValueError):
        return True


def heilen() -> tuple:
    """Einen Neustart anstossen. (geklappt, text)"""
    befehl = ["launchctl", "kickstart", "-k",
              "gui/%d/%s" % (os.getuid(), DIENST)]
    try:
        lauf = subprocess.run(befehl, capture_output=True, text=True,
                              timeout=30)
    except (OSError, subprocess.SubprocessError) as f:
        return False, "Neustart nicht moeglich: %s: %s" % (type(f).__name__, f)
    if lauf.returncode == 0:
        return True, "Neustart abgesetzt (%s)" % " ".join(befehl)
    return False, "Neustart fehlgeschlagen (Exit %s): %s" % (
        lauf.returncode, (lauf.stderr or "").strip()[:200])


def bericht(stufe: str, text: str, zusatz: list = None) -> str:
    zeilen = ["Kamera-Waechter: sieht Tims Auge noch etwas?",
              "Gemessen: %s" % datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
              "",
              "%s %s" % ({"OK": "  ok     ",
                          "HINWEIS": "  HINWEIS",
                          "EINGEFROREN": "  PROBLEM"}.get(stufe, "  ?      "),
                         text)]
    zeilen.extend(zusatz or [])
    return "\n".join(zeilen)


def main(argumente: list) -> int:
    if "--selbsttest" in argumente:
        return selbsttest()

    stop = stop_aktiv()
    auge = auge_lesen()
    stufe, text = urteilen(auge, stop)
    zusatz = []

    if stufe == "EINGEFROREN" and "--heilen" in argumente:
        zustand = zustand_lesen()
        if darf_heilen(zustand):
            geklappt, meldung = heilen()
            zusatz.append("  %s" % meldung)
            zustand["letzte_heilung"] = time.time()
            zustand["heilungen"] = int(zustand.get("heilungen") or 0) + 1
            zustand_schreiben(zustand)
            if geklappt:
                zusatz.append("  Heilversuch Nummer %d insgesamt."
                              % zustand["heilungen"])
        else:
            zusatz.append("  Kein Neustart: Sperrzeit laeuft noch "
                          "(hoechstens einer je %d Minuten)."
                          % (HEILUNG_SPERRE_S // 60))

    print(bericht(stufe, text, zusatz))
    return 2 if stufe == "EINGEFROREN" else 0


def selbsttest() -> int:
    fehler = 0

    def pruefe(bedingung, was, zusatz=""):
        nonlocal fehler
        if bedingung:
            print("  ok      %s" % was)
        else:
            print("  FEHLER  %s%s" % (was, "  [%s]" % zusatz if zusatz else ""))
            fehler += 1

    print("Kamera-Waechter Selbsttest (nur Fixtures):")

    # Der Fall, der diesen Waechter ausgeloest hat: Auge an, kein
    # Fehlertext, Bild eingefroren. Genau so sah es am 29.08.2026 aus.
    stufe, text = urteilen({"da": True, "an": True, "bildalter_s": 61.3,
                            "kamera_fehler": ""})
    pruefe(stufe == "EINGEFROREN", "eingefrorenes Bild OHNE Fehlertext "
           "wird erkannt (der Fall vom 29.08.2026)", "%s: %s" % (stufe, text))
    pruefe("KEINEN Fehler" in text,
           "und der Bericht sagt ausdruecklich, dass der Dienst schweigt")
    pruefe("kickstart" in text, "der Neustart-Befehl steht im Befund")

    # Die Gegenprobe: frisches Bild ist in Ordnung.
    pruefe(urteilen({"da": True, "an": True, "bildalter_s": 0.1})[0] == "OK",
           "frisches Bild ist in Ordnung")

    # Genau auf der Grenze gilt als NICHT abgelaufen - dieselbe Lesart
    # wie bei den Zeitgrenzen im Rest des Hauses.
    pruefe(urteilen({"da": True, "an": True,
                     "bildalter_s": BILDALTER_GRENZE_S})[0] == "OK",
           "exakt auf der Grenze ist noch in Ordnung")
    pruefe(urteilen({"da": True, "an": True,
                     "bildalter_s": BILDALTER_GRENZE_S + 0.1})[0]
           == "EINGEFROREN", "einen Wimpernschlag darueber nicht mehr")

    # --- Die Schreihals-Faelle: harmlos heisst harmlos ---
    pruefe(urteilen({"da": True, "an": False, "bildalter_s": 9999})[0] == "OK",
           "ausgeschaltetes Auge schlaegt KEINEN Alarm")
    pruefe(urteilen({"da": True, "an": True, "bildalter_s": 9999},
                    stop="/opt/ki-server/STOP")[0] == "OK",
           "bei Kill-Switch schlaegt der Waechter KEINEN Alarm")
    pruefe(urteilen({})[0] == "HINWEIS",
           "ein nicht antwortender Dienst ist ein Hinweis, kein Problem "
           "(er kann gerade neu starten)")
    pruefe(urteilen({"da": False, "an": True})[0] == "HINWEIS",
           "Dienst ohne Kamera ist ein Hinweis")

    # Die ECHTE Antwortform von Port 8781, woertlich abgeschrieben
    # (29.08.2026, gekuerzt um die Fundliste). Sie hat KEIN "da" - das
    # setzt erst die Zentrale an /api/auge dazu. Ohne diesen Testfall
    # bestand der Selbsttest gruen, waehrend der Waechter am echten
    # Dienst "hat aber keine Kamera" meldete: Ein Test gegen selbst
    # ausgedachte Antworten prueft die eigene Vorstellung, nicht die
    # Schnittstelle.
    _echt = {"an": True, "kamera_fehler": "", "bildalter_s": 0.0,
             "gesehen": [{"name": "lamp", "vertrauen": 0.664}]}
    pruefe(urteilen(_echt)[0] == "OK",
           "die ECHTE Antwort des Kameradienstes wird als gut erkannt "
           "(sie kennt kein 'da')", str(urteilen(_echt)))
    _echt_alt = dict(_echt, bildalter_s=61.3)
    pruefe(urteilen(_echt_alt)[0] == "EINGEFROREN",
           "und dieselbe Antwort mit altem Bild als eingefroren")
    pruefe(urteilen({"da": True, "an": True})[0] == "HINWEIS",
           "fehlendes Bildalter ist ein Hinweis, kein Alarm")
    pruefe(urteilen({"da": True, "an": True, "bildalter_s": "kaputt"})[0]
           == "HINWEIS", "unlesbares Bildalter stuerzt nicht ab")

    # Ein Fehlertext bei frischem Bild ist erwaehnenswert, aber kein
    # Problem - das Auge arbeitet ja.
    pruefe(urteilen({"da": True, "an": True, "bildalter_s": 0.1,
                     "kamera_fehler": "irgendwas"})[0] == "HINWEIS",
           "Fehlertext bei frischem Bild ist ein Hinweis")

    # --- Die Sperrzeit der Heilung ---
    jetzt = 1_000_000.0
    pruefe(darf_heilen({}, jetzt), "ohne Vorgeschichte darf geheilt werden")
    pruefe(not darf_heilen({"letzte_heilung": jetzt - 10}, jetzt),
           "kurz nach einer Heilung nicht nochmal")
    pruefe(darf_heilen({"letzte_heilung": jetzt - HEILUNG_SPERRE_S}, jetzt),
           "nach Ablauf der Sperrzeit wieder")
    pruefe(darf_heilen({"letzte_heilung": "kaputt"}, jetzt),
           "ein kaputter Zeitstempel blockiert die Heilung nicht")
    # Der teuerste Fall vom 29.08.2026: Waehrend einer Pruefung wurde
    # ein Dienst neu gestartet - T3 fiel in allen fuenf Runden mit
    # "Connection refused" aus und machte einen halbstuendigen Lauf
    # ungueltig, obwohl T1 und T2 sauber 5/5 standen.
    pruefe(not darf_heilen({}, jetzt, pruefung=True),
           "waehrend einer Pruefung wird NIE geheilt")
    pruefe(darf_heilen({}, jetzt, pruefung=False),
           "ohne Pruefung darf geheilt werden (sonst prueft das nichts)")
    import tempfile as _tf_k
    with _tf_k.TemporaryDirectory() as _o:
        _s = Path(_o) / "PRUEFUNGSMODUS"
        pruefe(not pruefung_laeuft(_s), "ohne Schalterdatei laeuft keine Pruefung")
        _s.write_text("probe")
        pruefe(pruefung_laeuft(_s), "mit Schalterdatei laeuft eine Pruefung")

    # --- Der Bericht traegt den Problem-Marker, auf den die Routine
    #     schaut (PROBLEM_MARKER in routine.py) - sonst bliebe ein
    #     echtes Einfrieren still.
    pruefe("PROBLEM" in bericht("EINGEFROREN", "x"),
           "der Problem-Bericht traegt das Wort PROBLEM")
    pruefe("PROBLEM" not in bericht("OK", "x"),
           "und der gruene Bericht traegt es NICHT")

    # --- Der Waechter darf nichts anfassen ---
    pruefe("--heilen" in __doc__ and "nur lesend" in __doc__,
           "der Kopf sagt, dass Heilen ein Schalter ist und nicht der "
           "Normalfall")

    # --- Zustandsdatei: nur unter logs/, nie im Quellbaum ---
    pruefe(str(ZUSTANDSDATEI).startswith(str(BASIS / "logs")),
           "der Zaehlerstand liegt unter logs/", str(ZUSTANDSDATEI))

    print()
    if fehler:
        print("%d Fehler." % fehler)
        return 1
    print("Alle Pruefungen gruen.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
