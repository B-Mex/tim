#!/usr/bin/env python3
#
# Lizenz: GNU AGPL-3.0 - siehe kamera/LICENSE. ABWEICHEND vom uebrigen
# Projekt (MIT), weil dieser Dienst ueber objekterkennung.py Ultralytics
# einbindet, das unter AGPL-3.0 steht. Naeheres: kamera/README.md
#
"""Kameradienst - Tims Auge auf die Lampen.

Warum es diesen Dienst gibt: Beim Einstellen der Lampenfarben muss
jemand nachsehen, ob wirklich passiert, was gesendet wurde. Genau das
macht dieser Dienst - er haelt die Webcam offen, zeigt ein Livebild im
Browser und liefert die gemessene Farbe als Zahlen zurueck.

Der zweite Grund ist macOS: Die Kameraerlaubnis haengt am Programm, das
zugreift. Statt sie jedem Werkzeug einzeln zu geben, greift NUR dieser
Dienst auf die Kamera zu. Alle anderen - Tim, Selbsttests, Claude -
fragen ihn ueber HTTP. Einmal erlaubt, immer nutzbar.

Aufruf:

    kamera_dienst.py                 startet den Dienst auf Port 8781
    kamera_dienst.py --selbsttest    prueft die Farblogik ohne Kamera

Im Browser: http://127.0.0.1:8781/

Adressen fuer Programme:

    GET /messung        Farbe im Messfeld als JSON
    GET /bild.jpg       aktuelles Einzelbild
    GET /strom.mjpg     Livebild als Datenstrom
    GET /messfeld?x=&y=&breite=&hoehe=    Messfeld verschieben
"""

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8781
# Bewusst nur die eigene Maschine. Das Bild aus der Wohnung gehoert nicht
# ins Netz - wer es von aussen sehen will, geht ueber die Zentrale.
ADRESSE = "127.0.0.1"

# Messfeld: der Ausschnitt, in dem die Lampe steht. Anteilig zum Bild,
# damit es bei jeder Aufloesung passt.
messfeld = {"x": 0.40, "y": 0.35, "breite": 0.20, "hoehe": 0.20}

# Seit dem 31.08.2026 haelt das Auge MEHRERE Messfelder - eines je
# Leuchte. Vorher sah es zwar mehrere Lampen, konnte aber nur eine
# messen; die Frage "welche Lampe gehoert zu welchem Raum" war damit
# nicht zu beantworten, denn sie wird durch Schalten und Vergleichen
# beantwortet, und dafuer muss jede Leuchte einzeln messbar sein.
#
# `messfeld` bleibt bestehen und IST das erste Feld - dasselbe Objekt,
# keine Kopie. Alles, was bisher ein einzelnes Messfeld gelesen oder
# gesetzt hat (kamera_cli, der Sprachassistent, die Zentrale,
# /messfeld), arbeitet unveraendert weiter und wirkt auf Feld 0. Neu
# ist nur, was zusaetzlich danebenliegt.
messfelder = [messfeld]

# Einmal ausgerichtet, dauerhaft gemerkt: Die Lampe haengt fest an der
# Wand, das Messfeld muss also nicht bei jeder Messung neu gesucht
# werden. Im Gegenteil - staendiges Nachsuchen ist schaedlich: Steht
# jemand vor der Lampe, ist die angestrahlte Hand selbst ausgebrannt und
# wird faelschlich fuer die Leuchte gehalten (am 22.08.2026 genau so
# passiert). Gesucht wird deshalb nur auf Zuruf, und das Ergebnis
# ueberlebt einen Neustart.
MESSFELD_DATEI = Path(__file__).resolve().parent / "messfeld.json"

# ----------------------------------------------------------------------
# Das Auge: schaltbare Objekterkennung
#
# Ein einzelnes Bild dieser Webcam ist zu verrauscht, um daraus zu
# schliessen, was im Zimmer steht - Funde am Rand der Sicherheit
# flackern von Bild zu Bild. Geglaettet wird deshalb ueber mehrere
# Bilder (siehe objekterkennung.Gedaechtnis), und dafuer muss jemand
# regelmaessig hinschauen.
#
# Dauerhaft rechnen will man aber nicht: Jeder Blick kostet Rechenzeit,
# seit der GPU-Umstellung am 27.08.2026 rund 0.04 s (davor auf der CPU
# rund eine Fuenftelsekunde). Deshalb ist das Auge ein Schalter. Ist es
# aus, laeuft nur die Kamera weiter (fuers Livebild und die Farbmessung),
# und es wird kein einziges Bild durch das Modell geschickt.
# ----------------------------------------------------------------------
_auge_an = False
_auge_gedaechtnis = None
_auge_letzte = []            # zuletzt gemeldete, geglaettete Liste
_auge_zeit = 0.0             # wann zuletzt geschaut wurde
_auge_dauer = 0.0            # wie lange ein Blick brauchte
_auge_fehler = ""
_auge_sperre = threading.Lock()
AUGE_TAKT = 1.5              # Sekunden zwischen zwei Blicken


def _feld_saeubern(roh):
    """Aus einem gelesenen Eintrag ein gueltiges Feld machen - oder None."""
    if not isinstance(roh, dict):
        return None
    try:
        if not all(k in roh for k in ("x", "y", "breite", "hoehe")):
            return None
        feld = {k: float(roh[k]) for k in ("x", "y", "breite", "hoehe")}
    except (TypeError, ValueError):
        return None
    if roh.get("raum"):
        feld["raum"] = str(roh["raum"])
    return feld


def felder_setzen(felder):
    """Die Feldliste ersetzen, ohne `messfeld` zu entwurzeln.

    Feld 0 wird IN das bestehende `messfeld`-Objekt hineingeschrieben,
    statt es zu ersetzen: Andere Stellen halten eine Referenz darauf.
    Wer sie durch ein neues Objekt ersetzt, laesst sie stumm auf die
    alte Kopie zeigen - die Sorte Fehler, die erst Wochen spaeter im
    Betrieb auffaellt.
    """
    if not felder:
        return False
    messfeld.clear()
    messfeld.update(felder[0])
    del messfelder[1:]
    messfelder.extend(dict(f) for f in felder[1:])
    return True


def messfeld_laden():
    """Gemerkte Felder einlesen. Versteht BEIDE Dateiformate.

    Alt (bis 29.08.2026): ein einzelnes Objekt. Neu: eine Liste. Das
    alte Format muss lesbar bleiben - es liegt noch auf der Platte, und
    ein Rueckbau der Software darf nicht am Dateiformat scheitern.
    """
    try:
        gemerkt = json.loads(MESSFELD_DATEI.read_text(encoding="utf-8"))
    except Exception:
        return False
    roh_liste = gemerkt if isinstance(gemerkt, list) else [gemerkt]
    felder = [f for f in (_feld_saeubern(r) for r in roh_liste) if f]
    return felder_setzen(felder)


def messfeld_sichern():
    try:
        MESSFELD_DATEI.write_text(json.dumps(messfelder, indent=2) + "\n",
                                  encoding="utf-8")
        return True
    except Exception:
        return False


def _ueberlappung(a, b):
    """Anteil der gemeinsamen Flaeche an der kleineren der beiden."""
    links = max(a["x"], b["x"])
    oben = max(a["y"], b["y"])
    rechts = min(a["x"] + a["breite"], b["x"] + b["breite"])
    unten = min(a["y"] + a["hoehe"], b["y"] + b["hoehe"])
    if rechts <= links or unten <= oben:
        return 0.0
    gemeinsam = (rechts - links) * (unten - oben)
    kleiner = min(a["breite"] * a["hoehe"], b["breite"] * b["hoehe"])
    return gemeinsam / kleiner if kleiner > 0 else 0.0


def _mitte(feld):
    return feld["x"] + feld["breite"] / 2, feld["y"] + feld["hoehe"] / 2


def _dasselbe_licht(a, b, abstand=0.05):
    """Zeigen zwei Felder auf dieselbe Lichtquelle?

    Nicht ueber die Flaeche: Die Lampensuche schneidet denselben Spot je
    nach Helligkeit mal groesser, mal kleiner zu, und dann faellt die
    Ueberlappung unter jede Schwelle. Am 31.08.2026 im Betrieb genau so
    passiert - nach einem erneuten /lampe_suchen verlor der rechte
    Deckenspot seinen muehsam gemessenen Raumnamen wieder.

    Entscheidend ist die MITTE: Liegt sie im anderen Feld oder dicht
    daneben, ist es dieselbe Lampe.
    """
    ax, ay = _mitte(a)
    bx, by = _mitte(b)
    if (b["x"] <= ax <= b["x"] + b["breite"]
            and b["y"] <= ay <= b["y"] + b["hoehe"]):
        return True
    if (a["x"] <= bx <= a["x"] + a["breite"]
            and a["y"] <= by <= a["y"] + a["hoehe"]):
        return True
    return ((ax - bx) ** 2 + (ay - by) ** 2) ** 0.5 <= abstand


def raumnamen_uebernehmen(gefunden, alte):
    """Gemessene Raumnamen auf neu gefundene Felder retten.

    Ein Zuordnungslauf kostet Schaltvorgaenge und Zeit; sein Ergebnis
    darf nicht verloren gehen, nur weil jemand die Lampensuche erneut
    anstoesst.
    """
    for feld in gefunden:
        for alt_feld in alte:
            if alt_feld.get("raum") and _dasselbe_licht(feld, alt_feld):
                feld["raum"] = alt_feld["raum"]
                break
    return gefunden


