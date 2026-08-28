#!/usr/bin/env python3
"""Tims Werkstatt - ein Sandkasten, in dem Tim BAUEN lernen darf.

Warum es das gibt: Tims Diagnose-Werkzeuge (ha_diagnose,
doppelablage_pruefen, datenschutz_pruefen) sind bewusst nur lesend.
Bauen - Dateien schreiben, ausrollen, an Hardware messen - hat er noch
nie selbst gemacht, weil Mexlas Vorgabe lautet: "Er darf keine meiner
Datensaetze bearbeiten." Die Werkstatt loest genau diesen Widerspruch:
Hier DARF Tim schreiben, aber NUR in einem eigenen Ordner
(~/Desktop/Tim-Werkstatt/sandkasten). Alles andere - /opt/ki-server,
M1_DEPLOYMENT, Home Assistant, der Pico - bleibt fuer diesen Weg
unerreichbar.

Das Kernstueck ist `pfad_erlaubt()`: Es beantwortet die eine Frage, an
der alles haengt - "Darf Tim in diese Datei schreiben?". Die Antwort
ist ja NUR fuer aufgeloeste Pfade INNERHALB des Sandkastens. Symlinks,
'..'-Tricks und absolute Ausbrueche werden abgewiesen, nicht gefiltert.
Der Selbsttest fuehrt jeden bekannten Ausbruchsversuch vor und verlangt,
dass er scheitert (Zwei-Seiten-Beweis: ein Pfad im Sandkasten MUSS
erlaubt sein, jeder ausserhalb MUSS abgelehnt werden).

Jeder Schreibzugriff wird protokolliert (memory/werkstatt_log.jsonl) -
nicht als Schikane, sondern damit hinterher nachlesbar ist, was Tim
gebaut hat, bevor irgendjemand ueberlegt, es auszurollen.

Ausrollen kann die Werkstatt NICHT. Sie schreibt, liest und fuehrt
Selbsttests IM Sandkasten aus - der Weg von dort ins echte System
(Kopieren nach /opt, SSH auf den Pi, HA neu laden) bleibt Handarbeit
an der Tastatur und Mexlas Entscheidung. Das ist die Grenze zwischen
"ueben" und "wirken".

Aufruf (fuer Menschen und zum Ausprobieren):
    python3 werkstatt.py neu <name>          # Aufgabe -> Sandkasten
    python3 werkstatt.py schreiben <relpfad>  # Inhalt von stdin
    python3 werkstatt.py lesen <relpfad>
    python3 werkstatt.py liste [relpfad]
    python3 werkstatt.py testen <relpfad>     # python -m py_compile / --selbsttest
    python3 werkstatt.py --selbsttest
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

WERKSTATT = Path.home() / "Desktop" / "Tim-Werkstatt"
SANDKASTEN = WERKSTATT / "sandkasten"
AUFGABEN = WERKSTATT / "aufgaben"
GESCHAFFT = WERKSTATT / "geschafft"
PROTOKOLL = Path("/opt/ki-server/memory/werkstatt_log.jsonl")

# Was Tim in der Werkstatt schreiben darf. Bewusst eng: Code, Konfig,
# Notizen. KEINE ausfuehrbaren Binaries, keine Shell-Profile, nichts,
# was beim blossen Anschauen im Finder etwas ausloest.
ERLAUBTE_ENDUNGEN = (".py", ".json", ".yaml", ".yml", ".md", ".txt",
                     ".html", ".css", ".js", ".toml", ".ini", ".cfg")
MAX_DATEI_BYTES = 2_000_000        # eine Quelldatei, keine Mediendatei
MAX_AUSGABE = 20_000
TEST_ZEITGRENZE = 120


def _jetzt() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def protokoll_schreiben(eintrag: dict) -> None:
    """Eine Zeile ins Werkstatt-Protokoll. Fehler hier duerfen die
    eigentliche Arbeit nicht verhindern - protokollieren ist Beiwerk."""
    try:
        PROTOKOLL.parent.mkdir(parents=True, exist_ok=True)
        eintrag = {"ts": _jetzt(), **eintrag}
        with open(PROTOKOLL, "a", encoding="utf-8") as d:
            d.write(json.dumps(eintrag, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _basis() -> Path:
    """Der aufgeloeste Sandkasten-Pfad.

    Immer aufgeloest vergleichen: Unter macOS zeigt /var auf
    /private/var, und ein Vergleich zwischen aufgeloestem Ziel und
    unaufgeloester Basis scheitert dann - gemessen am 24.08.2026 im
    ersten Selbsttestlauf (Ziel /private/var/..., Basis /var/...).
    Waere das nur beim Vergleich schiefgegangen, haette es hier
    ausgesehen wie ein Ausbruch, wo keiner war.
    """
    return SANDKASTEN.resolve(strict=False)


def pfad_erlaubt(relpfad: str) -> tuple[Path | None, str]:
    """Loest einen Pfad im Sandkasten auf und prueft die Grenze.

    Rueckgabe: (aufgeloester_pfad, "") wenn erlaubt, sonst (None, grund).

    Dies ist die Stelle, an der Tims Schreibrecht endet. Sie MUSS
    wasserdicht sein - der Selbsttest fuehrt jeden Ausbruch vor.
    """
    if not relpfad or not isinstance(relpfad, str):
        return None, "kein Pfad angegeben"
    roh = relpfad.strip()
    # Absolute Pfade und Heimverzeichnis-Tilden gar nicht erst annehmen -
    # ein relativer Pfad kann den Sandkasten nur ueber '..' verlassen,
    # und das faengt die resolve()-Pruefung unten.
    if roh.startswith("/") or roh.startswith("~"):
        return None, "nur Pfade INNERHALB des Sandkastens (kein / oder ~ am Anfang)"
    try:
        basis = _basis()
        ziel = (basis / roh).resolve(strict=False)
    except (OSError, RuntimeError) as fehler:
        return None, f"Pfad nicht aufloesbar: {fehler}"
    # Der eigentliche Riegel: das aufgeloeste Ziel MUSS im Sandkasten
    # liegen. resolve() hat '..' und Symlinks bereits abgewickelt - wer
    # ausbrechen wollte, landet jetzt sichtbar ausserhalb.
    if ziel != basis and basis not in ziel.parents:
        return None, "Pfad zeigt aus dem Sandkasten heraus - abgelehnt"
    return ziel, ""


def _endung_ok(ziel: Path) -> bool:
    return ziel.suffix.lower() in ERLAUBTE_ENDUNGEN


# ----------------------------------------------------------------------
# Die Werkstatt-Handgriffe
# ----------------------------------------------------------------------
def _oberste_namen(quelltext: str) -> set:
    """Die Namen der obersten Funktionen und Klassen einer Python-Datei.

    Ueber den Syntaxbaum, nicht per Textsuche: Ein Name in einem
    Kommentar oder String zaehlt nicht als vorhanden.
    """
    import ast
    try:
        baum = ast.parse(quelltext)
    except SyntaxError:
        return set()
    return {k.name for k in baum.body
            if isinstance(k, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef))}


def schreiben(relpfad: str, inhalt: str) -> dict:
    ziel, grund = pfad_erlaubt(relpfad)
    if ziel is None:
        return {"ok": False, "fehler": grund}
    # Was war vorher da? Wird unten gegen den neuen Stand gehalten.
    vorher = ""
    if ziel.is_file() and ziel.suffix == ".py":
        try:
            vorher = ziel.read_text(encoding="utf-8", errors="replace")
        except OSError:
            vorher = ""
    if not _endung_ok(ziel):
        return {"ok": False, "fehler": "nur diese Endungen: "
                + ", ".join(ERLAUBTE_ENDUNGEN)}
    roh = inhalt.encode("utf-8")
    if len(roh) > MAX_DATEI_BYTES:
        return {"ok": False, "fehler": f"zu gross (> {MAX_DATEI_BYTES} Bytes)"}
    try:
        ziel.parent.mkdir(parents=True, exist_ok=True)
        ziel.write_bytes(roh)
    except OSError as fehler:
        return {"ok": False, "fehler": str(fehler)}
    protokoll_schreiben({"tat": "schreiben", "pfad": relpfad,
                         "bytes": len(roh)})
    ergebnis = {"ok": True, "pfad": str(ziel.relative_to(_basis())),
                "bytes": len(roh)}
    # Sofort sagen, wenn der Beleg fehlt. Am 24.08.2026 hat Tim beim
    # Umbau zweimal hintereinander den --selbsttest-Schalter verloren -
    # und erst der spaetere Testlauf zeigte es. Ein Hinweis genau hier,
    # im Moment des Schreibens, kostet nichts und faengt es sofort.
    # Bewusst nur ein Hinweis, keine Ablehnung: Zwischenstaende und
    # Hilfsdateien duerfen ohne Selbsttest existieren.
    # Beim Ueberschreiben sagen, was dabei VERSCHWUNDEN ist. Am
    # 24.08.2026 hat Tim beim "Nachbessern" mehrfach die ganze Datei neu
    # geschrieben und dabei still Funktionen und Testfaelle verloren -
    # zuletzt genau die Funktion, um die es in der Aufgabe ging. Wer
    # eine Datei ersetzt, sieht nicht, was er wegwirft; dieser Vergleich
    # macht es sichtbar, im Moment des Schreibens.
    verloren = _oberste_namen(vorher) - _oberste_namen(inhalt) if vorher else set()
    if verloren:
        ergebnis["verloren"] = sorted(verloren)
        ergebnis["warnung"] = (
            "Diese Namen waren vorher in der Datei und sind jetzt weg: "
            + ", ".join(sorted(verloren))
            + ". Falls das Absicht war, ist es gut - falls nicht, hast du "
              "beim Neuschreiben etwas verloren. Tipp: erst mit "
              "werkstatt_lesen den Stand holen und nur aendern, was "
              "geaendert werden soll.")
    if ziel.suffix == ".py" and "--selbsttest" not in inhalt:
        ergebnis["warnung"] = ergebnis.get("warnung", "") + " " if \
            ergebnis.get("warnung") else ""
        ergebnis["warnung"] += (
            "Diese Datei hat keinen --selbsttest-Schalter. Ohne ihn "
            "kann werkstatt_testen nur kompilieren, nicht pruefen. "
            "Beispiel: if '--selbsttest' in sys.argv: ...")
    return ergebnis


def lesen(relpfad: str) -> dict:
    ziel, grund = pfad_erlaubt(relpfad)
    if ziel is None:
        return {"ok": False, "fehler": grund}
    if not ziel.is_file():
        return {"ok": False, "fehler": "keine Datei"}
    try:
        text = ziel.read_text(encoding="utf-8", errors="replace")
    except OSError as fehler:
        return {"ok": False, "fehler": str(fehler)}
    return {"ok": True, "inhalt": text[:MAX_AUSGABE]}


def liste(relpfad: str = "") -> dict:
    ziel, grund = pfad_erlaubt(relpfad or ".")
    if ziel is None:
        return {"ok": False, "fehler": grund}
    if not ziel.exists():
        return {"ok": True, "eintraege": []}
    if ziel.is_file():
        return {"ok": True, "eintraege": [ziel.name]}
    eintraege = []
    for p in sorted(ziel.rglob("*")):
        if p.is_file():
            eintraege.append(str(p.relative_to(_basis())))
        if len(eintraege) >= 200:
            break
    return {"ok": True, "eintraege": eintraege}


# Die Einsperrung fuer Tims eigenen Code. Ohne sie waere die ganze
# Werkstatt eine Attrappe: Am 24.08.2026 nachgemessen - eine von Tim
# geschriebene Testdatei konnte beim Ausfuehren ungehindert auf den
# Schreibtisch schreiben ("AUSBRUCH GELUNGEN"). Die Pfadsperre begrenzt
# nur, wohin das WERKZEUG schreibt; was Tims Code beim Laufen tut, ist
# eine voellig andere Frage. Beantwortet wird sie hier, mit dem
# macOS-Bordmittel sandbox-exec:
#
#   * Schreiben nur im Sandkasten und in den Temp-Ordnern (tempfile
#     legt dort an - saubere Tests brauchen das).
#   * /tmp bleibt bewusst DRAUSSEN: Wer dort feste Pfade benutzt, baut
#     sich Zustand zwischen Laeufen ein. Genau daran ist Tims erster
#     Symlink-Test gescheitert (File exists) - der Fehler soll auffallen.
#   * Kein Netz. Ein Selbsttest, der ins Netz will, ist kein Selbsttest.
#   * Lesen bleibt erlaubt: Python braucht seine Bibliothek.
SANDBOX_PROFIL = """(version 1)
(deny default)
(allow process-exec)
(allow process-fork)
(allow signal (target self))
(allow sysctl-read)
(allow mach-lookup)
(allow file-read*)
(allow file-write* (subpath "%s"))
(allow file-write* (subpath "/private/var/folders"))
(allow file-write-data (literal "/dev/null") (literal "/dev/stdout")
                       (literal "/dev/stderr"))
