#!/usr/bin/env python3
"""Routinen: zeitgesteuerte Prueflaeufe mit Einordnung durch Tim.

Was eine Routine darf, entscheidet NICHT diese Datei: Jeder Schritt
ist eine Aktion aus der Positivliste des Job-Servers (Port 8765), und
der prueft selbst - Positivliste, Kill-Switch, NIEMALS-Grenzen. Diese
Datei ist nur der Takthalter und der Botengang: Schritte ausfuehren,
Rohbefund als Bericht ablegen, Tim um eine Einordnung bitten
(Unterhaltung "routine" in der Zentrale), und NUR bei Problemen eine
Benachrichtigung nach Home Assistant geben. Gruene Tage bleiben still.

Einzige eingebaute Ausnahme: der Schritt "git_stand" (nur lesend),
weil der Sonntags-Check "bereit zum Sichern?" den Stand der
Arbeitskopie braucht und git kein Fall fuer die Positivliste ist.
Der Push selbst bleibt Handarbeit - der letzte Blick bleibt bei Mexla.

Aufruf:  routine.py <name>          eine Routine aus config/routinen.json
         routine.py --liste         vorhandene Routinen zeigen
         routine.py --selbsttest    Pruefungen (ohne echte Server!)
"""

import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

BASIS = Path("/opt/ki-server")
ROUTINEN_DATEI = BASIS / "config" / "routinen.json"
BERICHTE_DIR = BASIS / "berichte"
TOKEN_DATEI = Path.home() / ".m1_job_token"
JOB_SERVER = "http://127.0.0.1:8765"
ZENTRALE = "http://127.0.0.1:8770"

# Dieselbe Form-Regel wie an den anderen Riegeln des Hauses: Namen und
# Argumente sind Woerter, nie Shell-Material. Punkt ist noetig
# (selbsttests.md), Schraegstrich bleibt draussen (kein Pfad).
SICHERES_WORT = re.compile(r"^[A-Za-z0-9_.\-]{1,80}$")

# Woran ein Rohbefund als PROBLEM erkannt wird. "HINWEIS" fehlt mit
# Absicht: Der bekannte kamera-plist-Hinweis (brew-Falle) wuerde sonst
# jeden Tag Alarm schlagen, und ein Alarm, der immer kommt, ist keiner.
PROBLEM_MARKER = ("FUND", "VERALTET", "ABWEICHUNG", "FEHLER",
                  "NICHT veroeffentlichen", "PROBLEM")


def _token() -> str:
    try:
        return TOKEN_DATEI.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _post(adresse: str, pfad: str, koerper: dict, timeout: int) -> dict:
    anfrage = urllib.request.Request(
        adresse + pfad, data=json.dumps(koerper).encode("utf-8"),
        method="POST")
    anfrage.add_header("Content-Type", "application/json")
    anfrage.add_header("X-M1-Token", _token())
    try:
        with urllib.request.urlopen(anfrage, timeout=timeout) as antwort:
            return json.loads(antwort.read().decode("utf-8"))
    except urllib.error.HTTPError as fehler:
        # Der Job-Server antwortet 400, wenn die AKTION ok:False meldet -
        # der Body traegt dann den echten Befund. Ein Befund ist kein
        # Verbindungsfehler; weggeworfen wuerde er zur Falschmeldung
        # "Server nicht erreichbar" (genau so beim ersten Echtlauf
        # am 26.08.2026 passiert).
        try:
            return json.loads(fehler.read().decode("utf-8"))
        except (ValueError, OSError):
            raise fehler


def schritt_aktion(aktion: str, argument: str = "") -> tuple[bool, str]:
    """Einen Schritt ueber die Positivliste des Job-Servers ausfuehren.

    Rueckgabe: (problem, text). Der Job-Server prueft selbst - hier wird
    nur die FORM vorab geprueft, damit kein Shell-Material die Leitung
    entlangreist.
    """
    if not SICHERES_WORT.match(aktion):
        return True, f"Unzulaessiger Aktionsname: {aktion!r}"
    if argument and not SICHERES_WORT.match(argument):
        return True, f"Unzulaessiges Argument: {argument!r}"
    try:
        daten = _post(JOB_SERVER, "/start",
                      {"aktion": aktion, "argument": argument}, timeout=1800)
    except (urllib.error.URLError, OSError, ValueError) as fehler:
        return True, f"Job-Server nicht erreichbar: {fehler}"
    text = str(daten.get("ausgabe", "")).strip()
    if daten.get("fehler"):
        return True, f"Abgelehnt/fehlgeschlagen: {daten['fehler']}"
    problem = (not daten.get("ok", False)) or any(
        m in text for m in PROBLEM_MARKER)
    return problem, text