def felder_zusammenfuehren(gefunden, alte):
    """Neu gefundene Leuchten mit den bisherigen Feldern vereinen.

    Die Suche findet nur, was GERADE leuchtet - eine ausgeschaltete
    Lampe faellt aus dem Ergebnis. Wuerde die Liste einfach ersetzt,
    loeschte jedes Suchen die Zuordnung der gerade dunklen Raeume. Am
    31.08.2026 genau so passiert: erst verschwand der Raumname des
    rechten Spots, dann das ganze Flur-Feld.

    Ein gemessener Raumname ist teuer - er kostet einen Schaltlauf mit
    echtem Licht. Ein Feld ohne Namen ist billig. Also: benannte Felder
    ueberleben, unbenannte werden ersetzt.
    """
    raumnamen_uebernehmen(gefunden, alte)
    behalten = [f for f in alte
                if f.get("raum")
                and not any(_dasselbe_licht(f, g) for g in gefunden)]
    return list(gefunden) + behalten


def _feld_bekannt(neu, schon, mindestens=0.5):
    """Zeigt ein neues Feld auf dieselbe Stelle wie ein vorhandenes?

    Das Modell meldet fuer eine einzige Leuchte gern mehrere Kaesten
    ("lamp" und "glowing light" am selben Ort). Ohne diese Pruefung
    bekaeme dieselbe Lampe zwei Felder - und der Zuordnungslauf zaehlte
    sie doppelt.
    """
    return any(_ueberlappung(neu, alt) >= mindestens for alt in schon)


# ----------------------------------------------------------------------
# Anzeigen im Livebild - jede einzeln schaltbar
#
# Was ins Bild gezeichnet wird, ist Geschmackssache und haengt davon ab,
# was man gerade tut: Beim Lampeneinstellen will man das gelbe Messfeld,
# beim blossen Zuschauen stoert es. Deshalb ist jede Einblendung ein
# Schalter, gemerkt ueber Neustarts hinweg. Die Messung selbst laeuft
# unabhaengig davon weiter - /messung liefert immer, nur das Zeichnen
# haengt am Schalter. Kuenftige Erkennungen bekommen hier einfach einen
# weiteren Eintrag.
# ----------------------------------------------------------------------
ANZEIGEN_DATEI = Path(__file__).resolve().parent / "anzeigen.json"
anzeigen = {
    "messfeld": True,     # gelber Rahmen + Farbmesswert der Lampenmessung
    "objekte": True,      # gruene Kaesten der Objekterkennung
    # Faktor fuer die Schriftgroesse der Beschriftungen im Bild. 1.0 war
    # bei 1920x1080, aufs Browserfenster verkleinert, kaum lesbar.
    "textgroesse": 1.6,
}

TEXTGROESSE_MIN, TEXTGROESSE_MAX = 0.6, 3.0


def _anzeigewert(name, roh):
    """Einen Wert typgerecht uebernehmen - Schalter bleiben Schalter."""
    if isinstance(anzeigen[name], bool):
        if isinstance(roh, str):
            return roh not in ("0", "aus", "nein", "false")
        return bool(roh)
    try:
        return min(TEXTGROESSE_MAX, max(TEXTGROESSE_MIN, float(roh)))
    except (TypeError, ValueError):
        return anzeigen[name]


def anzeigen_laden(datei=None):
    datei = datei or ANZEIGEN_DATEI
    try:
        gemerkt = json.loads(Path(datei).read_text(encoding="utf-8"))
        for name in anzeigen:
            if name in gemerkt:
                anzeigen[name] = _anzeigewert(name, gemerkt[name])
        return True
    except Exception:
        return False


def anzeigen_sichern(datei=None):
    datei = datei or ANZEIGEN_DATEI
    try:
        Path(datei).write_text(json.dumps(anzeigen, indent=2) + "\n",
                               encoding="utf-8")
        return True
    except Exception:
        return False

_bild = None                # letztes Bild von der Kamera
_bild_zeit = 0.0
_bild_sperre = threading.Lock()
_kamera_fehler = ""
_laeuft = True
_kamera = None


# ----------------------------------------------------------------------
# Farbmessung - reine Rechnung, ohne Kamera pruefbar
# ----------------------------------------------------------------------

def farbname(h, s, v):
    """Grobe Einordnung eines HSV-Werts in ein Wort.

    h ist der Farbton in Grad (0-360), s und v laufen von 0 bis 1.
    Die Grenzen sind bewusst grob: Es geht darum zu erkennen, ob die
    Lampe rot oder blau leuchtet, nicht um Farbmetrik.
    """
    if v < 0.10:
        return "aus"
    if s < 0.18:
        return "weiss"
    h = h % 360
    if h < 15 or h >= 345:
        return "rot"
    if h < 45:
        return "orange"
    if h < 70:
        return "gelb"
    if h < 160:
        return "gruen"
    if h < 200:
        return "cyan"
    if h < 260:
        return "blau"
    if h < 290:
        return "violett"
    return "magenta"


def farbe_messen(ausschnitt):
    """Die Farbe eines Bildausschnitts bestimmen.

    Ein einfacher Mittelwert taugt hier nicht: Um eine leuchtende Lampe
    herum ist viel dunkler Hintergrund, der die Farbe zu Grau zieht.
    Deshalb zaehlen nur die hellsten Bildpunkte - das ist die Lampe
    selbst.

    Erwartet ein BGR-Feld (so liefert OpenCV Bilder).
    """
    import numpy as np

    if ausschnitt is None or ausschnitt.size == 0:
        return None

    punkte = ausschnitt.reshape(-1, 3).astype("float32")
    helligkeit = punkte.max(axis=1)

    # Nur was mindestens halb so hell ist wie der hellste Punkt. Ein
    # fester Prozentsatz taugt nicht: Wie viel Platz die Lampe im
    # Messfeld einnimmt, weiss man vorher nicht. Eine kleine helle Lampe
    # vor viel Dunkelheit wuerde bei "die obersten 20 Prozent" trotzdem
    # weggemittelt - der Selbsttest hat genau das aufgedeckt.
    hellster = float(helligkeit.max())
    if hellster >= 10:
        auswahl = punkte[helligkeit >= hellster * 0.5]
    else:
        auswahl = punkte          # praktisch dunkel: nichts herauszupicken
    if len(auswahl) == 0:
        auswahl = punkte

    # Uebersteuerte Punkte tragen keine Farbe mehr: Laufen alle drei
    # Kanaele an den Anschlag, ist jede Farbe weiss - deshalb hielt die
    # Messung eine lila Lampe fuer weiss. Die Kamera laesst sich nicht
    # dunkler stellen (die Webcam nimmt die Einstellung nicht an,
    # nachgemessen am 22.08.2026), also werden die ausgebrannten Punkte
    # uebergangen. Am Rand der Leuchte steht die Farbe noch drin.
    brauchbar = auswahl[(auswahl < 250).any(axis=1)]
    if len(brauchbar) >= max(10, int(len(auswahl) * 0.05)):
        auswahl = brauchbar

    b, g, r = (float(x) for x in auswahl.mean(axis=0))

    hoch = max(r, g, b)
    tief = min(r, g, b)
    spanne = hoch - tief

    if spanne == 0:
        h = 0.0
    elif hoch == r:
        h = (60 * ((g - b) / spanne)) % 360
    elif hoch == g:
        h = 60 * ((b - r) / spanne) + 120
    else:
        h = 60 * ((r - g) / spanne) + 240

    s = 0.0 if hoch == 0 else spanne / hoch
    v = hoch / 255.0

    # Wie viele Bildpunkte kleben am oberen Anschlag? Ein uebersteuerter
    # Sensor verliert die Farbe: Alle drei Kanaele laufen auf 255, und
    # was in Wirklichkeit kraeftig lila leuchtet, misst sich als weiss.
    # Mexla hat genau das an der Lampe bemerkt. Ein Messwert, der das
    # verschweigt, ist schlimmer als gar keiner - deshalb steht es dabei.
    anschlag = float((auswahl >= 250).all(axis=1).mean())

    ergebnis = {
        "rot": round(r, 1), "gruen": round(g, 1), "blau": round(b, 1),
        "farbton": round(h, 1), "saettigung": round(s, 3), "helligkeit": round(v, 3),
        "name": farbname(h, s, v),
        "hex": "#%02x%02x%02x" % (min(255, int(r)), min(255, int(g)), min(255, int(b))),
        "anschlag": round(anschlag, 3),
        "ueberbelichtet": anschlag > 0.25,
    }
    if ergebnis["ueberbelichtet"]:
        ergebnis["hinweis"] = ("Kamera uebersteuert (%.0f%% am Anschlag) - der "
                               "Farbton ist unzuverlaessig, die Lampe wirkt "
                               "blasser als sie ist" % (anschlag * 100))
    return ergebnis


def feld_grenzen(bild, feld=None):
    """Ein Messfeld in Bildpunkten - anteilig auf die Bildgroesse.

    Ohne Angabe das Hauptfeld, damit alle bisherigen Aufrufe gelten.
    """
    feld = messfeld if feld is None else feld
    hoehe, breite = bild.shape[:2]
    x = int(feld["x"] * breite)
    y = int(feld["y"] * hoehe)
    b = max(1, int(feld["breite"] * breite))
    h = max(1, int(feld["hoehe"] * hoehe))
    x = max(0, min(x, breite - 1))
    y = max(0, min(y, hoehe - 1))
    b = min(b, breite - x)
    h = min(h, hoehe - y)
    return x, y, b, h


def aktuelle_messung():
    with _bild_sperre:
        bild = None if _bild is None else _bild.copy()
        alter = time.time() - _bild_zeit
    if bild is None:
        return {"fehler": _kamera_fehler or "noch kein Bild von der Kamera"}
    x, y, b, h = feld_grenzen(bild)
    messwert = farbe_messen(bild[y:y + h, x:x + b])
    messwert["bildalter_s"] = round(alter, 2)
    messwert["messfeld"] = dict(messfeld)
    # Die Werte oben bleiben, was sie waren: kamera_cli, der
    # Sprachassistent und die Zentrale lesen genau diese Schluessel.
    # Die Aufschluesselung je Feld kommt additiv daneben - wer nur eine
    # Lampe kennt, merkt von der Neuerung nichts.
    messwert["felder"] = []
    for nr, feld in enumerate(messfelder):
        fx, fy, fb, fh = feld_grenzen(bild, feld)
        einzeln = farbe_messen(bild[fy:fy + fh, fx:fx + fb]) or {}
        einzeln["nr"] = nr
        einzeln["messfeld"] = dict(feld)
        if feld.get("raum"):
            einzeln["raum"] = feld["raum"]
        messwert["felder"].append(einzeln)
    return messwert


