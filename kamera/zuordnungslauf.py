#!/usr/bin/env python3
#
# Lizenz: GNU AGPL-3.0 - siehe kamera/LICENSE.
#
"""Der Zuordnungslauf: durch Schalten herausfinden, welche Lampe wo steht.

Das Auge misst seit dem 31.08.2026 mehrere Leuchten einzeln, weiss aber
nicht, welche zu welchem Raum gehoert. Dieses Programm findet es heraus:
Es schaltet einen Raum, misst vorher und nachher JEDES Feld und legt die
Messungen `zuordnung.zuordnen()` vor - der Logik, die Tim am 24.08.2026
selbst gebaut hat.

    Feld 0 und Feld 2 werden hell, wenn "buero" geschaltet wird
    -> beide gehoeren zum Buero. Feld 1 nicht -> gehoert woandershin.

## Es schaltet ECHTES Licht

Darum passiert ohne `--schalten` gar nichts: Der Lauf zeigt nur, was er
tun WUERDE. Wer ihn wirklich laufen laesst, sieht die Lampen in der
Wohnung umspringen - das ist keine Nebenwirkung, sondern das Messmittel.

    zuordnungslauf.py                      nur zeigen, nichts schalten
    zuordnungslauf.py --schalten           wirklich messen
    zuordnungslauf.py --schalten --raum buero    nur einen Raum
    zuordnungslauf.py --schalten --merken  Ergebnis in die Felder schreiben

## Warum ein eigenes Programm und kein Endpunkt im Kameradienst

Der Kameradienst soll NICHTS schalten. Er haelt die Kamera und misst -
mehr nicht. Wuerde er Lampen schalten, haetten wir Kamera und Funkbruecke
aneinandergekettet, und ein Ausfall der einen legte die andere lahm.
Dieses Programm spricht beide ueber ihre normalen Wege an: die Kamera
ueber HTTP, die Lampen ueber lampen_steuern.py.

Selbsttest: zuordnungslauf.py --selbsttest
"""

import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import zuordnung

KAMERA = "http://127.0.0.1:8781"

# Die Steuerung liegt im Hardware-Ordner auf dem Schreibtisch, nicht im
# Repo - deshalb der Pfad hier und nicht als Import.
STEUERUNG = (Path.home() / "Desktop" / "M1_DEPLOYMENT" / "hardware"
             / "pico_bruecke" / "lampen_steuern.py")

# Geschaltet wird AUS und wieder AN, nicht von Farbe zu Farbe.
#
# Am 31.08.2026 zuerst mit Farben versucht (warmweiss -> blau): Im Buero
# reichte das knapp (Aenderung 0.16 bei Schwelle 0.15), fuer den Flur gar
# nicht. Mexla hat den Grund benannt: Seine Flurlampe steht nicht im
# Bild, sichtbar ist nur die von ihr angestrahlte weisse Wand - und
# darauf sieht ein Farbwechsel fast wie nichts aus. Helligkeit traegt
# dort viel weiter, und aus/an ist der groesste Helligkeitsunterschied,
# den es gibt. (Tims eigene Regeln rechnen aus demselben Grund mit
# Helligkeit.)
AUS = "aus"
AN = "an"
RUECKFARBE = "warmweiss"

# Nach dem Schalten kurz warten: Der Funkspruch braucht seinen Weg, die
# Lampe faehrt hoch, und die Kamera muss ein frisches Bild geliefert
# haben. Zu kurz gewartet heisst: gemessen wird der alte Zustand.
WARTEN_S = 2.5


def kamera_messung(zeit=8):
    """Die aktuelle Messung aller Felder holen - nur lesend."""
    with urllib.request.urlopen(KAMERA + "/messung", timeout=zeit) as antwort:
        return json.loads(antwort.read().decode("utf-8"))


def felder_messen():
    """Die Messwerte je Feld, in Feldreihenfolge."""
    messung = kamera_messung()
    if messung.get("fehler"):
        raise RuntimeError("Kamera: %s" % messung["fehler"])
    felder = messung.get("felder")
    if felder is None:
        # Aeltere Fassung des Dienstes: nur ein Feld, oben im Ergebnis.
        return [messung]
    return felder


def rohbild(zeit=10):
    """Das unbearbeitete Kamerabild als Bildmatrix - nur lesend."""
    import cv2
    import numpy as np
    with urllib.request.urlopen(KAMERA + "/rohbild.jpg", timeout=zeit) as a:
        roh = a.read()
    return cv2.imdecode(np.frombuffer(roh, np.uint8), cv2.IMREAD_COLOR)