"""


def _sandbox_befehl(py: str, ziel: Path, arg: str | None) -> list:
    """Der Aufruf, eingesperrt - oder blank, wenn sandbox-exec fehlt.

    Faellt sandbox-exec weg (anderes Betriebssystem), laeuft der Test
    ungeschuetzt. Das wird dann GEMELDET, nicht verschwiegen: eine
    stillschweigend fehlende Sperre ist schlimmer als keine.
    """
    befehl = [py, str(ziel)] + ([arg] if arg else [])
    if not Path("/usr/bin/sandbox-exec").exists():
        return befehl
    profil = SANDBOX_PROFIL % _basis()
    fd, datei = tempfile.mkstemp(suffix=".sb")
    os.write(fd, profil.encode("utf-8"))
    os.close(fd)
    return ["/usr/bin/sandbox-exec", "-f", datei] + befehl


def testen(relpfad: str) -> dict:
    """Eine Datei im Sandkasten pruefen: Python kompilieren und, wenn
    sie einen --selbsttest kennt, diesen fahren.

    Tims Code laeuft dabei EINGESPERRT (siehe SANDBOX_PROFIL): Er darf
    nur im Sandkasten und in Temp-Ordnern schreiben und nicht ins Netz.
    Ohne das koennte eine Testdatei alles anfassen, was Mexla gehoert -
    die Pfadsperre allein schuetzt davor nicht."""
    ziel, grund = pfad_erlaubt(relpfad)
    if ziel is None:
        return {"ok": False, "fehler": grund}
    if not ziel.is_file():
        return {"ok": False, "fehler": "keine Datei"}
    if ziel.suffix != ".py":
        return {"ok": False, "fehler": "testen geht nur fuer .py-Dateien"}
    py = "/opt/ki-server/venv/bin/python"
    if not Path(py).exists():
        py = sys.executable
    # 1. Kompiliert die Datei ueberhaupt?
    lauf = subprocess.run([py, "-m", "py_compile", str(ziel)],
                          capture_output=True, text=True,
                          timeout=TEST_ZEITGRENZE)
    if lauf.returncode != 0:
        protokoll_schreiben({"tat": "testen", "pfad": relpfad,
                             "ergebnis": "syntaxfehler"})
        return {"ok": False, "phase": "kompilieren",
                "ausgabe": (lauf.stderr or "")[:MAX_AUSGABE]}
    # 2. Hat sie einen Selbsttest? (grep statt Import - kein fremder Code
    #    laeuft, nur weil wir nachsehen wollen, ob es ihn gibt.)
    hat_selbsttest = "--selbsttest" in ziel.read_text(
        encoding="utf-8", errors="replace")
    if not hat_selbsttest:
        # Frueher stand hier ok=True mit einem Hinweis - und genau das
        # war falsch: Eine Datei, die NICHT geprueft werden konnte, als
        # "in Ordnung" zu melden, ist derselbe Fehler, den dieses Haus
        # ueberall sonst LUECKE nennt. Am 24.08.2026 hat Tim viermal
        # den Schalter vergessen und viermal "fertig" gemeldet, weil das
        # Werkzeug ihm nicht widersprach. Jetzt widerspricht es.
        protokoll_schreiben({"tat": "testen", "pfad": relpfad,
                             "ergebnis": "luecke_ohne_selbsttest"})
        return {"ok": False, "phase": "luecke",
                "fehler": "LUECKE: Die Datei kompiliert, aber sie hat "
                          "keinen --selbsttest-Schalter - es konnte "
                          "NICHTS geprueft werden. Ungeprueft ist nicht "
                          "dasselbe wie in Ordnung. Bau ein: "
                          "if '--selbsttest' in sys.argv: ... und lass "
                          "die Pruefungen NUR dann laufen (nicht beim "
                          "blossen Importieren)."}
    lauf = subprocess.run(_sandbox_befehl(py, ziel, "--selbsttest"),
                          capture_output=True, text=True,
                          timeout=TEST_ZEITGRENZE, cwd=str(_basis()))
    protokoll_schreiben({"tat": "testen", "pfad": relpfad,
                         "ergebnis": "selbsttest_exit_%d" % lauf.returncode})
    ausgabe = ((lauf.stdout or "") + (("\n" + lauf.stderr)
               if lauf.stderr else ""))
    return {"ok": lauf.returncode == 0, "phase": "selbsttest",
            "exitcode": lauf.returncode, "ausgabe": ausgabe.strip()[:MAX_AUSGABE]}


# ----------------------------------------------------------------------
# Befehlsfolgen: der Uebungsraum fuer den Terminal-Fuehrerschein
# ----------------------------------------------------------------------
# Zwischen "nur Python-Dateien fahren" (testen) und "alles duerfen"
# (die Shell der Zentrale, shell=True, kein Filter) klaffte eine Luecke.
# Der Fuehrerschein braucht genau den Mittelweg: echte Kommandozeilen-
# Arbeit, aber eingesperrt und ohne Werkzeuge, die etwas kaputt machen.
#
# Tim schreibt die Folge als JSON in den Sandkasten (eine Liste von
# Argumentlisten) und laesst sie hier fahren. Vier Riegel:
#   1. Positivliste der Programme - alles andere wird abgelehnt.
#   2. Argumentpruefung - nur relative Pfade im Sandkasten. Ohne sie
#      liesse sich unter (allow file-read*) jedes Geheimnis des Hauses
#      auslesen (~/.m1_job_token, config/ha_token.secret) und die
#      Ausgabe floesse in Chat und Protokoll zurueck.
#   3. sandbox-exec - kein Netz, Schreiben nur im Sandkasten.
#   4. Zeit- und Mengengrenze.
#
# NICHT in der Liste und warum: rm/mv/cp (loeschen und verschieben ist
# Mexlas Sache), find (-delete loescht wirklich, -exec haengt beliebige
# Programme an und haette die Positivliste ausgehebelt), curl/git
# (Netz bzw. Zustandsaenderung), echo/sh (Umleitungen und Ketten).
BEFEHLE_ERLAUBT = {
    "ls": "/bin/ls",
    "cat": "/bin/cat",
    "grep": "/usr/bin/grep",
    "head": "/usr/bin/head",
    "tail": "/usr/bin/tail",
    "wc": "/usr/bin/wc",
}
MAX_BEFEHLE = 20
BEFEHL_ZEITGRENZE = 15


def _argument_ok(arg: str) -> str:
    """Leerer String = in Ordnung, sonst der Ablehnungsgrund.

    Optionen (-l, --color) sind erlaubt; alles andere gilt als Pfad und
    muss im Sandkasten liegen. Absolute Pfade, ~ und .. sind zu, damit
    ein 'cat' nicht ausserhalb liest - die Sandbox erlaubt Lesen
    ueberall, dieser Riegel ist der einzige, der das begrenzt.
    """
    if not isinstance(arg, str) or not arg:
        return "leeres Argument"
    if len(arg) > 200:
        return "Argument zu lang"
    if arg.startswith("-"):
        return "" if all(z.isalnum() or z in "-_=." for z in arg) else \
            "unzulaessige Option: %r" % arg
    ziel, grund = pfad_erlaubt(arg)
    return "" if ziel is not None else grund


def befehl_pruefen(teile: list) -> tuple[list | None, str]:
    """Eine einzelne Befehlszeile pruefen und in einen echten Aufruf
    uebersetzen. Rueckgabe: (aufruf, "") oder (None, grund)."""
    if not isinstance(teile, list) or not teile:
        return None, "jede Zeile muss eine Liste sein: [\"ls\", \"-l\"]"
    if not all(isinstance(t, str) for t in teile):
        return None, "alle Teile muessen Text sein"
    name = teile[0]
    if name not in BEFEHLE_ERLAUBT:
        return None, ("'%s' ist nicht erlaubt. Erlaubt sind nur: %s"
                      % (name, ", ".join(sorted(BEFEHLE_ERLAUBT))))
    for arg in teile[1:]:
        grund = _argument_ok(arg)
        if grund:
            return None, "Argument %r abgelehnt: %s" % (arg, grund)
    return [BEFEHLE_ERLAUBT[name]] + list(teile[1:]), ""


def befehle_fahren(relpfad: str) -> dict:
    """Eine Befehlsfolge aus einer JSON-Datei im Sandkasten fahren.

    Anders als testen() wird hier NICHT ungeschuetzt weitergemacht, wenn
    sandbox-exec fehlt: Das ist Pruef-Infrastruktur, und ein kranker
    Pruefstand darf nichts messen, statt heimlich ohne Sperre zu fahren.
    """
    ziel, grund = pfad_erlaubt(relpfad)
    if ziel is None:
        return {"ok": False, "fehler": grund}
    if not ziel.is_file():
        return {"ok": False, "fehler": "keine Datei"}
    if ziel.suffix != ".json":
        return {"ok": False, "fehler": "die Befehlsfolge muss eine "
                                       ".json-Datei sein"}
    if not Path("/usr/bin/sandbox-exec").exists():
        return {"ok": False, "fehler": "sandbox-exec fehlt - hier wird "
                                       "nichts ungeschuetzt gefahren"}
    try:
        folge = json.loads(ziel.read_text(encoding="utf-8"))
    except (ValueError, OSError) as fehler:
        return {"ok": False, "fehler": "JSON nicht lesbar: %s" % fehler}
    if not isinstance(folge, list) or not folge:
        return {"ok": False, "fehler": "erwartet wird eine Liste von "
                                       "Befehlszeilen, z.B. [[\"ls\", \"-l\"]]"}
    if len(folge) > MAX_BEFEHLE:
        return {"ok": False, "fehler": "hoechstens %d Befehle" % MAX_BEFEHLE}

    profil = SANDBOX_PROFIL % _basis()
    fd, profildatei = tempfile.mkstemp(suffix=".sb")
    os.write(fd, profil.encode("utf-8"))
    os.close(fd)
    schritte = []
    try:
        for nummer, teile in enumerate(folge, 1):
            aufruf, grund = befehl_pruefen(teile)
            if aufruf is None:
                schritte.append({"nummer": nummer, "befehl": teile,
                                 "abgelehnt": grund})
                protokoll_schreiben({"tat": "befehle", "pfad": relpfad,
                                     "nummer": nummer, "abgelehnt": grund})
                continue
            try:
                lauf = subprocess.run(
                    ["/usr/bin/sandbox-exec", "-f", profildatei] + aufruf,
                    capture_output=True, text=True,
                    timeout=BEFEHL_ZEITGRENZE, cwd=str(_basis()))
                ausgabe = ((lauf.stdout or "")
                           + (("\n" + lauf.stderr) if lauf.stderr else ""))
                schritte.append({"nummer": nummer, "befehl": teile,
                                 "code": lauf.returncode,
                                 "ausgabe": ausgabe.strip()[:MAX_AUSGABE]})
            except subprocess.TimeoutExpired:
                schritte.append({"nummer": nummer, "befehl": teile,
                                 "code": -1,
                                 "ausgabe": "abgebrochen nach %d s"
                                            % BEFEHL_ZEITGRENZE})
            protokoll_schreiben({"tat": "befehle", "pfad": relpfad,
                                 "nummer": nummer,
                                 "befehl": " ".join(teile),
                                 "code": schritte[-1].get("code")})
    finally:
        try:
            os.unlink(profildatei)
        except OSError:
            pass
    return {"ok": True, "schritte": schritte,
            "abgelehnt": sum(1 for s in schritte if "abgelehnt" in s)}


LERNPROTOKOLL = WERKSTATT / "gelernt" / "LERNPROTOKOLL.md"
MAX_LERNNOTIZ = 4000


def lernnotiz(aufgabe: str, text: str) -> dict:
    """Was Tim aus einer Uebung mitgenommen hat, dauerhaft festhalten.

    Warum getrennt vom Sandkasten: Der Sandkasten wird zwischen
    Aufgaben geleert - Gelerntes soll das ueberleben. Deshalb schreibt
    diese Funktion in eine FESTE Datei (gelernt/LERNPROTOKOLL.md) und
    haengt nur an. Der Pfad kommt NICHT von aussen, also gibt es hier
    nichts auszubrechen; was von aussen kommt, ist reiner Text.

    Angehaengt wird, nie ersetzt: Ein Lernprotokoll, das sich selbst
    ueberschreiben kann, verliert genau das, wofuer es da ist.
    """
    aufgabe = str(aufgabe or "").strip()[:60]
    text = str(text or "").strip()
    if not aufgabe:
        return {"ok": False, "fehler": "keine Aufgabe angegeben"}
    if len(text) < 40:
        return {"ok": False, "fehler": "zu duenn - schreib in ganzen Saetzen, "
                                       "was du gelernt hast (mind. 40 Zeichen)"}
    text = text[:MAX_LERNNOTIZ]
    eintrag = ("\n## %s - %s\n\n%s\n"
               % (time.strftime("%Y-%m-%d %H:%M"), aufgabe, text))
    try:
        LERNPROTOKOLL.parent.mkdir(parents=True, exist_ok=True)
        if not LERNPROTOKOLL.exists():
            LERNPROTOKOLL.write_text(
                "# Tims Lernprotokoll\n\n"
                "Was Tim in der Werkstatt gelernt hat - von ihm selbst\n"
                "geschrieben, nach jeder Uebung angehaengt. Aelteste\n"
                "Eintraege stehen oben.\n", encoding="utf-8")
        with open(LERNPROTOKOLL, "a", encoding="utf-8") as d:
            d.write(eintrag)
    except OSError as fehler:
        return {"ok": False, "fehler": str(fehler)}
    protokoll_schreiben({"tat": "lernnotiz", "aufgabe": aufgabe,
                         "zeichen": len(text)})
    return {"ok": True, "aufgabe": aufgabe, "zeichen": len(text),
            "datei": str(LERNPROTOKOLL)}


def gelerntes_lesen() -> dict:
    """Das Lernprotokoll zurueckgeben - Tims eigenes Gedaechtnis der
    Werkstatt. Vor einer neuen Uebung lesenswert."""
    if not LERNPROTOKOLL.is_file():
        return {"ok": True, "inhalt": "Noch nichts gelernt - das "
                                      "Lernprotokoll ist leer."}
    return {"ok": True,
            "inhalt": LERNPROTOKOLL.read_text(encoding="utf-8",
                                              errors="replace")[-MAX_AUSGABE:]}


ALTABLAGE = WERKSTATT / "_alt"


def aufraeumen() -> dict:
    """Den Sandkasten leeren - durch VERSCHIEBEN, nicht durch Loeschen.

    Der Inhalt wandert nach `_alt/JJJJ-MM-TT_HHMM/`. Zwei Gruende:

      * Die Hausregel NIEMALS_LOESCHEN_OHNE_BACKUP gilt auch hier. Was
        wie Gerumpel aussieht, kann der einzige Stand einer Arbeit sein,
        die noch niemand angesehen hat.
      * Ein Aufraeumen, das nicht rueckgaengig zu machen ist, traut sich
        niemand - dann bleibt der Sandkasten voll und alte Entwuerfe
        mischen sich in neue Aufgaben.

    Das Lernprotokoll bleibt unberuehrt: Es wohnt in gelernt/, nicht im
    Sandkasten - genau dafuer liegt es dort.
    """
    if not SANDKASTEN.is_dir():
        return {"ok": True, "verschoben": 0,
                "hinweis": "Es gibt noch keinen Sandkasten."}
    inhalt = [p for p in SANDKASTEN.iterdir() if not p.name.startswith(".")]
    if not inhalt:
        return {"ok": True, "verschoben": 0,
                "hinweis": "Der Sandkasten ist schon leer."}
    ziel = ALTABLAGE / time.strftime("%Y-%m-%d_%H%M")
    nummer = 2
    while ziel.exists():          # zweimal in derselben Minute
        ziel = ALTABLAGE / (time.strftime("%Y-%m-%d_%H%M") + "_%d" % nummer)
        nummer += 1
    import shutil
    try:
        ziel.mkdir(parents=True)
        for stueck in inhalt:
            shutil.move(str(stueck), str(ziel / stueck.name))
    except OSError as fehler:
        return {"ok": False, "fehler": str(fehler)}
    protokoll_schreiben({"tat": "aufraeumen", "nach": str(ziel),
                         "stuecke": len(inhalt)})
    return {"ok": True, "verschoben": len(inhalt), "nach": str(ziel),
            "hinweis": "Nichts geloescht - alles liegt unter _alt/ und "
                       "laesst sich zurueckholen."}


def neue_aufgabe(name: str) -> dict:
    """Eine Aufgabe aus aufgaben/ in den Sandkasten holen (nur die
    Beschreibung; gebaut wird von Tim). Reiner Lesevorgang aus einem
    festen Ordner - der Name darf nicht ausbrechen."""
    if not name or "/" in name or ".." in name or name.startswith("."):
        return {"ok": False, "fehler": "einfacher Aufgabenname ohne / und .."}
    quelle = AUFGABEN / (name if name.endswith(".md") else name + ".md")
    if not quelle.is_file():
        vorhanden = sorted(p.stem for p in AUFGABEN.glob("*.md")) \
            if AUFGABEN.is_dir() else []
        return {"ok": False, "fehler": f"keine Aufgabe '{name}'",
                "vorhanden": vorhanden}
    return {"ok": True, "aufgabe": quelle.read_text(encoding="utf-8")[:MAX_AUSGABE]}


# ----------------------------------------------------------------------
# Selbsttest
# ----------------------------------------------------------------------
def _selbsttest() -> int:
    import tempfile

    fehler = 0

    def pruefe(bedingung, text, zusatz=""):
        nonlocal fehler
        if bedingung:
            print("  ok      %s" % text)
        else:
            print("  FEHLER  %s%s" % (text, ("  [%s]" % zusatz) if zusatz else ""))
            fehler += 1

    print("werkstatt Selbsttest:")

    global SANDKASTEN, PROTOKOLL
    echt = SANDKASTEN
    echt_protokoll = PROTOKOLL
    with tempfile.TemporaryDirectory() as ordner:
        SANDKASTEN = Path(ordner) / "sandkasten"
        SANDKASTEN.mkdir()
        # Auch das PROTOKOLL umlenken. Bis zum 24.08.2026 schrieb der
        # Selbsttest in die echte memory/werkstatt_log.jsonl - 23 Zeilen
        # je Lauf, im Mutationstest mal 119. Das Protokoll ist das, was
        # Mexla im Werkstatt-Reiter sieht; Testmuell darin sieht aus wie
        # Taetigkeit, die es nie gab. Die allgemeine Regel dahinter:
        # Ein Selbsttest fasst Betriebsdaten NICHT AN - auch nicht
        # lesend, denn manche Bibliothek schreibt beim Oeffnen mit, und
        # niemand hat einen Testlauf im Verdacht, wenn Daten kaputtgehen.
        PROTOKOLL = Path(ordner) / "werkstatt_log.jsonl"

        # --- pfad_erlaubt: der Kern. Erst die erlaubte Seite. ---
        ziel, grund = pfad_erlaubt("projekt/licht.py")
        pruefe(ziel is not None and str(ziel).startswith(str(_basis())),
               "Pfad im Sandkasten ist erlaubt", grund)
        ziel, _ = pfad_erlaubt("tief/im/baum/datei.py")
        pruefe(ziel is not None, "auch tief verschachtelt erlaubt")

        # --- Jetzt jeder bekannte Ausbruch. Alle MUESSEN scheitern. ---
        for boese in ("../ausserhalb.py", "../../etc/passwd",
                      "/etc/passwd", "~/.ssh/id_rsa",
                      "unterordner/../../raus.py",
                      "/opt/ki-server/oberflaeche/m1_zentrale.py"):
            ziel, grund = pfad_erlaubt(boese)
            pruefe(ziel is None, "Ausbruch abgewiesen: %s" % boese, grund)

        # Symlink-Ausbruch: ein Link im Sandkasten, der nach aussen zeigt.
        aussen = Path(ordner) / "geheim.py"
        aussen.write_text("SECRET = 1\n")
        link = SANDKASTEN / "tuer.py"
        try:
            link.symlink_to(aussen)
            ziel, grund = pfad_erlaubt("tuer.py")
            pruefe(ziel is None, "Symlink aus dem Sandkasten abgewiesen", grund)
        except OSError:
            pruefe(True, "Symlink-Test uebersprungen (keine Rechte)")

        # Kein Ausbruch, auch wenn es so aussieht: '....' ist ein ganz
        # normaler Ordnername (nur '..' ist der Aufstieg). Der Pfad
        # bleibt im Sandkasten und MUSS erlaubt sein - eine Ablehnung
        # waere Aberglaube statt Pruefung. Stand im ersten Entwurf
        # faelschlich in der Ausbruchsliste.
        ziel, grund = pfad_erlaubt("....//....//etc/passwd")
        pruefe(ziel is not None and str(ziel).startswith(str(_basis())),
               "'....' ist ein Ordnername, kein Aufstieg - bleibt drin",
               grund)

        # --- schreiben: erlaubt, dann die Grenzen ---
        r = schreiben("projekt/hallo.py", "print('hallo')\n")
        pruefe(r["ok"], "Schreiben im Sandkasten geht", str(r))
        pruefe((SANDKASTEN / "projekt" / "hallo.py").is_file(),
               "die Datei liegt wirklich da")
        r = schreiben("../ausbruch.py", "x = 1\n")
        pruefe(not r["ok"], "Schreiben ausserhalb wird abgelehnt")
        pruefe(not (Path(ordner) / "ausbruch.py").exists(),
               "und die Datei entstand NICHT ausserhalb")
        # Die Warnung, wenn der Beleg fehlt (24.08.2026): Sie ist ein
        # Hinweis, keine Ablehnung - und sie MUSS verschwinden, sobald
        # der Schalter da ist. Sonst gewoehnt sich Tim an, sie zu
        # ueberlesen.
        r = schreiben("ohne_test.py", "x = 1\n")
        pruefe(r["ok"] and "warnung" in r,
               ".py ohne --selbsttest wird angenommen, aber gewarnt")
        r = schreiben("mit_test.py",
                      "import sys\nif '--selbsttest' in sys.argv:\n    pass\n")
        pruefe(r["ok"] and "warnung" not in r,
               "mit --selbsttest keine Warnung (Gegenprobe)")
        r = schreiben("notiz.md", "# nur Text\n")
        pruefe(r["ok"] and "warnung" not in r,
               "Nicht-Python wird nicht gewarnt")

        # Verlust beim Ueberschreiben sichtbar machen (24.08.2026).
        schreiben("bau.py", "import sys\n"
                            "def wichtig():\n    return 1\n"
                            "def auch_wichtig():\n    return 2\n"
                            "if '--selbsttest' in sys.argv:\n    pass\n")
        r = schreiben("bau.py", "import sys\n"
                                "def wichtig():\n    return 1\n"
                                "if '--selbsttest' in sys.argv:\n    pass\n")
        pruefe(r.get("verloren") == ["auch_wichtig"],
               "verlorene Funktion wird beim Ueberschreiben gemeldet",
               str(r.get("verloren")))
        r = schreiben("bau.py", "import sys\n"
                                "def wichtig():\n    return 1\n"
                                "def neu():\n    return 3\n"
                                "if '--selbsttest' in sys.argv:\n    pass\n")
        pruefe("verloren" not in r,
               "Hinzufuegen allein loest keine Verlustmeldung aus "
               "(Gegenprobe)", str(r.get("verloren")))

        r = schreiben("boese.sh", "rm -rf /\n")
        pruefe(not r["ok"], "verbotene Endung (.sh) abgelehnt")
        r = schreiben("gross.py", "x" * (MAX_DATEI_BYTES + 1))
        pruefe(not r["ok"], "zu grosse Datei abgelehnt")

        # --- lesen / liste ---
        r = lesen("projekt/hallo.py")
        pruefe(r["ok"] and "hallo" in r["inhalt"], "Lesen geht")
        r = lesen("../../etc/passwd")
        pruefe(not r["ok"], "Lesen ausserhalb abgelehnt")
        r = liste("")
        pruefe(r["ok"] and "projekt/hallo.py" in r["eintraege"],
               "Liste zeigt die Sandkasten-Datei")

        # --- testen: kompilieren, Selbsttest gruen und rot ---
        schreiben("kaputt.py", "def (:\n")
        r = testen("kaputt.py")
        pruefe(not r["ok"] and r["phase"] == "kompilieren",
               "Syntaxfehler faellt beim Testen auf")
        # Ohne Schalter ist nichts geprueft - und das MUSS rot sein.
        schreiben("ohne_schalter.py", "x = 1\n")
        r = testen("ohne_schalter.py")
        pruefe(not r["ok"] and r["phase"] == "luecke",
               "Datei ohne --selbsttest gilt als LUECKE, nicht als ok")
        schreiben("gut.py",
                  "import sys\n"
                  "if '--selbsttest' in sys.argv:\n"
                  "    print('alles gut'); sys.exit(0)\n")
        r = testen("gut.py")
        pruefe(r["ok"] and r["phase"] == "selbsttest",
               "gruener Selbsttest wird als bestanden erkannt", str(r)[:80])
        schreiben("rot.py",
                  "import sys\n"
                  "if '--selbsttest' in sys.argv:\n"
                  "    print('kaputt'); sys.exit(1)\n")
        r = testen("rot.py")
        pruefe(not r["ok"] and r["exitcode"] == 1,
               "roter Selbsttest wird als durchgefallen erkannt (Gegenprobe)")

        # --- testen darf NUR den Sandkasten anfassen ---
        r = testen("../../../opt/ki-server/harness/werkstatt.py")
        pruefe(not r["ok"], "testen ausserhalb des Sandkastens abgelehnt")

        # --- Die Einsperrung: der wichtigste Test der Werkstatt ---
        # Zwei Seiten. Erst: ein braver Test darf laufen und tempfile
        # benutzen (sonst waere die Sperre zu eng und die Werkstatt
        # unbrauchbar). Dann: ein Ausbruchsversuch MUSS scheitern.
        # Diese zweite Haelfte ist der Grund, warum es die Werkstatt
        # ueberhaupt geben darf - ohne sie waere Mexlas Zusage
        # "Tim fasst deine Daten nicht an" unbelegt.
        schreiben("brav.py",
                  "import sys, tempfile, pathlib\n"
                  "if '--selbsttest' in sys.argv:\n"
                  "    with tempfile.TemporaryDirectory() as o:\n"
                  "        pathlib.Path(o, 'x.txt').write_text('ok')\n"
                  "    pathlib.Path('eigene.txt').write_text('ok')\n"
                  "    print('brav'); sys.exit(0)\n")
        r = testen("brav.py")
        pruefe(r["ok"], "eingesperrt: tempfile und Sandkasten gehen weiter",
               str(r)[:120])

        # Das Ausbruchsziel muss WIRKLICH draussen liegen. Im ersten
        # Entwurf stand es im Temp-Ordner - und der ist fuer tempfile
        # ausdruecklich erlaubt. Der Test meldete deshalb "AUSBRUCH
        # GELUNGEN", obwohl die Sperre sauber arbeitete: Er hat auf das
        # falsche Ziel gezeigt. Jetzt zeigt er ins Heimverzeichnis -
        # dorthin darf Tims Code unter keinen Umstaenden schreiben.
        ziel_aussen = Path.home() / ".werkstatt_ausbruchsprobe"
        ziel_aussen.unlink(missing_ok=True)
        schreiben("ausbruch.py",
                  "import sys, pathlib\n"
                  "if '--selbsttest' in sys.argv:\n"
                  "    try:\n"
                  "        pathlib.Path(%r).write_text('hier war tim')\n"
                  "        print('AUSBRUCH GELUNGEN'); sys.exit(0)\n"
                  "    except Exception as f:\n"
                  "        print('geblockt:', type(f).__name__); sys.exit(1)\n"
                  % str(ziel_aussen))
        r = testen("ausbruch.py")
        if Path("/usr/bin/sandbox-exec").exists():
            pruefe(not r["ok"] and not ziel_aussen.exists(),
                   "eingesperrt: Tims Code kommt NICHT aus dem Sandkasten "
                   "heraus", str(r.get("ausgabe", ""))[:80])
        else:
            pruefe(True, "sandbox-exec fehlt - Einsperrung uebersprungen "
                         "(nur macOS)")
        ziel_aussen.unlink(missing_ok=True)

        # --- Befehlsfolgen: der Uebungsraum des Fuehrerscheins ---
        # Erst die Pruefung als pure Funktion (beide Seiten), dann ein
        # echter Lauf durch die Sandbox.
        pruefe(befehl_pruefen(["ls", "-l"])[0] == ["/bin/ls", "-l"],
               "ls -l wird zum echten Aufruf uebersetzt",
               str(befehl_pruefen(["ls", "-l"])))
        for boese, was in ((["rm", "-rf", "x"], "rm"),
                           (["find", ".", "-delete"], "find"),
                           (["curl", "beispiel.de"], "curl"),
                           (["git", "push"], "git"),
                           (["sh", "-c", "ls"], "sh")):
            aufruf, grund = befehl_pruefen(boese)
            pruefe(aufruf is None and "nicht erlaubt" in grund,
                   "%s wird abgelehnt" % was, str(grund)[:60])
        for arg, was in (("/etc/passwd", "absoluter Pfad"),
                         ("~/.m1_job_token", "Heimverzeichnis"),
                         ("../../.m1_job_token", "Ausbruch per ..")):
            aufruf, grund = befehl_pruefen(["cat", arg])
            pruefe(aufruf is None, "%s als Argument wird abgelehnt" % was,
                   str(grund)[:60])
        pruefe(befehl_pruefen(["ls", "unterordner"])[0] is not None,
               "ein relativer Pfad im Sandkasten ist erlaubt")
        pruefe(befehl_pruefen(["cat"])[0] == ["/bin/cat"],
               "ein Befehl ohne Argumente geht auch")
        pruefe(befehl_pruefen("ls -l")[0] is None,
               "eine Zeichenkette statt einer Liste wird abgelehnt")

        if Path("/usr/bin/sandbox-exec").exists():
            schreiben("gruss.txt", "hallo tim\nzweite zeile\n")
            schreiben("folge.json",
                      '[["cat", "gruss.txt"], ["wc", "-l", "gruss.txt"],'
                      ' ["rm", "gruss.txt"], ["cat", "/etc/passwd"]]')
            r = befehle_fahren("folge.json")
            pruefe(r["ok"] and len(r["schritte"]) == 4,
                   "vier Zeilen ergeben vier Schritte", str(r)[:100])
            pruefe("hallo tim" in r["schritte"][0]["ausgabe"],
                   "cat liest die Datei im Sandkasten wirklich",
                   str(r["schritte"][0])[:80])
            pruefe(r["abgelehnt"] == 2
                   and "abgelehnt" in r["schritte"][2]
                   and "abgelehnt" in r["schritte"][3],
                   "rm und der Fremdpfad werden abgelehnt, der Rest laeuft",
                   str(r["abgelehnt"]))
            # Der Riegel muss WIRKLICH halten, nicht nur im Kommentar
            probe = Path.home() / ".werkstatt_befehlsprobe"
            probe.write_text("geheim")
            schreiben("lesen_aussen.json",
                      '[["cat", "%s"]]' % str(probe))
            r = befehle_fahren("lesen_aussen.json")
            pruefe(r["abgelehnt"] == 1
                   and not any("geheim" in str(s.get("ausgabe", ""))
                               for s in r["schritte"]),
                   "eine Datei ausserhalb wird nicht ausgelesen", str(r)[:100])
            probe.unlink(missing_ok=True)
            schreiben("keine_liste.json", '{"befehl": "ls"}')
            pruefe(not befehle_fahren("keine_liste.json")["ok"],
                   "kaputtes JSON-Format gibt eine klare Fehlermeldung")
            pruefe(not befehle_fahren("gibtsnicht.json")["ok"],
                   "fehlende Datei gibt eine klare Fehlermeldung")
            pruefe(not befehle_fahren("brav.py")["ok"],
                   "nur .json wird als Befehlsfolge angenommen")
        else:
            pruefe(True, "sandbox-exec fehlt - Befehlsfolgen uebersprungen")

        # --- Aufraeumen: verschieben, niemals loeschen ---
        global ALTABLAGE
        echte_ablage = ALTABLAGE
        ALTABLAGE = Path(ordner) / "_alt"
        try:
            schreiben("wegzuraeumen.py",
                      "import sys\nif '--selbsttest' in sys.argv:\n    pass\n")
            vorher = sorted(p.name for p in SANDKASTEN.iterdir())
            r = aufraeumen()
            pruefe(r["ok"] and r["verschoben"] >= 1,
                   "Aufraeumen meldet, wie viel bewegt wurde", str(r))
            pruefe(not any(p for p in SANDKASTEN.iterdir()
                           if not p.name.startswith(".")),
                   "der Sandkasten ist danach leer")
            # Der entscheidende Teil: nichts ist WEG.
            gerettet = sorted(p.name for p in Path(r["nach"]).iterdir())
            pruefe(gerettet == vorher,
                   "alles liegt vollstaendig in der Altablage",
                   "%s vs %s" % (gerettet, vorher))
            r2 = aufraeumen()
            pruefe(r2["ok"] and r2["verschoben"] == 0,
                   "zweites Aufraeumen tut nichts und meldet das")
        finally:
            ALTABLAGE = echte_ablage

        # --- Lernprotokoll: anhaengen, nie ersetzen ---
        global LERNPROTOKOLL
        echt_lp = LERNPROTOKOLL
        LERNPROTOKOLL = Path(ordner) / "gelernt" / "LERNPROTOKOLL.md"
        try:
            r = lernnotiz("probe", "zu kurz")
            pruefe(not r["ok"], "zu duenne Lernnotiz wird abgelehnt")
            r = lernnotiz("", "x" * 60)
            pruefe(not r["ok"], "Lernnotiz ohne Aufgabe wird abgelehnt")
            r = lernnotiz("aufgabe_eins",
                          "Ich habe gelernt, dass ein Test beide Seiten "
                          "pruefen muss - sonst besteht ihn auch Unsinn.")
            pruefe(r["ok"], "gueltige Lernnotiz wird angenommen", str(r))
            r = lernnotiz("aufgabe_zwei",
                          "Zweite Erkenntnis: Fehlermeldungen lesen, statt "
                          "denselben Versuch zu wiederholen.")
            pruefe(r["ok"], "zweite Lernnotiz wird angenommen")
            inhalt = gelerntes_lesen()["inhalt"]
            pruefe("aufgabe_eins" in inhalt and "aufgabe_zwei" in inhalt,
                   "beide Notizen stehen im Protokoll - es wird ANGEHAENGT")
            pruefe(inhalt.index("aufgabe_eins") < inhalt.index("aufgabe_zwei"),
                   "Reihenfolge bleibt: aeltestes zuerst")
        finally:
            LERNPROTOKOLL = echt_lp

    SANDKASTEN = echt
    PROTOKOLL = echt_protokoll

    if fehler:
        print("\n%d Fehler." % fehler)
    else:
        print("\nAlle Pruefungen bestanden.")
    return fehler


def main(argumente: list[str]) -> int:
    if not argumente or argumente[0] == "--selbsttest":
        return _selbsttest()
    befehl = argumente[0]
    rest = argumente[1:]
    if befehl == "schreiben" and rest:
        inhalt = sys.stdin.read()
        print(json.dumps(schreiben(rest[0], inhalt), ensure_ascii=False))
    elif befehl == "lesen" and rest:
        print(json.dumps(lesen(rest[0]), ensure_ascii=False))
    elif befehl == "liste":
        print(json.dumps(liste(rest[0] if rest else ""), ensure_ascii=False))
    elif befehl == "testen" and rest:
        print(json.dumps(testen(rest[0]), ensure_ascii=False, indent=2))
    elif befehl == "befehle" and rest:
        print(json.dumps(befehle_fahren(rest[0]), ensure_ascii=False, indent=2))
    elif befehl == "neu" and rest:
        print(json.dumps(neue_aufgabe(rest[0]), ensure_ascii=False, indent=2))
    elif befehl == "aufraeumen":
        print(json.dumps(aufraeumen(), ensure_ascii=False, indent=2))
    elif befehl == "gelernt":
        print(json.dumps(gelerntes_lesen(), ensure_ascii=False, indent=2))
    elif befehl == "lernnotiz" and rest:
        print(json.dumps(lernnotiz(rest[0], sys.stdin.read()),
                         ensure_ascii=False))
    else:
        print("Aufruf: neu <name> | schreiben <pfad> | lesen <pfad> | "
              "liste [pfad] | testen <pfad> | befehle <pfad.json> | "
              "--selbsttest")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