# ----------------------------------------------------------------------
# Kamera
# ----------------------------------------------------------------------

def kamera_schleife():
    global _bild, _bild_zeit, _kamera_fehler
    import cv2

    kamera = cv2.VideoCapture(0)
    if not kamera.isOpened():
        _kamera_fehler = ("Kamera laesst sich nicht oeffnen - fehlt die "
                          "Kameraerlaubnis fuer dieses Programm?")
        print("FEHLER " + _kamera_fehler, flush=True)
        return

    # Automatik abschalten, soweit die Kamera mitspielt: Regelt sie die
    # Belichtung selbst nach, wird eine hellere Lampe wieder dunkel
    # gerechnet - dann misst man die Regelung statt der Lampe.
    for eigenschaft, wert in ((cv2.CAP_PROP_AUTO_EXPOSURE, 0.25),
                              (cv2.CAP_PROP_AUTO_WB, 0)):
        try:
            kamera.set(eigenschaft, wert)
        except Exception:
            pass

    global _kamera
    _kamera = kamera

    print("Kamera offen: %dx%d" % (kamera.get(cv2.CAP_PROP_FRAME_WIDTH),
                                   kamera.get(cv2.CAP_PROP_FRAME_HEIGHT)), flush=True)
    _kamera_fehler = ""

    def _automatik_aus(kam):
        for eigenschaft, wert in ((cv2.CAP_PROP_AUTO_EXPOSURE, 0.25),
                                  (cv2.CAP_PROP_AUTO_WB, 0)):
            try:
                kam.set(eigenschaft, wert)
            except Exception:
                pass

    # Selbstheilen (05.09.2026): Bis heute drehte diese Schleife bei
    # ok=False ewig mit `continue` - sie oeffnete die Kamera NIE neu und
    # setzte KEINEN Fehler. Faellt kamera.read() dauerhaft aus (macOS nach
    # Ruhezustand, USB-Haenger), wuchs `bildalter_s` unbegrenzt, waehrend
    # `kamera_fehler` leer blieb: der Dienst lebte und arbeitete nicht -
    # genau der Zustand der Nacht auf den 05.09. (Auge 8,8 h eingefroren,
    # der Waechter musste ihn von aussen neu starten). Jetzt meldet die
    # Schleife den Stillstand ehrlich und oeffnet die Kamera selbst neu.
    STALL_GRENZE_S = 5.0        # so lange darf read() am Stueck scheitern,
                                # bevor die Kamera als haengend gilt
    WIEDER_TAKT_S = 5.0         # Mindestabstand zwischen Neuoeffnungs-Versuchen
    fehl_seit = None            # Beginn der laufenden Fehlerserie
    letzter_versuch = 0.0

    while _laeuft:
        ok, bild = kamera.read()
        if not ok or bild is None:
            jetzt = time.time()
            if fehl_seit is None:
                fehl_seit = jetzt
            # Ein einzelnes verworfenes Bild ist normal - erst laengerer
            # Stillstand ist ein Fehler, der gemeldet und geheilt wird.
            if jetzt - fehl_seit > STALL_GRENZE_S:
                _kamera_fehler = (
                    "Kamera liefert seit %.0f s kein Bild - versuche neu "
                    "zu oeffnen." % (jetzt - fehl_seit))
                if jetzt - letzter_versuch > WIEDER_TAKT_S:
                    letzter_versuch = jetzt
                    try:
                        kamera.release()
                    except Exception:
                        pass
                    kamera = cv2.VideoCapture(0)
                    if kamera.isOpened():
                        _automatik_aus(kamera)
                        globals()["_kamera"] = kamera
                        print("Kamera nach Stillstand neu geoeffnet.",
                              flush=True)
            time.sleep(0.1)
            continue
        # Bild da: war die Kamera vorher haengen geblieben, ist sie geheilt.
        if fehl_seit is not None:
            fehl_seit = None
            _kamera_fehler = ""
        with _bild_sperre:
            globals()["_bild"] = bild
            globals()["_bild_zeit"] = time.time()
        time.sleep(0.02)

    kamera.release()


def auge_schleife():
    """Solange das Auge an ist: regelmaessig schauen und glaetten.

    Laeuft in einem eigenen Faden, damit ein langsamer Blick weder das
    Livebild noch die Farbmessung aufhaelt. Faellt die Objekterkennung
    aus (kein Modell, kein ultralytics), schaltet sich das Auge selbst
    ab und hinterlaesst den Grund - besser als stumm im Kreis zu laufen.
    """
    global _auge_letzte, _auge_zeit, _auge_dauer, _auge_fehler, _auge_an
    global _auge_gedaechtnis

    try:
        import objekterkennung
    except Exception as fehler:                       # noqa: BLE001
        _auge_fehler = "Objekterkennung nicht ladbar: %s" % fehler
        _auge_an = False
        return

    if _auge_gedaechtnis is None:
        _auge_gedaechtnis = objekterkennung.Gedaechtnis()

    while _auge_an and _laeuft:
        bild = rohbild()
        if bild is None:
            time.sleep(0.3)
            continue
        begonnen = time.time()
        try:
            funde = objekterkennung.erkenne(bild)
            geglaettet = _auge_gedaechtnis.aufnehmen(funde)
            _auge_fehler = ""
        except Exception as fehler:                   # noqa: BLE001
            _auge_fehler = str(fehler)
            _auge_an = False
            return
        with _auge_sperre:
            _auge_letzte = geglaettet
            _auge_zeit = time.time()
            _auge_dauer = _auge_zeit - begonnen
        time.sleep(AUGE_TAKT)


# Der An/Aus-Zustand des Auges ueberlebt Neustarts: Sonst schaltet
# jeder Dienst-Neustart (Update, Reboot, launchd) die Erkennung still
# ab, und die gruenen Kaesten verschwinden, ohne dass jemand "aus"
# gedrueckt haette.
AUGE_DATEI = Path(__file__).resolve().parent / "auge.json"


def auge_an_lesen(datei=None):
    """Der gemerkte An/Aus-Stand - None, wenn nichts gemerkt ist."""
    try:
        gemerkt = json.loads(Path(datei or AUGE_DATEI).read_text(encoding="utf-8"))
        return bool(gemerkt["an"]) if "an" in gemerkt else None
    except Exception:
        return None


def auge_an_merken(an, datei=None):
    try:
        Path(datei or AUGE_DATEI).write_text(
            json.dumps({"an": bool(an)}) + "\n", encoding="utf-8")
        return True
    except Exception:
        return False


def auge_schalten(an):
    """Das Auge an- oder ausschalten. Gibt den neuen Zustand zurueck."""
    global _auge_an, _auge_letzte, _auge_gedaechtnis, _auge_fehler
    an = bool(an)
    auge_an_merken(an)
    if an == _auge_an:
        return auge_zustand()
    _auge_an = an
    if an:
        _auge_fehler = ""
        threading.Thread(target=auge_schleife, daemon=True).start()
    else:
        # Beim Ausschalten das Gedaechtnis leeren: Was Tim beim naechsten
        # Einschalten zeigt, soll von jetzt stammen, nicht von vorhin.
        if _auge_gedaechtnis is not None:
            _auge_gedaechtnis.leeren()
        with _auge_sperre:
            _auge_letzte = []
    return auge_zustand()


def auge_zustand():
    """Was das Auge gerade sieht - fuer die Anzeige in Tim."""
    try:
        import objekterkennung
        auskunft = objekterkennung.modell_auskunft()
    except Exception as fehler:                       # noqa: BLE001
        auskunft = {"modell": "-", "fehler": str(fehler)}
    with _auge_sperre:
        gesehen = list(_auge_letzte)
        zeit, dauer = _auge_zeit, _auge_dauer
    with _bild_sperre:
        bild_zeit = _bild_zeit
    return {
        "an": _auge_an,
        # Ob die Kamera wirklich liefert, sieht man am Bildalter - ein
        # Dienst ohne Kameraerlaubnis laeuft, hat aber nie ein Bild.
        "kamera_fehler": _kamera_fehler,
        "bildalter_s": round(time.time() - bild_zeit, 1) if bild_zeit else None,
        "gesehen": gesehen,
        "anzahl": len(gesehen),
        "zuletzt_vor_s": round(time.time() - zeit, 1) if zeit else None,
        "dauer_s": round(dauer, 2),
        "takt_s": AUGE_TAKT,
        "fehler": _auge_fehler,
        "anzeigen": dict(anzeigen),
        **auskunft,
    }


