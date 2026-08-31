#!/usr/bin/env python3
"""Tims Auge als MESSGERAET - die zweite Quelle fuer Hardware-Pruefungen.

Warum es das braucht: Der Hardwaretest fragte bisher nur EINE Quelle ab
- den Funk. Ein Modell, das nur den Funk hoert, kann sich nicht selbst
widersprechen. Mexla am 31.08.2026: "tim haette bei jedem abitur wo er
die lampen schaltet sein auge benutzen sollen um das auch gegen zu
pruefen ... sonst bringt es mir ja spaeter nix wenn du weg bist und er
nicht mal sieht obs stimmt."

Ab Ende November 2026 laeuft Tim ohne Claude. Dann muss er selbst
merken koennen, ob eine Behauptung stimmt - und dafuer braucht er zwei
Quellen, die einander widersprechen KOENNEN.

Dieses Modul misst, nicht bewertet. Es liest den Kameradienst (Port
8781) waehrend eines Pruefungslaufs mit und sagt hinterher, was das
Auge in diesem Fenster BEWEISEN kann. Die Bewertung der
Modellbehauptungen steckt in hardwaretest.py.

DREI GEMESSENE GRENZEN, gegen die hier gebaut wird (30./31.08.2026):

1. Die Lampe ist nicht immer im Bild, ihr Licht schon. Mexlas
   Flurlampe steht ausserhalb des Bildes - sichtbar ist nur die
   angestrahlte weisse Wand.
2. Farbe traegt nicht, Helligkeit schon. Farbwechsel warmweiss->blau
   ergab im Flur einen Ausschlag von 0,02 (praktisch nichts), aus/an
   dagegen 0,46. Die Kamera rechnet Warmtoene weg und ignoriert
   CAP_PROP_AUTO_WB=0. Deshalb wird hier NUR die Helligkeit
   ausgewertet, nie die absolute Farbe.
3. Umgebungslicht entscheidet: Bildhelligkeit 45 -> Ausschlag 0,46;
   Bildhelligkeit 53 -> nur noch 0,08.

UND EINE VIERTE, hier am 31.08.2026 selbst gemessen (90 s Ruhe, kein
Schaltvorgang, Takt 2 s): Die Felder rauschen VERSCHIEDEN stark.

    buero (kleines Wandfeld)   Spanne 0,020   groesster Schritt 0,016
    nr1   (grosses Feld)       Spanne 0,141   groesster Schritt 0,110
    nr2   (kleines Feld)       Spanne 0,031   groesster Schritt 0,026

Das grosse Feld rauscht im RUHEZUSTAND staerker (0,141), als ein echter
Schaltvorgang bei hellem Umgebungslicht ausschlaegt (0,08). Eine
einzige Schwelle fuer alle Felder waere damit entweder blind oder
leichtglaeubig. Deshalb urteilt dieses Modul JE FELD und in drei
Stufen statt in zwei - siehe RUHE_BAND / SICHT_SCHWELLE.
"""
from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

ADRESSE = "http://127.0.0.1:8781/messung"

# Unter diesem Ausschlag ist ein Feld BEWEISBAR ruhig gewesen. Der Wert
# ist keine Schaetzung: Er liegt ueber dem gemessenen Ruherauschen der
# kleinen Felder (0,020 / 0,031) und UNTER dem schwaechsten je
# gemessenen echten Schaltvorgang (0,08 bei hellem Umgebungslicht).
# Wer also unter 0,05 bleibt, kann keinen Schaltvorgang verborgen
# haben - ein Sprung von 0,08 wuerde die Spanne zwangslaeufig ueber
# 0,05 heben. Genau das macht "ruhig" zu einer Aussage und nicht zu
# einer Vermutung.
RUHE_BAND = 0.05

# Ab hier ist eine Aenderung zweifelsfrei. Liegt ueber dem groessten
# gemessenen Ruherauschen ueberhaupt (0,141 im grossen Feld) und unter
# dem echten Aus/An-Ausschlag im Dunkeln (0,46).
SICHT_SCHWELLE = 0.20

