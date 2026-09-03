#!/usr/bin/env python3
"""Die Pruefungsflagge mit Besitzern - damit sie niemand fremd abraeumt.

Am 02.09.2026 liefen Abitur und Fuehrerschein fuer muse-glimmer
gleichzeitig. Beide schreiben dieselbe Datei, beide loeschen sie am
Ende. Der Fuehrerschein wurde abgebrochen, sein finally-Block raeumte
auf - und nahm die Flagge des noch laufenden Abiturs mit. Danach lief
eine Pruefung ohne Riegel: Die Shell waere im Chat wieder offen
gewesen, und schaltende Routinen haetten mitten in die
Hardwaremessung gefunkt.

Der Fehler steckt nicht im Abbruch, sondern in der Annahme, es gebe
immer nur einen Lauf. Deshalb hier eine Besitzerliste statt eines
Schalters: Jeder Lauf traegt sich mit seiner PID ein und streicht am
Ende NUR SICH SELBST. Die Datei verschwindet erst, wenn der letzte
gegangen ist.

Die Datei bleibt eine gewoehnliche Textdatei an derselben Stelle -
m1_zentrale prueft weiterhin nur, OB sie da ist (_pruefung_laeuft).
An der Riegel-Seite aendert sich nichts.
"""
import fcntl
import os
import sys
from contextlib import contextmanager
from pathlib import Path

FLAGGE = Path("/opt/ki-server/config/PRUEFUNGSLAUF")

KOPF = ("Solange diese Datei liegt: kein Modell bekommt die Shell im "
        "Chat,\nund schaltende Routinen halten still. Jede Zeile "
        "unten ist ein laufender\nPruefungslauf; die Datei geht erst "
        "weg, wenn der letzte fertig ist.\n"
        "Bleibt sie nach einem Absturz liegen, kann sie von Hand "
        "geloescht werden.\n\n")


