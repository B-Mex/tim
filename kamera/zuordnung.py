#!/usr/bin/env python3
#
# Lizenz: GNU AGPL-3.0 - siehe kamera/LICENSE (gleicher Ordner, gleiche
# Bedingungen wie der uebrige Kameracode).
#
"""Welche Lampe gehoert zu welchem Raum? - durch Schalten herausfinden.

Das Auge sieht mehrere Leuchten, weiss aber nicht, welche wozu gehoert.
Herausfinden laesst sich das nur durch ein EXPERIMENT: einen Raum
schalten, vorher und nachher messen, vergleichen. Was sich aendert,
haengt an diesem Raum.

## Woher diese Datei kommt

Die Grundlogik hat **Tim selbst** am 24.08.2026 als Werkstatt-Aufgabe
`lampen_zuordnen` gebaut (Original:
~/Desktop/Tim-Werkstatt/geschafft/2026-08-24_lampen_zuordnen/zuordnen.py,
8 Pruefungen gruen). Seine Regeln sind unveraendert uebernommen:

  - deutliche Aenderung (>= 0.15)  -> gehoert dazu
  - praktisch keine (< 0.05)       -> gehoert nicht dazu
  - dazwischen                     -> unsicher, nicht raten
  - mehrere Raeume gleichzeitig    -> sagt ueber den einzelnen nichts

Zwei Dinge sind neu, weil das Auge seit dem 31.08.2026 mehrere
Messfelder hat:

1. Gemessen wird JE FELD statt einmal fuers ganze Bild. Damit beantwortet
   ein einziges Experiment gleich die ganze Frage: Alle Felder, die sich
   beim Schalten des Bueros aendern, sind die Buero-Gruppe.
2. Neben der Helligkeit zaehlt die FARBE. Farbe traegt weiter: "alle drei
   wurden gleichzeitig rot" ist eindeutig, waehrend Helligkeit auch
   Tageslicht oder eine Wolke sein kann. Der Farbton wird aber nur
   gewertet, wenn beide Messungen bunt genug sind - in einem grauen oder
   dunklen Feld ist der Farbton reines Rauschen.

Selbsttest: zuordnung.py --selbsttest
"""

import sys

# Tims Schwellen, unveraendert. Sie stammen aus seiner Aufgabenstellung
# und haben sich dort bewaehrt; ohne Not aendert man so etwas nicht.
DEUTLICH = 0.15
KAUM = 0.05

# Ab dieser Saettigung gilt ein Farbton als ablesbar. Darunter ist er
# geraten: Ein fast graues Feld hat rechnerisch auch einen Farbton, aber
# er springt bei jedem Rauschen quer durchs Spektrum.
FARBE_AB = 0.15


def farbton_abstand(a, b):
    """Abstand zweier Farbtoene (0-360 Grad), als Anteil von 0 bis 1.

    Der Farbkreis ist rund: 350 Grad und 10 Grad liegen 20 Grad
    auseinander, nicht 340. Wer das uebersieht, haelt jede Lampe an der
    Rot-Grenze fuer voellig veraendert.
    """
    roh = abs(float(a) - float(b)) % 360.0
    return min(roh, 360.0 - roh) / 180.0


def aenderung(vorher, nachher):
    """Wie stark hat sich EIN Feld veraendert? 0 bis 1.

    Genommen wird der groessere der beiden Hinweise - eine Lampe kann
    von Rot auf Blau wechseln, ohne heller zu werden, und sie kann
    heller werden, ohne die Farbe zu wechseln. Beides ist eine
    Aenderung.
    """
    if not vorher or not nachher:
        return 0.0
    hell = abs(float(nachher.get("helligkeit", 0.0))
               - float(vorher.get("helligkeit", 0.0)))
    bunt = min(float(vorher.get("saettigung", 0.0)),
               float(nachher.get("saettigung", 0.0)))
    farbe = 0.0
    if bunt >= FARBE_AB:
        farbe = farbton_abstand(vorher.get("farbton", 0.0),
                                nachher.get("farbton", 0.0))
    return max(hell, farbe)


