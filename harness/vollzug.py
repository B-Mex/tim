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

HARNESS_DIR = "/opt/ki-server/harness"

# Werkzeuge, die eine Datei anlegen oder ueberschreiben, und aus welchem
# Argument der Pfad bzw. der Inhalt kommt.
SCHREIBENDE = {
    "werkstatt_schreiben": ("pfad", "inhalt", "werkstatt"),
    "livewerkstatt_schreiben": ("pfad", "inhalt", "livewerkstatt"),
}


def _pfad_aufloesen(sandkasten: str, relpfad: str, aufloeser=None):
    """Den echten Ort einer Datei finden - ueber den Sandkasten-Riegel.

    Rueckgabe: (ziel_oder_None, grund). Der GRUND kommt aus der
    Werkstatt selbst und wird durchgereicht, statt hier neu erfunden zu
    werden: livewerkstatt.pfad_erlaubt() lehnt auch wegen der ENDUNG ab
    ("versuch.sh"), und die Fussnote schrieb dafuer bis zum 02.09.2026
    "Pfad ausserhalb des Sandkastens". Das las sich wie ein
    Ausbruchsversuch, wo nur eine Endung fehlte - eine Messung, die
    falsch anklagt, ist so schaedlich wie eine, die schweigt.

    Bewusst ueber pfad_erlaubt() der jeweiligen Werkstatt: Wer hier
    einen eigenen Pfad zusammenbaut, prueft am Ende woanders als
    geschrieben wurde, und das Ergebnis waere wertlos.
    """
    if aufloeser is not None:
        # Ein Fixture-Aufloeser darf nur den Pfad liefern (der haeufige
        # Fall) oder dasselbe Paar wie die Werkstatt - sonst liesse sich
        # das Durchreichen des Grundes nicht ohne echten Sandkasten
        # pruefen.
        antwort = aufloeser(sandkasten, relpfad)
        if isinstance(antwort, tuple):
            return antwort
        return antwort, ""
    # Nur EINMAL eintragen. Vorher wuchs sys.path bei jedem Aufruf um
    # einen Eintrag (gemessen 02.09.2026: 50 Aufrufe, 50 Eintraege) -
    # in einem Dauerdienst ist das ein Leck, das mit jedem Chat-Turn
    # groesser wird.
    if HARNESS_DIR not in sys.path:
        sys.path.insert(0, HARNESS_DIR)
    modul = __import__("werkstatt" if sandkasten == "werkstatt"
                       else "livewerkstatt")
    return modul.pfad_erlaubt(relpfad)


def _inhalt_vergleichen(ziel, soll: str) -> tuple:
    """Steht in der Datei, was drinstehen sollte? (gelandet, grund)

    Verglichen werden BYTES, nicht der von Python umgeschriebene Text.
    Path.read_text() uebersetzt \\r\\n und \\r still nach \\n
    (universal newlines); die Werkstatt schreibt aber roh
    (werkstatt.py:166 write_bytes). Gemessen am 02.09.2026: eine
    byte-genau geschriebene Datei mit CRLF kam als "Inhalt weicht ab
    (14 statt 16 Zeichen)" heraus - ein Falschalarm unter einer
    tadellosen Antwort. Genau das darf diese Fussnote nie tun.
    """
    try:
        sollbytes = soll.encode("utf-8")
    except UnicodeEncodeError:
        # Dann konnte die Werkstatt es auch nicht schreiben - sie
        # kodiert vor dem Schreiben mit derselben Regel.
        return False, "Inhalt ist nicht als UTF-8 schreibbar"
    ist = Path(ziel).read_bytes()
    if ist == sollbytes:
        return True, ""
    return False, ("Inhalt weicht ab (%d statt %d Bytes)"
                   % (len(ist), len(sollbytes)))


