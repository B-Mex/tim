#!/usr/bin/env python3
"""Recherchierte Benchmark-Testfaelle pruefen und uebernehmen.

Der Weg, auf dem Tim den Modell-Benchmark selbst erweitert:

  1. Der Ablauf modell_scan recherchiert neue Test-Ideen und schreibt sie
     als JSON ({"benchmark_faelle": [...]}) in seinen Bericht.
  2. DIESES Skript (Job-Server-Aktion 'benchmark_faelle_uebernehmen',
     in Tim ein Knopf) liest den juengsten Lauf des Berichts, prueft
     jeden Fall mit pruefe_extra_fall() aus modell_benchmark - bewusst
     derselbe Pruefer, kein zweiter Satz Code - und uebernimmt nur, was
     den Zwei-Seiten-Beweis besteht (gut_antwort besteht, schlecht_antwort
     faellt durch).
  3. modell_benchmark.py laedt die Datei bei jedem Lauf und prueft ERNEUT -
     die Uebernahme ist Komfort, kein Freifahrtschein.

Warum kein direktes Editieren von modell_benchmark.py durch das Modell:
Die Fall-Ideen stammen aus Suchtreffern, also aus fremdem Text. Duerfte
das Modell Code schreiben, der hier ausgefuehrt wird, waere jeder
praeparierte Treffer eine Codeausfuehrung auf diesem Mac. Daten-Faelle
koennen schlimmstenfalls nutzlos sein - und selbst die haelt die
Gegenprobe auf.

Aufruf:
  python3 benchmark_faelle_pflegen.py               # uebernehmen
  python3 benchmark_faelle_pflegen.py --zeigen      # nur anzeigen, nichts schreiben
  python3 benchmark_faelle_pflegen.py --selbsttest
"""

import json
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from modell_benchmark import (pruefe_extra_fall, MAX_EXTRA_FAELLE,
                              EXTRA_FAELLE_DATEI)
# Dieselbe SSRF-Sperre wie beim Werkzeug "Webseite lesen" - eine
# vorgeschlagene Quelle darf nie auf innere Dienste zeigen.
from crew_generic import web_ziel_pruefen

QUELLE = Path.home() / "Desktop/M1_DEPLOYMENT/berichte/modell_scan.md"
QUELLEN_DATEI = Path("/opt/ki-server/config/benchmark_quellen.json")
# Nur der juengste Lauf zaehlt, und auch der nur begrenzt - ein Bericht
# waechst per Anhaengen ueber Monate.
MAX_QUELLTEXT = 200_000
MAX_QUELLEN = 15
QUELLEN_NAME_MUSTER = re.compile(r"^[a-z0-9][a-z0-9_]{1,39}\Z")


def pruefe_quelle(quelle) -> list:
    """Probleme einer vorgeschlagenen Benchmark-Quelle. Leer = uebernehmbar.

    Die URL wird aufgeloest und gegen interne Adressen geprueft: Der
    Vorschlag stammt aus LLM-Text - "http://127.0.0.1:8765/..." als
    Quelle waere sonst ein Weg, den Kurator beim naechsten Scan auf die
    eigenen Dienste zu lenken.
    """
    if not isinstance(quelle, dict):
        return [f"Quelle muss ein JSON-Objekt sein (ist {type(quelle).__name__})"]
    probleme = []
    name = quelle.get("name")
    if not isinstance(name, str) or not QUELLEN_NAME_MUSTER.match(name):
        probleme.append(f"name {name!r}: nur a-z, 0-9 und _ (2-40 Zeichen)")
    url = quelle.get("url")
    if not isinstance(url, str) or not (10 <= len(url) <= 300):
        probleme.append("url fehlt oder nicht 10-300 Zeichen")
    else:
        grund = web_ziel_pruefen(url)
        if grund:
            probleme.append(f"url abgelehnt: {grund}")
    hinweis = quelle.get("hinweis", "")
    if not isinstance(hinweis, str) or len(hinweis) > 300:
        probleme.append("hinweis muss Text mit hoechstens 300 Zeichen sein")
    return probleme


def juengster_lauf(text: str) -> str:
    """Der Abschnitt ab dem letzten '## Lauf vom' (crew_generic haengt so an)."""
    stelle = text.rfind("## Lauf vom")
    return text[stelle:] if stelle >= 0 else text