def zuordnen(beobachtungen):
    """Aus Experimenten ablesen, welches Feld zu welchem Raum gehoert.

    beobachtungen: Liste von dicts, je ein Experiment:
        {"geschaltet": ["buero"],      # welche Raeume geschaltet wurden
         "vorher":  [messung_feld0, messung_feld1, ...],
         "nachher": [messung_feld0, messung_feld1, ...]}
      Eine Messung ist ein dict mit "helligkeit", "farbton", "saettigung"
      - genau das, was /messung je Feld liefert.

    Rueckgabe: (zuordnung, unsicher)
      zuordnung: dict feldnummer -> raumname
      unsicher:  Liste von (feldnummer, grund) fuer alles, worueber die
                 Messungen KEINE Aussage zulassen
    """
    belege = {}          # feld -> {raum: staerkste Aenderung}
    zweifel = {}         # feld -> Grund

    for versuch in beobachtungen or []:
        raeume = versuch.get("geschaltet") or []
        # Tims Regel: Nur einzelne Schaltungen geben Auskunft. Aendern
        # sich Felder, waehrend zwei Raeume zugleich geschaltet wurden,
        # weiss niemand, welcher der beiden es war.
        if len(raeume) != 1:
            continue
        raum = raeume[0]
        vorher = versuch.get("vorher") or []
        nachher = versuch.get("nachher") or []
        for feld in range(min(len(vorher), len(nachher))):
            wert = aenderung(vorher[feld], nachher[feld])
            if wert >= DEUTLICH:
                belege.setdefault(feld, {})
                belege[feld][raum] = max(belege[feld].get(raum, 0.0), wert)
            elif wert >= KAUM:
                # Dazwischen: Streulicht, Tageslicht, eine Wolke. Nicht
                # raten - aber auch nicht vergessen.
                zweifel.setdefault(feld, "Aenderung von %.2f beim Schalten "
                                         "von %s ist nicht eindeutig"
                                         % (wert, raum))

    zuordnung = {}
    unsicher = []
    for feld, raeume in sorted(belege.items()):
        if len(raeume) == 1:
            zuordnung[feld] = next(iter(raeume))
        else:
            # Ein Feld, das auf zwei Raeume anspricht, ist keine Lampe,
            # sondern eine Spiegelung oder eine angestrahlte Wand. Lieber
            # keine Auskunft als eine falsche.
            unsicher.append((feld, "spricht auf mehrere Raeume an: %s"
                             % ", ".join(sorted(raeume))))
    for feld, grund in sorted(zweifel.items()):
        if feld not in zuordnung and not any(f == feld for f, _ in unsicher):
            unsicher.append((feld, grund))
    return zuordnung, sorted(unsicher)


