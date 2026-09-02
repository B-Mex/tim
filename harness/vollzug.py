#!/usr/bin/env python3
"""Hat Tim wirklich getan, was er behauptet? Am Dateisystem nachgemessen.

Warum es das gibt
-----------------
Erfundener Vollzug ist in dieser Anlage das schaerfste
Ausschlusskriterium: qwen3.6 wurde dafuer deinstalliert (2 von 5
Kettentest-Laeufen mit erfundenem Schritt), gemma4 ist am 01.09.2026
daran gescheitert - er meldete drei gefahrene Dateien, es waren zwei,
und die dritte Ausgabe ("CAESAR") hatte er dazuerfunden.

Aber das Abitur misst das EINMAL. Ein bestandenes Zeugnis ist eine
Momentaufnahme; im Betrieb prueft danach niemand mehr nach. Genau da
ist Hermes weiter: Es prueft in jedem Turn, ob eine behauptete
Dateiaenderung wirklich passiert ist, und haengt die Wahrheit unter die
Antwort - "trust the file-mutation verifier footer over the assistant's
closing summary".

Der Grundsatz
-------------
Gemessen wird die PLATTE, nicht die Rueckgabe des Werkzeugs. Ein
Werkzeug, das "ok" meldet, ist eine zweite Behauptung - und der ganze
Sinn dieses Moduls ist, Behauptungen nicht zu glauben. Deshalb liest
es die Datei und vergleicht ihren Inhalt mit dem, was geschrieben
werden sollte.

Was NICHT geprueft wird
-----------------------
shell_befehl. Ein beliebiger Befehl hat keine vorhersagbare Wirkung auf
das Dateisystem; dort etwas zu behaupten waere selbst eine Erfindung.
Hermes deckt aus demselben Grund nur write_file und patch ab. Diese
Grenze steht hier, damit sie jemand sieht - nicht damit sie
verschwiegen wird.

Aufruf:
    vollzug.py --selbsttest
"""
from __future__ import annotations

import sys
from pathlib import Path

# Werkzeuge, die eine Datei anlegen oder ueberschreiben, und aus welchem
# Argument der Pfad bzw. der Inhalt kommt.
SCHREIBENDE = {
    "werkstatt_schreiben": ("pfad", "inhalt", "werkstatt"),
    "livewerkstatt_schreiben": ("pfad", "inhalt", "livewerkstatt"),
}


def _pfad_aufloesen(sandkasten: str, relpfad: str, aufloeser=None):
    """Den echten Ort einer Datei finden - ueber den Sandkasten-Riegel.

    Bewusst ueber pfad_erlaubt() der jeweiligen Werkstatt: Wer hier
    einen eigenen Pfad zusammenbaut, prueft am Ende woanders als
    geschrieben wurde, und das Ergebnis waere wertlos.
    """
    if aufloeser is not None:
        return aufloeser(sandkasten, relpfad)
    sys.path.insert(0, "/opt/ki-server/harness")
    modul = __import__("werkstatt" if sandkasten == "werkstatt"
                       else "livewerkstatt")
    ziel, _grund = modul.pfad_erlaubt(relpfad)
    return ziel


def pruefen(vollzuege: list, aufloeser=None) -> list:
    """Je Schreibvorgang: ist die Datei da, und steht drin, was drinstehen sollte?

    vollzuege: [{"werkzeug": str, "argumente": dict}, ...]
    Rueckgabe: [{"werkzeug", "pfad", "gelandet": bool, "grund": str}, ...]

    Wirft nie. Ein Pruefer, der eine Antwort verhindern kann, ist
    schlimmer als eine unbelegte Antwort.
    """
    befunde = []
    for v in vollzuege or []:
        name = str(v.get("werkzeug") or "")
        if name not in SCHREIBENDE:
            continue
        pfad_arg, inhalt_arg, sandkasten = SCHREIBENDE[name]
        argumente = v.get("argumente") or {}
        relpfad = str(argumente.get(pfad_arg, "")).strip()
        soll = str(argumente.get(inhalt_arg, ""))
        eintrag = {"werkzeug": name, "pfad": relpfad}
        if not relpfad:
            eintrag.update(gelandet=False, grund="kein Pfad im Aufruf")
            befunde.append(eintrag)
            continue
        try:
            ziel = _pfad_aufloesen(sandkasten, relpfad, aufloeser)
        except Exception as f:
            eintrag.update(gelandet=False,
                           grund="Pfad nicht aufloesbar (%s)" % type(f).__name__)
            befunde.append(eintrag)
            continue
        if ziel is None:
            eintrag.update(gelandet=False,
                           grund="Pfad ausserhalb des Sandkastens")
            befunde.append(eintrag)
            continue
        try:
            if not Path(ziel).is_file():
                eintrag.update(gelandet=False, grund="Datei existiert nicht")
            else:
                ist = Path(ziel).read_text(encoding="utf-8", errors="replace")
                if ist == soll:
                    eintrag.update(gelandet=True, grund="")
                else:
                    eintrag.update(
                        gelandet=False,
                        grund="Inhalt weicht ab (%d statt %d Zeichen)"
                              % (len(ist), len(soll)))
        except OSError as f:
            eintrag.update(gelandet=False,
                           grund="nicht lesbar (%s)" % type(f).__name__)
        befunde.append(eintrag)
    return befunde