def schritt_git_stand() -> tuple[bool, str]:
    """Nur lesend: Wie viele Aenderungen warten, was ist der letzte Commit?"""
    try:
        status = subprocess.run(
            ["git", "-C", str(BASIS), "status", "--porcelain"],
            capture_output=True, text=True, timeout=60)
        log = subprocess.run(
            ["git", "-C", str(BASIS), "log", "-1", "--format=%h %s (%cr)"],
            capture_output=True, text=True, timeout=60)
    except (subprocess.TimeoutExpired, FileNotFoundError) as fehler:
        return True, f"git nicht lesbar: {fehler}"
    zeilen = [z for z in status.stdout.splitlines() if z.strip()]
    text = (f"Uncommittete Aenderungen: {len(zeilen)}\n"
            + "\n".join(zeilen[:20])
            + ("\n..." if len(zeilen) > 20 else "")
            + f"\nLetzter Commit: {log.stdout.strip()}")
    # Wartende Aenderungen sind beim Sonntags-Check der NORMALFALL,
    # kein Problem - gemeldet wird "bereit zum Sichern", nicht "Fehler".
    return False, text


def einordnung_holen(name: str, rohbefund: str) -> str:
    """Tim den Rohbefund vorlegen - Unterhaltung 'routine' der Zentrale.

    Die Lehre aus dem Beobachter-Test steht im Auftrag: Ursache ZUERST
    und als Schluss aussprechen - ein Befund, der zwei Moeglichkeiten
    anbietet, bekommt eine Zusammenfassung zurueck.
    """
    auftrag = (
        f"Routine '{name}' ist gelaufen. Hier der Rohbefund:\n\n"
        f"{rohbefund[:6000]}\n\n"
        "Ordne das in HOECHSTENS fuenf Saetzen ein. Regeln: (1) Wenn es "
        "Probleme gibt, nenne ZUERST die wahrscheinlichste URSACHE als "
        "Schluss, nicht als Moeglichkeit. (2) Dann den naechsten Schritt "
        "als Vorschlag an Mexla - du aenderst nichts. (3) Wenn alles "
        "gruen ist, sage das in einem Satz und hoer auf.")
    try:
        daten = _post(ZENTRALE, "/api/chat",
                      {"modell": "auto", "stil": "text", "chat": "routine",
                       "nachrichten": [{"role": "user", "content": auftrag}]},
                      timeout=600)
        return str(daten.get("antwort", "")).strip() or "(keine Einordnung)"
    except (urllib.error.URLError, OSError, ValueError) as fehler:
        return f"(Einordnung nicht moeglich: {fehler})"


def benachrichtigen(titel: str, text: str) -> bool:
    """Laut nur bei Problemen: Meldung an Home Assistant (Handy).

    Nutzt denselben Token und dieselbe Adresse wie ha_diagnose -
    bewusst importiert statt kopiert, damit es nicht auseinanderlaeuft.
    """
    sys.path.insert(0, str(BASIS / "harness"))
    try:
        from ha_diagnose import token_laden, STANDARD_ADRESSE
    except ImportError:
        return False
    token = token_laden()
    if not token:
        return False
    anfrage = urllib.request.Request(
        STANDARD_ADRESSE + "/api/services/notify/notify",
        data=json.dumps({"title": titel, "message": text[:900]}).encode(),
        method="POST")
    anfrage.add_header("Authorization", f"Bearer {token}")
    anfrage.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(anfrage, timeout=15):
            return True
    except (urllib.error.URLError, OSError):
        return False