def selbsttest():
    fehler = 0

    def pruefe(bedingung, text, zusatz=""):
        nonlocal fehler
        if bedingung:
            print("  ok      %s" % text)
        else:
            print("  FEHLER  %s%s" % (text, "  [%s]" % zusatz if zusatz else ""))
            fehler += 1

    def m(hell, ton=0.0, satt=0.0):
        return {"helligkeit": hell, "farbton": ton, "saettigung": satt}

    print("zuordnung Selbsttest (reine Logik, keine Hardware):")

    pruefe(zuordnen([]) == ({}, []), "ohne Experimente keine Behauptung")
    pruefe(zuordnen(None) == ({}, []), "und auch nicht bei gar nichts")

    # Der Normalfall: Buero schalten, Feld 0 wird hell, Feld 1 nicht.
    einer = [{"geschaltet": ["buero"],
              "vorher":  [m(0.10), m(0.50)],
              "nachher": [m(0.80), m(0.51)]}]
    zu, un = zuordnen(einer)
    pruefe(zu == {0: "buero"}, "das Feld, das sich aendert, gehoert zum Raum",
           str(zu))
    pruefe(un == [], "das unveraenderte Feld erzeugt keinen Zweifel", str(un))

    # Mexlas Frage: eine GRUPPE aendert sich gleichzeitig, eine andere Lampe
    # nicht. Genau dafuer ist der Umbau da.
    gruppe = [{"geschaltet": ["buero"],
               "vorher":  [m(0.10), m(0.10), m(0.60)],
               "nachher": [m(0.75), m(0.72), m(0.61)]},
              {"geschaltet": ["flur"],
               "vorher":  [m(0.75), m(0.72), m(0.60)],
               "nachher": [m(0.76), m(0.73), m(0.10)]}]
    zu, un = zuordnen(gruppe)
    pruefe(zu == {0: "buero", 1: "buero", 2: "flur"},
           "zwei Lampen, die zusammen reagieren, werden EINE Gruppe - "
           "die dritte gehoert zum Flur", str(zu))

    # Farbe allein reicht: gleich hell, aber rot statt blau.
    farbe = [{"geschaltet": ["buero"],
              "vorher":  [m(0.50, 0.0, 0.9)],
              "nachher": [m(0.50, 240.0, 0.9)]}]
    pruefe(zuordnen(farbe)[0] == {0: "buero"},
           "ein Farbwechsel bei gleicher Helligkeit zaehlt auch")

    # Aber nur, wenn wirklich Farbe da ist. Grau hat keinen ablesbaren Ton.
    grau = [{"geschaltet": ["buero"],
             "vorher":  [m(0.50, 0.0, 0.02)],
             "nachher": [m(0.50, 240.0, 0.02)]}]
    pruefe(zuordnen(grau)[0] == {},
           "im grauen Feld ist der Farbton Rauschen und zaehlt nicht")

    # Der runde Farbkreis: 350 und 10 Grad sind NAH beieinander.
    pruefe(farbton_abstand(350, 10) < 0.15,
           "350 und 10 Grad liegen dicht beieinander, nicht weit auseinander",
           "%.3f" % farbton_abstand(350, 10))
    pruefe(farbton_abstand(0, 180) > 0.99, "Gegenfarben liegen maximal weit")

    # Tims Regel 4, unveraendert: gleichzeitig geschaltet sagt nichts.
    doppelt = [{"geschaltet": ["buero", "flur"],
                "vorher":  [m(0.10)], "nachher": [m(0.90)]}]
    pruefe(zuordnen(doppelt) == ({}, []),
           "zwei Raeume gleichzeitig geschaltet lassen keinen Schluss zu")

    # Tims Zwischenbereich: nicht raten.
    knapp = [{"geschaltet": ["buero"],
              "vorher":  [m(0.50)], "nachher": [m(0.60)]}]
    zu, un = zuordnen(knapp)
    pruefe(zu == {} and len(un) == 1,
           "eine Aenderung zwischen 0.05 und 0.15 gilt als unsicher",
           "%s / %s" % (zu, un))
    kaum = [{"geschaltet": ["buero"],
             "vorher":  [m(0.50)], "nachher": [m(0.52)]}]
    pruefe(zuordnen(kaum) == ({}, []),
           "eine winzige Aenderung ist gar keine")

    # Widerspruch: ein Feld reagiert auf zwei Raeume - Spiegelung.
    spiegel = [{"geschaltet": ["buero"],
                "vorher": [m(0.10)], "nachher": [m(0.80)]},
               {"geschaltet": ["flur"],
                "vorher": [m(0.10)], "nachher": [m(0.80)]}]
    zu, un = zuordnen(spiegel)
    pruefe(zu == {} and un and "mehrere" in un[0][1],
           "ein Feld, das auf zwei Raeume anspricht, wird NICHT zugeordnet",
           "%s / %s" % (zu, un))

    # Verschieden viele Felder in vorher/nachher darf nicht stuerzen.
    schief = [{"geschaltet": ["buero"],
               "vorher": [m(0.1), m(0.1)], "nachher": [m(0.9)]}]
    pruefe(zuordnen(schief)[0] == {0: "buero"},
           "ungleich lange Messreihen stuerzen nicht, sondern messen, "
           "was da ist")

    if fehler:
        print("\n%d Fehler." % fehler)
    else:
        print("\nAlles in Ordnung.")
    return fehler


def main(argumente):
    if argumente and argumente[0] == "--selbsttest":
        return selbsttest()
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