def json_objekte_mit(text: str, schluessel: str) -> list:
    """Alle parsebaren JSON-Objekte, die den Schluessel enthalten.

    LLM-Text liefert kein sauberes Dokument, sondern JSON irgendwo im
    Fliesstext oder in ```-Zaeunen. Deshalb: jede Fundstelle des
    Schluessels nehmen, zur oeffnenden Klammer zuruecklaufen und mit
    Tiefenzaehler das Objekt ausschneiden.
    """
    objekte = []
    for treffer in re.finditer(re.escape(f'"{schluessel}"'), text):
        anfang = text.rfind("{", 0, treffer.start())
        while anfang >= 0:
            tiefe = 0
            ende = -1
            im_string = False
            fluchtzeichen = False
            for i in range(anfang, min(len(text), anfang + MAX_QUELLTEXT)):
                z = text[i]
                if im_string:
                    if fluchtzeichen:
                        fluchtzeichen = False
                    elif z == "\\":
                        fluchtzeichen = True
                    elif z == '"':
                        im_string = False
                    continue
                if z == '"':
                    im_string = True
                elif z == "{":
                    tiefe += 1
                elif z == "}":
                    tiefe -= 1
                    if tiefe == 0:
                        ende = i
                        break
            if ende > treffer.start():
                try:
                    obj = json.loads(text[anfang:ende + 1])
                    if isinstance(obj, dict) and schluessel in obj:
                        objekte.append(obj)
                    break
                except ValueError:
                    pass
            # Das gefundene '{' gehoerte nicht zum Objekt (z.B. eines im
            # Fliesstext davor) - eine Klammer weiter zurueck probieren.
            anfang = text.rfind("{", 0, anfang)
    return objekte


def bestehende_laden(ziel: Path) -> list:
    if not ziel.is_file():
        return []
    try:
        daten = json.loads(ziel.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return []
    faelle = daten.get("faelle") if isinstance(daten, dict) else None
    return faelle if isinstance(faelle, list) else []


def atomar_schreiben(ziel: Path, daten: dict) -> None:
    """tempfile + os.replace: ersetzt auch einen untergeschobenen Symlink,
    statt ihm zu folgen - und laesst bei Absturz nie eine halbe Datei da."""
    ziel.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(ziel.parent), suffix=".neu")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(daten, f, ensure_ascii=False, indent=2)
            f.write("\n")
        os.replace(tmp, ziel)
    finally:
        Path(tmp).unlink(missing_ok=True)