def _lebt(pid: int) -> bool:
    """Laeuft dieser Prozess noch?

    Im Zweifel JA: Ein Irrtum in diese Richtung laesst die Flagge
    liegen (harmlos, sie ist von Hand loeschbar). Der Irrtum in die
    andere Richtung oeffnet die Shell waehrend einer Pruefung.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


# Zeilen, die nicht von uns stammen (von Hand gesetzt, alte Fassung
# ohne Besitzerliste, fremdes Werkzeug), bekommen diesen Besitzer. Er
# lebt immer und wird nie automatisch gestrichen - nur von Hand.
FREMD = "fremd"
_KOPFZEILEN = {z.strip() for z in KOPF.splitlines() if z.strip()}


def _zeilen(roh: str) -> list:
    """Die Besitzerzeilen aus dem Dateiinhalt - (pid | FREMD, text).

    Gutachten 03.09.2026: Vorher fiel jede Zeile ohne Tabulator einfach
    weg - und beim naechsten abmelden() war die Datei leer und wurde
    geloescht. Eine von Hand gesetzte Flagge (am 02.09. selbst so
    "wiederhergestellt") oder die eines noch laufenden Prozesses mit
    alter Fassung raeumte damit der naechste Lauf ab. Jetzt bleibt
    Fremdes als FREMD-Besitzer stehen, bis jemand die Datei von Hand
    loescht - so, wie der Kopf es verspricht.
    """
    eintraege = []
    for zeile in (roh or "").splitlines():
        if not zeile.strip() or zeile.strip() in _KOPFZEILEN:
            continue
        if "\t" not in zeile:
            eintraege.append((FREMD, zeile.strip()))
            continue
        kopf, _, text = zeile.partition("\t")
        try:
            eintraege.append((int(kopf.strip()), text.strip()))
        except ValueError:
            eintraege.append((FREMD, zeile.strip()))
    return eintraege


@contextmanager
def _gesperrt():
    """Lese-Aendere-Schreibe unter einer Dateisperre.

    Gutachten 03.09.2026: Zwei Laeufe, die fast gleichzeitig anmelden,
    lasen denselben Stand - der zweite ueberschrieb den ersten, und
    beim Abmelden des zweiten war die Datei weg, obwohl der erste noch
    lief. Dasselbe Muster wie am 02.09., nur im Millisekundenfenster.
    Die Sperre liegt auf einer Nachbardatei, damit unlink() der Flagge
    selbst die Sperre nicht mit wegnimmt.
    """
    FLAGGE.parent.mkdir(parents=True, exist_ok=True)
    schloss = FLAGGE.with_suffix(".lock")
    with open(schloss, "a+") as h:
        fcntl.flock(h.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(h.fileno(), fcntl.LOCK_UN)


def _schreiben(eintraege: list) -> None:
    if not eintraege:
        FLAGGE.unlink(missing_ok=True)
        return
    FLAGGE.parent.mkdir(parents=True, exist_ok=True)
    FLAGGE.write_text(
        KOPF + "".join("%s\t%s\n" % (p, t) for p, t in eintraege),
        encoding="utf-8")


def _lebt_oder_fremd(p) -> bool:
    return p == FREMD or _lebt(p)


def _lesen() -> list:
    try:
        return _zeilen(FLAGGE.read_text(encoding="utf-8"))
    except OSError:
        return []


def anmelden(text: str, pid: int = None) -> None:
    """Diesen Lauf als Besitzer eintragen.

    Tote Eintraege werden dabei aufgeraeumt - ein abgestuerzter Lauf
    soll die Flagge nicht ewig halten. Das passiert nur HIER, beim
    Start eines neuen Laufs: Wer schon laeuft, verlaesst sich darauf,
    dass niemand hinter seinem Ruecken aufraeumt.
    """
    eigen = os.getpid() if pid is None else pid
    with _gesperrt():
        eintraege = [(p, t) for p, t in _lesen()
                     if p != eigen and _lebt_oder_fremd(p)]
        eintraege.append((eigen, text))
        _schreiben(eintraege)


def abmelden(pid: int = None) -> None:
    """Diesen Lauf austragen - und NUR diesen.

    Bleibt ein anderer Lauf uebrig, bleibt die Datei liegen. Genau das
    fehlte am 02.09.2026. Tote Eintraege werden hier NICHT geraeumt
    (nur beim naechsten anmelden) - lieber eine Flagge, die einen Start
    zu lang liegt, als eine, die zu frueh faellt.
    """
    eigen = os.getpid() if pid is None else pid
    with _gesperrt():
        _schreiben([(p, t) for p, t in _lesen() if p != eigen])


def laeuft() -> bool:
    return FLAGGE.exists()


def selbsttest() -> int:
    import tempfile
    fehler = []

    def pruefe(bedingung, was, zusatz=""):
        if bedingung:
            print("  ok      %s" % was)
        else:
            fehler.append(was)
            print("  FEHLER  %s  <- %s" % (was, zusatz))

    print("Selbsttest pruefungsflagge\n")
    global FLAGGE, _lebt
    echt = FLAGGE
    with tempfile.TemporaryDirectory() as ordner:
        # Betriebsdaten bleiben unangetastet - auch lesend (24.08.2026).
        FLAGGE = Path(ordner) / "PRUEFUNGSLAUF"
        eigen = os.getpid()

        pruefe(not laeuft(), "vorher liegt keine Flagge")
        anmelden("Abitur fuer muse-glimmer", eigen)
        pruefe(laeuft(), "nach dem Anmelden liegt sie")

        # Der eigentliche Fall vom 02.09.: zwei Laeufe, einer bricht ab.
        anmelden("Fuehrerschein fuer muse-glimmer", eigen + 100000)
        pruefe(len(_lesen()) == 2, "zwei Laeufe koennen sie zugleich halten",
               str(_lesen()))
        abmelden(eigen + 100000)
        pruefe(laeuft(),
               "der abgebrochene Lauf nimmt sie dem anderen NICHT weg",
               str(_lesen()))
        pruefe([t for _, t in _lesen()] == ["Abitur fuer muse-glimmer"],
               "und der verbliebene Besitzer steht noch drin",
               str(_lesen()))
        abmelden(eigen)
        pruefe(not laeuft(), "erst der letzte raeumt sie weg")

        # Ein abgestuerzter Lauf darf sie nicht ewig halten.
        tot = 2
        while _lebt(tot) and tot < 40000:
            tot += 1
        anmelden("Leiche", tot)
        anmelden("frischer Lauf", eigen)
        pruefe([t for _, t in _lesen()] == ["frischer Lauf"],
               "ein toter Eintrag wird beim naechsten Start aufgeraeumt",
               str(_lesen()))
        abmelden(eigen)

        # Fremder Inhalt (von Hand, alte Fassung) ist ein Besitzer, den
        # niemand automatisch streicht (Gutachten 03.09.2026).
        FLAGGE.write_text("Abiturlauf seit 02.09.2026 fuer alle\n",
                          encoding="utf-8")
        pruefe(_lesen() == [(FREMD, "Abiturlauf seit 02.09.2026 fuer alle")],
               "eine Zeile ohne Tabulator ist ein FREMDER Besitzer")
        anmelden("neuer Lauf", eigen)
        abmelden(eigen)
        pruefe(laeuft() and any(p == FREMD for p, _ in _lesen()),
               "an- und abmelden raeumt die fremde Zeile NICHT weg",
               str(_lesen()))
        FLAGGE.unlink(missing_ok=True)
        # Gleichzeitiges Anmelden darf keinen Eintrag verlieren. Die
        # acht PIDs sind erfunden - damit anmelden() sie nicht als tot
        # wegraeumt (das waere der andere, gewollte Mechanismus), gilt
        # fuer diesen Block: alle leben. Gemessen wird allein die
        # Sperre um Lesen-Aendern-Schreiben.
        import threading
        _echt_lebt = _lebt
        _lebt = lambda p: True
        try:
            def _an(n):
                anmelden("Lauf %d" % n, eigen + 1000 + n)
            faeden = [threading.Thread(target=_an, args=(n,)) for n in range(8)]
            for f in faeden: f.start()
            for f in faeden: f.join()
            pruefe(len(_lesen()) == 8,
                   "acht gleichzeitige Anmeldungen -> acht Eintraege",
                   str(len(_lesen())))
            for n in range(8):
                abmelden(eigen + 1000 + n)
            pruefe(not laeuft(), "und nach acht Abmeldungen ist sie weg")
        finally:
            _lebt = _echt_lebt
    FLAGGE = echt
    pruefe(FLAGGE == Path("/opt/ki-server/config/PRUEFUNGSLAUF"),
           "der echte Pfad ist nach dem Test wiederhergestellt")

    print("\n%s" % ("Alle Pruefungen bestanden." if not fehler
                    else "%d FEHLER." % len(fehler)))
    return 1 if fehler else 0


if __name__ == "__main__":
    sys.exit(selbsttest() if "--selbsttest" in sys.argv[1:] else 0)