def bild_mit_rahmen():
    """Das Livebild mit eingezeichnetem Messfeld und Messwert."""
    import cv2

    with _bild_sperre:
        bild = None if _bild is None else _bild.copy()
    if bild is None:
        return None

    # Erkanntes zuerst, damit der gelbe Messrahmen obenauf liegt: Das
    # Messfeld ist das, worauf es bei der Farbmessung ankommt, und es
    # soll von keinem Objektkasten verdeckt werden.
    hoehe_b, breite_b = bild.shape[:2]
    with _auge_sperre:
        gesehen = list(_auge_letzte) if anzeigen.get("objekte", True) else []
    for fund in gesehen:
        fx, fy, fb, fh = fund["kasten"]
        links, oben = int(fx * breite_b), int(fy * hoehe_b)
        rechts, unten = int((fx + fb) * breite_b), int((fy + fh) * hoehe_b)
        farbe = (80, 220, 80)
        gr = float(anzeigen.get("textgroesse", 1.0))
        cv2.rectangle(bild, (links, oben), (rechts, unten), farbe, 2)
        text = "%s %.0f%%" % (fund.get("deutsch") or fund["name"],
                              fund["vertrauen"] * 100)
        y_text = max(int(18 * gr), oben - 6)
        cv2.putText(bild, text, (links, y_text),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55 * gr, (0, 0, 0),
                    max(3, round(2.5 * gr)))
        cv2.putText(bild, text, (links, y_text),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55 * gr, farbe,
                    max(1, round(1.2 * gr)))

    if anzeigen.get("messfeld", True):
        for _nr, _feld in enumerate(messfelder):
            x, y, b, h = feld_grenzen(bild, _feld)
            messwert = farbe_messen(bild[y:y + h, x:x + b])
            cv2.rectangle(bild, (x, y), (x + b, y + h), (0, 255, 255), 2)
            if messwert:
                gr = float(anzeigen.get("textgroesse", 1.0))
                # Der Raumname steht vorn, sobald er gemessen wurde - er
                # ist beim Hinsehen die wichtigere Auskunft als der Farbton.
                vorn = _feld.get("raum") or ("Feld %d" % (_nr + 1)
                                             if len(messfelder) > 1 else "")
                beschriftung = "%s%s  H%.0f S%.2f V%.2f" % (
                    (vorn + ": ") if vorn else "",
                    messwert["name"], messwert["farbton"],
                    messwert["saettigung"], messwert["helligkeit"])
                y_text = max(int(20 * gr), y - 8)
                cv2.putText(bild, beschriftung, (x, y_text),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6 * gr, (0, 0, 0),
                            max(3, round(2.5 * gr)))
                cv2.putText(bild, beschriftung, (x, y_text),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.6 * gr, (0, 255, 255),
                            max(1, round(1.2 * gr)))

    ok, puffer = cv2.imencode(".jpg", bild, [cv2.IMWRITE_JPEG_QUALITY, 80])
    return puffer.tobytes() if ok else None


def felder_aus_leuchten(bild, leuchten):
    """Aus den Leuchten-Kaesten des Modells die Messfelder machen.

    Das Modell SCHLAEGT VOR, die Helligkeitssuche ENTSCHEIDET: In jedem
    vorgeschlagenen Kasten wird nach der hellsten Stelle gesucht; ist
    dort nichts Helles, entsteht kein Feld. Darum darf der Vorschlag
    ruhig unsicher sein - ein Fehlvorschlag faellt hier von selbst raus.

    Gemessen am 31.08.2026 an Mexlas Buero: Das Modell meldete den einen
    Deckenspot mit 0.351, den zweiten nur mit 0.098. Beide sind echt -
    die Helligkeitsanalyse fand unabhaengig genau diese zwei Flecken.
    Eine feste Schwelle von 0.12 warf den zweiten weg, und Tim sah
    weiterhin nur eine Lampe.
    """
    gefunden = []
    for kasten in leuchten:
        feld = lampe_im_bild(bild, eingrenzen=kasten)
        if feld and not _feld_bekannt(feld, gefunden):
            gefunden.append(feld)
    return gefunden