def pruefen(vollzuege: list, aufloeser=None) -> list:
    """Je Schreibvorgang: ist die Datei da, und steht drin, was drinstehen sollte?

    vollzuege: [{"werkzeug": str, "argumente": dict}, ...]
    Rueckgabe: [{"werkzeug", "pfad", "gelandet": bool, "grund": str}, ...]

    Wird dieselbe Datei in EINEM Turn mehrfach geschrieben, zaehlt nur
    der LETZTE Schreibvorgang. Vorher meldete der erste zwangslaeufig
    "Inhalt weicht ab", obwohl beide Schreibvorgaenge geglueckt waren -
    ein Falschalarm bei voellig korrekter Arbeit (gemessen 02.09.2026).
    Hermes zieht dieselbe Grenze und nennt sie "never superseded by a
    successful write to the same path" (turn_finalizer.py:523).

    Wirft nie. Ein Pruefer, der eine Antwort verhindern kann, ist
    schlimmer als eine unbelegte Antwort. Das gilt auch fuer
    Werkzeugargumente, die kein dict sind: Was ein Modell in
    "arguments" schreibt, ist nicht vorhersagbar - eine Liste dort
    liess pruefen() bis zum 02.09.2026 mit AttributeError abstuerzen,
    und der Aufrufer verschluckte damit die Fussnote fuer den GANZEN
    Turn, also auch fuer echte Fehlschlaege daneben.
    """
    roh = []
    for v in vollzuege or []:
        if not isinstance(v, dict):
            continue
        name = str(v.get("werkzeug") or "")
        if name not in SCHREIBENDE:
            continue
        pfad_arg, inhalt_arg, sandkasten = SCHREIBENDE[name]
        argumente = v.get("argumente")
        if argumente is None:
            argumente = {}
        if not isinstance(argumente, dict):
            roh.append({"werkzeug": name, "pfad": "", "ziel": None,
                        "gelandet": False,
                        "grund": "Argumente unlesbar (%s statt Objekt)"
                                 % type(argumente).__name__})
            continue
        relpfad = str(argumente.get(pfad_arg, "")).strip()
        soll = str(argumente.get(inhalt_arg, ""))
        eintrag = {"werkzeug": name, "pfad": relpfad, "ziel": None}
        if not relpfad:
            eintrag.update(gelandet=False, grund="kein Pfad im Aufruf")
            roh.append(eintrag)
            continue
        try:
            ziel, grund = _pfad_aufloesen(sandkasten, relpfad, aufloeser)
        except Exception as f:
            eintrag.update(gelandet=False,
                           grund="Pfad nicht aufloesbar (%s)" % type(f).__name__)
            roh.append(eintrag)
            continue
        if ziel is None:
            eintrag.update(gelandet=False,
                           grund=grund or "Pfad ausserhalb des Sandkastens")
            roh.append(eintrag)
            continue
        # Der Schluessel ist der AUFGELOESTE Ort, nicht die Zeichenkette:
        # "./a.py" und "a.py" sind dieselbe Datei.
        eintrag["ziel"] = str(ziel)
        try:
            gelandet, grund = _inhalt_vergleichen(ziel, soll) \
                if Path(ziel).is_file() else (False, "Datei existiert nicht")
            eintrag.update(gelandet=gelandet, grund=grund)
        except OSError as f:
            eintrag.update(gelandet=False,
                           grund="nicht lesbar (%s)" % type(f).__name__)
        roh.append(eintrag)

    # Ueberholte Schreibvorgaenge fallen raus: Wer dieselbe Datei im
    # selben Turn zweimal schreibt, wird am ZWEITEN Stand gemessen.
    spaeter = {}
    for i, e in enumerate(roh):
        if e.get("ziel"):
            spaeter[(e["werkzeug"], e["ziel"])] = i
    befunde = []
    for i, e in enumerate(roh):
        if e.get("ziel") and spaeter.get((e["werkzeug"], e["ziel"])) != i:
            continue
        e.pop("ziel", None)
        befunde.append(e)
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

        # 6. Der Grund der Werkstatt wird DURCHGEREICHT, nicht erfunden.
        #    livewerkstatt.pfad_erlaubt() lehnt auch wegen der ENDUNG ab
        #    (livewerkstatt.py:100) und gibt genau diesen Grund zurueck.
        #    Bis zum 02.09.2026 warf vollzug ihn weg und schrieb "Pfad
        #    ausserhalb des Sandkastens" - eine falsche Anklage unter
        #    Tims Antwort, wo nur eine Endung fehlte.
        b = pruefen([{"werkzeug": "livewerkstatt_schreiben",
                      "argumente": {"pfad": "versuch.sh", "inhalt": "x"}}],
                    lambda s, r: (None, "Endung nicht erlaubt (.py, .json)"))
        pruefe(not b[0]["gelandet"] and "Endung" in b[0]["grund"],
               "eine abgelehnte Endung wird als Endung gemeldet, nicht "
               "als Ausbruchsversuch", str(b))

        # 7. FALSCHALARM: zweimal dieselbe Datei in EINEM Turn.
        #    Beide Schreibvorgaenge geglueckt - trotzdem meldete der
        #    erste "Inhalt weicht ab", weil auf der Platte laengst der
        #    zweite Stand liegt (gemessen 02.09.2026).
        (ordner / "zweimal.py").write_bytes(b"stand 2\n")
        b = pruefen([{"werkzeug": "werkstatt_schreiben",
                      "argumente": {"pfad": "zweimal.py",
                                    "inhalt": "stand 1\n"}},
                     {"werkzeug": "werkstatt_schreiben",
                      "argumente": {"pfad": "./zweimal.py",
                                    "inhalt": "stand 2\n"}}], aufl)
        pruefe(len(b) == 1 and b[0]["gelandet"] and fussnote(b) == "",
               "zweimal dieselbe Datei: nur der LETZTE Stand zaehlt - "
               "keine Fussnote unter tadelloser Arbeit", str(b))
        #    Gegenprobe: ueberholt heisst nicht blind. Landet der zweite
        #    Schreibvorgang nicht, MUSS die Fussnote kommen.
        b = pruefen([{"werkzeug": "werkstatt_schreiben",
                      "argumente": {"pfad": "zweimal.py",
                                    "inhalt": "stand 2\n"}},
                     {"werkzeug": "werkstatt_schreiben",
                      "argumente": {"pfad": "zweimal.py",
                                    "inhalt": "stand 3\n"}}], aufl)
        pruefe(len(b) == 1 and not b[0]["gelandet"] and fussnote(b) != "",
               "aber ein nicht gelandeter ZWEITER Stand faellt weiter auf",
               str(b))
        #    Und zwei verschiedene Dateien bleiben zwei Befunde.
        b = pruefen([{"werkzeug": "werkstatt_schreiben",
                      "argumente": {"pfad": "da.py", "inhalt": "x"}},
                     {"werkzeug": "werkstatt_schreiben",
                      "argumente": {"pfad": "anders.py", "inhalt": "y"}}],
                    aufl)
        pruefe(len(b) == 2,
               "zwei verschiedene Dateien bleiben zwei Befunde", str(b))

        # 8. FALSCHALARM: CRLF. Die Werkstatt schreibt roh
        #    (write_bytes); read_text() haette \r\n still nach \n
        #    uebersetzt und eine byte-genaue Datei angeschwaerzt.
        _crlf = "zeile1\r\nzeile2\r\n"
        (ordner / "crlf.py").write_bytes(_crlf.encode("utf-8"))
        b = pruefen([{"werkzeug": "werkstatt_schreiben",
                      "argumente": {"pfad": "crlf.py", "inhalt": _crlf}}],
                    aufl)
        pruefe(b[0]["gelandet"] and fussnote(b) == "",
               "CRLF byte-genau geschrieben gilt als gelandet", str(b))
        _cr = "zeile1\rzeile2"
        (ordner / "cr.py").write_bytes(_cr.encode("utf-8"))
        b = pruefen([{"werkzeug": "werkstatt_schreiben",
                      "argumente": {"pfad": "cr.py", "inhalt": _cr}}], aufl)
        pruefe(b[0]["gelandet"], "und ein einzelnes \\r ebenso", str(b))
        #    Gegenprobe: der Byte-Vergleich ist nicht zu grosszuegig.
        b = pruefen([{"werkzeug": "werkstatt_schreiben",
                      "argumente": {"pfad": "crlf.py",
                                    "inhalt": "zeile1\nzeile2\n"}}], aufl)
        pruefe(not b[0]["gelandet"] and "Bytes" in b[0]["grund"],
               "wer \\n schreiben wollte und \\r\\n vorfindet, faellt auf",
               str(b))

        # 9. Kaputte Argumente werfen nicht. Ein Modell darf in
        #    "arguments" alles schreiben; eine Liste dort liess pruefen()
        #    mit AttributeError abstuerzen - und der Aufrufer verschluckte
        #    dann die Fussnote fuer den GANZEN Turn.
        b = pruefen([{"werkzeug": "werkstatt_schreiben",
                      "argumente": ["kaputt"]}], aufl)
        pruefe(len(b) == 1 and not b[0]["gelandet"],
               "eine Liste in den Argumenten wird gemeldet, nicht geworfen",
               str(b))
        b = pruefen([{"werkzeug": "werkstatt_schreiben",
                      "argumente": {"pfad": "kette3.py", "inhalt": "CAESAR"}},
                     {"werkzeug": "werkstatt_schreiben",
                      "argumente": "auch kaputt"},
                     "gar kein Aufruf"], aufl)
        pruefe(len(b) == 2 and "kette3.py" in fussnote(b),
               "und ein echter Fehlschlag daneben geht NICHT mit unter",
               str(b))

    # 10. Der echte Aufloeser darf sys.path nicht aufblaehen.
    #     Gemessen 02.09.2026: 50 Aufrufe -> 50 neue sys.path-Eintraege.
    #     Der absolute Pfad wird von livewerkstatt.pfad_erlaubt()
    #     abgelehnt, BEVOR irgendetwas aufgeloest wird (livewerkstatt.py:99,
    #     reine Zeichenkettenpruefung) - der Sandkasten bleibt also auch
    #     hier unangetastet, und der echte Grund kommt mit.
    _absolut = [{"werkzeug": "livewerkstatt_schreiben",
                 "argumente": {"pfad": "/etc/passwd", "inhalt": "x"}}]
    b = pruefen(_absolut)
    pruefe(not b[0]["gelandet"] and "Sandkasten" in b[0]["grund"],
           "der echte Aufloeser weist einen absoluten Pfad ab", str(b))
    # Erst NACH dem ersten Aufruf zaehlen: livewerkstatt traegt sich beim
    # Import selbst einmal ein (livewerkstatt.py:54). Gemessen wird das
    # Wachstum je Aufruf, nicht der Anfangsstand.
    _vorher = sys.path.count(HARNESS_DIR)
    for _ in range(5):
        pruefen(_absolut)
    pruefe(sys.path.count(HARNESS_DIR) == _vorher,
           "und traegt sich dabei nicht bei jedem Aufruf neu in sys.path ein",
           "%d statt %d Eintraege" % (sys.path.count(HARNESS_DIR), _vorher))

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