def schalten(raum, befehl, wirklich):
    """Einen Raum schalten. Ohne `wirklich` nur ansagen."""
    if not wirklich:
        print("    [nur gezeigt] %s %s" % (raum, befehl))
        return True
    if not STEUERUNG.exists():
        raise RuntimeError("Lampensteuerung nicht gefunden: %s" % STEUERUNG)
    lauf = subprocess.run([sys.executable, str(STEUERUNG), raum, befehl],
                          capture_output=True, text=True, timeout=30)
    erste = (lauf.stdout or lauf.stderr or "").strip().splitlines()
    print("    %s %s -> %s" % (raum, befehl, erste[0] if erste else "(still)"))
    return lauf.returncode == 0


def raeume_lesen():
    """Welche Raeume die Bruecke kennt."""
    if not STEUERUNG.exists():
        return []
    lauf = subprocess.run([sys.executable, str(STEUERUNG), "raeume"],
                          capture_output=True, text=True, timeout=30)
    raeume = []
    for zeile in (lauf.stdout or "").splitlines():
        teile = zeile.split()
        if len(teile) >= 3 and teile[1] == "Nummer":
            raeume.append(teile[0])
    return raeume


def feld_verengen(bild_an, grob, hoechstflaeche=0.06):
    """Aus dem aufgehellten Bereich ein brauchbares Messfeld machen.

    Der Bildvergleich liefert ALLES, was heller wurde. Steht die Lampe
    selbst im Bild, ist das ein kleiner Fleck. Steht sie es nicht -
    Mexlas Flurlampe leuchtet nur um die Ecke auf eine Wand -, dann ist
    es der halbe Raum. Am 31.08.2026 entstand so ein "Messfeld" ueber
    13 Prozent der Bildflaeche; das misst das Zimmer, nicht das Licht.

    Deshalb: im aufgehellten Bereich die hellste Stelle suchen, und
    wenn selbst die noch zu gross ist, lieber KEIN Feld anlegen als ein
    unbrauchbares.
    """
    import kamera_dienst
    if not grob:
        return None
    kern = kamera_dienst.lampe_im_bild(
        bild_an, eingrenzen=(grob["x"], grob["y"],
                             grob["breite"], grob["hoehe"]))
    feld = kern or grob
    if feld["breite"] * feld["hoehe"] > hoechstflaeche:
        return None
    return feld


# Ab dieser mittleren Bildhelligkeit ist es zu hell fuer eine
# verlaessliche Zuordnung nicht sichtbarer Lampen. Gemessen am
# 31.08.2026: Bei 45 gelang die Flur-Zuordnung deutlich (Aenderung
# 0.46), bei 53 nicht mehr (0.08) - das Tageslicht ueberdeckt das
# Lampenlicht. 30 liegt sicher darunter, ohne bis zur Stockdunkelheit
# zu warten.
DUNKEL_BIS = 30.0


# Solange eine dieser Dateien liegt, wird geprueft - und dann darf hier
# niemand Licht schalten. Der Grund ist nicht Hoeflichkeit: Der
# Hardwaretest im Abitur misst Funkpakete ueber dieselbe BRMesh-Bruecke.
# Wer waehrenddessen einen zweiten Raum funken laesst, macht die
# Zuordnung unhoerbar - und das Ergebnis sieht hinterher wie ein
# Modellfehler aus. Es sind ZWEI Dateien mit zwei Zwecken:
# PRUEFUNGSMODUS fuer Werkstatt-Pruefungen, PRUEFUNGSLAUF fuer
# Pruefungslaeufe (seit 31.08.2026 getrennt).
PRUEFUNGSDATEIEN = (Path("/opt/ki-server/config/PRUEFUNGSMODUS"),
                    Path("/opt/ki-server/config/PRUEFUNGSLAUF"))


def pruefung_grund(dateien=PRUEFUNGSDATEIEN):
    """Was steht in der Pruefungsdatei? Fuer eine brauchbare Log-Zeile.

    Seit dem 31.08.2026 vermerkt PRUEFUNGSLAUF, seit wann und fuer
    welches Modell geprueft wird. "Es laeuft eine Pruefung" ist eine
    Auskunft, "laguna-xs-2.1 seit 20:05" ist eine bessere - wer morgens
    ins Log schaut, will wissen, WARUM der Abend ausgefallen ist.
    """
    for datei in dateien:
        try:
            if not datei.exists():
                continue
            inhalt = datei.read_text(encoding="utf-8").strip()
        except OSError:
            return "%s nicht lesbar" % datei.name
        if inhalt:
            return "%s: %s" % (datei.name, inhalt.splitlines()[0][:120])
        return datei.name
    return ""