def routine_laden(name: str) -> dict | None:
    try:
        daten = json.loads(ROUTINEN_DATEI.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for r in daten.get("routinen", []):
        if r.get("name") == name:
            return r
    return None


def routine_fahren(name: str) -> int:
    r = routine_laden(name)
    if not r:
        print(f"Unbekannte Routine: {name} (siehe --liste)")
        return 2
    stempel = datetime.now().strftime("%d.%m.%Y %H:%M")
    teile, probleme = [], []
    for schritt in r.get("schritte", []):
        if schritt.get("eingebaut") == "git_stand":
            problem, text = schritt_git_stand()
            kopf = "git_stand"
        else:
            aktion = str(schritt.get("aktion", ""))
            argument = str(schritt.get("argument", "") or "")
            problem, text = schritt_aktion(aktion, argument)
            kopf = aktion + (f" {argument}" if argument else "")
        teile.append(f"### {kopf}\n\n```\n{text[:4000]}\n```")
        if problem:
            probleme.append(kopf)
    rohbefund = "\n\n".join(teile)

    einordnung = ""
    if r.get("einordnen", True):
        einordnung = einordnung_holen(name, rohbefund)

    BERICHTE_DIR.mkdir(exist_ok=True)
    bericht = BERICHTE_DIR / f"routine_{name}.md"
    bericht.write_text(
        f"# Routine {name} - {stempel}\n\n"
        f"Probleme: {', '.join(probleme) or 'keine'}\n\n"
        f"## Tims Einordnung\n\n{einordnung or '(keine angefordert)'}\n\n"
        f"## Rohbefund\n\n{rohbefund}\n",
        encoding="utf-8")
    print(f"Bericht: {bericht}")

    if probleme and r.get("melden", "leise+laut") == "leise+laut":
        laut = benachrichtigen(
            f"Tim-Routine {name}: {len(probleme)} Problem(e)",
            einordnung or ("Probleme in: " + ", ".join(probleme)))
        print("Benachrichtigung:", "gesendet" if laut else "NICHT zustellbar")
    return 1 if probleme else 0


# ====================== Selbsttest ==========================
# Kein Test erreicht echte Server - Job-Server, Zentrale und HA werden
# durch Doppelgaenger ersetzt (Lehre vom 26.08.: die Regel "keine
# Betriebsdaten" gilt auch fuer den kaputten Zustand, den eine
# Mutation herstellt). Zwei-Seiten-Beweis ueberall: der gesunde Fall
# besteht, der kranke faellt durch.

def _selbsttest() -> int:
    import tempfile
    fehler = [0]

    def pruefe(bedingung, text, zusatz=""):
        stand = "ok     " if bedingung else "FEHLER "
        print(f"  {stand} {text}" + (f"  [{zusatz}]" if zusatz and not bedingung else ""))
        if not bedingung:
            fehler[0] += 1

    global _post, benachrichtigen, ROUTINEN_DATEI, BERICHTE_DIR
    echte_post, echte_benachr = _post, benachrichtigen
    echte_routinen, echte_berichte = ROUTINEN_DATEI, BERICHTE_DIR
    rufe = []

    def post_doppelgaenger(adresse, pfad, koerper, timeout):
        rufe.append((adresse, pfad, koerper))
        if pfad == "/start":
            a = koerper.get("aktion", "")
            if a == "kaputt":
                return {"ok": False, "ausgabe": "FEHLER irgendwo"}
            return {"ok": True, "ausgabe": f"Aktion {a} sauber gelaufen"}
        if pfad == "/api/chat":
            return {"antwort": "Mexla, alles gruen."}
        return {}

    benachr_rufe = []

    def benachr_doppelgaenger(titel, text):
        benachr_rufe.append(titel)
        return True

    print("routine.py Selbsttest:")
    tmp = Path(tempfile.mkdtemp(prefix="m1_routine_test_"))
    try:
        _post = post_doppelgaenger
        benachrichtigen = benachr_doppelgaenger
        ROUTINEN_DATEI = tmp / "routinen.json"
        BERICHTE_DIR = tmp / "berichte"

        # --- Formriegel: beide Seiten ---
        problem, text = schritt_aktion("boese; rm -rf")
        pruefe(problem and "Unzulaessiger" in text,
               "Aktionsname mit Shell-Material wird abgewiesen", text[:50])
        pruefe(not rufe, "und erreicht den Server NICHT", str(rufe))
        problem, text = schritt_aktion("status", "a b")
        pruefe(problem and "Unzulaessiges" in text and not rufe,
               "Argument mit Leerzeichen ebenso")
        problem, text = schritt_aktion("bericht_lesen", "selbsttests.md")
        pruefe(not problem and rufe,
               "gueltiger Schritt geht durch (Punkt im Argument erlaubt)")

        # --- Problem-Erkennung: beide Seiten ---
        rufe.clear()
        problem, _ = schritt_aktion("status")
        pruefe(problem is False, "saubere Ausgabe ist kein Problem")
        problem, _ = schritt_aktion("kaputt")
        pruefe(problem is True, "ok=False wird zum Problem")
        pruefe(any(m for m in PROBLEM_MARKER if m in "FUND x"),
               "Marker-Erkennung vorhanden")
        pruefe("HINWEIS" not in PROBLEM_MARKER,
               "HINWEIS ist bewusst KEIN Problem (kamera-plist-Falle)")

        # --- Ganze Routine: leise bei gruen, laut bei Problemen ---
        ROUTINEN_DATEI.write_text(json.dumps({"routinen": [
            {"name": "probe_gruen", "schritte": [{"aktion": "status"}],
             "einordnen": True, "melden": "leise+laut"},
            {"name": "probe_rot", "schritte": [{"aktion": "kaputt"}],
             "einordnen": False, "melden": "leise+laut"},
            {"name": "probe_rot_leise", "schritte": [{"aktion": "kaputt"}],
             "einordnen": False, "melden": "leise"},
        ]}), encoding="utf-8")
        rc = routine_fahren("probe_gruen")
        pruefe(rc == 0 and not benachr_rufe,
               "gruener Lauf: Exit 0, KEINE Benachrichtigung")
        pruefe((BERICHTE_DIR / "routine_probe_gruen.md").exists(),
               "Bericht liegt trotzdem da (leise heisst nicht stumm)")
        inhalt = (BERICHTE_DIR / "routine_probe_gruen.md").read_text(encoding="utf-8")
        pruefe("alles gruen" in inhalt, "Einordnung steht im Bericht")
        rc = routine_fahren("probe_rot")
        pruefe(rc == 1 and len(benachr_rufe) == 1,
               "roter Lauf: Exit 1 und GENAU EINE Benachrichtigung",
               str(benachr_rufe))
        rc = routine_fahren("probe_rot_leise")
        pruefe(rc == 1 and len(benachr_rufe) == 1,
               "melden=leise unterdrueckt die Benachrichtigung auch bei rot")
        rc = routine_fahren("gibtsnicht")
        pruefe(rc == 2, "unbekannte Routine: Exit 2, kein Lauf")

        # --- Der Import, den der Doppelgaenger sonst versteckt ---
        # Der erste Echtlauf scheiterte an einem falschen Importnamen
        # (HA_ADRESSE statt STANDARD_ADRESSE) - der Doppelgaenger
        # ersetzt benachrichtigen() komplett, also muss der Import
        # separat bewiesen werden. Kein Senden, nur Namensaufloesung.
        sys.path.insert(0, str(BASIS / "harness"))
        try:
            from ha_diagnose import token_laden, STANDARD_ADRESSE  # noqa: F401
            import_ok = True
        except ImportError:
            import_ok = False
        pruefe(import_ok, "benachrichtigen() findet seine ha_diagnose-Namen")

        # --- HTTP 400 mit Befund im Body ist ein Befund ---
        # Getestet wird die ECHTE _post (der Handler lebt dort), mit
        # gefaktem urlopen - die erste Testfassung ersetzte _post selbst
        # und prueft damit gar nichts.
        _post = echte_post
        echtes_urlopen = urllib.request.urlopen

        def urlopen_400(anfrage, timeout=0):
            raise urllib.error.HTTPError(
                "http://doppelgaenger/start", 400, "Bad Request", {},
                __import__("io").BytesIO(
                    b'{"ok": false, "ausgabe": "Ergebnis: 1 Befund(e)."}'))
        urllib.request.urlopen = urlopen_400
        try:
            problem, text = schritt_aktion("doppelablage_pruefen")
        finally:
            urllib.request.urlopen = echtes_urlopen
            _post = post_doppelgaenger
        pruefe(problem and "Befund" in text,
               "HTTP 400 mit Body wird als Befund gelesen, nicht als "
               "Verbindungsfehler", text[:60])

        # --- git_stand liest nur ---
        vorher = subprocess.run(["git", "-C", str(BASIS), "status",
                                 "--porcelain"], capture_output=True,
                                text=True).stdout
        problem, text = schritt_git_stand()
        nachher = subprocess.run(["git", "-C", str(BASIS), "status",
                                  "--porcelain"], capture_output=True,
                                 text=True).stdout
        pruefe(vorher == nachher, "git_stand veraendert nichts")
        pruefe(not problem and "Letzter Commit" in text,
               "git_stand liefert Stand samt letztem Commit")

        # --- Die ECHTE routinen.json (wenn vorhanden) ist wohlgeformt ---
        if echte_routinen.exists():
            daten = json.loads(echte_routinen.read_text(encoding="utf-8"))
            for r in daten.get("routinen", []):
                for s in r.get("schritte", []):
                    ok = (s.get("eingebaut") == "git_stand" or
                          SICHERES_WORT.match(str(s.get("aktion", ""))))
                    pruefe(bool(ok), f"Routine {r.get('name')}: Schritt wohlgeformt",
                           str(s))
    finally:
        _post, benachrichtigen = echte_post, echte_benachr
        ROUTINEN_DATEI, BERICHTE_DIR = echte_routinen, echte_berichte
    print(f"\n{'ROT: ' + str(fehler[0]) + ' Fehler' if fehler[0] else 'Alle Pruefungen gruen.'}")
    return 1 if fehler[0] else 0


if __name__ == "__main__":
    if "--selbsttest" in sys.argv:
        sys.exit(_selbsttest())
    if "--liste" in sys.argv:
        daten = json.loads(ROUTINEN_DATEI.read_text(encoding="utf-8")) \
            if ROUTINEN_DATEI.exists() else {"routinen": []}
        for r in daten.get("routinen", []):
            print(f"  {r['name']}: {r.get('beschreibung', '')}")
        sys.exit(0)
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    sys.exit(routine_fahren(sys.argv[1]))
