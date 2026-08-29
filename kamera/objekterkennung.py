#!/usr/bin/env python3
#
# Lizenz: GNU AGPL-3.0 - siehe kamera/LICENSE. ABWEICHEND vom uebrigen
# Projekt (MIT), weil diese Datei Ultralytics einbindet, das unter
# AGPL-3.0 steht. Begruendung und Auswirkungen: kamera/README.md
#
"""Objekte im Kamerabild erkennen - was Tim sieht, und was die Lampensuche stoert.

Zwei Aufgaben, die verschieden streng sein muessen:

1. **Melden**, was im Bild ist (fuer die Anzeige in Tim). Hier ist ein
   Fehlalarm laestig: Tim behauptet dann Dinge, die nicht da sind.
   Also strenge Schwelle und zusaetzlich zeitliche Glaettung.
2. **Ausblenden**, was der Lampensuche im Weg steht. Hier ist ein
   uebersehener Mensch schlimmer als ein zu grosszuegiger schwarzer
   Fleck. Also lockere Schwelle, kein Warten auf Bestaetigung.

Deshalb hat jede der beiden Aufgaben ihre eigene Schwelle.

## Warum YOLOE und nicht mehr yolo26n

Am 23.08.2026 nachgemessen an Mexlas echtem Zimmer (dunkel, verrauscht,
Fischauge). yolo26n und alle COCO-Modelle kennen nur 80 feste Dinge und
fanden davon in diesem Zimmer fast keines - meist gar nichts, dafuer
regelmaessig eine "person" auf dem dunklen Sitzball rechts.

YOLOE ist **open-vocabulary**: Es bekommt eine Liste von Begriffen in
Worten und sucht danach. Dieselbe Aufnahme, dieselbe Sekunde:

    yolo26n   nichts
    tim_auge  person 0.97, chair 0.77, shelf 0.67, door 0.42,
              laundry rack 0.39, desk 0.32

Die Begriffe stehen in `begriffe.json` und sind frei erweiterbar. Sie
sind ins Modell `tim_auge.pt` **eingebacken**: Ihre Wort-Vektoren wurden
einmal berechnet und mitgespeichert. Darum braucht der Start weder CLIP
noch Netz - wichtig fuer einen Rechner, der ohne Netz laufen soll. Wer
die Liste aendert, muss das Modell einmal neu backen:

    objekterkennung.py --begriffe-neu        (braucht einmalig Netz)

Der prompt-freie Modus von YOLOE (4585 Klassen ohne Vorgabe) wurde
verworfen: Er lieferte am selben Bild mit hohem Vertrauen "veterinarians
office 0.55", "elevator door 0.60" und "dinosaur" - Unsinn, den Tim
nicht weitererzaehlen soll.

Modell: YOLOE-11L (Ultralytics, AGPL-3.0). Fuer den Hausgebrauch im
eigenen Netz unbedenklich; wer die Anlage oeffentlich anbietet, braucht
eine Lizenz oder ein Modell unter Apache-2.0 (z.B. RF-DETR).

Selbsttest: objekterkennung.py --selbsttest
"""

import json
import sys
import time
from collections import deque
from pathlib import Path

# Neben dieser Datei, mit vollem Pfad: Wird der Dienst aus einem anderen
# Ordner gestartet, wuerde ein blosser Dateiname ins Leere zeigen - und
# das Modell stillschweigend neu aus dem Netz geladen. Auf einem Rechner,
# der bewusst ohne Netz arbeiten soll, ist das keine Kleinigkeit.
HIER = Path(__file__).resolve().parent
MODELL = HIER / "tim_auge.pt"              # YOLOE mit eingebackenen Begriffen
MODELL_ERSATZ = HIER / "yolo26n.pt"        # falls das grosse fehlt
BEGRIFFE_DATEI = HIER / "begriffe.json"

