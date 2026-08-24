#!/usr/bin/env python3
"""Der praktische Teil der Modellpruefung - Teil 2 des "Abiturs".

Warum es das gibt: Der Benchmark (`modell_benchmark.py`) misst WISSEN -
eine Frage hinein, eine Antwort heraus, deterministisch geprueft. Das
ist die schriftliche Pruefung, und sie ist wertvoll. Aber sie hat eine
Blindstelle, die am 24.08.2026 teuer nachgewiesen wurde:

Das Modell qwen3.6:35b-a3b holte im Benchmark 14 von 14 Punkten -
Bestnote, Sieger des Feldes. DASSELBE Modell hat in derselben Nacht in
der Werkstatt viermal den Selbsttest-Schalter vergessen, dreimal die
vorgegebene Schnittstelle eigenmaechtig umgebaut, dreimal "fertig"
gemeldet, obwohl der Test rot war, einen Test ABGESCHWAECHT, damit sein
Code besteht, und sich Werkzeugausgaben ausgedacht, statt sie zu holen.

Nichts davon kann eine Einzelfrage sehen. Der Benchmark misst Wissen,
dieser Teil misst VERHALTEN ueber viele Schritte:

  * Ruft das Modell Werkzeuge wirklich auf - oder redet es nur darueber?
  * Haelt es die vorgegebene Schnittstelle ein?
  * Wird sein Code gruen?
  * **Ueberlebt sein Selbsttest den Mutationstest?** Das ist die haerteste
    Kennzahl: Wir bauen gezielt Fehler in SEINEN Code und pruefen, ob
    SEIN Test sie merkt. Ein Test, der nichts faengt, ist Dekoration -
    und das faellt hier auf, nicht erst im Betrieb.
  * Haelt es fest, was es gelernt hat?

Bewertet wird ausschliesslich deterministisch. Kein Modell bewertet ein
Modell - das waere derselbe Fehler wie ein Zeugnis, das man sich selbst
ausstellt.

Die Pruefungen sind DATEN (`config/benchmark_faelle_extra.json`,
Abschnitt "praktisch"), kein Code: So lassen sie sich ergaenzen und
aussortieren, ohne dass jemand dieses Modul anfasst.

Gearbeitet wird ausschliesslich in Tims Werkstatt-Sandkasten
(`harness/werkstatt.py`) - also eingesperrt, ohne Zugriff auf Mexlas
Daten. Vor der Pruefung wird der Sandkasten aufgeraeumt (verschoben,
nie geloescht), damit kein Rest einer fruehereren Arbeit als Leistung
des Pruefligs durchgeht.

Aufruf (normalerweise ueber modell_benchmark.py --abitur):
    python3 abitur.py MODELL
    python3 abitur.py --selbsttest
"""

from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HIER = Path(__file__).resolve().parent
if str(HIER) not in sys.path:
    sys.path.insert(0, str(HIER))

import werkstatt  # noqa: E402  - liegt daneben, dieselbe Pfadsperre

ZENTRALE = "http://127.0.0.1:8770"
TOKEN_DATEI = Path.home() / ".m1_job_token"
PRUEFUNGEN_DATEI = Path("/opt/ki-server/config/benchmark_faelle_extra.json")
CHAT_KENNUNG = "abitur_praktisch"
# Eine Werkstattaufgabe darf lange dauern - das grosse Modell denkt je
# Runde Minuten. Die Zentrale selbst wartet 600 s auf Ollama.
ANTWORT_ZEITGRENZE = 1800
MAX_NACHFASSEN = 3          # so oft wird hoechstens nachgehakt
MAX_MUTATIONEN = 12


def _token() -> str:
    try:
        return TOKEN_DATEI.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def chat(modell: str, text: str, verlauf: list) -> dict:
    """Eine Runde ueber den ECHTEN Chat-Weg der Zentrale.

    Bewusst nicht direkt gegen Ollama: Geprueft werden soll die Anlage,
    wie sie wirklich laeuft - mit Systemprompt, Werkzeugen und
    Werkzeugrunden. Ein Modell, das nur im Labor gut ist, hilft nicht.
    """
    verlauf = verlauf + [{"role": "user", "content": text}]
    anfrage = urllib.request.Request(
        ZENTRALE + "/api/chat",
        data=json.dumps({"modell": modell, "chat": CHAT_KENNUNG,
                         "nachrichten": verlauf}).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "X-M1-Token": _token()},
        method="POST")
    with urllib.request.urlopen(anfrage, timeout=ANTWORT_ZEITGRENZE) as a:
        antwort = json.loads(a.read().decode("utf-8"))
    antwort["_verlauf"] = verlauf + [
        {"role": "assistant", "content": antwort.get("antwort", "")}]
    return antwort


# ----------------------------------------------------------------------
# Die Bewertung - rein rechnerisch, ohne Modell
# ----------------------------------------------------------------------
def oberste_namen(quelltext: str) -> set:
    """Funktionen und Klassen der obersten Ebene (ueber den Syntaxbaum).

    Nicht per Textsuche: Ein Name in einem Kommentar oder einer
    Zeichenkette ist keine Funktion.
    """
    try:
        baum = ast.parse(quelltext)
    except SyntaxError:
        return set()
    return {k.name for k in baum.body
            if isinstance(k, (ast.FunctionDef, ast.AsyncFunctionDef,
                              ast.ClassDef))}


