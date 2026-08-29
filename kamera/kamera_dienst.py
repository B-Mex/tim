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


def messfeld_laden():
    try:
        gemerkt = json.loads(MESSFELD_DATEI.read_text(encoding="utf-8"))
        if all(k in gemerkt for k in ("x", "y", "breite", "hoehe")):
            messfeld.update({k: float(gemerkt[k]) for k in
                             ("x", "y", "breite", "hoehe")})
            return True
    except Exception:
        pass
    return False


def messfeld_sichern():
    try:
        MESSFELD_DATEI.write_text(json.dumps(messfeld, indent=2) + "\n",
                                  encoding="utf-8")
        return True
    except Exception:
        return False


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


def feld_grenzen(bild):
    """Das Messfeld in Bildpunkten - anteilig auf die Bildgroesse."""
    hoehe, breite = bild.shape[:2]
    x = int(messfeld["x"] * breite)
    y = int(messfeld["y"] * hoehe)
    b = max(1, int(messfeld["breite"] * breite))
    h = max(1, int(messfeld["hoehe"] * hoehe))
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

    while _laeuft:
        ok, bild = kamera.read()
        if not ok or bild is None:
            time.sleep(0.05)
            continue
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
        x, y, b, h = feld_grenzen(bild)
        messwert = farbe_messen(bild[y:y + h, x:x + b])
        cv2.rectangle(bild, (x, y), (x + b, y + h), (0, 255, 255), 2)
        if messwert:
            gr = float(anzeigen.get("textgroesse", 1.0))
            beschriftung = "%s  H%.0f S%.2f V%.2f" % (
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
                    gesehen = objekterkennung.erkenne(bild, mindestens=0.12)
                    leuchten = objekterkennung.leuchten_kaesten(gesehen)
                    kaesten = objekterkennung.stoerer_kaesten(gesehen)
                    # Waere fast das ganze Bild weg, bringt Ausblenden
                    # nichts mehr - dann bliebe nichts zum Suchen uebrig.
                    if kaesten and objekterkennung.anteil_geschwaerzt(kaesten) < 0.8:
                        bild = objekterkennung.ausblenden(bild, kaesten)
                except Exception:
                    pass
            # Sieht das Modell eine Leuchte, wird zuerst nur dort
            # gesucht; sonst (oder wenn dort nichts Helles ist) im
            # ganzen Bild, wie bisher.
            feld = None
            eingegrenzt = False
            if bild is not None:
                for kasten in leuchten:
                    feld = lampe_im_bild(bild, eingrenzen=kasten)
                    if feld:
                        eingegrenzt = True
                        break
                if feld is None:
                    feld = lampe_im_bild(bild)
            if feld:
                messfeld.update(feld)
                messfeld_sichern()
                antwort = {"gefunden": True, "messfeld": dict(messfeld),
                           "gemerkt": True,
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
            for name in ("x", "y", "breite", "hoehe"):
                if name in werte:
                    try:
                        messfeld[name] = max(0.0, min(1.0, float(werte[name][0])))
                    except ValueError:
                        pass
            messfeld_sichern()
            self._kopf("application/json; charset=utf-8")
            self.wfile.write(json.dumps(dict(messfeld)).encode("utf-8"))

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
