#!/usr/bin/env python3
"""Die Farbmessung des Kameradienstes im Klartext holen.

Warum es diese Datei gibt: Der Kameradienst liefert JSON. Das ist richtig
so fuer Programme, aber Tim spricht seine Antworten - und der Job-Server
zeigt sie im Browser. Beide brauchen Saetze, keine geschweiften Klammern.
Deshalb sitzt die Uebersetzung hier und nicht im Dienst: Der Dienst haelt
die Kamera (und damit die macOS-Kameraerlaubnis), diese Datei haelt die
Sprache.

Aufruf:

    kamera_cli.py                  aktuelle Messung, lesbar
    kamera_cli.py messung          dasselbe, ausgeschrieben
    kamera_cli.py --selbsttest     prueft die Textausgabe ohne Dienst

Der Rueckgabewert ist 0, wenn wirklich gemessen wurde, sonst 1. Der
Job-Server faerbt daran seine Ausgabe, und die Selbsttests haengen daran.
"""

import json
import sys
import urllib.error
import urllib.request

# Derselbe Ort, den kamera_dienst.py bedient. Bewusst 127.0.0.1: Das Bild
# aus der Wohnung gehoert nicht ins Netz.
ADRESSE = "http://127.0.0.1:8781/messung"
ZEITGRENZE = 5


def messung_holen(adresse: str = ADRESSE) -> dict:
    """Die Messung abfragen. Gibt bei Problemen ein dict mit 'fehler'.

    Kein Absturz, keine Ausnahme nach aussen: Der Aufrufer ist der
    Job-Server oder der Sprachassistent, und dort ist ein abgestuerztes
    Werkzeug schlechter als ein Satz, der sagt, was fehlt.
    """
    try:
        with urllib.request.urlopen(adresse, timeout=ZEITGRENZE) as antwort:
            roh = antwort.read().decode("utf-8", "replace")
    except urllib.error.URLError as fehler:
        return {"fehler": "Kameradienst nicht erreichbar (%s). Laeuft "
                          "kamera_dienst.py?" % fehler.reason}
    except OSError as fehler:
        return {"fehler": "Kameradienst nicht erreichbar: %s" % fehler}
    try:
        daten = json.loads(roh)
    except ValueError:
        return {"fehler": "Der Kameradienst hat kein JSON geliefert."}
    if not isinstance(daten, dict):
        return {"fehler": "Der Kameradienst hat kein Messergebnis geliefert."}
    return daten


def zeilen_bauen(daten: dict) -> list:
    """Aus dem Messergebnis lesbare Zeilen machen.

    Reine Rechnung ohne Netz - deshalb im Selbsttest pruefbar. Der
    Sprachassistent liest die letzten Zeilen vor, darum bleiben es
    wenige und kurze.
    """
    if not isinstance(daten, dict) or daten.get("fehler"):
        grund = (daten or {}).get("fehler") or "unbekannter Fehler"
        return ["FEHLER %s" % grund]

    name = daten.get("name")
    if name is None:
        return ["FEHLER Der Kameradienst hat keine Farbe gemeldet."]

    zeilen = ["Kamera sieht: %s  %s" % (name, daten.get("hex", "?"))]

    # Saettigung und Helligkeit kommen als 0 bis 1 - Prozent versteht man
    # beim Zuhoeren sofort, 0.424 nicht.
    farbton = daten.get("farbton")
    saettigung = daten.get("saettigung")
    helligkeit = daten.get("helligkeit")
    if None not in (farbton, saettigung, helligkeit):
        zeilen.append("Farbton %d Grad, Saettigung %d Prozent, "
                      "Helligkeit %d Prozent"
                      % (round(farbton), round(saettigung * 100),
                         round(helligkeit * 100)))

    # Ein altes Bild ist die stille Falle: Der Dienst antwortet, die
    # Kamera liefert aber seit Minuten nichts mehr. Dann misst man die
    # Vergangenheit und haelt sie fuer die Lampe von jetzt.
    alter = daten.get("bildalter_s")
    if alter is not None and alter > 3:
        zeilen.append("ACHTUNG Das Bild ist %.0f Sekunden alt - die Kamera "
                      "liefert gerade nichts Neues." % alter)
    return zeilen


def main(argumente) -> int:
    if argumente and argumente[0] == "--selbsttest":
        return selbsttest()
    if argumente and argumente[0] not in ("messung", "schauen"):
        print("FEHLER unbekannter Aufruf: %s" % argumente[0])
        return 1

    daten = messung_holen()
    zeilen = zeilen_bauen(daten)
    for zeile in zeilen:
        print(zeile)
    return 1 if zeilen[0].startswith("FEHLER") else 0


# ----------------------------------------------------------------------
# Selbsttest - prueft die Uebersetzung, ohne den Dienst zu brauchen
# ----------------------------------------------------------------------

def selbsttest() -> int:
    fehler = 0

    def pruefe(bedingung, text, zusatz=""):
        nonlocal fehler
        if bedingung:
            print("  ok      %s" % text)
        else:
            print("  FEHLER  %s%s" % (text, "  [%s]" % zusatz if zusatz else ""))
            fehler += 1

    print("kamera_cli Selbsttest (ohne Kameradienst):")

    echt = {"rot": 112.5, "gruen": 101.7, "blau": 176.4, "farbton": 248.7,
            "saettigung": 0.424, "helligkeit": 0.692, "name": "blau",
            "hex": "#7065b0", "bildalter_s": 0.02}
    zeilen = zeilen_bauen(echt)
    pruefe("blau" in zeilen[0] and "#7065b0" in zeilen[0],
           "Farbe und Hexwert stehen in der ersten Zeile", str(zeilen[0]))
    # Gegenprobe zur Prozentrechnung: 0.424 muss 42 werden, nicht 0 und
    # nicht 424. Genau hier faellt ein vergessener Faktor 100 auf.
    pruefe("42 Prozent" in zeilen[1] and "69 Prozent" in zeilen[1],
           "Saettigung und Helligkeit kommen als Prozent", str(zeilen[1]))
    pruefe("249 Grad" in zeilen[1], "Farbton wird gerundet", str(zeilen[1]))
    pruefe(len(zeilen) == 2, "frisches Bild bekommt keine Altwarnung",
           str(zeilen))

    alt = dict(echt, bildalter_s=42.0)
    pruefe(any("ACHTUNG" in z for z in zeilen_bauen(alt)),
           "altes Bild wird als solches gemeldet", str(zeilen_bauen(alt)))

    # Der Dienst meldet genau so, wenn die Kamera noch kein Bild hat.
    pruefe(zeilen_bauen({"fehler": "noch kein Bild von der Kamera"})[0]
           .startswith("FEHLER"),
           "Fehlermeldung des Dienstes wird durchgereicht")
    pruefe(zeilen_bauen({})[0].startswith("FEHLER"),
           "leere Antwort ergibt einen Fehler, keinen Absturz")
    pruefe(zeilen_bauen({"name": "rot"})[0].startswith("Kamera sieht: rot"),
           "unvollstaendige Antwort stuerzt nicht ab",
           str(zeilen_bauen({"name": "rot"})))

    # Ein nicht belegter Port muss als Fehler zurueckkommen, nicht als
    # Ausnahme - sonst sieht der Bediener einen Stacktrace statt eines
    # Satzes.
    aus = messung_holen("http://127.0.0.1:9/messung")
    pruefe("fehler" in aus, "toter Port ergibt eine Fehlermeldung", str(aus))

    if fehler:
        print("\n%d Fehler." % fehler)
    else:
        print("\nAlles ok.")
    return fehler


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