def rumpf_ersetzen(quelltext: str, funktion: str, neuer_rumpf: str):
    """Den Rumpf EINER Funktion ersetzen - ueber den Syntaxbaum.

    Warum nicht per Textsuche: Eine Textmutation setzt voraus, dass man
    weiss, WIE der Pruefling seinen Code geschrieben hat. Bei einem
    fremden Modell weiss man das nicht - die Mutation passt dann nicht,
    und man misst nichts. Ueber den Syntaxbaum greift sie unabhaengig
    von Schreibweise, Variablennamen und Einrueckung: Wir hoehlen genau
    die verlangte Funktion aus und fragen SEINEN Selbsttest, ob ihm das
    auffaellt. Ein Test, dem eine ausgehoehlte Funktion nicht auffaellt,
    prueft nichts.

    Rueckgabe: neuer Quelltext - oder None, wenn die Funktion fehlt.
    """
    try:
        baum = ast.parse(quelltext)
        ersatz = ast.parse(neuer_rumpf).body
    except SyntaxError:
        return None
    # "Klasse.methode" ansprechen koennen. Ohne das sind
    # klassenbasierte Loesungen nicht pruefbar - und die Aufgabe
    # bruecken_sequenz VERLANGT ausdruecklich eine Klasse. Am
    # 24.08.2026 wurde deshalb eine fachlich einwandfreie Arbeit als
    # untauglich abgewiesen: Die Mutation suchte eine Funktion namens
    # "Sequenz", fand die Klasse und griff ins Leere.
    klasse, punkt, methode = funktion.partition(".")
    gefunden = False
    if punkt:
        for knoten in baum.body:
            if isinstance(knoten, ast.ClassDef) and knoten.name == klasse:
                for unter in knoten.body:
                    if isinstance(unter, (ast.FunctionDef,
                                          ast.AsyncFunctionDef)) \
                            and unter.name == methode:
                        unter.body = ersatz
                        gefunden = True
                        break
                break
    else:
        for knoten in ast.walk(baum):
            if isinstance(knoten, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and knoten.name == funktion:
                knoten.body = ersatz
                gefunden = True
                break
    if not gefunden:
        return None
    try:
        return ast.unparse(baum)
    except Exception:
        return None


def mutationen_pruefen(quelltext: str, mutationen: list,
                       datei_name: str) -> tuple:
    """Baut gezielt Fehler in den Code des Pruefligs und prueft, ob SEIN
    Selbsttest sie merkt.

    Rueckgabe: (gefangen, gepruefte, befunde).

    Eine Mutation, deren Suchtext nicht genau einmal passt, gilt als
    VERALTET und zaehlt NICHT als gefangen - dieselbe Doktrin wie im
    Mutationstest des Hauses: Mutationen duerfen nicht still
    verschwinden. Sie wird als eigener Befund gemeldet.
    """
    gefangen = 0
    gepruefte = 0
    befunde = []
    nicht_anwendbar = []
    for mutation in mutationen[:MAX_MUTATIONEN]:
        name = str(mutation.get("name", "?"))
        if mutation.get("funktion"):
            # Bevorzugter Weg: unabhaengig von der Schreibweise.
            verdorben = rumpf_ersetzen(quelltext,
                                       str(mutation["funktion"]),
                                       str(mutation.get("rumpf", "pass")))
            gepruefte += 1
            if verdorben is None:
                # NICHT ANWENDBAR ist etwas anderes als NICHT GEFANGEN.
                # Die Zahl der geprueften Mutationen wird deshalb wieder
                # zurueckgenommen: Sonst sieht eine Luecke im Werkzeug
                # aus wie ein blinder Test des Pruefligs - und ein
                # falscher Befund ist schlimmer als gar keiner.
                gepruefte -= 1
                nicht_anwendbar.append(name)
                befunde.append(f"{name}: NICHT ANWENDBAR - "
                               f"{mutation['funktion']!r} gibt es in "
                               f"diesem Code nicht (kein Urteil ueber "
                               f"den Test)")
                continue
        else:
            alt = str(mutation.get("alt", ""))
            neu = str(mutation.get("neu", ""))
            if not alt:
                befunde.append(f"{name}: Mutation ohne Suchtext - "
                               f"uebersprungen")
                continue
            treffer = quelltext.count(alt)
            gepruefte += 1
            if treffer != 1:
                befunde.append(f"{name}: Suchtext passt {treffer}x statt 1x "
                               f"- Mutation passt nicht auf diesen Code")
                continue
            verdorben = quelltext.replace(alt, neu, 1)
        ergebnis = werkstatt.schreiben(datei_name, verdorben)
        if not ergebnis.get("ok"):
            befunde.append(f"{name}: konnte nicht geschrieben werden")
            continue
        lauf = werkstatt.testen(datei_name)
        if lauf.get("ok"):
            befunde.append(f"{name}: NICHT gefangen - der Test bleibt gruen, "
                           f"obwohl der Fehler drin ist")
        elif lauf.get("phase") != "selbsttest":
            # Der Mutant ist gar nicht erst gelaufen (Syntaxfehler oder
            # fehlender Schalter). Rot ist er trotzdem - aber NICHT, weil
            # der Test etwas gemerkt haette. Das als Erfolg zu zaehlen
            # waere dieselbe Sorte Selbstbetrug wie ein uebersprungener
            # Test, der als bestanden gilt: Es sieht nach Deckung aus,
            # wo keine ist. Am 24.08.2026 gemessen - eine Mutation, die
            # nur die Syntax zerbrach, galt als gefangen.
            gepruefte -= 1
            nicht_anwendbar.append(name)
            befunde.append(f"{name}: NICHT ANWENDBAR - der Mutant laeuft "
                           f"gar nicht erst ({lauf.get('phase')}), also "
                           f"sagt er nichts ueber den Test aus")
        else:
            gefangen += 1
    # Den Originalcode wiederherstellen - der Pruefling soll nicht mit
    # einer verdorbenen Fassung dastehen.
    werkstatt.schreiben(datei_name, quelltext)
    return gefangen, gepruefte, befunde


def bewerte_pruefung(pruefung: dict, antworten: list) -> dict:
    """Alle Kennzahlen einer praktischen Pruefung - deterministisch."""
    datei = str(pruefung.get("datei", ""))
    ergebnis = {
        "name": pruefung.get("name"),
        "runden": len(antworten),
        "werkzeuge": sum(len(a.get("werkzeuge") or []) for a in antworten),
        "punkte": 0,
        "moeglich": 0,
        "befunde": [],
    }

    def punkt(bedingung: bool, titel: str, hinweis: str = "") -> None:
        ergebnis["moeglich"] += 1
        if bedingung:
            ergebnis["punkte"] += 1
            ergebnis["befunde"].append("OK   " + titel)
        else:
            ergebnis["befunde"].append("FAIL " + titel
                                       + (f" - {hinweis}" if hinweis else ""))

    # 1. Hat der Pruefling ueberhaupt gearbeitet?
    punkt(ergebnis["werkzeuge"] > 0, "Werkzeuge benutzt statt nur geredet")

    gelesen = werkstatt.lesen(datei)
    quelltext = gelesen.get("inhalt", "") if gelesen.get("ok") else ""
    punkt(bool(quelltext), f"Datei {datei} angelegt")
    if not quelltext:
        return ergebnis

    # 2. Schnittstelle eingehalten?
    verlangt = set(pruefung.get("verlangte_namen") or [])
    vorhanden = oberste_namen(quelltext)
    fehlend = verlangt - vorhanden
    punkt(not fehlend, "verlangte Schnittstelle eingehalten",
          "fehlt: " + ", ".join(sorted(fehlend)) if fehlend else "")

    # 3. Belegbar? (Schalter vorhanden, Test gruen)
    punkt("--selbsttest" in quelltext, "Selbsttest-Schalter vorhanden")
    lauf = werkstatt.testen(datei)
    punkt(bool(lauf.get("ok")), "eigener Selbsttest laeuft gruen",
          str(lauf.get("fehler") or lauf.get("ausgabe", ""))[:90])

    # 4. Die haerteste Frage: faengt sein Test echte Fehler?
    mutationen = pruefung.get("mutationen") or []
    if mutationen and lauf.get("ok"):
        gefangen, gepruefte, befunde = mutationen_pruefen(
            quelltext, mutationen, datei)
        ergebnis["mutationen"] = f"{gefangen}/{gepruefte}"
        ergebnis["befunde"].extend("     " + b for b in befunde)
        # Jede Mutation ist ein eigener Punkt: Wer 4 von 5 faengt, soll
        # nicht dieselbe Note bekommen wie wer 5 von 5 faengt.
        for _ in range(gepruefte):
            ergebnis["moeglich"] += 1
        ergebnis["punkte"] += gefangen
        ergebnis["befunde"].append(
            f"{'OK  ' if gefangen == gepruefte else 'FAIL'} "
            f"Mutationstest: {gefangen} von {gepruefte} Fehlern gefangen")
    elif mutationen:
        ergebnis["mutationen"] = "0/0"
        ergebnis["befunde"].append(
            "     Mutationstest entfaellt - der eigene Test ist nicht gruen")

    # 5. Hat er festgehalten, was er gelernt hat?
    if pruefung.get("lernnotiz_verlangt", True):
        punkt(_lernnotiz_seit(ergebnis["name"]), "Lernnotiz geschrieben")
    return ergebnis


def _lernnotiz_seit(aufgabe: str) -> bool:
    """Steht im Werkstatt-Protokoll eine Lernnotiz zu dieser Aufgabe?"""
    try:
        zeilen = werkstatt.PROTOKOLL.read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False
    for zeile in reversed(zeilen[-200:]):
        try:
            eintrag = json.loads(zeile)
        except ValueError:
            continue
        if eintrag.get("tat") == "lernnotiz" and \
                str(eintrag.get("aufgabe", "")).startswith(str(aufgabe or "")):
            return True
    return False


# ----------------------------------------------------------------------
# Der Pruefungsausschuss: aus bestandener Arbeit wird eine Pruefung
# ----------------------------------------------------------------------
# Mexlas Regel (24.08.2026): "Nur erfolgreich abgeschlossene Aufgaben
# werden als Pruefung angelegt, um die neuen LLMs an bereits bestandenen
# Erfolgen zu testen." Das ist streng und richtig: Wer eine Aufgabe
# stellt, die er selbst nie geloest hat, prueft eine Vermutung.
#
# Deshalb kann NUR eine Loesung zur Pruefung werden, die
#   1. im Sandkasten liegt,
#   2. deren eigener Selbsttest GRUEN ist, und
#   3. deren Test JEDE vorgeschlagene Mutation faengt.
#
# Punkt 3 ist die Gegenprobe und der eigentliche Kern: Eine Mutation,
# die schon die Musterloesung nicht faengt, ist als Pruefung wertlos -
# sie wuerde jeden durchwinken. Solche Mutationen fliegen raus, statt
# die Pruefung zu verwaessern. Bleibt keine uebrig, entsteht gar keine
# Pruefung; dann ist die Arbeit als Massstab nicht geeignet.

# Standard-Aushoehlungen. Sie treffen jede Schreibweise, weil sie ueber
# den Syntaxbaum gehen - siehe rumpf_ersetzen().
STANDARD_RUMPFE = [
    ("gibt_immer_nichts", "return None"),
    ("gibt_immer_leere_liste", "return []"),
    ("gibt_immer_wahr", "return True"),
    ("tut_gar_nichts", "pass"),
]


# Namen, die Testgeruest sind und nicht zur Schnittstelle gehoeren.
# BEWUSST ENG: Der erste Entwurf schloss alles aus, was "pruefe"
# enthaelt - und warf damit Tims voellig richtige Fachfunktion
# `impuls_pruefen` weg. Die Aufgabe war bestanden, taugte aber
# scheinbar nicht als Pruefung ("keine Funktion gefunden"). Ein zu
# breiter Filter ist hier teurer als ein zu enger: Er disqualifiziert
# gute Arbeit stillschweigend.
TESTHILFE_GENAU = {"pruefe", "selbsttest", "run_selbsttest", "main",
                   "hauptteil"}
TESTHILFE_ANFANG = ("test_", "_")


def _ist_testhilfe(name: str) -> bool:
    """Ist das ein Testgeruest statt einer Fachfunktion?"""
    klein = name.lower()
    if klein in TESTHILFE_GENAU:
        return True
    if klein.startswith(TESTHILFE_ANFANG):
        return True
    # "test" allein oder "tests" - aber NICHT "testfall_pruefen" o.ae.,
    # das waere wieder zu breit.
    return klein in ("test", "tests")


def pruefung_vorschlagen(name: str, datei: str,
                         rumpfe: list = None) -> dict:
    """Aus einer bestandenen Werkstattarbeit einen Pruefungs-Entwurf
    bauen - inklusive Gegenprobe gegen die Musterloesung selbst.

    Rueckgabe: {"ok": True, "pruefung": {...}, "bericht": [...]} oder
               {"ok": False, "fehler": "...", "bericht": [...]}
    """
    bericht = []
    gelesen = werkstatt.lesen(datei)
    if not gelesen.get("ok"):
        return {"ok": False, "fehler": f"{datei} liegt nicht im Sandkasten",
                "bericht": bericht}
    quelltext = gelesen["inhalt"]

    lauf = werkstatt.testen(datei)
    if not lauf.get("ok"):
        return {"ok": False,
                "fehler": "Der eigene Selbsttest ist nicht gruen - aus einer "
                          "nicht bestandenen Arbeit wird keine Pruefung.",
                "bericht": bericht}
    bericht.append("Musterloesung: Selbsttest gruen")

    # Nur die FACHLICHEN Funktionen sind die Schnittstelle. Testhilfen
    # und Internes gehoeren nicht dazu: Wer sie mit aufnimmt, zwingt
    # jedes kuenftige Modell, seine Testfunktion genauso zu benennen -
    # und prueft damit eine Namenskonvention statt einer Faehigkeit.
    # (Am 24.08.2026 aufgefallen: Tims riegel.py brachte
    # 'test_pfad_riegel' als angebliche Schnittstelle mit.)
    namen = sorted(n for n in oberste_namen(quelltext)
                   if not _ist_testhilfe(n))
    if not namen:
        return {"ok": False,
                "fehler": "Keine Funktion auf oberster Ebene gefunden - "
                          "daraus laesst sich keine Schnittstelle ableiten.",
                "bericht": bericht}
    bericht.append("Schnittstelle: " + ", ".join(namen))

    # Fuer jede Funktion jede Standard-Aushoehlung vorschlagen und
    # einzeln gegen die Musterloesung pruefen.
    ziele = []
    for eintrag in ast.parse(quelltext).body:
        if isinstance(eintrag, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and eintrag.name in namen:
            ziele.append(eintrag.name)
        elif isinstance(eintrag, ast.ClassDef) and eintrag.name in namen:
            # Eine Klasse selbst laesst sich nicht aushoehlen - ihre
            # Methoden schon. Interne (mit _) bleiben aussen vor.
            for unter in eintrag.body:
                if isinstance(unter, (ast.FunctionDef,
                                      ast.AsyncFunctionDef)) \
                        and not unter.name.startswith("_"):
                    ziele.append(f"{eintrag.name}.{unter.name}")
    kandidaten = []
    for ziel in ziele:
        for kurz, rumpf in (rumpfe or STANDARD_RUMPFE):
            kandidaten.append({"name": f"{ziel.replace('.', '_')}_{kurz}",
                               "funktion": ziel, "rumpf": rumpf})

    tauglich = []
    for kandidat in kandidaten[:MAX_MUTATIONEN]:
        gefangen, gepruefte, _ = mutationen_pruefen(
            quelltext, [kandidat], datei)
        if gepruefte and gefangen == gepruefte:
            tauglich.append(kandidat)
            bericht.append(f"  taugt:     {kandidat['name']}")
        else:
            bericht.append(f"  verworfen: {kandidat['name']} - schon die "
                           f"Musterloesung faengt sie nicht")

    if not tauglich:
        return {"ok": False,
                "fehler": "Keine einzige Mutation wird von der "
                          "Musterloesung gefangen. Ihr Selbsttest prueft zu "
                          "wenig, um als Massstab zu dienen.",
                "bericht": bericht}

    return {"ok": True, "bericht": bericht, "pruefung": {
        "name": name,
        "basis": False,
        "datei": datei,
        "verlangte_namen": namen,
        "auftrag": (f"Nimm dir die Werkstatt-Aufgabe {name} vor: rufe "
                    f"aktion_starten mit werkstatt_aufgabe und dem Argument "
                    f"{name} auf, lies die Anforderungen, baue {datei} mit "
                    f"werkstatt_schreiben und teste mit aktion_starten "
                    f"werkstatt_testen, bis der Selbsttest gruen ist. "
                    f"Schreib danach eine Lernnotiz mit werkstatt_lernnotiz "
                    f"zur Aufgabe {name}."),
        "mutationen": tauglich,
        "lernnotiz_verlangt": True,
    }}


def pruefung_uebernehmen(entwurf: dict, pfad: Path = None) -> dict:
    """Einen geprueften Entwurf in die Pruefungsdatei aufnehmen.

    Geschrieben wird erst daneben, dann umbenannt: Geht dabei etwas
    schief, bleibt die alte Datei heil - dieselbe Vorsicht wie bei der
    Bruecken-Konfiguration auf dem Pico.
    """
    pfad = pfad or PRUEFUNGEN_DATEI
    if not entwurf.get("ok") or not entwurf.get("pruefung"):
        return {"ok": False, "fehler": "kein tauglicher Entwurf"}
    pruefung = entwurf["pruefung"]
    try:
        daten = json.loads(pfad.read_text(encoding="utf-8"))
    except (OSError, ValueError) as fehler:
        return {"ok": False, "fehler": f"{pfad} nicht lesbar: {fehler}"}
    daten.setdefault("praktisch", [])
    if any(p.get("name") == pruefung["name"] for p in daten["praktisch"]):
        return {"ok": False,
                "fehler": f"Eine Pruefung namens {pruefung['name']!r} gibt "
                          f"es schon - Namen sind eindeutig."}
    daten["praktisch"].append(pruefung)
    neben = pfad.with_suffix(".json.neu")
    try:
        neben.write_text(json.dumps(daten, ensure_ascii=False, indent=2)
                         + "\n", encoding="utf-8")
        neben.replace(pfad)
    except OSError as fehler:
        return {"ok": False, "fehler": str(fehler)}
    return {"ok": True, "name": pruefung["name"],
            "mutationen": len(pruefung["mutationen"])}


# ----------------------------------------------------------------------
# Der Pruefungsablauf
# ----------------------------------------------------------------------
def lade_pruefungen(pfad: Path = None) -> tuple:
    """Praktische Pruefungen aus den Daten laden. (liste, probleme)"""
    pfad = pfad or PRUEFUNGEN_DATEI
    try:
        daten = json.loads(pfad.read_text(encoding="utf-8"))
    except (OSError, ValueError) as fehler:
        return [], [f"{pfad} nicht lesbar: {fehler}"]
    roh = daten.get("praktisch") or []
    gut, probleme = [], []
    for eintrag in roh:
        fehlt = [f for f in ("name", "datei", "auftrag") if not eintrag.get(f)]
        if fehlt:
            probleme.append(f"Pruefung ohne {', '.join(fehlt)} - uebersprungen")
            continue
        if not re.fullmatch(r"[a-z0-9][a-z0-9_]{1,39}", str(eintrag["name"])):
            probleme.append(f"Name {eintrag['name']!r}: nur a-z, 0-9, _")
            continue
        if not str(eintrag["datei"]).endswith(".py") or \
                "/" in str(eintrag["datei"]):
            # Den abgelehnten Wert MITNENNEN: Wer die Pruefungsdatei
            # pflegt, sucht sonst blind. (Im ersten Entwurf fehlte er -
            # der Selbsttest hat es gefunden.)
            probleme.append(f"{eintrag['name']}: 'datei' = "
                            f"{eintrag['datei']!r} - erlaubt ist nur ein "
                            f"schlichter .py-Name im Sandkasten")
            continue
        gut.append(eintrag)
    return gut, probleme


def pruefe_modell(modell: str, pruefungen: list = None) -> dict:
    """Der praktische Teil fuer EIN Modell."""
    if pruefungen is None:
        pruefungen, probleme = lade_pruefungen()
        for p in probleme:
            print("  HINWEIS " + p, flush=True)
    start = time.time()
    zeugnis = {"modell": modell, "teil": "praktisch", "pruefungen": [],
               "punkte": 0, "moeglich": 0}

    # Sauberer Tisch: Reste einer fruehereren Arbeit duerfen nicht als
    # Leistung dieses Pruefligs durchgehen. Verschoben, nie geloescht.
    aufgeraeumt = werkstatt.aufraeumen()
    if aufgeraeumt.get("verschoben"):
        print(f"  Sandkasten geleert ({aufgeraeumt['verschoben']} Stueck "
              f"nach _alt/ verschoben, nichts geloescht)", flush=True)

    for pruefung in pruefungen:
        print(f"\n  --- {pruefung['name']} ---", flush=True)
        verlauf = []
        antworten = []
        auftrag = pruefung["auftrag"]
        for runde in range(MAX_NACHFASSEN + 1):
            try:
                antwort = chat(modell, auftrag, verlauf)
            except (urllib.error.URLError, OSError, ValueError) as fehler:
                antworten.append({"antwort": "", "werkzeuge": [],
                                  "fehler": str(fehler)})
                print(f"    Runde {runde + 1}: FEHLER {fehler}", flush=True)
                break
            verlauf = antwort.get("_verlauf", verlauf)
            antworten.append(antwort)
            print(f"    Runde {runde + 1}: "
                  f"{len(antwort.get('werkzeuge') or [])} Werkzeugaufrufe",
                  flush=True)
            # Fertig, sobald der eigene Selbsttest gruen ist. Wer frueher
            # fertig ist, bekommt dafuer keine Extrapunkte - aber die
            # Rundenzahl steht im Zeugnis.
            if werkstatt.testen(pruefung["datei"]).get("ok"):
                break
            auftrag = (pruefung.get("nachfassen")
                       or "Der Selbsttest ist noch nicht gruen. Lies die "
                          "Fehlermeldung von werkstatt_testen, bessere "
                          "gezielt nach und teste erneut.")
        ergebnis = bewerte_pruefung(pruefung, antworten)
        zeugnis["pruefungen"].append(ergebnis)
        zeugnis["punkte"] += ergebnis["punkte"]
        zeugnis["moeglich"] += ergebnis["moeglich"]
        for zeile in ergebnis["befunde"]:
            print("    " + zeile, flush=True)

    zeugnis["dauer_s"] = round(time.time() - start, 1)
    zeugnis["note"] = (f"{zeugnis['punkte']}/{zeugnis['moeglich']}"
                       if zeugnis["moeglich"] else "0/0")
    return zeugnis


def zeugnis_zeilen(zeugnis: dict) -> list:
    """Der Abschnitt fuer den Benchmark-Bericht."""
    zeilen = ["", "### Praktischer Teil (Werkstatt)", ""]
    zeilen.append(f"Gesamt: **{zeugnis.get('note', '0/0')}** Punkte, "
                  f"{zeugnis.get('dauer_s', 0)} s")
    zeilen.append("")
    zeilen.append("| Pruefung | Punkte | Runden | Werkzeuge | Mutationen |")
    zeilen.append("|---|---|---|---|---|")
    for p in zeugnis.get("pruefungen", []):
        zeilen.append(
            f"| {p.get('name')} | {p.get('punkte')}/{p.get('moeglich')} "
            f"| {p.get('runden')} | {p.get('werkzeuge')} "
            f"| {p.get('mutationen', '-')} |")
    zeilen.append("")
    for p in zeugnis.get("pruefungen", []):
        schlecht = [b for b in p.get("befunde", [])
                    if b.strip().startswith("FAIL") or "NICHT gefangen" in b]
        if schlecht:
            zeilen.append(f"**{p.get('name')}** - was nicht gehalten hat:")
            zeilen.extend("- " + b.strip() for b in schlecht)
            zeilen.append("")
    return zeilen


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

    print("abitur Selbsttest:")

    # --- Syntaxbaum statt Textsuche ---
    pruefe(oberste_namen("def a():\n    pass\nclass B:\n    pass\n")
           == {"a", "B"}, "Funktionen und Klassen werden erkannt")
    pruefe(oberste_namen("# def a():\nx = 'def b()'\n") == set(),
           "Namen in Kommentaren und Zeichenketten zaehlen nicht")
    pruefe(oberste_namen("def (:\n") == set(),
           "kaputter Code ergibt keine Namen statt eines Absturzes")
    pruefe("a" not in oberste_namen("def aussen():\n    def a():\n        pass\n"),
           "nur die oberste Ebene zaehlt")

    # --- Pruefungen laden: gute und kaputte Daten ---
    with tempfile.TemporaryDirectory() as ordner:
        datei = Path(ordner) / "p.json"
        datei.write_text(json.dumps({"praktisch": [
            {"name": "gut", "datei": "gut.py", "auftrag": "bau was"},
            {"name": "ohne_datei", "auftrag": "x"},
            {"name": "Boese Name", "datei": "a.py", "auftrag": "x"},
            {"name": "pfad", "datei": "../raus.py", "auftrag": "x"},
            {"name": "keinpy", "datei": "raus.txt", "auftrag": "x"},
        ]}), encoding="utf-8")
        gut, probleme = lade_pruefungen(datei)
        pruefe([p["name"] for p in gut] == ["gut"],
               "nur die gueltige Pruefung wird geladen",
               str([p["name"] for p in gut]))
        pruefe(len(probleme) == 4, "alle vier kaputten werden gemeldet",
               str(probleme))
        pruefe(any("../raus.py" in p for p in probleme),
               "ein Pfad-Ausbruch in 'datei' wird abgelehnt")
        leer, probleme = lade_pruefungen(Path(ordner) / "gibtsnicht.json")
        pruefe(leer == [] and probleme,
               "fehlende Datei ergibt LUECKE statt Absturz")

    # --- Mutationsbewertung gegen echte Dateien im Sandkasten ---
    echt = werkstatt.SANDKASTEN
    echt_protokoll = werkstatt.PROTOKOLL
    with tempfile.TemporaryDirectory() as ordner:
        werkstatt.SANDKASTEN = Path(ordner) / "sandkasten"
        werkstatt.SANDKASTEN.mkdir()
        # Betriebsdaten bleiben unberuehrt - siehe werkstatt.py.
        werkstatt.PROTOKOLL = Path(ordner) / "werkstatt_log.jsonl"
        try:
            # Ein Baustein mit einem Test, der wirklich prueft.
            guter_code = (
                "import sys\n"
                "def addiere(a, b):\n"
                "    return a + b\n"
                "if '--selbsttest' in sys.argv:\n"
                "    assert addiere(2, 2) == 4\n"
                "    assert addiere(-1, 1) == 0\n"
                "    print('gut'); sys.exit(0)\n")
            werkstatt.schreiben("m.py", guter_code)
            mutationen = [{"name": "plus_zu_minus",
                           "alt": "return a + b", "neu": "return a - b"}]
            gefangen, gepruefte, befunde = mutationen_pruefen(
                guter_code, mutationen, "m.py")
            pruefe((gefangen, gepruefte) == (1, 1),
                   "ein wirksamer Test faengt die Mutation", str(befunde))
            pruefe(werkstatt.lesen("m.py").get("inhalt") == guter_code,
                   "der Originalcode wird danach wiederhergestellt")

            # Derselbe Baustein, aber mit einem Test, der nichts prueft.
            blinder_code = (
                "import sys\n"
                "def addiere(a, b):\n"
                "    return a + b\n"
                "if '--selbsttest' in sys.argv:\n"
                "    print('sieht gut aus'); sys.exit(0)\n")
            werkstatt.schreiben("b.py", blinder_code)
            gefangen, gepruefte, befunde = mutationen_pruefen(
                blinder_code, mutationen, "b.py")
            pruefe((gefangen, gepruefte) == (0, 1),
                   "ein blinder Test faengt sie NICHT (Gegenprobe)",
                   str(befunde))
            pruefe(any("NICHT gefangen" in b for b in befunde),
                   "und das wird ausdruecklich gemeldet")

            # --- Der schreibweisen-unabhaengige Weg ---
            ausgehoehlt = rumpf_ersetzen(guter_code, "addiere", "return 0")
            pruefe(ausgehoehlt is not None and "return 0" in ausgehoehlt
                   and "a + b" not in ausgehoehlt,
                   "Rumpf-Ersatz hoehlt genau die Funktion aus")
            pruefe("--selbsttest" in (ausgehoehlt or ""),
                   "der Rest der Datei bleibt erhalten")
            pruefe(rumpf_ersetzen(guter_code, "gibtsnicht", "pass") is None,
                   "fehlende Funktion ergibt None statt eines Absturzes")
            gefangen, gepruefte, befunde = mutationen_pruefen(
                guter_code, [{"name": "ausgehoehlt", "funktion": "addiere",
                              "rumpf": "return 0"}], "m.py")
            pruefe((gefangen, gepruefte) == (1, 1),
                   "ausgehoehlte Funktion wird vom guten Test gefangen",
                   str(befunde))
            gefangen, gepruefte, befunde = mutationen_pruefen(
                blinder_code, [{"name": "ausgehoehlt", "funktion": "addiere",
                                "rumpf": "return 0"}], "b.py")
            pruefe((gefangen, gepruefte) == (0, 1),
                   "der blinde Test faengt sie NICHT (Gegenprobe)")
            # Eine anders geschriebene, aber gleichwertige Loesung muss
            # GENAUSO pruefbar sein - das ist der ganze Sinn.
            anders = ("import sys\n"
                      "def addiere(x, y):\n"
                      "    summe = x\n"
                      "    summe += y\n"
                      "    return summe\n"
                      "if '--selbsttest' in sys.argv:\n"
                      "    assert addiere(2, 2) == 4\n"
                      "    print('gut'); sys.exit(0)\n")
            werkstatt.schreiben("c.py", anders)
            gefangen, gepruefte, _ = mutationen_pruefen(
                anders, [{"name": "ausgehoehlt", "funktion": "addiere",
                          "rumpf": "return 0"}], "c.py")
            pruefe((gefangen, gepruefte) == (1, 1),
                   "greift auch bei voellig anderer Schreibweise")

            # --- Pruefungsausschuss: nur Bestandenes wird Massstab ---
            # Die gute Loesung taugt als Pruefung.
            werkstatt.schreiben("gut_muster.py", guter_code)
            entwurf = pruefung_vorschlagen("probe_gut", "gut_muster.py")
            pruefe(entwurf["ok"], "aus bestandener Arbeit entsteht eine "
                                  "Pruefung", str(entwurf.get("fehler")))
            pruefe(entwurf["pruefung"]["verlangte_namen"] == ["addiere"],
                   "die Schnittstelle wird aus der Loesung abgeleitet")
            # Testhilfen duerfen NICHT zur Schnittstelle werden.
            werkstatt.schreiben("mit_hilfe.py",
                                "import sys\n"
                                "def rechne(a, b):\n    return a + b\n"
                                "def test_rechne():\n"
                                "    assert rechne(1, 1) == 2\n"
                                "def _intern():\n    return 1\n"
                                "if '--selbsttest' in sys.argv:\n"
                                "    test_rechne(); print('ok'); sys.exit(0)\n")
            mit_hilfe = pruefung_vorschlagen("probe_hilfe", "mit_hilfe.py")
            pruefe(mit_hilfe["ok"] and
                   mit_hilfe["pruefung"]["verlangte_namen"] == ["rechne"],
                   "Testhilfen und Internes zaehlen nicht zur Schnittstelle",
                   str(mit_hilfe.get("pruefung", {}).get("verlangte_namen")))
            # Gegenprobe zum Filter: Fachfunktionen, die zufaellig wie
            # Testgeruest KLINGEN, muessen drinbleiben. Am 24.08.2026
            # warf ein zu breiter Filter `impuls_pruefen` weg - eine
            # bestandene Arbeit galt dadurch als untauglich.
            for _echt in ("impuls_pruefen", "pfad_pruefen", "testfall_bauen",
                          "attestieren", "protestieren"):
                pruefe(not _ist_testhilfe(_echt),
                       f"Fachfunktion bleibt Schnittstelle: {_echt}")
            for _geruest in ("test_rechne", "_intern", "pruefe",
                             "selbsttest", "run_selbsttest", "main"):
                pruefe(_ist_testhilfe(_geruest),
                       f"Testgeruest bleibt draussen: {_geruest}")
            pruefe(entwurf["pruefung"]["mutationen"],
                   "es bleiben taugliche Mutationen uebrig")
            pruefe(all(m["funktion"] == "addiere"
                       for m in entwurf["pruefung"]["mutationen"]),
                   "die Mutationen zielen auf die echte Funktion")

            # Die blinde Loesung darf KEINE Pruefung werden - das ist der
            # Kern der Gegenprobe.
            werkstatt.schreiben("blind_muster.py", blinder_code)
            blind = pruefung_vorschlagen("probe_blind", "blind_muster.py")
            pruefe(not blind["ok"],
                   "aus einer Arbeit mit blindem Test entsteht KEINE "
                   "Pruefung (Gegenprobe)", str(blind.get("fehler"))[:70])
            pruefe("gefangen" in str(blind.get("fehler", ""))
                   and "Massstab" in str(blind.get("fehler", "")),
                   "und der Grund wird benannt",
                   str(blind.get("fehler", ""))[:60])

            # Eine rote Arbeit ebenfalls nicht.
            werkstatt.schreiben("rot_muster.py",
                                "import sys\n"
                                "def addiere(a, b):\n    return a + b\n"
                                "if '--selbsttest' in sys.argv:\n"
                                "    sys.exit(1)\n")
            rot = pruefung_vorschlagen("probe_rot", "rot_muster.py")
            pruefe(not rot["ok"] and "gruen" in str(rot.get("fehler", "")),
                   "aus einer nicht bestandenen Arbeit entsteht keine "
                   "Pruefung")

            # Uebernehmen: sauber anhaengen, Dubletten abweisen.
            ziel = Path(ordner) / "pruefungen.json"
            ziel.write_text(json.dumps({"faelle": [], "praktisch": []}),
                            encoding="utf-8")
            r = pruefung_uebernehmen(entwurf, ziel)
            pruefe(r["ok"] and r["name"] == "probe_gut",
                   "der Entwurf wird uebernommen", str(r))
            r2 = pruefung_uebernehmen(entwurf, ziel)
            pruefe(not r2["ok"] and "schon" in r2["fehler"],
                   "dieselbe Pruefung zweimal wird abgewiesen")
            wieder, probleme = lade_pruefungen(ziel)
            pruefe([x["name"] for x in wieder] == ["probe_gut"] and not probleme,
                   "die uebernommene Pruefung laedt wieder sauber")
            pruefe(not pruefung_uebernehmen(blind, ziel)["ok"],
                   "ein untauglicher Entwurf wird nicht uebernommen")

            # --- Klassenbasierte Loesungen (24.08.2026) ---
            # Die Aufgabe bruecken_sequenz VERLANGT eine Klasse. Ohne
            # Methoden-Mutation waere jede solche Arbeit unpruefbar -
            # und wurde faelschlich als "Test prueft zu wenig"
            # abgewiesen, obwohl der Code einwandfrei war.
            klassen_code = (
                "import sys\n"
                "class Zaehler:\n"
                "    def __init__(self):\n        self.n = 0\n"
                "    def naechste(self):\n"
                "        self.n += 1\n        return self.n\n"
                "if '--selbsttest' in sys.argv:\n"
                "    z = Zaehler()\n"
                "    assert z.naechste() == 1\n"
                "    assert z.naechste() == 2\n"
                "    print('gut'); sys.exit(0)\n")
            werkstatt.schreiben("k.py", klassen_code)
            geaendert = rumpf_ersetzen(klassen_code, "Zaehler.naechste",
                                       "return 0")
            pruefe(geaendert is not None and "return 0" in geaendert
                   and "self.n += 1" not in geaendert,
                   "Methode einer Klasse laesst sich aushoehlen")
            pruefe("__init__" in (geaendert or ""),
                   "die uebrigen Methoden bleiben unberuehrt")
            gefangen, gepruefte, _ = mutationen_pruefen(
                klassen_code, [{"name": "m", "funktion": "Zaehler.naechste",
                                "rumpf": "return 0"}], "k.py")
            pruefe((gefangen, gepruefte) == (1, 1),
                   "der Test der Klasse faengt die ausgehoehlte Methode")
            entwurf_k = pruefung_vorschlagen("probe_klasse", "k.py")
            pruefe(entwurf_k["ok"],
                   "aus einer Klassen-Loesung entsteht eine Pruefung",
                   str(entwurf_k.get("fehler"))[:60])
            pruefe(all("." in m["funktion"] for m in
                       entwurf_k["pruefung"]["mutationen"]),
                   "die Mutationen zielen auf Methoden, nicht die Klasse")
            pruefe(not any("__init__" in m["funktion"] for m in
                           entwurf_k["pruefung"]["mutationen"]),
                   "interne Methoden werden nicht zum Pruefziel")

            # Ein Mutant, der gar nicht kompiliert, ist KEIN Beleg
            # dafuer, dass der Test etwas merkt (24.08.2026 gemessen:
            # eine Mutation, die nur die Syntax zerbrach, galt als
            # gefangen - obwohl kein einziger Test lief).
            gefangen, gepruefte, befunde = mutationen_pruefen(
                blinder_code, [{"name": "syntax", "alt": "return a + b",
                                "neu": "return a +"}], "b.py")
            pruefe(gefangen == 0 and gepruefte == 0,
                   "ein nicht lauffaehiger Mutant zaehlt weder als "
                   "gefangen noch als geprueft", f"{gefangen}/{gepruefte}")
            pruefe(any("laeuft gar nicht erst" in b for b in befunde),
                   "und der Grund wird benannt", str(befunde))
            # Gegenprobe: ein LAUFFAEHIGER Mutant, den ein guter Test
            # bemerkt, zaehlt weiterhin voll.
            gefangen, gepruefte, _ = mutationen_pruefen(
                guter_code, [{"name": "echt", "alt": "return a + b",
                              "neu": "return a - b"}], "m.py")
            pruefe((gefangen, gepruefte) == (1, 1),
                   "ein echter, lauffaehiger Fehler zaehlt weiterhin")

            # --- NICHT ANWENDBAR ist nicht NICHT GEFANGEN ---
            # Eine Luecke im Werkzeug darf nicht wie ein blinder Test
            # des Pruefligs aussehen.
            gefangen, gepruefte, befunde = mutationen_pruefen(
                guter_code, [{"name": "fehlt", "funktion": "gibtsnicht",
                              "rumpf": "return 0"}], "m.py")
            pruefe(gepruefte == 0,
                   "eine nicht anwendbare Mutation zaehlt NICHT als geprueft",
                   f"gepruefte={gepruefte}")
            pruefe(any("NICHT ANWENDBAR" in b for b in befunde),
                   "und wird ausdruecklich so benannt", str(befunde))

            # Veraltete Mutation: Suchtext passt nicht.
            gefangen, gepruefte, befunde = mutationen_pruefen(
                guter_code, [{"name": "alt", "alt": "gibt es nicht",
                              "neu": "x"}], "m.py")
            pruefe(gefangen == 0 and any("passt 0x" in b for b in befunde),
                   "eine veraltete Mutation faellt auf, statt zu verschwinden",
                   str(befunde))

            # --- Die Gesamtbewertung einer Pruefung ---
            werkstatt.schreiben("v.py", guter_code)
            pruefung = {"name": "probe", "datei": "v.py",
                        "verlangte_namen": ["addiere"],
                        "mutationen": mutationen,
                        "lernnotiz_verlangt": False}
            antworten = [{"antwort": "fertig", "werkzeuge": ["a", "b"]}]
            bewertung = bewerte_pruefung(pruefung, antworten)
            pruefe(bewertung["punkte"] == bewertung["moeglich"],
                   "vollstaendige Arbeit bekommt volle Punkte",
                   f"{bewertung['punkte']}/{bewertung['moeglich']}")
            pruefe(bewertung["mutationen"] == "1/1",
                   "die Mutationsquote steht im Zeugnis")

            # Gegenprobe: fehlende Schnittstelle kostet Punkte.
            pruefung_streng = dict(pruefung, verlangte_namen=["fehlt_hier"])
            schlecht = bewerte_pruefung(pruefung_streng, antworten)
            pruefe(schlecht["punkte"] < schlecht["moeglich"],
                   "fehlende Schnittstelle kostet einen Punkt")
            pruefe(any("fehlt: fehlt_hier" in b for b in schlecht["befunde"]),
                   "und der Befund sagt, was fehlt")

            # Gegenprobe: wer nichts anlegt, bekommt fast nichts.
            leer = bewerte_pruefung(
                {"name": "x", "datei": "gibtsnicht.py",
                 "lernnotiz_verlangt": False},
                [{"antwort": "ich wuerde ja gerne", "werkzeuge": []}])
            pruefe(leer["punkte"] == 0,
                   "wer nur redet und nichts anlegt, bekommt 0 Punkte",
                   str(leer["punkte"]))

            # Der Zeugnis-Abschnitt muss die Zahlen tragen.
            zeilen = zeugnis_zeilen({"note": "7/8", "dauer_s": 12.0,
                                     "pruefungen": [bewertung]})
            text = "\n".join(zeilen)
            pruefe("7/8" in text and "probe" in text,
                   "der Zeugnis-Abschnitt nennt Note und Pruefung")
        finally:
            werkstatt.SANDKASTEN = echt
            werkstatt.PROTOKOLL = echt_protokoll

    if fehler:
        print(f"\n{fehler} Fehler.")
    else:
        print("\nAlle Pruefungen bestanden.")
    return fehler


def main(argumente: list) -> int:
    if not argumente or "--selbsttest" in argumente:
        return _selbsttest()
    if argumente[0] == "--pruefung-vorschlagen":
        # Nur VORSCHLAGEN: Der Entwurf wird gedruckt, nicht eingetragen.
        # Was zum Massstab fuer alle kuenftigen Modelle wird, entscheidet
        # Mexla - ein Pruefling, der sich seine eigenen Pruefungen
        # schreibt, pruefte am Ende nur noch das, was er selbst kann.
        if len(argumente) < 3:
            print("Aufruf: --pruefung-vorschlagen NAME DATEI.py")
            return 2
        entwurf = pruefung_vorschlagen(argumente[1], argumente[2])
        for zeile in entwurf.get("bericht", []):
            print(zeile)
        if not entwurf.get("ok"):
            print("\nKEINE PRUEFUNG: " + entwurf.get("fehler", ""))
            return 1
        print("\nEntwurf (noch NICHT eingetragen - das macht Mexla mit "
              "--pruefung-uebernehmen):")
        print(json.dumps(entwurf["pruefung"], ensure_ascii=False, indent=2))
        return 0
    if argumente[0] == "--pruefung-uebernehmen":
        if len(argumente) < 3:
            print("Aufruf: --pruefung-uebernehmen NAME DATEI.py")
            return 2
        entwurf = pruefung_vorschlagen(argumente[1], argumente[2])
        for zeile in entwurf.get("bericht", []):
            print(zeile)
        if not entwurf.get("ok"):
            print("\nKEINE PRUEFUNG: " + entwurf.get("fehler", ""))
            return 1
        ergebnis = pruefung_uebernehmen(entwurf)
        print(json.dumps(ergebnis, ensure_ascii=False))
        return 0 if ergebnis.get("ok") else 1
    zeugnis = pruefe_modell(argumente[0])
    print("\n" + "\n".join(zeugnis_zeilen(zeugnis)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