def uebernehmen(quelle: Path = None, ziel: Path = None,
                nur_zeigen: bool = False, quellen_ziel: Path = None) -> tuple:
    """(exitcode, meldungstext) - die eigentliche Arbeit, testbar mit
    eigenen Pfaden. Exit 0 auch bei 'nichts zu uebernehmen': fuer den
    Knopf in Tim ist das ein Ergebnis, kein Fehler."""
    quelle = quelle or QUELLE
    ziel = ziel or EXTRA_FAELLE_DATEI
    quellen_ziel = quellen_ziel or QUELLEN_DATEI
    if not quelle.is_file():
        return 0, (f"Keine Vorschlaege: {quelle.name} gibt es noch nicht. "
                   "Erst den Ablauf modell_scan laufen lassen.")
    text = quelle.read_text(encoding="utf-8", errors="replace")[-MAX_QUELLTEXT:]
    abschnitt = juengster_lauf(text)

    kandidaten = []
    for obj in json_objekte_mit(abschnitt, "benchmark_faelle"):
        rohliste = obj.get("benchmark_faelle")
        if isinstance(rohliste, list):
            kandidaten.extend(rohliste[:MAX_EXTRA_FAELLE])

    quellen_kandidaten = []
    for obj in json_objekte_mit(abschnitt, "benchmark_quellen"):
        rohliste = obj.get("benchmark_quellen")
        if isinstance(rohliste, list):
            quellen_kandidaten.extend(rohliste[:MAX_QUELLEN])

    if not kandidaten and not quellen_kandidaten:
        return 0, ("Im juengsten modell_scan-Lauf stehen keine "
                   "benchmark_faelle- oder benchmark_quellen-Vorschlaege.")

    bestehend = bestehende_laden(ziel)
    vorhandene_namen = {f.get("name") for f in bestehend if isinstance(f, dict)}

    zeilen = [f"Vorschlaege im juengsten Scan-Lauf: {len(kandidaten)} "
              f"Testfaelle, {len(quellen_kandidaten)} Quellen"]
    uebernommen = []
    for roh in kandidaten:
        name = roh.get("name", "?") if isinstance(roh, dict) else "?"
        if name in vorhandene_namen:
            zeilen.append(f"- '{name}': schon vorhanden, uebersprungen")
            continue
        probleme = pruefe_extra_fall(roh)
        if probleme:
            zeilen.append(f"- '{name}' ABGELEHNT: {probleme[0]}")
            continue
        if len(bestehend) + len(uebernommen) >= MAX_EXTRA_FAELLE:
            zeilen.append(f"- '{name}': Obergrenze {MAX_EXTRA_FAELLE} erreicht, "
                          "nicht uebernommen (erst alte Faelle ausmisten)")
            continue
        uebernommen.append(roh)
        vorhandene_namen.add(name)
        zeilen.append(f"- '{name}' uebernommen (Gegenprobe bestanden)")

    # --- Quellen: gleiche Mechanik, eigene Datei ---
    quellen_daten = {}
    if quellen_ziel.is_file():
        try:
            quellen_daten = json.loads(quellen_ziel.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            quellen_daten = {}
    quellen_bestand = quellen_daten.get("quellen")
    if not isinstance(quellen_bestand, list):
        quellen_bestand = []
    bekannte = {q.get("url") for q in quellen_bestand if isinstance(q, dict)}
    bekannte |= {q.get("name") for q in quellen_bestand if isinstance(q, dict)}
    neue_quellen = []
    for roh in quellen_kandidaten:
        name = roh.get("name", "?") if isinstance(roh, dict) else "?"
        if isinstance(roh, dict) and (roh.get("url") in bekannte
                                      or roh.get("name") in bekannte):
            zeilen.append(f"- Quelle '{name}': schon vorhanden, uebersprungen")
            continue
        probleme = pruefe_quelle(roh)
        if probleme:
            zeilen.append(f"- Quelle '{name}' ABGELEHNT: {probleme[0]}")
            continue
        if len(quellen_bestand) + len(neue_quellen) >= MAX_QUELLEN:
            zeilen.append(f"- Quelle '{name}': Obergrenze {MAX_QUELLEN} "
                          "erreicht, nicht uebernommen")
            continue
        neue_quellen.append({"name": roh["name"], "url": roh["url"],
                             "hinweis": roh.get("hinweis", "")})
        bekannte |= {roh["url"], roh["name"]}
        zeilen.append(f"- Quelle '{name}' uebernommen")

    if nur_zeigen and (uebernommen or neue_quellen):
        zeilen.append(f"\n--zeigen: {len(uebernommen)} Faelle und "
                      f"{len(neue_quellen)} Quellen waeren uebernommen "
                      "worden, nichts geschrieben.")
        return 0, "\n".join(zeilen)

    if uebernommen:
        atomar_schreiben(ziel, {"faelle": bestehend + uebernommen})
        zeilen.append(f"\n{len(uebernommen)} neue Faelle in {ziel} - "
                      "sie laufen beim naechsten Benchmark automatisch mit "
                      "(dort werden sie erneut geprueft).")
    if neue_quellen:
        atomar_schreiben(quellen_ziel,
                         {"quellen": quellen_bestand + neue_quellen})
        zeilen.append(f"{len(neue_quellen)} neue Quellen in {quellen_ziel} - "
                      "der naechste modell_scan liest sie mit.")
    if not uebernommen and not neue_quellen:
        zeilen.append("\nNichts uebernommen.")
    return 0, "\n".join(zeilen)


def _selbsttest() -> int:
    fehler = 0

    def pruefe(bedingung, text):
        nonlocal fehler
        if bedingung:
            print(f"  ok      {text}")
        else:
            print(f"  FEHLER  {text}")
            fehler += 1

    print("benchmark_faelle_pflegen Selbsttest:")

    guter_fall = {"name": "einheiten_minuten",
                  "prompt": "Wie viele Minuten sind 2,5 Stunden? Nur die Zahl.",
                  "pruefung": {"muss_eines": ["150"]},
                  "gut_antwort": "150", "schlecht_antwort": "125"}
    boeser_fall = {"name": "trennt_nicht", "prompt": "Sag irgendwas Nettes.",
                   "pruefung": {"muss_eines": ["a"]},
                   "gut_antwort": "ja", "schlecht_antwort": "ja"}

    # JSON-Extraktion aus LLM-Fliesstext, mit Zaun und Geschwaetz drumrum.
    text = ('Mexla, hier meine Funde.\n```json\n'
            + json.dumps({"benchmark_faelle": [guter_fall]})
            + '\n```\nDas wars. {"anderes": 1}')
    objekte = json_objekte_mit(text, "benchmark_faelle")
    pruefe(len(objekte) == 1
           and objekte[0]["benchmark_faelle"][0]["name"] == "einheiten_minuten",
           "JSON wird aus Fliesstext samt Zaun herausgeschnitten")
    pruefe(json_objekte_mit("kein json hier", "benchmark_faelle") == [],
           "Text ohne JSON liefert leer (kein Absturz)")
    verschachtelt = json.dumps(
        {"benchmark_faelle": [dict(guter_fall, prompt='Er sagte: "150 {gern}"')]})
    objekte = json_objekte_mit("vorne { kaputt " + verschachtelt, "benchmark_faelle")
    pruefe(len(objekte) == 1,
           "Klammern in Strings verwirren den Zaehler nicht")

    # Nur der juengste Lauf zaehlt - alte Vorschlaege nicht nochmal.
    zwei_laeufe = ("## Lauf vom 2026-08-01\n"
                   + json.dumps({"benchmark_faelle": [boeser_fall]})
                   + "\n\n## Lauf vom 2026-08-23\n"
                   + json.dumps({"benchmark_faelle": [guter_fall]}))
    pruefe("trennt_nicht" not in juengster_lauf(zwei_laeufe),
           "juengster_lauf schneidet alte Laeufe ab")

    import shutil
    tmp = Path(tempfile.mkdtemp(prefix="bfp_probe_"))
    try:
        quelle = tmp / "modell_scan.md"
        ziel = tmp / "extra.json"
        # ALLE Testaufrufe bekommen ein eigenes quellen_ziel - sonst
        # schriebe ein Test in die echte benchmark_quellen.json (die
        # Chat-Migration hat am 23.08.2026 auf diese Weise einen echten
        # Verlauf gekostet).
        qz = tmp / "quellen.json"

        # Uebernahme: gut kommt durch, boese wird abgelehnt und gemeldet.
        quelle.write_text("## Lauf vom 2026-08-23\n" + json.dumps(
            {"benchmark_faelle": [guter_fall, boeser_fall]}), encoding="utf-8")
        rc, meldung = uebernehmen(quelle, ziel, quellen_ziel=qz)
        pruefe(rc == 0 and "'einheiten_minuten' uebernommen" in meldung,
               "guter Fall wird uebernommen")
        pruefe("'trennt_nicht' ABGELEHNT" in meldung,
               "Fall ohne Trennschaerfe wird abgelehnt und gemeldet")
        gespeichert = json.loads(ziel.read_text(encoding="utf-8"))["faelle"]
        pruefe([f["name"] for f in gespeichert] == ["einheiten_minuten"],
               "nur der geprueft gute Fall steht in der Datei")

        # Zweiter Lauf: Dublette wird uebersprungen, Datei bleibt gleich.
        rc, meldung = uebernehmen(quelle, ziel, quellen_ziel=qz)
        pruefe("schon vorhanden" in meldung,
               "Dublette wird uebersprungen statt doppelt gespeichert")
        pruefe(len(json.loads(ziel.read_text(encoding="utf-8"))["faelle"]) == 1,
               "die Datei waechst durch Dubletten nicht")

        # --zeigen schreibt nichts.
        ziel2 = tmp / "extra2.json"
        rc, meldung = uebernehmen(quelle, ziel2, nur_zeigen=True, quellen_ziel=qz)
        pruefe("nichts geschrieben" in meldung and not ziel2.exists(),
               "--zeigen laesst die Datei unangetastet")

        # Obergrenze: mehr als MAX_EXTRA_FAELLE gehen nicht in die Datei.
        viele = [dict(guter_fall, name=f"fall_{i:02d}")
                 for i in range(MAX_EXTRA_FAELLE + 5)]
        quelle.write_text("## Lauf vom 2026-08-23\n" + json.dumps(
            {"benchmark_faelle": viele}), encoding="utf-8")
        ziel3 = tmp / "extra3.json"
        rc, meldung = uebernehmen(quelle, ziel3, quellen_ziel=qz)
        anzahl = len(json.loads(ziel3.read_text(encoding="utf-8"))["faelle"])
        pruefe(anzahl == MAX_EXTRA_FAELLE,
               f"Obergrenze haelt ({anzahl}/{MAX_EXTRA_FAELLE})")

        # Fehlende Quelle ist ein Ergebnis, kein Absturz.
        rc, meldung = uebernehmen(tmp / "fehlt.md", ziel, quellen_ziel=qz)
        pruefe(rc == 0 and "gibt es noch nicht" in meldung,
               "fehlende Quelle wird sauber gemeldet")

        # Ein Symlink als Ziel wird ERSETZT, nicht befuellt - sonst koennte
        # eine untergeschobene Verknuepfung die Faelle woandershin lenken.
        koeder = tmp / "koeder.txt"
        koeder.write_text("unangetastet", encoding="utf-8")
        symlink_ziel = tmp / "extra4.json"
        try:
            symlink_ziel.symlink_to(koeder)
            quelle.write_text("## Lauf vom 2026-08-23\n" + json.dumps(
                {"benchmark_faelle": [guter_fall]}), encoding="utf-8")
            uebernehmen(quelle, symlink_ziel, quellen_ziel=qz)
            pruefe(koeder.read_text(encoding="utf-8") == "unangetastet"
                   and not symlink_ziel.is_symlink(),
                   "Symlink am Ziel wird ersetzt, das Linkziel bleibt unberuehrt")
        except OSError:
            print("  LUECKE  Symlink-Probe nicht moeglich (Dateisystem)")

        # --- Quellen-Uebernahme ---
        # Der Kurator darf neue Fundorte vorschlagen; interne Adressen
        # muessen an der SSRF-Sperre scheitern (die Vorschlaege stammen
        # aus LLM-Text).
        gute_quelle = {"name": "beispiel_quelle",
                       "url": "https://example.com/llm-tests",
                       "hinweis": "Probe"}
        quelle.write_text("## Lauf vom 2026-08-23\n" + json.dumps(
            {"benchmark_quellen": [
                gute_quelle,
                {"name": "boese_intern", "url": "http://127.0.0.1:8765/aktionen"},
                {"name": "boese_schema", "url": "ftp://example.com/x"},
            ]}), encoding="utf-8")
        ziel5 = tmp / "extra5.json"
        qz5 = tmp / "quellen5.json"
        rc, meldung = uebernehmen(quelle, ziel5, quellen_ziel=qz5)
        pruefe("Quelle 'beispiel_quelle' uebernommen" in meldung,
               "oeffentliche Quelle wird uebernommen")
        pruefe("Quelle 'boese_intern' ABGELEHNT" in meldung,
               "interne Adresse scheitert an der SSRF-Sperre")
        pruefe("Quelle 'boese_schema' ABGELEHNT" in meldung,
               "Nicht-http-Quelle wird abgelehnt")
        gespeicherte_q = json.loads(qz5.read_text(encoding="utf-8"))["quellen"]
        pruefe([q["name"] for q in gespeicherte_q] == ["beispiel_quelle"],
               "nur die geprueft gute Quelle steht in der Datei")
        rc, meldung = uebernehmen(quelle, ziel5, quellen_ziel=qz5)
        pruefe("Quelle 'beispiel_quelle': schon vorhanden" in meldung,
               "Quellen-Dublette wird uebersprungen")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    if fehler:
        print(f"\n{fehler} Fehler.")
    else:
        print("\nAlle Selbsttests bestanden.")
    return fehler


def main() -> int:
    if "--selbsttest" in sys.argv[1:]:
        return _selbsttest()
    rc, meldung = uebernehmen(nur_zeigen="--zeigen" in sys.argv[1:])
    print(meldung)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