# Ab hier gilt ein Fund als vorzeigbar bzw. als Stoerer. Auseinander,
# weil die beiden Aufgaben verschieden empfindlich sind (siehe oben).
# Melden war anfangs 0.40 - im dunklen Zimmer sass Mexla damit als
# "person 0.38" knapp drunter und blieb unsichtbar. Ueber vier echte
# Dunkelbilder gemessen: person stabil 0.34-0.42, dabei kein einziger
# Fehlfund bei 0.25. Die zeitliche Glaettung (3 von 5 Bildern) faengt
# ab, was eine niedrigere Rohschwelle an Rauschen durchlaesst.
SCHWELLE_MELDEN = 0.32
SCHWELLE_AUSBLENDEN = 0.25

# Bildgroesse fuer die Erkennung. 640 war im Vergleich nicht nur doppelt
# so schnell wie 960, sondern beim Hauptmotiv auch treffsicherer
# (person 0.97 statt 0.91). Groesser hilft nur bei kleinen Dingen weit
# hinten - dafuer gibt es genau() unten.
BILDGROESSE = 640

# Was uns beim Blick auf eine Lampe im Weg stehen kann.
STOERER = {"person", "face", "hand", "cat", "dog", "teddy bear", "bird"}

# Was selbst leuchtet. Wird bei der Lampensuche bevorzugt: Die
# Helligkeitssuche allein greift daneben, sobald im Zimmer das grosse
# Licht an ist - dann ist eine angestrahlte weisse Wand heller als die
# farbige Lampe (am 23.08.2026 genau so passiert: Messfeld sass auf der
# Wand ueber der Tuer statt auf der violetten Leuchte im Flur).
LEUCHTEN = {"lamp", "ceiling light", "glowing light", "light bulb", "candle"}

# Fuer die Anzeige. Was hier fehlt, wird unveraendert durchgereicht -
# lieber ein englisches Wort als gar keins.
DEUTSCH = {
    "person": "Mensch", "face": "Gesicht", "hand": "Hand",
    "cat": "Katze", "dog": "Hund",
    "lamp": "Lampe", "ceiling light": "Deckenlampe",
    "glowing light": "leuchtende Lampe", "light bulb": "Gluehbirne",
    "candle": "Kerze",
    "cabinet": "Schrank", "wardrobe": "Kleiderschrank", "shelf": "Regal",
    "drawer": "Schublade", "mirror": "Spiegel", "doorway": "Tuerdurchgang",
    "door": "Tuer", "window": "Fenster", "curtain": "Vorhang",
    "chair": "Stuhl", "desk": "Schreibtisch", "table": "Tisch",
    "bed": "Bett", "mattress": "Matratze", "sofa": "Sofa",
    "laundry rack": "Waeschestaender", "basket": "Korb",
    "cardboard box": "Karton", "suitcase": "Koffer",
    "toilet": "Toilette", "sink": "Waschbecken",
    "exercise ball": "Gymnastikball", "pillow": "Kissen",
    "blanket": "Decke", "clothes": "Kleidung", "shoe": "Schuh",
    "towel": "Handtuch",
    "monitor": "Bildschirm", "laptop": "Laptop", "keyboard": "Tastatur",
    "phone": "Telefon", "remote control": "Fernbedienung",
    "speaker": "Lautsprecher", "camera": "Kamera", "cable": "Kabel",
    "bottle": "Flasche", "cup": "Tasse", "glass": "Glas",
    "plate": "Teller", "bowl": "Schuessel", "food": "Essen",
    "plant": "Pflanze", "book": "Buch", "paper": "Papier",
    "bag": "Tasche", "clock": "Uhr", "picture": "Bild",
}

_modell = None
_modell_fehler = ""
_modell_name = ""
_geraet = ""


def geraet_waehlen(mps_da=None):
    """Wo gerechnet wird: Apple-GPU wenn vorhanden, sonst CPU.

    Ultralytics nimmt von sich aus nie die Apple-GPU - ohne
    ausdrueckliches device= landet jeder Blick auf der CPU. Am
    27.08.2026 an zwei echten Szenen aus dem Buero nachgemessen:
    CPU 0.196 s je Blick, MPS 0.038 s - die Funde sind bis auf die
    dritte Nachkommastelle identisch. Darum wird die GPU angefordert,
    sobald torch sie kennt. torch wird erst hier importiert, damit
    der Import dieser Datei leicht bleibt (siehe modell_laden).
    """
    if mps_da is None:
        import torch
        mps_da = torch.backends.mps.is_available()
    return "mps" if mps_da else "cpu"