def pruefung_laeuft(dateien=PRUEFUNGSDATEIEN):
    """Laeuft gerade eine Pruefung? Im Zweifel JA.

    Die Zweifelsrichtung ist der ganze Punkt: Laesst sich ein Pfad nicht
    lesen, wird angenommen, dass geprueft wird. Ein ausgelassener Abend
    kostet nichts - die Routine kommt alle 30 Minuten wieder. Ein
    zerschossener Pruefungslauf kostet eine halbe Stunde und erzeugt ein
    Fehlurteil ueber ein Modell.
    """
    for datei in dateien:
        try:
            if datei.exists():
                return True
        except OSError:
            return True
    return False


def ist_dunkel_genug(grenze=DUNKEL_BIS):
    """Wie hell ist es gerade? (mittlere Bildhelligkeit 0-255)"""
    bild = rohbild()
    if bild is None:
        return False, 0.0
    mittel = float(bild.mean())
    return mittel <= grenze, mittel


def schon_zugeordnet(raeume):
    """Welche der Raeume tragen bereits ein Feld?"""
    try:
        felder = kamera_messung().get("felder") or []
    except Exception:
        return set()
    return {f.get("raum") for f in felder if f.get("raum")} & set(raeume)


def lauf(raeume=None, wirklich=False, merken=False):
    """Ein Experiment je Raum, dann die Auswertung."""
    raeume = raeume or raeume_lesen()
    if not raeume:
        print("Keine Raeume bekannt - laeuft die Funkbruecke?")
        return 1

    felder = felder_messen()
    print("Das Auge hat %d Messfeld(er)." % len(felder))
    if len(felder) < 2:
        print("HINWEIS: Mit nur einem Feld kann der Lauf keine Gruppen "
              "unterscheiden. Erst /lampe_suchen aufrufen, damit jede "
              "Leuchte ihr eigenes Feld bekommt.")

    beobachtungen = []
    bilder = {}
    for raum in raeume:
        print("\n  Raum %s:" % raum)
        schalten(raum, AUS, wirklich)
        time.sleep(WARTEN_S if wirklich else 0)
        vorher = felder_messen()
        bild_aus = rohbild() if wirklich else None
        schalten(raum, AN, wirklich)
        time.sleep(WARTEN_S if wirklich else 0)
        nachher = felder_messen()
        bild_an = rohbild() if wirklich else None
        bilder[raum] = (bild_aus, bild_an)
        beobachtungen.append({"geschaltet": [raum],
                              "vorher": vorher, "nachher": nachher})
        for nr, (v, n) in enumerate(zip(vorher, nachher)):
            print("    Feld %d: Aenderung %.2f" % (nr, zuordnung.aenderung(v, n)))
        schalten(raum, RUECKFARBE, wirklich)
        time.sleep(WARTEN_S if wirklich else 0)

    zu, unsicher = zuordnung.zuordnen(beobachtungen)
    print("\nErgebnis:")
    if not zu:
        print("  Keine sichere Zuordnung.")
    for feld, raum in sorted(zu.items()):
        print("  Feld %d gehoert zu %s" % (feld, raum))
    for feld, grund in unsicher:
        print("  Feld %d unsicher: %s" % (feld, grund))

    # Ein Raum ohne Feld heisst nicht, dass er unsichtbar ist - vielleicht
    # steht seine Lampe nur nicht im Bild, sondern beleuchtet eine Wand.
    # Wo genau sich beim Einschalten etwas geaendert hat, weiss der
    # Bildvergleich aus kamera_dienst - dieselbe Funktion, mit der das
    # Messfeld urspruenglich auf die Lampe gesetzt wird.
    ohne_feld = [r for r in raeume if r not in zu.values()]
    neue = []
    if ohne_feld and wirklich:
        import kamera_dienst
        for raum in ohne_feld:
            bild_aus, bild_an = bilder.get(raum, (None, None))
            feld = kamera_dienst.lampe_suchen(bild_aus, bild_an)
            if feld:
                feld = feld_verengen(bild_an, feld)
                if not feld:
                    print("  %s: das Licht hellt eine grosse Flaeche auf, "
                          "ohne erkennbare Quelle - daraus wird kein "
                          "Messfeld. Steht die Lampe ueberhaupt im Bild?"
                          % raum)
                    continue
            if not feld:
                print("  %s: nichts im Bild hat sich geaendert - dieser "
                      "Raum ist fuer die Kamera nicht sichtbar." % raum)
                continue
            print("  %s: kein vorhandenes Feld reagierte, aber das Bild "
                  "aendert sich bei x=%.2f y=%.2f - dort entsteht ein "
                  "neues Feld." % (raum, feld["x"] + feld["breite"] / 2,
                                   feld["y"] + feld["hoehe"] / 2))
            neue.append((raum, feld))

    if merken and neue and wirklich:
        for raum, feld in neue:
            urllib.request.urlopen(
                "%s/messfeld?neu=1&x=%f&y=%f&breite=%f&hoehe=%f&raum=%s"
                % (KAMERA, feld["x"], feld["y"], feld["breite"],
                   feld["hoehe"], raum), timeout=8).read()
        print("  %d neue(s) Feld(er) angelegt und benannt." % len(neue))

    if merken and zu and wirklich:
        for feld, raum in sorted(zu.items()):
            urllib.request.urlopen(
                "%s/messfeld?nr=%d&raum=%s" % (KAMERA, feld, raum),
                timeout=8).read()
        print("\nDie Raumnamen stehen jetzt an den Feldern.")
    elif merken and not neue:
        print("\n(Nichts zu merken.)" if wirklich else
              "\n(Nicht gemerkt - dafuer braucht es einen echten Lauf.)")
    return 0