def fussnote(befunde: list) -> str:
    """Die Wahrheit unter die Antwort - nur wenn es etwas zu sagen gibt.

    Bewusst KEINE Fussnote, wenn alles gelandet ist: Eine Zeile, die bei
    jeder Antwort steht, liest nach drei Tagen niemand mehr. Sie soll
    auffallen, wenn sie da ist.
    """
    schief = [b for b in befunde or [] if not b.get("gelandet")]
    if not schief:
        return ""
    zeilen = ["", "---",
              "**Nachgemessen am Dateisystem — nicht alles ist gelandet:**"]
    for b in schief:
        zeilen.append("- `%s` (%s): **%s**"
                      % (b.get("pfad") or "?", b.get("werkzeug", "?"),
                         b.get("grund") or "nicht gelandet"))
    zeilen.append("")
    zeilen.append("*Diese Zeilen stammen von der Zentrale, nicht vom "
                  "Modell. Wo sie der Antwort widersprechen, gilt die "
                  "Messung.*")
    return "\n".join(zeilen)


def selbsttest() -> int:
    import tempfile
    fehler = []

    def pruefe(b, t, z=""):
        print("  %-7s %s%s" % ("ok" if b else "FEHLER", t,
                               "" if b else "  <- " + str(z)[:140]))
        if not b:
            fehler.append(t)

    print("vollzug Selbsttest (nur Fixtures, kein Sandkasten angefasst):")

    with tempfile.TemporaryDirectory() as tmp:
        ordner = Path(tmp)
        (ordner / "da.py").write_text("print('ANTON')\n", encoding="utf-8")
        (ordner / "anders.py").write_text("was anderes\n", encoding="utf-8")

        def aufl(sandkasten, rel):
            return ordner / rel

        # 1. Gelandet
        b = pruefen([{"werkzeug": "werkstatt_schreiben",
                      "argumente": {"pfad": "da.py",
                                    "inhalt": "print('ANTON')\n"}}], aufl)
        pruefe(b[0]["gelandet"], "eine wirklich geschriebene Datei gilt als "
                                 "gelandet", str(b))
        pruefe(fussnote(b) == "",
               "und erzeugt KEINE Fussnote - sonst liest sie bald niemand")

        # 2. Gar nicht da - der gemma4-Fall
        b = pruefen([{"werkzeug": "werkstatt_schreiben",
                      "argumente": {"pfad": "kette3.py",
                                    "inhalt": "print('CAESAR')\n"}}], aufl)
        pruefe(not b[0]["gelandet"] and "existiert nicht" in b[0]["grund"],
               "eine behauptete, aber fehlende Datei faellt auf", str(b))
        f = fussnote(b)
        pruefe("kette3.py" in f and "Messung" in f,
               "die Fussnote nennt die Datei und sagt, wer misst")

        # 3. Da, aber mit anderem Inhalt - die heimtueckische Form
        b = pruefen([{"werkzeug": "werkstatt_schreiben",
                      "argumente": {"pfad": "anders.py",
                                    "inhalt": "print('BERTA')\n"}}], aufl)
        pruefe(not b[0]["gelandet"] and "weicht ab" in b[0]["grund"],
               "eine Datei mit falschem Inhalt gilt NICHT als gelandet",
               str(b))

        # 4. Werkzeuge ohne Dateiwirkung werden nicht bewertet
        b = pruefen([{"werkzeug": "shell_befehl",
                      "argumente": {"befehl": "rm -rf /"}},
                     {"werkzeug": "kamerabild", "argumente": {}}], aufl)
        pruefe(b == [],
               "shell_befehl und Lesewerkzeuge werden NICHT beurteilt - "
               "ihre Wirkung ist nicht vorhersagbar")

        # 5. Kein Pfad, Pfad ausserhalb, kaputter Aufloeser
        b = pruefen([{"werkzeug": "werkstatt_schreiben",
                      "argumente": {"inhalt": "x"}}], aufl)
        pruefe(not b[0]["gelandet"], "ein Aufruf ohne Pfad gilt als nicht "
                                     "gelandet")
        b = pruefen([{"werkzeug": "werkstatt_schreiben",
                      "argumente": {"pfad": "x.py", "inhalt": "x"}}],
                    lambda s, r: None)
        pruefe(not b[0]["gelandet"] and "Sandkasten" in b[0]["grund"],
               "ein Pfad ausserhalb des Sandkastens faellt auf")

        def kaputt(s, r):
            raise RuntimeError("Aufloeser kaputt")

        b = pruefen([{"werkzeug": "werkstatt_schreiben",
                      "argumente": {"pfad": "x.py", "inhalt": "x"}}], kaputt)
        pruefe(not b[0]["gelandet"],
               "ein kaputter Aufloeser wirft nicht, er meldet")

    pruefe(pruefen([]) == [] and pruefen(None) == [],
           "leere Eingabe ergibt leere Befunde")
    pruefe(fussnote([]) == "" and fussnote(None) == "",
           "und leere Befunde ergeben keine Fussnote")

    print("\n%s" % ("Alle Pruefungen gruen." if not fehler
                    else "%d FEHLER." % len(fehler)))
    return 1 if fehler else 0


if __name__ == "__main__":
    if "--selbsttest" in sys.argv:
        sys.exit(selbsttest())
    print(__doc__)