def lampe_suchen(bild_aus, bild_an, rand=0.06):
    """Das Messfeld dorthin legen, wo sich beim Einschalten etwas aendert.

    Ein von Hand gesetztes Rechteck erfasst fast immer zu viel Raum
    daneben - dann misst man halb die Wand mit, und "aus" sieht kaum
    dunkler aus als "an". Der Unterschied zwischen einem Bild mit
    ausgeschalteter und einem mit eingeschalteter Lampe zeigt dagegen
    genau eines: die Lampe.

    Erwartet zwei BGR-Bilder gleicher Groesse, gibt das gefundene
    Messfeld anteilig zurueck (oder None).
    """
    import cv2
    import numpy as np

    if bild_aus is None or bild_an is None:
        return None
    if bild_aus.shape != bild_an.shape:
        return None

    aus = cv2.cvtColor(bild_aus, cv2.COLOR_BGR2GRAY).astype("int16")
    an = cv2.cvtColor(bild_an, cv2.COLOR_BGR2GRAY).astype("int16")
    unterschied = np.clip(an - aus, 0, 255).astype("uint8")
    unterschied = cv2.GaussianBlur(unterschied, (21, 21), 0)

    staerkste = int(unterschied.max())
    if staerkste < 12:
        return None               # nichts hat sich sichtbar geaendert

    # Alles, was mindestens halb so stark aufgehellt wurde wie die
    # hellste Stelle, zaehlt zur Lampe.
    _, maske = cv2.threshold(unterschied, staerkste // 2, 255, cv2.THRESH_BINARY)
    punkte = cv2.findNonZero(maske)
    if punkte is None:
        return None

    x, y, breite, hoehe = cv2.boundingRect(punkte)
    bildhoehe, bildbreite = maske.shape[:2]

    # Etwas Rand abziehen: Am Saum der Lampe mischt sich schon die Wand
    # dazu, und die verfaelscht den Farbton.
    schrumpf_x = int(breite * rand)
    schrumpf_y = int(hoehe * rand)
    x += schrumpf_x
    y += schrumpf_y
    breite = max(1, breite - 2 * schrumpf_x)
    hoehe = max(1, hoehe - 2 * schrumpf_y)

    return {"x": x / bildbreite, "y": y / bildhoehe,
            "breite": breite / bildbreite, "hoehe": hoehe / bildhoehe}


def lampe_im_bild(bild, kernanteil=0.7, eingrenzen=None, rand=0.05):
    """Siehe unten - `eingrenzen` beschraenkt die Suche auf einen Kasten.

    Der Kasten kommt von der Objekterkennung: Sieht das Modell eine
    leuchtende Lampe, wird nur dort nach der hellsten Stelle gesucht.
    Grund: "Die Lampe ist immer das Hellste im Bild" stimmt nur im
    dunklen Zimmer. Ist das grosse Licht an, ist eine angestrahlte
    weisse Wand heller als die farbige Leuchte - am 23.08.2026 sass das
    Messfeld deshalb auf der Wand ueber der Tuer. Innerhalb des
    Lampenkastens dagegen ist die hellste Stelle wirklich die Lampe.
    """
    if bild is not None and eingrenzen is not None:
        hoehe, breite = bild.shape[:2]
        x, y, b, h = eingrenzen
        # Etwas Rand dazu, falls das Modell knapp geschnitten hat.
        links = max(0, int((x - rand) * breite))
        oben = max(0, int((y - rand) * hoehe))
        rechts = min(breite, int((x + b + rand) * breite))
        unten = min(hoehe, int((y + h + rand) * hoehe))
        if rechts - links < 4 or unten - oben < 4:
            return None
        feld = _lampe_im_bild(bild[oben:unten, links:rechts], kernanteil)
        if feld is None:
            return None
        # Zurueck in Koordinaten des ganzen Bilds.
        ab, ah = (rechts - links) / breite, (unten - oben) / hoehe
        return {"x": links / breite + feld["x"] * ab,
                "y": oben / hoehe + feld["y"] * ah,
                "breite": feld["breite"] * ab,
                "hoehe": feld["hoehe"] * ah}
    return _lampe_im_bild(bild, kernanteil)


def _lampe_im_bild(bild, kernanteil=0.7):
    """Die Leuchte in einem einzelnen Bild finden - ohne sie zu schalten.

    Das feste Rechteck in der Bildmitte war der eigentliche Schwachpunkt:
    Steht jemand davor, misst es die Hand statt die Lampe. Am 22.08.2026
    sah Mexla genau das - Saettigung 0.27, weil vier Fuenftel des Messfelds
    seine Handflaeche waren.

    Eine leuchtende Lampe hat ein sehr eindeutiges Merkmal: Sie ist die
    hellste Stelle im Bild, und zwar mit Abstand. Angestrahlte Haut oder
    Wand ist heller als der Hintergrund, aber nie so hell wie die
    Lichtquelle selbst. Deshalb wird nach den hellsten paar Prozent
    gesucht und davon der groesste zusammenhaengende Fleck genommen -
    einzelne helle Spritzer (Spiegelungen, Rauschen) fallen so heraus.

    Die Schwelle richtet sich nach dem hellsten Punkt im Bild, nicht nach
    einem festen Bruchteil der Bildflaeche. Der Grund steht in Mexlas
    Wohnung: Die Kamera schaut durch einen Tuerspalt in einen
    beleuchteten Raum. Nimmt man "die hellsten vier Prozent", faengt man
    den ganzen Spalt ein - angestrahlte Wand, Boden, einen Waeschekorb -
    und misst am Ende beige Wand statt Lampe. Wie viel Wand im Bild ist,
    weiss man vorher nicht; wie hell die Lampe im Verhaeltnis zu ihr ist,
    schon: Sie ist immer das Hellste.

    `kernanteil` ist der Bruchteil der Spitzenhelligkeit, ab dem ein
    Punkt zur Lampe zaehlt.
    """
    import cv2
    import numpy as np

    if bild is None or bild.size == 0:
        return None

    grau = cv2.cvtColor(bild, cv2.COLOR_BGR2GRAY)
    grau = cv2.GaussianBlur(grau, (9, 9), 0)

    # Die Schwelle ergibt sich aus dem Bild selbst, nicht aus einer festen
    # Zahl: Wie hell "hell" ist, haengt von Raum und Belichtung ab.
    hellster = float(grau.max())
    if hellster < 60:
        return None                    # nirgends leuchtet etwas
    schwelle = max(40.0, hellster * kernanteil)

    _, maske = cv2.threshold(grau, schwelle, 255, cv2.THRESH_BINARY)
    maske = cv2.morphologyEx(maske, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))

    konturen, _ = cv2.findContours(maske, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not konturen:
        return None

    groesste = max(konturen, key=cv2.contourArea)
    if cv2.contourArea(groesste) < 25:
        return None                    # zu klein, um eine Lampe zu sein

    x, y, breite, hoehe = cv2.boundingRect(groesste)
    bildhoehe, bildbreite = grau.shape[:2]
    return {"x": x / bildbreite, "y": y / bildhoehe,
            "breite": max(1, breite) / bildbreite,
            "hoehe": max(1, hoehe) / bildhoehe}


def _stoerer_namen():
    try:
        import objekterkennung
        return objekterkennung.STOERER
    except Exception:
        return set()


def rohbild():
    """Das aktuelle Bild ohne Beschriftung - fuer Vergleichsmessungen."""
    with _bild_sperre:
        return None if _bild is None else _bild.copy()


SEITE = """<!doctype html><html lang="de"><head><meta charset="utf-8">
<title>Tims Auge</title><style>
body{background:#14161a;color:#e8e8ea;font-family:-apple-system,system-ui,sans-serif;
margin:0;padding:16px}
h1{font-size:17px;font-weight:600;margin:0 0 12px}
img{max-width:100%;border-radius:10px;display:block;background:#000}
.werte{margin-top:12px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.klecks{width:42px;height:42px;border-radius:8px;border:1px solid #444}
.zahl{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:13px;color:#b9bcc4}
.name{font-size:22px;font-weight:600}
</style></head><body>
<h1>Tims Auge &mdash; Messfeld ist der gelbe Rahmen</h1>
<img src="/strom.mjpg" alt="Livebild">
<div class="werte">
  <div class="klecks" id="klecks"></div>
  <div><div class="name" id="name">&mdash;</div><div class="zahl" id="zahl"></div></div>
</div>
<script>
setInterval(async () => {
  try {
    const m = await (await fetch('/messung')).json();
    if (m.fehler) { document.getElementById('name').textContent = m.fehler; return; }
    document.getElementById('name').textContent = m.name;
    document.getElementById('klecks').style.background = m.hex;
    document.getElementById('zahl').textContent =
      `RGB ${m.rot|0} ${m.gruen|0} ${m.blau|0}  ·  Farbton ${m.farbton}°  ·  ` +
      `Sättigung ${m.saettigung}  ·  Helligkeit ${m.helligkeit}`;
  } catch (e) {}
}, 400);
</script></body></html>"""


class Anfrage(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass                      # sonst eine Zeile je Bild im Protokoll

    def _kopf(self, typ, code=200):
        self.send_response(code)
        self.send_header("Content-Type", typ)
        self.end_headers()

    def do_GET(self):
        pfad = self.path.split("?")[0]

        if pfad == "/":
            self._kopf("text/html; charset=utf-8")
            self.wfile.write(SEITE.encode("utf-8"))

        elif pfad == "/messung":
            self._kopf("application/json; charset=utf-8")
            self.wfile.write(json.dumps(aktuelle_messung()).encode("utf-8"))

        elif pfad == "/bild.jpg":
            daten = bild_mit_rahmen()
            if daten is None:
                self._kopf("text/plain; charset=utf-8", 503)
                self.wfile.write(b"noch kein Bild")
                return
            self._kopf("image/jpeg")
            self.wfile.write(daten)

        elif pfad == "/strom.mjpg":
            self.send_response(200)
            self.send_header("Content-Type",
                             "multipart/x-mixed-replace; boundary=bild")
            self.end_headers()
            try:
                while _laeuft:
                    daten = bild_mit_rahmen()
                    if daten:
                        self.wfile.write(b"--bild\r\nContent-Type: image/jpeg\r\n"
                                         b"Content-Length: %d\r\n\r\n" % len(daten))
                        self.wfile.write(daten)
                        self.wfile.write(b"\r\n")
                    time.sleep(0.07)
            except (BrokenPipeError, ConnectionResetError):
                pass              # Browser zugemacht, das ist normal

        elif pfad == "/rohbild.jpg":
            # Ohne Beschriftung: Der eingezeichnete Rahmen wuerde beim
            # Bildvergleich als Unterschied mitgezaehlt.
            import cv2
            bild = rohbild()
            if bild is None:
                self._kopf("text/plain; charset=utf-8", 503)
                self.wfile.write(b"noch kein Bild")
                return
            ok, puffer = cv2.imencode(".jpg", bild, [cv2.IMWRITE_JPEG_QUALITY, 92])
            self._kopf("image/jpeg")
            self.wfile.write(puffer.tobytes())

        elif pfad == "/lampe_suchen":
            bild = rohbild()
            # Erst Menschen aus dem Bild nehmen, dann die Leuchte suchen.
            # Wer direkt vor der Lampe steht, ist selbst ausgebrannt und
            # wuerde sonst fuer sie gehalten. Faellt die Objekterkennung
            # aus, wird trotzdem gesucht - lieber ungeschuetzt suchen als
            # gar nicht.
            gesehen = []
            leuchten = []
            if bild is not None:
                try:
                    import objekterkennung
                    # Niedrige Schwelle: Die Leuchten-Kandidaten liegen
                    # oft unter 0.25 (nachgemessen: 0.156 bei roter
                    # Lampe). stoerer_kaesten und leuchten_kaesten
                    # filtern selbst, jeder mit seiner eigenen Schwelle.
                    # 0.05 statt 0.12: Das ist die Schwelle fuer
                    # VORSCHLAEGE, nicht fuer Funde. Was davon ein Feld
                    # wird, entscheidet felder_aus_leuchten() an der
                    # Helligkeit - ein Vorschlag ohne helle Stelle
                    # verschwindet dort folgenlos.
                    gesehen = objekterkennung.erkenne(bild, mindestens=0.05)
                    leuchten = objekterkennung.leuchten_kaesten(
                        gesehen, mindestens=0.06)
                    kaesten = objekterkennung.stoerer_kaesten(gesehen)
                    # Waere fast das ganze Bild weg, bringt Ausblenden
                    # nichts mehr - dann bliebe nichts zum Suchen uebrig.
                    if kaesten and objekterkennung.anteil_geschwaerzt(kaesten) < 0.8:
                        bild = objekterkennung.ausblenden(bild, kaesten)
                except Exception:
                    pass
            # Jede erkannte Leuchte bekommt ihr EIGENES Feld. Bis zum
            # 31.08.2026 brach diese Schleife beim ersten Treffer ab -
            # das Auge sah dann zwar zwei Lampen, mass aber nur eine.
            # Findet das Modell gar keine Leuchte, wird wie bisher im
            # ganzen Bild nach der hellsten Stelle gesucht.
            gefunden = []
            eingegrenzt = False
            if bild is not None:
                gefunden = felder_aus_leuchten(bild, leuchten)
                eingegrenzt = bool(gefunden)
                if not gefunden:
                    feld = lampe_im_bild(bild)
                    if feld:
                        gefunden.append(feld)
            if gefunden:
                # Schon gemessene Raumnamen retten: Wer die Zuordnung
                # einmal erarbeitet hat, soll sie nicht verlieren, nur
                # weil die Lampensuche erneut lief.
                felder_setzen(felder_zusammenfuehren(gefunden, messfelder))
                messfeld_sichern()
                antwort = {"gefunden": True, "messfeld": dict(messfeld),
                           "gemerkt": True,
                           "anzahl": len(messfelder),
                           "felder": [dict(f) for f in messfelder],
                           "leuchte_vom_modell": eingegrenzt,
                           "ausgeblendet": [f["name"] for f in gesehen
                                            if f["name"] in _stoerer_namen()]}
            else:
                antwort = {"gefunden": False,
                           "grund": "keine deutlich hellste Stelle im Bild"}
            self._kopf("application/json; charset=utf-8")
            self.wfile.write(json.dumps(antwort).encode("utf-8"))

        elif pfad == "/auge":
            # An- und Ausschalten der Objekterkennung. Ohne Argument nur
            # nachfragen, was gerade Sache ist.
            from urllib.parse import parse_qs, urlparse
            werte = parse_qs(urlparse(self.path).query)
            if "an" in werte:
                antwort = auge_schalten(werte["an"][0] not in ("0", "aus", "nein"))
            else:
                antwort = auge_zustand()
            self._kopf("application/json; charset=utf-8")
            self.wfile.write(json.dumps(antwort, ensure_ascii=False).encode("utf-8"))

        elif pfad == "/anzeigen":
            # Einblendungen im Livebild schalten. Ohne Argument nur lesen.
            from urllib.parse import parse_qs, urlparse
            werte = parse_qs(urlparse(self.path).query)
            geaendert = False
            for name in anzeigen:
                if name in werte:
                    anzeigen[name] = _anzeigewert(name, werte[name][0])
                    geaendert = True
            if geaendert:
                anzeigen_sichern()
            self._kopf("application/json; charset=utf-8")
            self.wfile.write(json.dumps(dict(anzeigen)).encode("utf-8"))

        elif pfad == "/objekte":
            # Ist das Auge an, kommt die ueber mehrere Bilder geglaettete
            # Liste - die ist verlaesslich. Ist es aus, wird einmal
            # nachgeschaut, damit ein einzelner Abruf trotzdem etwas
            # liefert; das Ergebnis ist dann aber ungeglaettet und wird
            # als solches gekennzeichnet.
            from urllib.parse import parse_qs, urlparse
            werte = parse_qs(urlparse(self.path).query)
            if _auge_an and "sofort" not in werte:
                antwort = auge_zustand()
                antwort["geglaettet"] = True
            else:
                bild = rohbild()
                if bild is None:
                    self._kopf("application/json; charset=utf-8", 503)
                    self.wfile.write(json.dumps({"fehler": "noch kein Bild"}).encode())
                    return
                try:
                    import objekterkennung
                    funde = [f for f in objekterkennung.erkenne(bild)
                             if f["vertrauen"] >= objekterkennung.SCHWELLE_MELDEN]
                    antwort = {"an": _auge_an, "gesehen": funde,
                               "anzahl": len(funde), "geglaettet": False}
                except Exception as fehler:
                    antwort = {"fehler": "Objekterkennung nicht verfuegbar: %s" % fehler}
            self._kopf("application/json; charset=utf-8")
            self.wfile.write(json.dumps(antwort, ensure_ascii=False).encode("utf-8"))

        elif pfad == "/belichtung":
            # Gegen die Uebersteuerung: dunkler stellen, bis die Lampe
            # ihre Farbe zeigt. Ob die Kamera es annimmt, sagt sie selbst -
            # viele USB-Kameras ignorieren solche Wuensche unter macOS
            # stillschweigend, deshalb wird der Wert zurueckgelesen.
            import cv2
            from urllib.parse import parse_qs, urlparse
            werte = parse_qs(urlparse(self.path).query)
            antwort = {}
            if _kamera is not None:
                if "wert" in werte:
                    try:
                        gewuenscht = float(werte["wert"][0])
                        _kamera.set(cv2.CAP_PROP_EXPOSURE, gewuenscht)
                        antwort["gewuenscht"] = gewuenscht
                    except (ValueError, Exception):
                        pass
                antwort["belichtung"] = _kamera.get(cv2.CAP_PROP_EXPOSURE)
                antwort["automatik"] = _kamera.get(cv2.CAP_PROP_AUTO_EXPOSURE)
                if "gewuenscht" in antwort:
                    antwort["angenommen"] = (
                        abs(antwort["belichtung"] - antwort["gewuenscht"]) < 0.51)
            else:
                antwort["fehler"] = "keine Kamera offen"
            self._kopf("application/json; charset=utf-8")
            self.wfile.write(json.dumps(antwort).encode("utf-8"))

        elif pfad == "/messfeld":
            from urllib.parse import parse_qs, urlparse
            werte = parse_qs(urlparse(self.path).query)
            # Ohne Angabe immer Feld 0 - so bedeutet ein Aufruf ohne
            # "nr" genau das, was er vor dem Umbau bedeutet hat.
            nummer = 0
            if "nr" in werte:
                try:
                    nummer = int(werte["nr"][0])
                except ValueError:
                    nummer = 0
            # "neu=1" haengt ein Feld an, statt eines zu aendern. Das
            # braucht der Zuordnungslauf: Steht eine Lampe nicht selbst
            # im Bild, sondern beleuchtet nur eine Wand, gibt es dort
            # noch kein Feld - wohl aber eine Stelle, die sich beim
            # Schalten aendert. Genau die wird hier eingetragen.
            if werte.get("neu"):
                messfelder.append({"x": 0.4, "y": 0.4,
                                   "breite": 0.1, "hoehe": 0.1})
                nummer = len(messfelder) - 1
            ziel = messfelder[nummer] if 0 <= nummer < len(messfelder) else messfeld
            for name in ("x", "y", "breite", "hoehe"):
                if name in werte:
                    try:
                        ziel[name] = max(0.0, min(1.0, float(werte[name][0])))
                    except ValueError:
                        pass
            if werte.get("raum"):
                ziel["raum"] = werte["raum"][0][:40]
            messfeld_sichern()
            antwort = dict(ziel)
            antwort["nr"] = messfelder.index(ziel) if ziel in messfelder else 0
            antwort["anzahl"] = len(messfelder)
            self._kopf("application/json; charset=utf-8")
            self.wfile.write(json.dumps(antwort).encode("utf-8"))

        else:
            self._kopf("text/plain; charset=utf-8", 404)
            self.wfile.write(b"unbekannte Adresse")


# ----------------------------------------------------------------------
# Selbsttest - prueft die Farblogik, ohne die Kamera anzufassen
# ----------------------------------------------------------------------

def selbsttest():
    import numpy as np
    fehler = 0

    def pruefe(bedingung, text, zusatz=""):
        nonlocal fehler
        if bedingung:
            print("  ok      %s" % text)
        else:
            print("  FEHLER  %s%s" % (text, "  [%s]" % zusatz if zusatz else ""))
            fehler += 1

    print("Kameradienst Selbsttest (Farblogik, ohne Kamera):")

    def flaeche(b, g, r, groesse=40):
        feld = np.zeros((groesse, groesse, 3), dtype="uint8")
        feld[:, :] = (b, g, r)
        return feld

    # OpenCV liefert BGR - wer das verwechselt, haelt Rot fuer Blau.
    rot = farbe_messen(flaeche(0, 0, 255))
    pruefe(rot["name"] == "rot", "reines Rot wird als rot erkannt", str(rot))
    pruefe(rot["rot"] > 200 and rot["blau"] < 50,
           "BGR wird nicht mit RGB verwechselt", str(rot))

    pruefe(farbe_messen(flaeche(255, 0, 0))["name"] == "blau", "Blau wird erkannt")
    pruefe(farbe_messen(flaeche(0, 255, 0))["name"] == "gruen", "Gruen wird erkannt")
    pruefe(farbe_messen(flaeche(0, 255, 255))["name"] == "gelb", "Gelb wird erkannt")
    pruefe(farbe_messen(flaeche(255, 255, 255))["name"] == "weiss", "Weiss wird erkannt")
    pruefe(farbe_messen(flaeche(0, 0, 0))["name"] == "aus", "Dunkel gilt als aus")

    # Der eigentliche Trick: eine kleine helle Lampe vor viel Dunkelheit.
    # Ein schlichter Mittelwert wuerde hier "aus" oder "grau" sagen.
    szene = np.zeros((40, 40, 3), dtype="uint8")
    szene[18:22, 18:22] = (0, 0, 255)
    gemessen = farbe_messen(szene)
    pruefe(gemessen["name"] == "rot",
           "kleine helle Lampe vor dunklem Grund wird erkannt", str(gemessen))

    mittelwert = szene.reshape(-1, 3).mean(axis=0)
    pruefe(mittelwert[2] < 30,
           "Gegenprobe: der schlichte Mittelwert waere hier zu dunkel",
           "Mittelwert rot=%.1f" % mittelwert[2])

    # Randfaelle duerfen nicht stuerzen.
    pruefe(farbe_messen(None) is None, "leere Eingabe ergibt kein Ergebnis")
    pruefe(farbe_messen(np.zeros((0, 0, 3), dtype="uint8")) is None,
           "Bild ohne Punkte ergibt kein Ergebnis")

    # Messfeld darf nie ueber den Bildrand hinauszeigen.
    messfeld.update({"x": 0.95, "y": 0.95, "breite": 0.50, "hoehe": 0.50})
    x, y, b, h = feld_grenzen(np.zeros((100, 200, 3), dtype="uint8"))
    pruefe(x + b <= 200 and y + h <= 100,
           "Messfeld bleibt im Bild, auch wenn es ueber den Rand geschoben wird",
           "x=%d b=%d y=%d h=%d" % (x, b, y, h))
    messfeld.update({"x": 0.40, "y": 0.35, "breite": 0.20, "hoehe": 0.20})

    # --- Mehrere Messfelder (seit 31.08.2026) --------------------------
    #
    # Der Zweck: Zwei Lampen im Bild muessen EINZELN messbar sein, sonst
    # laesst sich nicht herausfinden, welche zu welchem Raum gehoert.
    # Zugleich darf nichts brechen, was nur ein Feld kennt.
    print("\n  Mehrere Messfelder:")
    gesichert = [dict(f) for f in messfelder]

    hauptfeld_vorher = messfeld            # dieselbe Referenz wie draussen
    felder_setzen([{"x": 0.10, "y": 0.10, "breite": 0.10, "hoehe": 0.10},
                   {"x": 0.60, "y": 0.60, "breite": 0.10, "hoehe": 0.10}])
    pruefe(len(messfelder) == 2, "zwei Leuchten ergeben zwei Felder",
           str(len(messfelder)))
    pruefe(messfeld is hauptfeld_vorher and messfeld is messfelder[0],
           "das Hauptfeld bleibt DASSELBE Objekt - sonst zeigen "
           "kamera_cli und der Sprachassistent stumm auf eine alte Kopie")
    pruefe(abs(messfeld["x"] - 0.10) < 1e-9,
           "und traegt die Werte des ersten Feldes", str(messfeld))

    # Zwei Felder muessen an verschiedenen Stellen messen.
    szene = np.zeros((100, 200, 3), dtype="uint8")
    szene[10:20, 20:40] = (0, 0, 255)      # links rot
    szene[60:70, 120:140] = (255, 0, 0)    # rechts blau
    links = feld_grenzen(szene, messfelder[0])
    rechts = feld_grenzen(szene, messfelder[1])
    pruefe(links != rechts, "die Felder schauen an verschiedene Stellen",
           "%s / %s" % (links, rechts))
    x, y, b, h = links
    x2, y2, b2, h2 = rechts
    pruefe(farbe_messen(szene[y:y + h, x:x + b])["name"]
           != farbe_messen(szene[y2:y2 + h2, x2:x2 + b2])["name"],
           "und melden verschiedene Farben")

    # Dieselbe Leuchte darf nicht zweimal gezaehlt werden.
    eins = {"x": 0.10, "y": 0.10, "breite": 0.10, "hoehe": 0.10}
    fast_gleich = {"x": 0.11, "y": 0.11, "breite": 0.10, "hoehe": 0.10}
    weit_weg = {"x": 0.70, "y": 0.70, "breite": 0.10, "hoehe": 0.10}
    pruefe(_feld_bekannt(fast_gleich, [eins]),
           "zwei Kaesten auf derselben Lampe ergeben EIN Feld",
           "%.2f" % _ueberlappung(fast_gleich, eins))
    pruefe(not _feld_bekannt(weit_weg, [eins]),
           "zwei Lampen an verschiedenen Orten bleiben zwei Felder")

    # Raumnamen: gemessen wird selten, gemerkt wird dauerhaft.
    messfelder[1]["raum"] = "flur"
    pruefe(aktuelle_messung().get("fehler") or True, "Messung stuerzt nicht ab")

    # Dateiformat: Das ALTE Einzelobjekt muss lesbar bleiben.
    import tempfile
    with tempfile.TemporaryDirectory() as ordner:
        probe = Path(ordner) / "messfeld.json"
        echt = globals()["MESSFELD_DATEI"]
        globals()["MESSFELD_DATEI"] = probe
        try:
            probe.write_text('{"x": 0.3, "y": 0.4, "breite": 0.1, '
                             '"hoehe": 0.2}', encoding="utf-8")
            pruefe(messfeld_laden() and len(messfelder) == 1
                   and abs(messfeld["x"] - 0.3) < 1e-9,
                   "eine alte messfeld.json mit EINEM Feld wird weiter "
                   "gelesen", str(messfelder))
            felder_setzen([{"x": 0.1, "y": 0.1, "breite": 0.1, "hoehe": 0.1},
                           {"x": 0.5, "y": 0.5, "breite": 0.1, "hoehe": 0.1,
                            "raum": "buero"}])
            messfeld_sichern()
            felder_setzen([{"x": 0.9, "y": 0.9, "breite": 0.1, "hoehe": 0.1}])
            pruefe(messfeld_laden() and len(messfelder) == 2
                   and messfelder[1].get("raum") == "buero",
                   "gespeicherte Felder samt Raumnamen kommen zurueck",
                   str(messfelder))
            probe.write_text('[{"x": 0.1, "y": 0.1, "breite": 0.1, '
                             '"hoehe": 0.1}, {"kaputt": true}]',
                             encoding="utf-8")
            pruefe(messfeld_laden() and len(messfelder) == 1,
                   "ein unbrauchbarer Eintrag wird verworfen, der gute "
                   "bleibt", str(messfelder))
            probe.write_text("kein json", encoding="utf-8")
            vorher = [dict(f) for f in messfelder]
            pruefe(not messfeld_laden()
                   and [dict(f) for f in messfelder] == vorher,
                   "eine kaputte Datei aendert gar nichts")
        finally:
            globals()["MESSFELD_DATEI"] = echt

    # Zwei Leuchten im Bild muessen ZWEI Felder ergeben - das ist der
    # ganze Zweck. Der Fall ist Mexlas Buero nachgebaut: zwei
    # Deckenspots, einer davon vom Modell nur schwach vorgeschlagen.
    zimmer = np.full((120, 240, 3), 20, dtype="uint8")
    zimmer[20:34, 30:52] = (250, 250, 250)      # Spot links
    zimmer[20:34, 160:182] = (250, 250, 250)    # Spot rechts
    vorschlaege = [(0.08, 0.10, 0.20, 0.25), (0.60, 0.10, 0.20, 0.25)]
    zwei = felder_aus_leuchten(zimmer, vorschlaege)
    pruefe(len(zwei) == 2, "zwei Leuchten ergeben zwei Messfelder",
           str(zwei))
    pruefe(zwei[0]["x"] < 0.5 < zwei[1]["x"],
           "und sie sitzen auf verschiedenen Lampen", str(zwei))

    # Ein Vorschlag ohne helle Stelle darf kein Feld werden - genau
    # deshalb darf die Vorschlagsschwelle niedrig sein.
    dunkel = felder_aus_leuchten(zimmer, [(0.30, 0.60, 0.20, 0.25)])
    pruefe(dunkel == [],
           "ein Vorschlag ins Dunkle ergibt kein Feld - die Helligkeit "
           "entscheidet, nicht das Modell", str(dunkel))

    # Zwei Vorschlaege auf DERSELBEN Lampe (lamp + light bulb) ergeben eins.
    doppelt = felder_aus_leuchten(zimmer, [(0.08, 0.10, 0.20, 0.25),
                                           (0.09, 0.11, 0.20, 0.25)])
    pruefe(len(doppelt) == 1,
           "zwei Vorschlaege auf derselben Lampe ergeben EIN Feld",
           str(len(doppelt)))

    # Raumnamen ueberleben eine erneute Lampensuche - auch wenn der
    # Spot dabei anders zugeschnitten wird. Genau das ging am
    # 31.08.2026 im Betrieb verloren.
    alt_felder = [{"x": 0.64, "y": 0.08, "breite": 0.04, "hoehe": 0.05,
                   "raum": "buero"}]
    # Verschoben und anders zugeschnitten: Die Flaechen decken sich nur
    # zu einem Drittel, die Mitten liegen aber dicht beieinander.
    neu_gefunden = [{"x": 0.665, "y": 0.095, "breite": 0.04, "hoehe": 0.05}]
    ueberdeckung = _ueberlappung(neu_gefunden[0], alt_felder[0])
    raumnamen_uebernehmen(neu_gefunden, alt_felder)
    pruefe(neu_gefunden[0].get("raum") == "buero",
           "ein verschobener, anders zugeschnittener Spot behaelt seinen "
           "gemessenen Raumnamen", str(neu_gefunden[0]))
    pruefe(ueberdeckung < 0.5,
           "und zwar OBWOHL die Flaechen die alte 0.5-Huerde reissen - "
           "die Mitte entscheidet, nicht die Flaeche",
           "%.2f" % ueberdeckung)
    weit = [{"x": 0.10, "y": 0.80, "breite": 0.02, "hoehe": 0.03}]
    raumnamen_uebernehmen(weit, alt_felder)
    pruefe("raum" not in weit[0],
           "eine ganz andere Lampe erbt den Namen NICHT", str(weit[0]))

    # Eine ausgeschaltete Lampe darf ihre Zuordnung nicht verlieren.
    # Nachgestellt: Die Suche findet nur das Buero, der Flur ist dunkel.
    felder_setzen([{"x": 0.35, "y": 0.10, "breite": 0.05, "hoehe": 0.04,
                    "raum": "buero"},
                   {"x": 0.40, "y": 0.52, "breite": 0.02, "hoehe": 0.03,
                    "raum": "flur"},
                   {"x": 0.80, "y": 0.80, "breite": 0.02, "hoehe": 0.02}])
    nur_buero = [{"x": 0.352, "y": 0.101, "breite": 0.05, "hoehe": 0.04}]
    felder_setzen(felder_zusammenfuehren(nur_buero, messfelder))
    raeume = sorted(f.get("raum", "") for f in messfelder)
    pruefe(raeume == ["buero", "flur"],
           "eine gerade dunkle Lampe behaelt ihr Feld und ihren Raum",
           str(raeume))
    pruefe(len(messfelder) == 2,
           "das namenlose Feld faellt dabei weg - es ist billig zu "
           "ersetzen, ein gemessener Raum nicht", str(len(messfelder)))

    felder_setzen(gesichert)

    # --- Uebersteuerung ---
    # Eine Flaeche am Anschlag muss als solche gemeldet werden, sonst
    # haelt man ein ausgebranntes Bild fuer eine weisse Lampe.
    voll = farbe_messen(flaeche(255, 255, 255))
    pruefe(voll["ueberbelichtet"] is True and voll["anschlag"] > 0.9,
           "ausgebranntes Weiss wird als uebersteuert gemeldet", str(voll))
    pruefe("hinweis" in voll, "und es steht ein Hinweis dabei")

    # Eine ausgebrannte Mitte mit farbigem Rand: Der Farbton muss aus dem
    # Rand kommen, sonst meldet die Messung "weiss" fuer eine bunte Lampe.
    leuchte = np.zeros((30, 30, 3), dtype="uint8")
    leuchte[8:22, 8:22] = (200, 60, 200)      # violetter Rand
    leuchte[12:18, 12:18] = (255, 255, 255)   # ausgebrannte Mitte
    gemessen = farbe_messen(leuchte)
    pruefe(gemessen["saettigung"] > 0.25,
           "Farbe wird aus dem nicht ausgebrannten Rand gelesen", str(gemessen))
    pruefe(gemessen["name"] in ("violett", "magenta"),
           "die ausgebrannte Mitte macht die Lampe nicht weiss", gemessen["name"])

    massvoll = farbe_messen(flaeche(0, 0, 200))
    pruefe(massvoll["ueberbelichtet"] is False,
           "ein massvoll belichtetes Rot gilt nicht als uebersteuert",
           str(massvoll))
    pruefe("hinweis" not in massvoll, "und traegt keinen Hinweis")

    # --- Lampe in EINEM Bild finden (Mexlas Fall vom 22.08.2026) ---
    # Nachgebaut: dunkler Raum, eine helle Leuchte rechts, davor eine
    # angestrahlte Hand. Die Hand ist heller als der Hintergrund - aber
    # eben nicht so hell wie die Lampe. Genau das muss die Suche trennen.
    szene = np.full((120, 160, 3), 30, dtype="uint8")
    szene[30:90, 20:70] = (110, 140, 170)      # Hand, angestrahlt
    szene[20:100, 120:134] = (250, 250, 250)   # die Leuchte selbst
    # Eine Spiegelung unten links - auf Mexlas Bild spiegelt der Schrank
    # daneben. Sie ist genauso hell wie die Lampe, aber deutlich kleiner
    # (Flaeche 121 gegen 511, nachgemessen). Ohne sie waere der Test
    # wertlos: Bei nur einem hellen Fleck ist "der groesste" zufaellig
    # auch "der erste". Sie liegt bewusst unten, weil OpenCV die
    # Konturen von unten nach oben liefert - sie kommt also zuerst, und
    # wer nicht nach der Groesse auswaehlt, greift daneben. Genau diese
    # Luecke hat der Mutationstest aufgedeckt.
    szene[100:118, 6:24] = (250, 250, 250)
    feld = lampe_im_bild(szene)
    pruefe(feld is not None, "Lampe wird im Einzelbild gefunden")
    if feld:
        mitte_x = feld["x"] + feld["breite"] / 2
        pruefe(mitte_x > 0.65,
               "gefunden wird die Lampe rechts, nicht die Hand links",
               "Mitte x=%.2f" % mitte_x)
        pruefe(feld["breite"] < 0.35,
               "das Messfeld umfasst die Lampe, nicht die halbe Szene",
               "Breite=%.2f" % feld["breite"])

    # Ohne Lichtquelle darf nichts behauptet werden.
    pruefe(lampe_im_bild(np.full((60, 80, 3), 20, dtype="uint8")) is None,
           "im dunklen Bild wird keine Lampe behauptet")
    pruefe(lampe_im_bild(None) is None, "kein Bild ergibt kein Ergebnis")

    # --- Eingrenzen auf den Lampenkasten (Mexlas Fall vom 23.08.2026) ---
    # Zimmerlicht an: Die weisse Wand oben ist ausgebrannt und damit
    # heller als die farbige Leuchte rechts. Ohne Eingrenzung landet die
    # Suche auf der Wand; mit dem Kasten vom Modell auf der Lampe.
    tag = np.full((120, 160, 3), 50, dtype="uint8")
    tag[0:30, 40:150] = (255, 255, 255)        # ausgebrannte Wand oben
    tag[60:90, 120:140] = (235, 180, 235)      # die violette Leuchte
    frei = lampe_im_bild(tag)
    pruefe(frei is not None and frei["y"] + frei["hoehe"] / 2 < 0.35,
           "ohne Eingrenzung gewinnt die helle Wand (der bekannte Fehler)")
    kasten = (0.70, 0.45, 0.20, 0.35)          # wo das Modell die Lampe sieht
    begrenzt = lampe_im_bild(tag, eingrenzen=kasten)
    pruefe(begrenzt is not None, "mit Lampenkasten wird etwas gefunden")
    if begrenzt:
        pruefe(begrenzt["x"] + begrenzt["breite"] / 2 > 0.65
               and begrenzt["y"] + begrenzt["hoehe"] / 2 > 0.45,
               "gefunden wird die Leuchte im Kasten, nicht die Wand",
               "Mitte x=%.2f y=%.2f" % (begrenzt["x"] + begrenzt["breite"] / 2,
                                        begrenzt["y"] + begrenzt["hoehe"] / 2))
        pruefe(begrenzt["breite"] <= kasten[2] + 0.12
               and begrenzt["hoehe"] <= kasten[3] + 0.12,
               "das Ergebnis bleibt im Rahmen des Kastens")
    # Ist im Kasten nichts Helles, darf nichts behauptet werden - der
    # Aufrufer soll dann aufs ganze Bild zurueckfallen.
    pruefe(lampe_im_bild(tag, eingrenzen=(0.0, 0.6, 0.2, 0.3)) is None,
           "ein leerer Kasten liefert nichts statt Unsinn")
    pruefe(lampe_im_bild(tag, eingrenzen=(0.5, 0.5, 0.001, 0.001), rand=0) is None,
           "ein winziger Kasten wird abgewiesen")

    # --- Lampensuche per Bildvergleich ---
    dunkel = np.full((60, 80, 3), 40, dtype="uint8")
    hell = dunkel.copy()
    hell[20:32, 50:66] = 240              # eine helle Stelle rechts unten
    feld = lampe_suchen(dunkel, hell)
    pruefe(feld is not None, "Lampe wird im Bildvergleich gefunden")
    if feld:
        mitte_x = feld["x"] + feld["breite"] / 2
        mitte_y = feld["y"] + feld["hoehe"] / 2
        pruefe(0.60 < mitte_x < 0.85,
               "Messfeld sitzt waagerecht auf der Lampe", "Mitte x=%.2f" % mitte_x)
        pruefe(0.30 < mitte_y < 0.60,
               "Messfeld sitzt senkrecht auf der Lampe", "Mitte y=%.2f" % mitte_y)
        pruefe(feld["breite"] < 0.5 and feld["hoehe"] < 0.5,
               "Messfeld umschliesst nur die Lampe, nicht das halbe Bild",
               "b=%.2f h=%.2f" % (feld["breite"], feld["hoehe"]))

    # Ohne Unterschied darf nichts behauptet werden.
    pruefe(lampe_suchen(dunkel, dunkel.copy()) is None,
           "ohne sichtbare Aenderung wird keine Lampe behauptet")
    pruefe(lampe_suchen(None, hell) is None, "fehlendes Bild ergibt kein Ergebnis")
    pruefe(lampe_suchen(dunkel, np.zeros((10, 10, 3), dtype="uint8")) is None,
           "Bilder verschiedener Groesse werden abgewiesen")

    # --- Auge-Zustand ueberlebt Neustarts ---
    import tempfile
    print("\n  Auge-Zustand:")
    with tempfile.TemporaryDirectory() as ordner:
        probe = Path(ordner) / "auge.json"
        pruefe(auge_an_lesen(probe) is None,
               "ohne Datei ist nichts gemerkt (Auge bleibt aus)")
        pruefe(auge_an_merken(True, probe) and auge_an_lesen(probe) is True,
               "'an' ueberlebt einen Neustart")
        pruefe(auge_an_merken(False, probe) and auge_an_lesen(probe) is False,
               "'aus' ebenso")
        probe.write_text("kaputt{", encoding="utf-8")
        pruefe(auge_an_lesen(probe) is None,
               "eine kaputte Datei schaltet nichts von selbst ein")

    # --- Anzeigen-Schalter (Einblendungen im Livebild) ---
    print("\n  Anzeigen-Schalter:")
    vorher = dict(anzeigen)
    try:
        with tempfile.TemporaryDirectory() as ordner:
            probe = Path(ordner) / "anzeigen.json"
            anzeigen["messfeld"] = False
            anzeigen["objekte"] = True
            pruefe(anzeigen_sichern(probe), "Schalterstand laesst sich sichern")
            anzeigen["messfeld"] = True
            pruefe(anzeigen_laden(probe) and anzeigen["messfeld"] is False,
                   "und ueberlebt einen Neustart", str(anzeigen))
            pruefe(anzeigen["objekte"] is True,
                   "die anderen Schalter bleiben unberuehrt")
            probe.write_text('{"messfeld": "kaputt"', encoding="utf-8")
            anzeigen["messfeld"] = True
            pruefe(not anzeigen_laden(probe) and anzeigen["messfeld"] is True,
                   "eine kaputte Datei aendert nichts am Stand")
            probe.write_text('{"messfeld": false, "quatsch": true}',
                             encoding="utf-8")
            anzeigen_laden(probe)
            pruefe("quatsch" not in anzeigen,
                   "unbekannte Eintraege werden nicht zu Schaltern")
            anzeigen["textgroesse"] = 2.0
            anzeigen_sichern(probe)
            anzeigen["textgroesse"] = 1.0
            pruefe(anzeigen_laden(probe) and anzeigen["textgroesse"] == 2.0,
                   "die Textgroesse ueberlebt einen Neustart")
            probe.write_text('{"textgroesse": 99}', encoding="utf-8")
            anzeigen_laden(probe)
            pruefe(anzeigen["textgroesse"] == TEXTGROESSE_MAX,
                   "eine absurde Textgroesse wird eingefangen",
                   str(anzeigen["textgroesse"]))
            probe.write_text('{"textgroesse": "quark"}', encoding="utf-8")
            vorher_gr = anzeigen["textgroesse"]
            anzeigen_laden(probe)
            pruefe(anzeigen["textgroesse"] == vorher_gr,
                   "Unsinn statt Zahl aendert nichts")
    finally:
        anzeigen.clear()
        anzeigen.update(vorher)

    if fehler:
        print("\n%d Fehler." % fehler)
    return fehler


def main(argumente):
    global _laeuft
    if argumente and argumente[0] == "--selbsttest":
        return selbsttest()

    if messfeld_laden():
        print("Messfeld aus %s uebernommen: %s" % (MESSFELD_DATEI.name, messfeld),
              flush=True)
    if anzeigen_laden():
        print("Anzeigen aus %s uebernommen: %s" % (ANZEIGEN_DATEI.name, anzeigen),
              flush=True)

    # Die Kameraerlaubnis einmal vom HAUPTFADEN aus anfordern: macOS
    # zeigt den Erlaubnis-Dialog nur, wenn die Anfrage aus dem Hauptlauf
    # des Programms kommt. Aus dem Nebenfaden meldet OpenCV nur "can not
    # spin main run loop from other thread" und gibt auf - genau daran
    # scheiterte der erste Start als launchd-Dienst am 23.08.2026
    # (status 0 = noch nie gefragt, nicht etwa verboten). Beim ersten
    # Mal blockiert diese Zeile, bis am Mac auf "Erlauben" geklickt
    # wurde; danach ist sie ein folgenloses Auf und Zu.
    try:
        import cv2
        print("Pruefe Kameraerlaubnis (beim ersten Mal fragt macOS) ...",
              flush=True)
        probe = cv2.VideoCapture(0)
        if probe.isOpened():
            print("Kameraerlaubnis vorhanden.", flush=True)
        else:
            print("Kamera nicht zugaenglich - Erlaubnis fehlt oder Kamera "
                  "belegt. Der Dienst laeuft weiter und meldet es unter "
                  "/messung.", flush=True)
        probe.release()
    except Exception as fehler:                       # noqa: BLE001
        print("Erlaubnispruefung fehlgeschlagen: %s" % fehler, flush=True)

    threading.Thread(target=kamera_schleife, daemon=True).start()

    # War das Auge vor dem Neustart an, geht es von selbst wieder an.
    if auge_an_lesen():
        print("Auge war an - schalte wieder ein.", flush=True)
        auge_schalten(True)
    server = ThreadingHTTPServer((ADRESSE, PORT), Anfrage)
    print("Kameradienst laeuft: http://%s:%d/" % (ADRESSE, PORT), flush=True)
    print("Beenden mit Strg-C", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nbeendet")
    finally:
        _laeuft = False
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