def selbsttest():
    """Prueft, was ohne Hardware pruefbar ist: die Verkabelung."""
    fehler = 0

    def pruefe(bedingung, text, zusatz=""):
        nonlocal fehler
        if bedingung:
            print("  ok      %s" % text)
        else:
            print("  FEHLER  %s%s" % (text, "  [%s]" % zusatz if zusatz else ""))
            fehler += 1

    print("zuordnungslauf Selbsttest:")
    pruefe(zuordnung.aenderung({"helligkeit": 0.1}, {"helligkeit": 0.9}) > 0.5,
           "die Auswertung ist eingebunden und rechnet")
    pruefe(schalten("buero", "blau", False) is True,
           "ohne --schalten wird nichts geschaltet, nur angesagt")
    pruefe(WARTEN_S >= 1.0,
           "nach dem Schalten wird gewartet - sonst misst man den alten "
           "Zustand", str(WARTEN_S))
    pruefe(AUS != AN,
           "aus und an sind verschieden, sonst aendert sich nichts "
           "Messbares")
    import kamera_dienst
    import numpy as np
    dunkel = np.zeros((80, 120, 3), dtype="uint8")
    hell = dunkel.copy()
    hell[20:40, 70:100] = (240, 240, 240)      # eine angestrahlte Wand
    gefunden = kamera_dienst.lampe_suchen(dunkel, hell)
    pruefe(gefunden is not None and gefunden["x"] > 0.4,
           "der Bildvergleich findet die Stelle, die sich aufhellt - "
           "auch wenn dort keine Lampe steht, sondern nur ihr Licht",
           str(gefunden))
    pruefe(kamera_dienst.lampe_suchen(dunkel, dunkel.copy()) is None,
           "ohne sichtbare Aenderung wird kein Feld erfunden")

    # Der Fall vom 31.08.2026: Das Einschalten hellt den halben Raum auf.
    # Aus dem grossen Bereich muss die hellste Stelle werden, nicht ein
    # Feld ueber das halbe Bild.
    weit = dunkel.copy()
    weit[10:70, 10:110] = (90, 90, 90)         # der halbe Raum wird heller
    weit[30:40, 60:75] = (250, 250, 250)       # die Lampe selbst
    grob = kamera_dienst.lampe_suchen(dunkel, weit)
    fein = feld_verengen(weit, grob)
    pruefe(fein is not None and fein["breite"] * fein["hoehe"] < 0.06,
           "steht die Lampe im Bild, wird daraus ein kleines Messfeld",
           "%.3f" % (fein["breite"] * fein["hoehe"]) if fein else "-")

    # Und der Flur-Fall: Das Licht hellt eine grosse Flaeche gleichmaessig
    # auf, die Lampe selbst ist nicht im Bild. Daraus darf KEIN Feld
    # werden - ein Rechteck ueber den halben Raum misst nicht die Lampe.
    diffus = dunkel.copy()
    diffus[10:70, 10:110] = (90, 90, 90)
    grob2 = kamera_dienst.lampe_suchen(dunkel, diffus)
    pruefe(grob2 is not None and grob2["breite"] * grob2["hoehe"] > 0.3,
           "diffuses Licht ohne Quelle ergibt einen riesigen Bereich",
           "%.2f" % (grob2["breite"] * grob2["hoehe"]) if grob2 else "-")
    pruefe(feld_verengen(diffus, grob2) is None,
           "und daraus wird bewusst KEIN Messfeld gemacht")
    # Die Pruefungssperre - und vor allem ihre Zweifelsrichtung.
    class Stolpert:
        def exists(self):
            raise OSError("Pfad nicht lesbar")
    leer = Path("/gibt/es/nicht/A"), Path("/gibt/es/nicht/B")
    pruefe(pruefung_laeuft(leer) is False,
           "ohne Pruefungsdatei darf die Routine arbeiten")
    pruefe(pruefung_laeuft((Path(__file__),) + leer) is True,
           "liegt eine Pruefungsdatei, wird nicht geschaltet")
    pruefe(pruefung_laeuft(leer + (Path(__file__),)) is True,
           "auch wenn sie die ZWEITE ist - beide Dateien zaehlen")
    pruefe(pruefung_laeuft((Stolpert(),)) is True,
           "ist der Pfad nicht lesbar, wird im Zweifel NICHT geschaltet")
    import tempfile
    with tempfile.TemporaryDirectory() as ordner:
        mit_inhalt = Path(ordner) / "PRUEFUNGSLAUF"
        mit_inhalt.write_text("laguna-xs-2.1 seit 20:05\nzweite Zeile\n")
        grund = pruefung_grund((mit_inhalt,))
        pruefe("laguna" in grund and "zweite" not in grund,
               "der Grund der Pruefung landet im Log - erste Zeile, "
               "nicht die ganze Datei", grund)
        ohne = Path(ordner) / "PRUEFUNGSMODUS"
        ohne.write_text("")
        pruefe(pruefung_grund((ohne,)) == "PRUEFUNGSMODUS",
               "eine leere Schalterdatei nennt wenigstens ihren Namen")
    pruefe(pruefung_grund(leer) == "",
           "ohne Pruefung gibt es keinen Grund zu nennen")

    pruefe(len(PRUEFUNGSDATEIEN) == 2,
           "beide Schalter werden abgefragt, nicht nur der alte",
           str([p.name for p in PRUEFUNGSDATEIEN]))

    pruefe(DUNKEL_BIS < 45,
           "die Dunkelheitsgrenze liegt unter der Helligkeit, bei der "
           "die Zuordnung schon einmal misslang", str(DUNKEL_BIS))
    pruefe(str(STEUERUNG).endswith("lampen_steuern.py"),
           "der Weg zur Lampensteuerung ist gesetzt", str(STEUERUNG))
    if fehler:
        print("\n%d Fehler." % fehler)
    else:
        print("\nAlles in Ordnung.")
    return fehler