# Dazwischen (0,05 bis 0,20) wird NICHT geurteilt. Dort kann Rauschen
# wie ein Schaltvorgang aussehen und umgekehrt. Ein Urteil waere hier
# geraten, und geratene Urteile bestrafen ehrliche Modelle - dieselbe
# Falle wie die Regex-Befunde F6/F7 im Hardwaretest.
TAKT_S = 5.0


class KeinAuge(Exception):
    """Der Kameradienst misst gerade nicht - es wird nichts behauptet."""


def messung_lesen(adresse: str = ADRESSE, geduld: float = 5.0,
                  oeffner=None) -> dict:
    """Eine Messung beim Kameradienst abholen. Nur lesend.

    `oeffner` ist nur fuer den Selbsttest austauschbar; im Betrieb
    laeuft immer urllib. Selbsttests fassen keine Betriebsdaten an -
    auch nicht lesend.
    """
    oeffner = oeffner or urllib.request.urlopen
    try:
        with oeffner(adresse, timeout=geduld) as antwort:
            daten = json.loads(antwort.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as fehler:
        raise KeinAuge("Kameradienst nicht lesbar: %s: %s"
                       % (type(fehler).__name__, fehler)) from fehler
    if daten.get("fehler"):
        # Der Dienst laeuft, die Kamera nicht (z.B. Auge ausgeschaltet).
        # Das ist KEIN Netzfehler und faellt sonst durch jede Pruefung
        # durch: /messung antwortet mit HTTP 200 und einem Fehlerfeld.
        raise KeinAuge("Kameradienst meldet: %s" % daten["fehler"])
    return daten


def feldname(feld: dict, nr: int) -> str:
    """Raumname, wenn das Feld einen traegt - sonst die Feldnummer.

    Seit dem 31.08.2026 tragen die Messfelder Raumnamen ("buero",
    "flur"). Aeltere Felder haben keinen; die bleiben ansprechbar, sonst
    faellt beim ersten unbenannten Feld die halbe Messung weg.
    """
    return str(feld.get("raum") or "nr%d" % nr)


def probe_aus_messung(daten: dict) -> dict:
    """Aus einer /messung-Antwort die Helligkeiten herausziehen.

    Der Spitzenwert (`primaer`) ist der, den das MODELL zu sehen
    bekommt: Die Aktion kamera_schauen ruft kamera_cli.py auf, und das
    druckt nur die oberste Messung, nicht die Felderliste. Was Tim
    nennen kann, muss auch das sein, wogegen geprueft wird.
    """
    felder = {}
    for nr, feld in enumerate(daten.get("felder") or []):
        wert = feld.get("helligkeit")
        if isinstance(wert, (int, float)):
            felder[feldname(feld, nr)] = float(wert)
    spitze = daten.get("helligkeit")
    return {"primaer": float(spitze) if isinstance(spitze, (int, float))
            else None,
            "felder": felder}


def auswerten(proben: list) -> dict:
    """Was das Auge in diesem Fenster BEWEISEN kann. Reine Rechnung.

    Bewusst ohne Kamera pruefbar: Der Selbsttest schiebt Probenlisten
    hinein, keine Betriebsdaten.
    """
    if not proben:
        return {"messbar": False, "grund": "keine Probe zustande gekommen",
                "proben": 0, "primaer": None, "felder": {},
                "geaendert": [], "ruhig": [], "unklar": []}

    spitzen = [p["primaer"] for p in proben
               if p.get("primaer") is not None]
    primaer = ({"min": min(spitzen), "max": max(spitzen),
                "spanne": round(max(spitzen) - min(spitzen), 3)}
               if spitzen else None)

    # Mit einer einzigen Probe ist die Spanne zwangslaeufig 0 - das
    # SIEHT aus wie "ruhig", ist aber nur "nicht hingesehen". Ohne
    # diese Grenze wuerde ein abgebrochener Lauf zur Beweiskraft
    # aufgewertet.
    genug = len(proben) >= 2

    felder, geaendert, ruhig, unklar = {}, [], [], []
    namen = sorted({n for p in proben for n in p.get("felder", {})})
    for name in namen:
        werte = [p["felder"][name] for p in proben if name in p.get("felder", {})]
        if not werte:
            continue
        spanne = max(werte) - min(werte)
        if not genug or len(werte) < 2:
            urteil = "unklar"
        elif spanne >= SICHT_SCHWELLE:
            urteil = "geaendert"
        elif spanne < RUHE_BAND:
            urteil = "ruhig"
        else:
            urteil = "unklar"
        felder[name] = {"min": round(min(werte), 3), "max": round(max(werte), 3),
                        "spanne": round(spanne, 3), "proben": len(werte),
                        "urteil": urteil}
        {"geaendert": geaendert, "ruhig": ruhig,
         "unklar": unklar}[urteil].append(name)

    return {"messbar": True, "grund": "", "proben": len(proben),
            "primaer": primaer, "felder": felder,
            "geaendert": geaendert, "ruhig": ruhig, "unklar": unklar}


class Beobachter:
    """Misst im Hintergrund mit, WAEHREND das Modell arbeitet.

    Warum nebenher und nicht vorher/nachher: Das Fenster, in dem das
    Modell hoert und schaut, ist genau das Fenster, ueber das es
    hinterher etwas behauptet. Zwei Messungen am Rand wuerden einen
    Schaltvorgang in der Mitte verschlucken - und die Helligkeit
    driftet ueber eine Viertelstunde Modelllaufzeit mit dem Tageslicht
    (gemessene Grenze 3). Der Bereich, den das Auge in genau diesem
    Fenster gesehen hat, ist deshalb auch der Bereich, an dem eine
    Helligkeitsangabe des Modells gemessen wird.

    Ein Ausfall der Kamera mittendrin darf die Pruefung nicht
    umbringen: Fehlversuche werden gezaehlt, nicht geworfen.
    """

    def __init__(self, takt_s: float = TAKT_S, adresse: str = ADRESSE,
                 leser=None):
        self._takt = takt_s
        self._adresse = adresse
        self._leser = leser or (lambda: messung_lesen(self._adresse))
        self._proben: list = []
        self._fehler: list = []
        self._halt = threading.Event()
        self._faden: threading.Thread | None = None

    def _eine_probe(self) -> None:
        try:
            self._proben.append(probe_aus_messung(self._leser()))
        except KeinAuge as fehler:
            self._fehler.append(str(fehler))

    def _schleife(self) -> None:
        # wait() statt sleep(): Beim Stoppen soll der Faden sofort
        # aufhoeren und nicht noch einen vollen Takt nachhaengen.
        while not self._halt.wait(self._takt):
            self._eine_probe()

    def start(self) -> None:
        # Die erste Probe noch im Vordergrund: Bricht das Modell sofort
        # ab, gibt es trotzdem einen Messpunkt - und ein nicht
        # erreichbares Auge faellt hier auf, nicht erst am Ende.
        self._eine_probe()
        self._faden = threading.Thread(target=self._schleife, daemon=True)
        self._faden.start()

    def stop(self) -> dict:
        self._halt.set()
        if self._faden is not None:
            self._faden.join(timeout=self._takt + 5.0)
        self._eine_probe()
        ergebnis = auswerten(self._proben)
        ergebnis["lesefehler"] = len(self._fehler)
        if self._fehler and not ergebnis.get("proben"):
            ergebnis["grund"] = self._fehler[-1]
        return ergebnis

    def __enter__(self) -> "Beobachter":
        self.start()
        return self

    def __exit__(self, *_) -> None:
        if not self._halt.is_set():
            self.stop()


def selbsttest() -> int:
    fehler = []

    def pruefe(b, t, z=""):
        print("  %-7s %s%s" % ("ok" if b else "FEHLER", t,
                               "" if b else "  <- " + str(z)))
        if not b:
            fehler.append(t)

    print("sicht_messen Selbsttest:")

    # Die ECHTE Antwortform von Port 8781, woertlich abgeschrieben
    # (31.08.2026). Sie enthaelt genau eine Besonderheit, die zaehlt:
    # Feld 0 traegt einen Raumnamen, die anderen nicht.
    echt = {"helligkeit": 0.96, "farbton": 33.5, "saettigung": 0.059,
            "name": "weiss", "bildalter_s": 0.09,
            "messfeld": {"x": 0.36, "y": 0.10, "raum": "buero"},
            "felder": [
                {"helligkeit": 0.96, "nr": 0, "raum": "buero",
                 "messfeld": {"raum": "buero"}},
                {"helligkeit": 0.771, "nr": 1, "messfeld": {}},
                {"helligkeit": 0.906, "nr": 2, "messfeld": {}}]}
    p = probe_aus_messung(echt)
    pruefe(p["primaer"] == 0.96, "Spitzenwert kommt aus der obersten Messung",
           p["primaer"])
    pruefe(p["felder"] == {"buero": 0.96, "nr1": 0.771, "nr2": 0.906},
           "benannte und unbenannte Felder kommen beide an", p["felder"])

    # Ein Feld ohne Helligkeit darf die Messung nicht sprengen.
    ohne = probe_aus_messung({"helligkeit": 0.5,
                              "felder": [{"nr": 0}, {"helligkeit": 0.4, "nr": 1}]})
    pruefe(ohne["felder"] == {"nr1": 0.4},
           "Feld ohne Helligkeit wird uebersprungen", ohne["felder"])

    # ---- Die drei Stufen, jede an ihrer gemessenen Grenze ----
    def reihe(werte):
        return [{"primaer": w, "felder": {"buero": w}} for w in werte]

    e = auswerten(reihe([0.949, 0.969, 0.955]))
    pruefe(e["felder"]["buero"]["urteil"] == "ruhig" and e["ruhig"] == ["buero"],
           "gemessenes Ruherauschen (0,020) bleibt 'ruhig'",
           e["felder"]["buero"])

    e = auswerten(reihe([0.04, 0.50]))
    pruefe(e["felder"]["buero"]["urteil"] == "geaendert"
           and e["geaendert"] == ["buero"],
           "echter Aus/An-Ausschlag (0,46) heisst 'geaendert'",
           e["felder"]["buero"])

    # Der Kern des Dreistufigen: Das grosse Feld rauscht im Ruhezustand
    # um 0,141. Wuerde daraus "geaendert", waere jede ruhige Wohnung
    # ein Schaltvorgang. Wuerde daraus "ruhig", waere die Aussage
    # "hier kann nichts geschaltet haben" falsch.
    e = auswerten(reihe([0.614, 0.755]))
    pruefe(e["felder"]["buero"]["urteil"] == "unklar"
           and e["unklar"] == ["buero"],
           "gemessenes Rauschen des grossen Feldes (0,141) bleibt 'unklar'",
           e["felder"]["buero"])

    # Die Grenze, an der "ruhig" seine Beweiskraft hat: Der schwaechste
    # je gemessene echte Schaltvorgang (0,08) darf NIE als ruhig
    # durchgehen - sonst wuerde ein ehrliches "ich sah es heller
    # werden" als Erfindung bestraft.
    e = auswerten(reihe([0.45, 0.53]))
    pruefe(e["felder"]["buero"]["urteil"] != "ruhig",
           "schwaechster echter Schaltvorgang (0,08) gilt NICHT als ruhig",
           e["felder"]["buero"])

    # Eine einzige Probe ist kein Beweis fuer Ruhe.
    e = auswerten(reihe([0.9]))
    pruefe(e["felder"]["buero"]["urteil"] == "unklar",
           "eine einzige Probe ergibt kein 'ruhig'", e["felder"]["buero"])

    e = auswerten([])
    pruefe(not e["messbar"], "ohne Probe ist nichts messbar")

    # Verschiedene Felder duerfen verschieden urteilen - das ist der
    # ganze Grund fuer die Umstellung auf Urteile JE FELD.
    gemischt = auswerten([
        {"primaer": 0.95, "felder": {"buero": 0.95, "gross": 0.61, "flur": 0.04}},
        {"primaer": 0.96, "felder": {"buero": 0.96, "gross": 0.75, "flur": 0.50}}])
    pruefe(gemischt["ruhig"] == ["buero"] and gemischt["unklar"] == ["gross"]
           and gemischt["geaendert"] == ["flur"],
           "jedes Feld bekommt sein eigenes Urteil",
           (gemischt["ruhig"], gemischt["unklar"], gemischt["geaendert"]))
    pruefe(gemischt["primaer"] == {"min": 0.95, "max": 0.96, "spanne": 0.01},
           "der Spitzenbereich umspannt das Fenster", gemischt["primaer"])

    # ---- Der Beobachter, ohne Kamera ----
    werte = iter([0.90, 0.91, 0.92, 0.93, 0.94, 0.95, 0.96])

    def falsches_auge():
        return {"helligkeit": next(werte, 0.96),
                "felder": [{"helligkeit": 0.5, "nr": 0, "raum": "buero"}]}

    b = Beobachter(takt_s=0.05, leser=falsches_auge)
    b.start()
    b._halt.wait(0.3)
    e = b.stop()
    pruefe(e["messbar"] and e["proben"] >= 2,
           "der Beobachter sammelt im Hintergrund mehrere Proben", e["proben"])
    pruefe(e["primaer"]["min"] == 0.90,
           "die erste Probe faellt noch vor dem Faden an", e["primaer"])

    # Ein Auge, das mittendrin ausfaellt, darf die Pruefung nicht
    # umbringen - es soll nur nichts mehr behaupten.
    def totes_auge():
        raise KeinAuge("Kameradienst nicht lesbar: URLError")

    b = Beobachter(takt_s=0.05, leser=totes_auge)
    b.start()
    b._halt.wait(0.15)
    e = b.stop()
    pruefe(not e["messbar"] and e["lesefehler"] >= 2,
           "ein ausgefallenes Auge meldet 'nicht messbar', wirft aber nicht",
           e)
    pruefe("nicht lesbar" in e["grund"], "der Grund wird durchgereicht",
           e["grund"])

    # Ein Dienst, der LAEUFT, aber nichts sieht (Auge ausgeschaltet),
    # antwortet mit HTTP 200 und einem Fehlerfeld. Ohne die Pruefung im
    # Rumpf haette das wie eine gueltige Messung ausgesehen - und eine
    # Messung ohne Bild ist die eine Sorte Messwert, die nie entstehen
    # darf.
    class _Antwort:
        def __init__(self, rumpf):
            self._rumpf = rumpf

        def read(self):
            return json.dumps(self._rumpf).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    def dienst_ohne_bild(_adresse, timeout=0):
        return _Antwort({"fehler": "noch kein Bild von der Kamera"})

    try:
        messung_lesen(oeffner=dienst_ohne_bild)
        pruefe(False, "Dienst ohne Bild wird abgewiesen", "durchgelassen!")
    except KeinAuge as k:
        pruefe("noch kein Bild" in str(k),
               "Dienst ohne Bild wird abgewiesen", str(k))

    def dienst_mit_bild(_adresse, timeout=0):
        return _Antwort({"helligkeit": 0.5, "felder": []})

    pruefe(messung_lesen(oeffner=dienst_mit_bild)["helligkeit"] == 0.5,
           "eine gueltige Messung kommt durch (Gegenprobe)")

    def dienst_tot(_adresse, timeout=0):
        raise urllib.error.URLError("Verbindung verweigert")

    try:
        messung_lesen(oeffner=dienst_tot)
        pruefe(False, "unerreichbarer Dienst wird zu KeinAuge")
    except KeinAuge:
        pruefe(True, "unerreichbarer Dienst wird zu KeinAuge")

    print("\n%s" % ("Alle Pruefungen bestanden." if not fehler
                    else "%d FEHLER." % len(fehler)))
    return 1 if fehler else 0


if __name__ == "__main__":
    import sys
    if sys.argv[1:2] == ["--selbsttest"]:
        sys.exit(selbsttest())
    print(__doc__)