def geraet():
    """Die einmal getroffene Geraetewahl merken.

    torch wird nur beim allerersten Blick gefragt; danach steht die
    Antwort fest, denn die GPU kommt zur Laufzeit weder dazu noch weg.
    """
    global _geraet
    if not _geraet:
        _geraet = geraet_waehlen()
    return _geraet


def begriffe_lesen():
    """Die Wortliste, nach der gesucht wird."""
    try:
        return json.loads(BEGRIFFE_DATEI.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def deutsch(name):
    return DEUTSCH.get(name, name)


def modell_laden(pfad=None):
    """Das Modell einmal laden und behalten.

    Erst beim ersten Bedarf, nicht beim Import: Der Kameradienst soll
    auch dann starten, wenn kein Modell da ist - dann eben ohne
    Objekterkennung, statt gar nicht.
    """
    global _modell, _modell_fehler, _modell_name
    if _modell is not None or _modell_fehler:
        return _modell

    kandidaten = [Path(pfad)] if pfad else [MODELL, MODELL_ERSATZ]
    fehler = []
    for kandidat in kandidaten:
        if not kandidat.exists():
            fehler.append("%s fehlt" % kandidat.name)
            continue
        try:
            # YOLOE-Gewichte brauchen die YOLOE-Klasse; die alten
            # COCO-Modelle die schlichte YOLO. Am Dateinamen zu
            # entscheiden ist haesslich, aber verlaesslicher als ein
            # Fehlversuch mit halb geladenem Modell.
            if "yoloe" in kandidat.name or kandidat.name == "tim_auge.pt":
                from ultralytics import YOLOE as Lader
            else:
                from ultralytics import YOLO as Lader
            _modell = Lader(str(kandidat))
            _modell_name = kandidat.name
            return _modell
        except Exception as f:                        # noqa: BLE001
            fehler.append("%s: %s" % (kandidat.name, f))
    _modell_fehler = "; ".join(fehler)
    return None


def modell_auskunft():
    """Womit wird gerade geschaut? Fuer die Anzeige in Tim."""
    modell_laden()
    return {
        "modell": _modell_name or "-",
        "geraet": _geraet or "-",
        "fehler": _modell_fehler,
        "begriffe": len(begriffe_lesen()),
        "schwelle_melden": SCHWELLE_MELDEN,
        "schwelle_ausblenden": SCHWELLE_AUSBLENDEN,
    }


def erkenne(bild, mindestens=SCHWELLE_AUSBLENDEN, genau=False):
    """Was ist im Bild? Liste aus Fundstuecken.

    Der Kasten ist anteilig zur Bildgroesse: (x, y, breite, hoehe).
    `genau=True` schaut in doppelter Aufloesung nach - langsamer, aber
    besser bei kleinen Dingen weit hinten im Raum.
    """
    modell = modell_laden()
    if modell is None or bild is None:
        return []

    hoehe, breite = bild.shape[:2]
    funde = []
    for ergebnis in modell.predict(bild, verbose=False, conf=mindestens,
                                   imgsz=BILDGROESSE * (2 if genau else 1),
                                   device=geraet()):
        for kasten in ergebnis.boxes:
            name = ergebnis.names[int(kasten.cls[0])]
            x1, y1, x2, y2 = (float(v) for v in kasten.xyxy[0])
            funde.append({
                "name": name,
                "deutsch": deutsch(name),
                "vertrauen": round(float(kasten.conf[0]), 3),
                "kasten": (x1 / breite, y1 / hoehe,
                           (x2 - x1) / breite, (y2 - y1) / hoehe),
            })
    funde.sort(key=lambda f: -f["vertrauen"])
    return funde


# ----------------------------------------------------------------------
# Zeitliche Glaettung
#
# Ein einzelnes Bild dieser Webcam ist verrauscht; Funde am Rand der
# Sicherheit flackern von Bild zu Bild. Was wirklich im Zimmer steht,
# steht auch im naechsten Bild noch da. Deshalb wird nur gemeldet, was
# sich mehrfach gezeigt hat - und einmal Gemeldetes verschwindet nicht
# sofort wieder, wenn ein Bild es mal verfehlt.
# ----------------------------------------------------------------------

class Gedaechtnis:
    """Merkt sich die letzten Bilder und meldet nur Bestaendiges."""

    def __init__(self, fenster=5, noetig=3, haltbar_s=8.0):
        self.fenster = fenster              # so viele Bilder werden behalten
        self.noetig = noetig                # so oft muss ein Ding vorkommen
        self.haltbar_s = haltbar_s          # so lange gilt ein Fund nach
        self.bilder = deque(maxlen=fenster)
        self.zuletzt = {}                   # Name -> (Zeit, bester Fund)

    def aufnehmen(self, funde, jetzt=None):
        """Ein neues Bild einsortieren und die vorzeigbare Liste liefern."""
        jetzt = time.time() if jetzt is None else jetzt
        vorzeigbar = [f for f in funde if f["vertrauen"] >= SCHWELLE_MELDEN]
        self.bilder.append({f["name"] for f in vorzeigbar})

        haeufig = {}
        for name in set().union(*self.bilder) if self.bilder else set():
            if sum(1 for b in self.bilder if name in b) >= self.noetig:
                haeufig[name] = True

        for fund in vorzeigbar:
            if fund["name"] in haeufig:
                # Immer der NEUESTE Fund: Der Kasten muss dem Ding
                # folgen, wenn es sich bewegt. Anfangs wurde der Fund
                # mit dem besten Vertrauen behalten - dann klebte der
                # Rahmen an der Stelle, wo jemand mal stand, statt an
                # ihm dranzubleiben (Mexla hat es am eigenen Bild
                # gesehen). Nur das Vertrauen darf sich das Beste aus
                # der juengsten Vergangenheit merken, sonst zappelt
                # die Prozentzahl mit jedem verrauschten Bild.
                alt = self.zuletzt.get(fund["name"])
                frisch = dict(fund)
                if alt is not None:
                    # Der alte Wert klingt ab - so bleibt die Anzeige
                    # ruhig, ohne dass ein einmaliger Spitzenwert
                    # ewig stehen bleibt.
                    frisch["vertrauen"] = round(
                        max(frisch["vertrauen"],
                            alt[1]["vertrauen"] * 0.95), 3)
                self.zuletzt[fund["name"]] = (jetzt, frisch)

        # Verfallenes wegwerfen, damit Tim nicht von gestern erzaehlt.
        for name in [n for n, (t, _) in self.zuletzt.items()
                     if jetzt - t > self.haltbar_s]:
            del self.zuletzt[name]

        raus = [dict(f, alter_s=round(jetzt - t, 1))
                for t, f in self.zuletzt.values()]
        raus.sort(key=lambda f: -f["vertrauen"])
        return raus

    def leeren(self):
        self.bilder.clear()
        self.zuletzt.clear()


def stoerer_kaesten(funde, rand=0.03, mindestens=SCHWELLE_AUSBLENDEN):
    """Die Kaesten, die beim Lampensuchen ausgeblendet werden sollen.

    Etwas groesser als der Fund selbst: Modelle schneiden gern knapp,
    und ein heller Saum am Rand der Hand wuerde sonst stehen bleiben und
    weiterhin als Lampe durchgehen.
    """
    kaesten = []
    for fund in funde:
        if fund["name"] not in STOERER:
            continue
        if fund["vertrauen"] < mindestens:
            continue
        x, y, breite, hoehe = fund["kasten"]
        kaesten.append((max(0.0, x - rand), max(0.0, y - rand),
                        min(1.0, breite + 2 * rand), min(1.0, hoehe + 2 * rand)))
    return kaesten


def leuchten_kaesten(funde, mindestens=0.12):
    """Wo das Modell etwas Leuchtendes sieht - sicherste zuerst.

    Die Schwelle ist bewusst niedrig: Eine leuchtende Lampe ist fuer
    solche Modelle ein schwieriges Motiv (ueberstrahlt, kaum Kontur).
    Nachgemessen am 23.08.2026 an Mexlas rot leuchtender Flurlampe:
    glowing light kam auf 0.156 - ein echter Fund, der bei einer
    Schwelle von 0.2 verworfen wurde. 0.12 laesst ihn durch. Und der
    Kasten dient nur zum Eingrenzen der Helligkeitssuche - liegt er
    daneben, findet die Suche darin schlicht nichts Helles und der
    Aufrufer faellt aufs ganze Bild zurueck.
    """
    treffer = [f for f in funde
               if f["name"] in LEUCHTEN and f["vertrauen"] >= mindestens]
    treffer.sort(key=lambda f: -f["vertrauen"])
    return [f["kasten"] for f in treffer]


def ausblenden(bild, kaesten):
    """Die genannten Bereiche schwarz machen - eine Kopie, nie das Original."""
    if bild is None:
        return None
    ohne = bild.copy()
    hoehe, breite = ohne.shape[:2]
    for x, y, kasten_breite, kasten_hoehe in kaesten:
        links = max(0, min(breite - 1, int(x * breite)))
        oben = max(0, min(hoehe - 1, int(y * hoehe)))
        rechts = max(links, min(breite, int((x + kasten_breite) * breite)))
        unten = max(oben, min(hoehe, int((y + kasten_hoehe) * hoehe)))
        ohne[oben:unten, links:rechts] = 0
    return ohne


def anteil_geschwaerzt(kaesten):
    """Wie viel vom Bild waere weg? Grob, ohne Ueberlappungen zu verrechnen.

    Wenn fast alles ausgeblendet wuerde, ist Ausblenden keine gute Idee -
    dann bleibt kein Bild mehr uebrig, in dem man suchen koennte.
    """
    return min(1.0, sum(b * h for _, _, b, h in kaesten))


def begriffe_neu_backen():
    """Die Wortliste neu ins Modell backen. Braucht einmalig Netz (CLIP)."""
    begriffe = begriffe_lesen()
    if not begriffe:
        print("begriffe.json ist leer oder fehlt - nichts zu tun.")
        return 1
    quelle = HIER / "yoloe-11l-seg.pt"
    if not quelle.exists():
        print("Grundmodell %s fehlt." % quelle.name)
        print("Einmalig holen:  yolo predict model=yoloe-11l-seg.pt")
        return 1
    from ultralytics import YOLOE
    modell = YOLOE(str(quelle))
    modell.set_classes(begriffe, modell.get_text_pe(begriffe))
    modell.save(str(MODELL))
    print("%s neu gebacken, %d Begriffe." % (MODELL.name, len(begriffe)))
    return 0


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

    print("objekterkennung Selbsttest (Auswahl-Logik, ohne Modell):")

    funde = [
        {"name": "person", "vertrauen": 0.9, "kasten": (0.1, 0.2, 0.3, 0.6)},
        {"name": "chair", "vertrauen": 0.8, "kasten": (0.6, 0.5, 0.2, 0.3)},
        {"name": "dog", "vertrauen": 0.7, "kasten": (0.0, 0.8, 0.1, 0.1)},
    ]
    kaesten = stoerer_kaesten(funde)
    pruefe(len(kaesten) == 2, "Person und Hund gelten als Stoerer, der Stuhl nicht",
           str(len(kaesten)))

    # Der Kasten muss groesser werden als der Fund - ein heller Saum am
    # Rand der Hand wuerde sonst weiterhin als Lampe durchgehen.
    pruefe(kaesten[0][0] < 0.1 and kaesten[0][2] > 0.3,
           "der ausgeblendete Bereich ist grosszuegiger als der Fund",
           str(kaesten[0]))
    pruefe(all(0.0 <= k[0] and 0.0 <= k[1] for k in kaesten),
           "kein Kasten ragt ins Negative")

    # Ein unsicherer Stoerer wird beim Ausblenden trotzdem beruecksichtigt,
    # ein noch unsichererer nicht mehr.
    knapp = [{"name": "person", "vertrauen": 0.30, "kasten": (0.1, 0.1, 0.2, 0.2)}]
    pruefe(len(stoerer_kaesten(knapp)) == 1,
           "ein halbwegs sicherer Mensch wird ausgeblendet (lockere Schwelle)")
    kaum = [{"name": "person", "vertrauen": 0.10, "kasten": (0.1, 0.1, 0.2, 0.2)}]
    pruefe(stoerer_kaesten(kaum) == [],
           "ein blosses Rauschen wird nicht ausgeblendet")
    pruefe(SCHWELLE_AUSBLENDEN < SCHWELLE_MELDEN,
           "Ausblenden ist lockerer als Melden - ein uebersehener Mensch "
           "waere schlimmer als ein zu grosser schwarzer Fleck")

    # Ausblenden muss wirklich schwaerzen - und das Original in Ruhe lassen.
    bild = np.full((100, 200, 3), 200, dtype="uint8")
    ohne = ausblenden(bild, [(0.0, 0.0, 0.5, 1.0)])
    pruefe(int(ohne[:, :100].max()) == 0, "die linke Haelfte ist geschwaerzt")
    pruefe(int(ohne[:, 120:].min()) == 200, "die rechte Haelfte bleibt unberuehrt")
    pruefe(int(bild.min()) == 200, "das Originalbild wird nicht veraendert")

    # Genau Mexlas Fall: Hand links ausgebrannt, Lampe rechts. Nach dem
    # Ausblenden der Hand muss die Lampe uebrig bleiben.
    szene = np.full((120, 160, 3), 30, dtype="uint8")
    szene[20:100, 10:80] = (255, 255, 255)     # angestrahlte Hand, ausgebrannt
    szene[30:90, 130:145] = (250, 250, 250)    # die Leuchte
    hand = [{"name": "person", "vertrauen": 0.9, "kasten": (0.05, 0.15, 0.45, 0.7)}]
    bereinigt = ausblenden(szene, stoerer_kaesten(hand))
    pruefe(int(bereinigt[20:100, 10:80].max()) == 0,
           "die Hand ist aus dem Bild verschwunden")
    pruefe(int(bereinigt[30:90, 130:145].min()) > 200,
           "die Lampe steht noch da")

    # Randfaelle
    pruefe(stoerer_kaesten([]) == [], "ohne Funde gibt es nichts auszublenden")
    pruefe(ausblenden(None, []) is None, "kein Bild ergibt kein Bild")
    pruefe(ausblenden(bild, []) is not None and int(ausblenden(bild, []).min()) == 200,
           "ohne Stoerer bleibt das Bild vollstaendig")

    # Wenn fast alles ausgeblendet wuerde, darf man es nicht tun.
    pruefe(anteil_geschwaerzt([(0, 0, 1.0, 1.0)]) >= 0.99,
           "ein bildfuellender Stoerer wird als solcher erkannt")
    pruefe(anteil_geschwaerzt([(0, 0, 0.2, 0.2)]) < 0.1,
           "ein kleiner Stoerer faellt kaum ins Gewicht")

    # --- Leuchten: was die Lampensuche eingrenzt ----------------------
    print("\n  Leuchten (grenzen die Lampensuche ein):")
    gemischt = [
        {"name": "wall", "vertrauen": 0.9, "kasten": (0, 0, 1, 0.3)},
        {"name": "glowing light", "vertrauen": 0.3, "kasten": (0.5, 0.3, 0.2, 0.2)},
        {"name": "lamp", "vertrauen": 0.6, "kasten": (0.1, 0.1, 0.1, 0.1)},
    ]
    leuchten = leuchten_kaesten(gemischt)
    pruefe(len(leuchten) == 2, "nur Leuchtendes zaehlt, die Wand nicht",
           str(len(leuchten)))
    pruefe(leuchten[0] == (0.1, 0.1, 0.1, 0.1),
           "die sicherste Leuchte steht vorn")
    pruefe(leuchten_kaesten(gemischt, mindestens=0.5) == [(0.1, 0.1, 0.1, 0.1)],
           "eine zu unsichere Leuchte faellt bei strenger Schwelle raus")
    pruefe(leuchten_kaesten([]) == [], "ohne Funde keine Leuchten")
    pruefe(all(n in begriffe_lesen() for n in LEUCHTEN),
           "jede Leuchte steht auch in der Wortliste - sonst wird sie nie "
           "gefunden", str([n for n in LEUCHTEN if n not in begriffe_lesen()]))

    # --- Gedaechtnis: das Herzstueck gegen Flackern -------------------
    print("\n  Gedaechtnis (nur Bestaendiges wird gemeldet):")
    g = Gedaechtnis(fenster=5, noetig=3, haltbar_s=8.0)
    stuhl = {"name": "chair", "deutsch": "Stuhl", "vertrauen": 0.8,
             "kasten": (0, 0, 0.1, 0.1)}
    geist = {"name": "dog", "deutsch": "Hund", "vertrauen": 0.7,
             "kasten": (0, 0, 0.1, 0.1)}

    t = 1000.0
    pruefe(g.aufnehmen([stuhl], t) == [],
           "nach einem Bild wird noch nichts gemeldet")
    pruefe(g.aufnehmen([stuhl], t + 1) == [],
           "nach zwei Bildern auch noch nicht")
    drei = g.aufnehmen([stuhl], t + 2)
    pruefe([f["name"] for f in drei] == ["chair"],
           "ab dem dritten Bild ist der Stuhl vorzeigbar", str(drei))

    # Ein einzelner Ausreisser darf es nie in die Meldung schaffen -
    # genau das war der Fehlalarm auf dem dunklen Sitzball.
    g2 = Gedaechtnis(fenster=5, noetig=3)
    for i in range(5):
        raus = g2.aufnehmen([stuhl] + ([geist] if i == 2 else []), t + i)
    pruefe([f["name"] for f in raus] == ["chair"],
           "ein einmaliger Geisterfund wird nie gemeldet", str(raus))

    # Zu unsicher: darf gar nicht erst ins Gedaechtnis.
    g3 = Gedaechtnis(fenster=3, noetig=2)
    schwach = dict(stuhl, vertrauen=SCHWELLE_MELDEN - 0.05)
    for i in range(3):
        raus3 = g3.aufnehmen([schwach], t + i)
    pruefe(raus3 == [], "was unter der Meldeschwelle liegt, zaehlt nicht mit",
           str(raus3))

    # Der Kasten muss dem Ding folgen: Wer durchs Bild geht, dessen
    # Rahmen darf nicht an der alten Stelle kleben bleiben (genau so
    # war es zuerst - Mexla stand ohne Rahmen im Bild, weil sein
    # 97-Prozent-Kasten noch an der Position von vorher hing).
    g5 = Gedaechtnis(fenster=5, noetig=3)
    links = dict(stuhl, kasten=(0.1, 0.1, 0.2, 0.2), vertrauen=0.9)
    rechts = dict(stuhl, kasten=(0.6, 0.1, 0.2, 0.2), vertrauen=0.5)
    for i in range(3):
        g5.aufnehmen([links], t + i)
    raus5 = g5.aufnehmen([rechts], t + 3)
    pruefe(raus5 and raus5[0]["kasten"] == (0.6, 0.1, 0.2, 0.2),
           "der Kasten folgt dem neuesten Fund, nicht dem besten",
           str(raus5 and raus5[0]["kasten"]))
    pruefe(raus5 and raus5[0]["vertrauen"] > 0.5,
           "das Vertrauen bricht dabei nicht sofort ein")
    for i in range(4, 40):
        raus5 = g5.aufnehmen([rechts], t + i)
    pruefe(raus5 and abs(raus5[0]["vertrauen"] - 0.5) < 0.1,
           "ein alter Spitzenwert klingt ab, statt ewig zu kleben",
           str(raus5 and raus5[0]["vertrauen"]))

    # Einmal gemeldet, kurz verfehlt: soll nicht sofort verschwinden -
    # sonst blinkt die Anzeige bei jedem verrauschten Bild.
    g4 = Gedaechtnis(fenster=5, noetig=3, haltbar_s=8.0)
    for i in range(3):
        g4.aufnehmen([stuhl], t + i)
    pruefe([f["name"] for f in g4.aufnehmen([], t + 3.5)] == ["chair"],
           "ein kurz verfehltes Ding bleibt noch stehen")
    pruefe(g4.aufnehmen([], t + 30) == [],
           "nach der Haltbarkeit ist es weg - Tim erzaehlt nicht von gestern")

    # --- Begriffe und Modell -----------------------------------------
    print("\n  Modell und Begriffe:")
    begriffe = begriffe_lesen()
    pruefe(len(begriffe) > 20, "die Wortliste ist gefuellt", str(len(begriffe)))
    pruefe("person" in begriffe, "Mensch steht in der Wortliste")
    pruefe(any("light" in b or "lamp" in b for b in begriffe),
           "nach Lampen wird ausdruecklich gesucht")
    pruefe(all(n in begriffe or n in ("teddy bear", "bird", "hand", "face")
               for n in STOERER),
           "jeder Stoerer steht auch in der Wortliste - sonst wird er nie "
           "gefunden und nie ausgeblendet",
           str([n for n in STOERER if n not in begriffe]))
    pruefe(deutsch("person") == "Mensch", "Namen werden uebersetzt")
    pruefe(deutsch("hutzelkram") == "hutzelkram",
           "Unbekanntes wird unveraendert durchgereicht")

    # Die Geraetewahl: Ohne ausdrueckliches device= rechnet Ultralytics
    # immer auf der CPU - am 27.08.2026 nachgemessen war die Apple-GPU
    # fuenfmal schneller bei identischen Funden. Diese Wahl darf also
    # nie stillschweigend zurueck auf die CPU fallen.
    pruefe(geraet_waehlen(mps_da=True) == "mps",
           "mit Apple-GPU wird auf der GPU gerechnet")
    pruefe(geraet_waehlen(mps_da=False) == "cpu",
           "ohne Apple-GPU bleibt es bei der CPU")
    pruefe(geraet() in ("mps", "cpu"),
           "die echte Wahl auf diesem Rechner ist eine von beiden",
           str(geraet()))

    # Das Modell selbst wird hier nicht geprueft - ohne es muss die Logik
    # trotzdem laufen, und erkenne() darf nicht stuerzen.
    pruefe(erkenne(None) == [], "ohne Bild liefert die Erkennung nichts")
    pruefe(MODELL.is_absolute(),
           "der Modellpfad ist absolut, nicht vom Arbeitsordner abhaengig",
           str(MODELL))
    pruefe(MODELL.name.endswith(".pt"), "und zeigt auf eine Modelldatei")
    pruefe(MODELL.exists() or MODELL_ERSATZ.exists(),
           "mindestens ein Modell liegt bereit")

    if fehler:
        print("\n%d Fehler." % fehler)
    else:
        print("\nAlles in Ordnung.")
    return fehler


def main(argumente):
    if argumente and argumente[0] == "--selbsttest":
        return selbsttest()
    if argumente and argumente[0] == "--begriffe-neu":
        return begriffe_neu_backen()
    if argumente and argumente[0] == "--probe":
        modell = modell_laden()
        if modell is None:
            print("Modell nicht ladbar: %s" % _modell_fehler)
            return 1
        print("Modell geladen: %s" % _modell_name)
        print("Begriffe: %d" % len(begriffe_lesen()))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