def main(argumente):
    if "--selbsttest" in argumente:
        return selbsttest()
    raeume = None
    if "--raum" in argumente:
        stelle = argumente.index("--raum")
        if stelle + 1 < len(argumente):
            # Mehrere Raeume mit Komma: Die Kamera sieht ohnehin nur
            # einen Ausschnitt der Wohnung - das halbe Haus zu schalten,
            # um zwei Lampen zuzuordnen, waere sinnlose Unruhe.
            raeume = [t.strip().lower()
                      for t in argumente[stelle + 1].split(",") if t.strip()]
    # Fuer die Routine: nur handeln, wenn es sich lohnt. Beides sind
    # bewusst STILLE Abbrueche mit Rueckgabe 0 - eine Routine, die
    # jeden Abend "Fehler" meldet, weil es noch hell ist, wird nach
    # drei Tagen ignoriert.
    if "--wenn-dunkel" in argumente and pruefung_laeuft():
        print("Es laeuft eine Pruefung (%s) - kein Lauf. Licht zu "
              "schalten wuerde ihre Funkmessung stoeren."
              % (pruefung_grund() or "Grund unbekannt"))
        return 0
    if "--nur-offene" in argumente and raeume:
        fertig = schon_zugeordnet(raeume)
        raeume = [r for r in raeume if r not in fertig]
        if not raeume:
            print("Alle genannten Raeume sind bereits zugeordnet - "
                  "nichts zu tun.")
            return 0
    if "--wenn-dunkel" in argumente:
        dunkel, mittel = ist_dunkel_genug()
        if not dunkel:
            print("Noch zu hell (Helligkeit %.0f, noetig hoechstens %.0f) - "
                  "kein Lauf. Das Tageslicht wuerde das Lampenlicht "
                  "ueberdecken." % (mittel, DUNKEL_BIS))
            return 0
        print("Dunkel genug (Helligkeit %.0f)." % mittel)

    return lauf(raeume=raeume,
                wirklich="--schalten" in argumente,
                merken="--merken" in argumente)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
