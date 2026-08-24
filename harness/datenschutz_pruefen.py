#!/usr/bin/env python3
"""Datenschutz-Pruefung vor dem Veroeffentlichen - nur lesend.

Warum es dieses Werkzeug gibt: In der Nacht auf den 24.08.2026 wurden
zwei Repos fuer GitHub vorbereitet - und dabei musste von Hand geprueft
werden, dass kein Mesh-Schluessel, kein Token, keine E-Mail und kein
Klarname mitgeht. Der Mesh-Schluessel steckte da bereits in der
HISTORIE, nicht mehr in der Arbeitskopie - genau der Fall, den ein
Blick auf die Dateien allein nie findet.

Dieses Werkzeug macht diese Pruefung wiederholbar und gibt sie Tim als
Knopf. Es ist der grosse Bruder des pre-commit-Riegels
(scripts/git_hook_datenschutz.sh): Der Riegel prueft beim Commit die
NEU angemeldeten Zeilen - dieses Werkzeug prueft jederzeit das ganze
Repo: Arbeitskopie, komplette Historie und die Commit-Identitaeten.
Beide lesen DIESELBE Musterdatei (config/datenschutz_muster.txt, '#'
Kommentar, '!' Ausnahme, sonst erweiterter regulaerer Ausdruck).
Ein zweites Musterformat gaebe zwei Wahrheiten - deshalb gibt es keins.

Die Sicherheitslinie:

  * Nur lesend. Kein Commit, kein Loeschen, kein Umschreiben - der
    Selbsttest belegt, dass 'git status' vor und nach einem Lauf
    identisch ist.
  * Die Fundmeldung nennt Datei, Zeile und die NUMMER des Musters -
    nie den Inhalt. Ein Waechter, der die Geheimnisse ausplaudert,
    waere selbst das Leck (dieselbe Lehre wie beim Riegel: die Muster
    stehen in einer ignorierten Datei, nicht im Code).
  * Feste Repo-Liste statt freiem Pfad-Argument.

Veroeffentlichen kann dieses Werkzeug NICHT - kein Push, kein Commit.
Es sagt nur ja oder nein. Den Push macht Mexla (oder die
Wochensicherung am Sonntag, die vorher dieselbe Frage stellt).

Aufruf:
    python3 datenschutz_pruefen.py              # alle bekannten Repos
    python3 datenschutz_pruefen.py tim          # nur eines
    python3 datenschutz_pruefen.py --selbsttest

Exit: 0 = sauber, 1 = Funde, 2 = LUECKE (Musterdatei fehlt o.ae.).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPOS = {
    "tim": Path("/opt/ki-server"),
    "brmesh": Path.home() / "Desktop" / "brmesh-bridge",
}
HAUS_MUSTER = Path("/opt/ki-server/config/datenschutz_muster.txt")
# Dateinamen, die in KEINEM veroeffentlichten Repo verfolgt sein duerfen -
# sie enthalten ihrer Natur nach Geheimnisse oder eben die Suchmuster.
NIE_VERFOLGT = ("datenschutz_muster.txt", "ha_token.secret",
                "autonomie.conf", "mitschnitte.json", "bruecke_konfig.json",
                "bruecke_wlan.json", "bruecke_zugang.json",
                "com.mexla.iogpu-limit.plist")
HISTORIE_GRENZE = 4000         # Objekt-Obergrenze; mehr meldet eine LUECKE


def muster_laden(pfad: Path) -> tuple[list, list] | None:
    """(verboten, ausnahmen) aus der Musterdatei - oder None."""
    try:
        text = pfad.read_text(encoding="utf-8")
    except OSError:
        return None
    verboten, ausnahmen = [], []
    for zeile in text.splitlines():
        zeile = zeile.strip()
        if not zeile or zeile.startswith("#"):
            continue
        if zeile.startswith("!"):
            ausnahmen.append(zeile[1:])
        else:
            verboten.append(zeile)
    if not verboten:
        return None
    return verboten, ausnahmen


def zeile_verletzt(zeile: str, verboten: list, ausnahmen: list) -> int | None:
    """Nummer (1-basiert) des ersten verletzten Musters - oder None.

    Dieselbe Logik wie im pre-commit-Riegel: Um jeden Treffer wird ein
    kleines Umfeld gelegt (14 Zeichen davor, 16 danach), und nur wenn
    KEINE Ausnahme auf dieses Umfeld passt, zaehlt der Treffer.
    Ausnahmen matchen wie im Riegel ohne Beachtung der Schreibung.
    """
    for nummer, muster in enumerate(verboten, start=1):
        try:
            treffer = list(re.finditer(muster, zeile))
        except re.error:
            continue
        for t in treffer:
            umfeld = zeile[max(0, t.start() - 14):t.end() + 16]
            if not any(re.search(a, umfeld, re.IGNORECASE)
                       for a in ausnahmen):
                return nummer
    return None


def _git(repo: Path, *argumente, eingabe: str | None = None):
    """Ein Git-Aufruf im Repo. Rueckgabe (exitcode, stdout)."""
    lauf = subprocess.run(["git", "-C", str(repo)] + list(argumente),
                          capture_output=True, text=True, timeout=120,
                          input=eingabe)
    return lauf.returncode, lauf.stdout


def _ist_text(pfad: Path) -> bool:
    try:
        with open(pfad, "rb") as d:
            return b"\0" not in d.read(8000)
    except OSError:
        return False


def pruefe_arbeitskopie(repo: Path, verboten: list, ausnahmen: list) -> list:
    """Funde in allen Dateien, die ein 'git add .' erfassen wuerde."""
    rc, liste = _git(repo, "ls-files", "--cached", "--others",
                     "--exclude-standard")
    if rc != 0:
        return ["LUECKE %s ist kein Git-Repo." % repo]
    funde = []
    for rel in liste.splitlines():
        pfad = repo / rel
        if not pfad.is_file() or not _ist_text(pfad):
            continue
        try:
            zeilen = pfad.read_text(encoding="utf-8",
                                    errors="replace").splitlines()
        except OSError:
            continue
        for nr, zeile in enumerate(zeilen, start=1):
            muster = zeile_verletzt(zeile, verboten, ausnahmen)
            if muster is not None:
                funde.append("Arbeitskopie %s:%d - Muster Nr. %d"
                             % (rel, nr, muster))
    return funde


def pruefe_historie(repo: Path, verboten: list, ausnahmen: list) -> list:
    """Funde in JEDEM Dateistand, der je committet wurde.

    Ein Geheimnis, das nur noch in der Historie steckt, geht beim Push
    trotzdem mit - genau so lag der Mesh-Schluessel vor der Bereinigung
    im brmesh-Repo. Geprueft wird jeder Blob der Historie mit DERSELBEN
    zeile_verletzt-Logik wie die Arbeitskopie - eine Regex-Engine, eine
    Wahrheit. 'git grep -E' waere schneller, aber es kennt \\b nicht:
    gemessen am 24.08.2026 fand ein Wortgrenzen-Muster in einer Datei
    mit sechs echten Klarnamen-Treffern NICHTS - der Scan waere auf
    genau dieses Muster blind gewesen. Der Selbsttest haelt den Fall
    fest. (Lehre des Riegels, die auch hier gilt: kein verbotenes Wort
    als Beispiel in den eigenen Text schreiben - sonst meldet der
    Waechter sich selbst, wie beim ersten Entwurf geschehen.)
    """
    rc, objekte = _git(repo, "rev-list", "--all", "--objects")
    if rc != 0:
        return []
    pfade: dict[str, str] = {}          # Blob-Kennung -> ein Fundpfad
    for zeile in objekte.splitlines():
        teile = zeile.split(" ", 1)
        if len(teile) == 2 and teile[1]:
            pfade.setdefault(teile[0], teile[1])
    if not pfade:
        return []
    funde = []
    if len(pfade) > HISTORIE_GRENZE:
        funde.append("LUECKE Historie hat %d Objekte, geprueft werden "
                     "die ersten %d." % (len(pfade), HISTORIE_GRENZE))
        pfade = dict(list(pfade.items())[:HISTORIE_GRENZE])
    # Ein einziger cat-file-Prozess liefert alle Inhalte am Stueck.
    lauf = subprocess.run(["git", "-C", str(repo), "cat-file", "--batch"],
                          input="\n".join(pfade).encode("utf-8"),
                          capture_output=True, timeout=600)
    daten = lauf.stdout
    stelle = 0
    while stelle < len(daten):
        ende = daten.find(b"\n", stelle)
        if ende < 0:
            break
        kopf = daten[stelle:ende].decode("utf-8", "replace").split()
        stelle = ende + 1
        if len(kopf) != 3 or kopf[1] == "missing":
            continue
        laenge = int(kopf[2])
        inhalt = daten[stelle:stelle + laenge]
        stelle += laenge + 1            # das abschliessende \n
        if kopf[1] != "blob" or b"\0" in inhalt[:8000]:
            continue
        text = inhalt.decode("utf-8", "replace")
        for nr, zeile in enumerate(text.splitlines(), start=1):
            muster = zeile_verletzt(zeile, verboten, ausnahmen)
            if muster is not None:
                funde.append("Historie %s:%d (Objekt %s) - Muster Nr. %d"
                             % (pfade.get(kopf[0], "?"), nr, kopf[0][:10],
                                muster))
    return funde


def pruefe_identitaeten(repo: Path, verboten: list, ausnahmen: list) -> list:
    """Autoren und Committer der Historie gegen die Muster."""
    rc, text = _git(repo, "log", "--all", "--format=%h%x09%an <%ae>%x09%cn <%ce>")
    if rc != 0:
        return []
    funde = []
    for zeile in text.splitlines():
        teile = zeile.split("\t")
        if len(teile) != 3:
            continue
        for rolle, wer in (("Autor", teile[1]), ("Committer", teile[2])):
            muster = zeile_verletzt(wer, verboten, ausnahmen)
            if muster is not None:
                funde.append("Identitaet Commit %s: %s verletzt Muster "
                             "Nr. %d" % (teile[0], rolle, muster))
    return funde


def pruefe_verfolgte_namen(repo: Path) -> list:
    """Dateien, die ihrem Namen nach nie verfolgt sein duerfen."""
    rc, liste = _git(repo, "ls-files")
    if rc != 0:
        return []
    return ["Verfolgt, gehoert aber in .gitignore: %s" % rel
            for rel in liste.splitlines()
            if Path(rel).name in NIE_VERFOLGT]


def repo_pruefen(name: str, repo: Path) -> tuple[int, list]:
    """Alle vier Blicke auf ein Repo. Rueckgabe (exitcode, funde)."""
    if not (repo / ".git").exists():
        return 2, ["LUECKE %s (%s) ist kein Git-Repo - keine Aussage."
                   % (name, repo)]
    eigene = repo / "config" / "datenschutz_muster.txt"
    musterdatei = eigene if eigene.is_file() else HAUS_MUSTER
    geladen = muster_laden(musterdatei)
    if geladen is None:
        return 2, ["LUECKE Musterdatei %s fehlt oder ist leer - ohne "
                   "Muster keine Pruefung. Anlegen nach dem Muster von "
                   "config/datenschutz_muster.txt.example." % musterdatei]
    verboten, ausnahmen = geladen
    funde = (pruefe_arbeitskopie(repo, verboten, ausnahmen)
             + pruefe_historie(repo, verboten, ausnahmen)
             + pruefe_identitaeten(repo, verboten, ausnahmen)
             + pruefe_verfolgte_namen(repo))
    return (1 if funde else 0), funde


def bericht(nur: str | None = None) -> int:
    schlimmster = 0
    for name, repo in REPOS.items():
        if nur and name != nur:
            continue
        print("Datenschutz-Pruefung %s (%s):" % (name, repo))
        code, funde = repo_pruefen(name, repo)
        if code == 0:
            print("  ok      Arbeitskopie, Historie, Identitaeten und "
                  "Dateinamen sind sauber.")
        for f in funde:
            print("  FUND    %s" % f)
        schlimmster = max(schlimmster, code)
        print()
    if schlimmster == 0:
        print("Ergebnis: veroeffentlichen unbedenklich. (Der letzte "
              "Blick vor einem Push bleibt trotzdem Mexlas - dieses "
              "Werkzeug kennt nur die eingetragenen Muster.)")
    elif schlimmster == 1:
        print("Ergebnis: NICHT veroeffentlichen. Fundstellen bereinigen "
              "(steht ein Fund nur in der Historie, muss die Historie "
              "umgeschrieben werden - Dateien loeschen reicht nicht), "
              "danach HIER gegenpruefen.")
    return schlimmster


# ----------------------------------------------------------------------
# Selbsttest - gegen ein Wegwerf-Repo, nie gegen die echten
# ----------------------------------------------------------------------
def _selbsttest() -> int:
    import io
    from contextlib import redirect_stdout

    fehler = 0

    def pruefe(bedingung, text, zusatz=""):
        nonlocal fehler
        if bedingung:
            print("  ok      %s" % text)
        else:
            print("  FEHLER  %s%s" % (text, ("  [%s]" % zusatz) if zusatz else ""))
            fehler += 1

    print("datenschutz_pruefen Selbsttest:")

    # --- Musterdatei-Format (identisch zum Riegel) ---
    with tempfile.TemporaryDirectory() as ordner:
        md = Path(ordner) / "muster.txt"
        md.write_text("# Kommentar\n\ngeheimwert123\n"
                      "verraten@beispiel\\.de\n!erlaubtes geheimwert123\n")
        geladen = muster_laden(md)
        pruefe(geladen == (["geheimwert123", "verraten@beispiel\\.de"],
                           ["erlaubtes geheimwert123"]),
               "Musterdatei: Kommentare, Muster und Ausnahmen", str(geladen))
        pruefe(muster_laden(Path(ordner) / "fehlt.txt") is None,
               "fehlende Musterdatei ergibt None")
        md.write_text("# nur Kommentar\n")
        pruefe(muster_laden(md) is None, "Musterdatei ohne Muster ergibt None")

    verboten = ["geheimwert123", "verraten@beispiel\\.de"]
    ausnahmen = ["erlaubtes geheimwert123"]
    pruefe(zeile_verletzt("hier steht geheimwert123 drin",
                          verboten, ausnahmen) == 1,
           "Treffer wird mit Muster-Nummer gemeldet")
    pruefe(zeile_verletzt("ein erlaubtes geheimwert123 eben",
                          verboten, ausnahmen) is None,
           "Ausnahme im Umfeld laesst den Treffer durch")
    pruefe(zeile_verletzt("post an verraten@beispiel.de senden",
                          verboten, ausnahmen) == 2,
           "regulaere Ausdruecke greifen (E-Mail)")
    pruefe(zeile_verletzt("alles harmlos", verboten, ausnahmen) is None,
           "saubere Zeile ergibt None (Gegenprobe)")

    # --- Ein Wegwerf-Repo mit eingebauten Suenden ---
    umgebung = dict(os.environ,
                    GIT_AUTHOR_NAME="Probe", GIT_AUTHOR_EMAIL="p@example.org",
                    GIT_COMMITTER_NAME="Probe",
                    GIT_COMMITTER_EMAIL="p@example.org",
                    GIT_CONFIG_GLOBAL="/dev/null",
                    GIT_CONFIG_SYSTEM="/dev/null")

    def git(repo, *argumente, env=None):
        return subprocess.run(["git", "-C", str(repo)] + list(argumente),
                              capture_output=True, text=True,
                              env=env or umgebung, timeout=60)

    with tempfile.TemporaryDirectory() as ordner:
        repo = Path(ordner)
        git(repo, "init", "-q")
        (repo / "sauber.txt").write_text("nichts zu sehen\n")
        git(repo, "add", "."); git(repo, "commit", "-qm", "start")

        pruefe(pruefe_arbeitskopie(repo, verboten, ausnahmen) == [],
               "sauberes Repo: keine Funde in der Arbeitskopie")
        pruefe(pruefe_historie(repo, verboten, ausnahmen) == [],
               "sauberes Repo: keine Funde in der Historie")
        pruefe(pruefe_identitaeten(repo, verboten, ausnahmen) == [],
               "sauberes Repo: keine Funde in den Identitaeten")

        # Suende 1: Geheimnis in einer NEUEN, noch unverfolgten Datei -
        # genau die wuerde ein 'git add .' der Wochensicherung erfassen.
        (repo / "neu.txt").write_text("x = 'geheimwert123'\n")
        funde = pruefe_arbeitskopie(repo, verboten, ausnahmen)
        pruefe(funde == ["Arbeitskopie neu.txt:1 - Muster Nr. 1"],
               "Geheimnis in unverfolgter Datei wird gefunden", str(funde))

        # Suende 2: Geheimnis committen, dann "loeschen" - es bleibt in
        # der Historie und MUSS dort gefunden werden.
        git(repo, "add", "."); git(repo, "commit", "-qm", "leck")
        (repo / "neu.txt").write_text("bereinigt\n")
        git(repo, "add", "."); git(repo, "commit", "-qm", "aufgeraeumt")
        pruefe(pruefe_arbeitskopie(repo, verboten, ausnahmen) == [],
               "nach dem Loeschen ist die Arbeitskopie sauber")
        funde = pruefe_historie(repo, verboten, ausnahmen)
        pruefe(any("neu.txt" in f and "Muster Nr. 1" in f for f in funde),
               "dasselbe Geheimnis wird in der HISTORIE gefunden",
               str(funde))

        # Der Fall, an dem 'git grep -E' gescheitert waere (gemessen am
        # 24.08.2026: '\\bMax\\b' fand in 6 echten Treffern NICHTS):
        # Ein Muster mit Wortgrenze muss auch in der Historie greifen -
        # und sein laengeres Zwillingswort weiter durchlassen.
        wortmuster = verboten + ["\\bKlarname\\b"]
        (repo / "neu.txt").write_text("Klarname war hier\n"
                                      "Klarnamen sind ein anderes Wort\n")
        git(repo, "add", "."); git(repo, "commit", "-qm", "wort")
        (repo / "neu.txt").write_text("wieder bereinigt\n")
        git(repo, "add", "."); git(repo, "commit", "-qm", "wort weg")
        funde = pruefe_historie(repo, wortmuster, ausnahmen)
        pruefe(any(":1 " in f and "Muster Nr. 3" in f for f in funde),
               "Wortgrenzen-Muster (\\b) greift auch in der Historie",
               str(funde))
        pruefe(not any(":2 " in f and "Muster Nr. 3" in f for f in funde),
               "das laengere Zwillingswort bleibt erlaubt (Gegenprobe)")

        # Suende 3: private E-Mail als Commit-Identitaet.
        boese = dict(umgebung, GIT_AUTHOR_EMAIL="verraten@beispiel.de")
        (repo / "sauber.txt").write_text("neuer stand\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "identitaet", env=boese)
        funde = pruefe_identitaeten(repo, verboten, ausnahmen)
        pruefe(any("Autor" in f for f in funde),
               "private E-Mail in der Commit-Identitaet wird gefunden",
               str(funde))

        # Suende 4: eine Geheimnis-Datei ist verfolgt.
        (repo / "mitschnitte.json").write_text("[]\n")
        git(repo, "add", "mitschnitte.json")
        git(repo, "commit", "-qm", "datei")
        funde = pruefe_verfolgte_namen(repo)
        pruefe(funde and "mitschnitte.json" in funde[0],
               "verfolgte Geheimnis-Datei wird gemeldet", str(funde))

        # Nur-lesend-Beweis: git status vor und nach einem vollen Lauf.
        vorher = git(repo, "status", "--porcelain").stdout
        for blick in (pruefe_arbeitskopie, pruefe_historie,
                      pruefe_identitaeten):
            blick(repo, verboten, ausnahmen)
        pruefe_verfolgte_namen(repo)
        pruefe(git(repo, "status", "--porcelain").stdout == vorher,
               "die Pruefung veraendert das Repo nicht")

        # Der Waechter darf die Geheimnisse NICHT ausplaudern: Die
        # Fundmeldungen nennen Nummern, nie den Inhalt.
        (repo / "neu.txt").write_text("x = 'geheimwert123'\n")
        alle = (pruefe_arbeitskopie(repo, verboten, ausnahmen)
                + pruefe_historie(repo, verboten, ausnahmen))
        pruefe(alle and all("geheimwert123" not in f for f in alle),
               "keine Fundmeldung enthaelt das Geheimnis selbst")

    # --- Die Repo-Liste ist fest - kein freier Pfad von aussen ---
    puffer = io.StringIO()
    with redirect_stdout(puffer):
        code = bericht("gibtsnicht")
    pruefe(code == 0 and "Pruefung" not in puffer.getvalue().split("\n")[0],
           "unbekannter Repo-Name prueft nichts", puffer.getvalue()[:50])

    if fehler:
        print("\n%d Fehler." % fehler)
    else:
        print("\nAlle Pruefungen bestanden.")
    return fehler


def main(argumente: list[str]) -> int:
    if "--selbsttest" in argumente:
        return _selbsttest()
    nur = argumente[0] if argumente else None
    if nur and nur not in REPOS:
        print("Unbekanntes Repo: %s. Bekannt: %s"
              % (nur, ", ".join(sorted(REPOS))))
        return 2
    return bericht(nur)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
