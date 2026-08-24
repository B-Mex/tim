#!/usr/bin/env python3
"""Modell-Benchmark im Hintergrund starten und beobachten.

Warum dieses Skript: Der Job-Server fuehrt Aktionen synchron aus und
bricht nach 30 Minuten ab - ein Benchmark ueber mehrere Modelle braucht
Stunden. Deshalb startet die Aktion nur DIESEN Starter: er prueft, ob
schon ein Lauf laeuft, startet modell_benchmark.py als eigenen Prozess
mit Logdatei und kehrt sofort zurueck. Den Fortschritt zeigt --status.

Aufruf (so stehen sie in der Positivliste des Job-Servers):
  python3 benchmark_starter.py --neue           # alle ungetesteten Modelle
  python3 benchmark_starter.py --modell NAME    # ein bestimmtes Modell
  python3 benchmark_starter.py --vergleich NAME__NAME[__NAME ...]
                                                # Auswahl gegeneinander,
                                                # EIN Lauf, EIN Bericht
  python3 benchmark_starter.py --status         # laeuft was? letzte Zeilen
  python3 benchmark_starter.py --selbsttest

Der Modellname darf Punkte statt Doppelpunkt tragen ("qwen3.5.9b" fuer
"qwen3.5:9b"): Der Chat der Zentrale laesst in Argumenten keinen
Doppelpunkt durch (SICHERER_NAME), und hier wird ohnehin gegen die Liste
der INSTALLIERTEN Modelle aufgeloest - eine Positivliste, kein Filter.
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from autonomie import killswitch_aktiv
from modell_benchmark import ungetestete_modelle

BENCHMARK = Path(__file__).parent / "modell_benchmark.py"
LOG_DIR = Path("/opt/ki-server/logs/benchmarks")
LOCK = LOG_DIR / "laeuft.json"
OLLAMA = "http://127.0.0.1:11434"
# Unter so viel freiem Speicher leidet die Messung (Modell faellt teils
# auf die CPU, Tempo bricht ein) - gemessen sinngemaess am 22.08.2026,
# als die Bauteil-Recherche bei 6,9 GB frei ein Notmodell bekam.
RAM_WARNGRENZE_GB = 10.0
# Ein Vergleichslauf misst jedes Modell komplett - bei mehr als sechs
# ist der Mac einen halben Tag belegt. Wer wirklich alle will, nimmt
# zweimal drei.
MAX_VERGLEICH = 6


def installierte_modelle() -> list:
    try:
        with urllib.request.urlopen(OLLAMA + "/api/tags", timeout=10) as antwort:
            daten = json.loads(antwort.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return []
    namen = []
    for m in daten.get("models", []):
        name = m.get("name", "")
        if name.endswith(":latest"):
            name = name[:-7]
        if name:
            namen.append(name)
    return sorted(namen)


def modell_aufloesen(wunsch: str, installiert: list) -> tuple:
    """(name oder None, meldung). Punkt darf fuer Doppelpunkt stehen.

    Aufgeloest wird gegen die installierten Modelle - was nicht
    installiert ist, laesst sich nicht benchmarken, egal wie es heisst.
    """
    wunsch = (wunsch or "").strip()
    if not wunsch:
        return None, "Kein Modellname angegeben."

    # ':' UND '/' duerfen als '.' geschrieben werden - der Riegel der
    # Zentrale (SICHERER_NAME) laesst beide Zeichen nicht durch, und
    # Namen wie 'saracen9/amoral-...:q3_k_m' tragen beide.
    def glatt(name: str) -> str:
        return name.replace(":", ".").replace("/", ".")

    treffer = [m for m in installiert
               if m == wunsch or glatt(m) == glatt(wunsch)]
    if len(treffer) == 1:
        return treffer[0], f"Modell: {treffer[0]}"
    if len(treffer) > 1:
        return None, ("Mehrdeutig - gemeint sein koennten: "
                      + ", ".join(treffer))
    return None, (f"'{wunsch}' ist nicht installiert. Installiert sind: "
                  + (", ".join(installiert) or "keine (Ollama aus?)"))


def vergleich_aufloesen(wunsch: str, installiert: list) -> tuple:
    """(Liste von Namen oder None, meldung) fuer '--vergleich a__b__c'.

    Doppel-Unterstrich als Trenner: Komma und Leerzeichen kommen nicht
    durch den Argument-Riegel der Zentrale, und '__' taucht in echten
    Ollama-Namen nicht auf (einfache '_' wie in q3_k_m schon). Jeder
    Teil wird einzeln gegen die installierten Modelle aufgeloest -
    ein Tippfehler lehnt den GANZEN Lauf ab, statt still weniger zu
    messen als bestellt.
    """
    teile = [t for t in (wunsch or "").split("__") if t.strip()]
    if len(teile) < 2:
        return None, ("Ein Vergleich braucht mindestens zwei Modelle, "
                      "getrennt durch zwei Unterstriche - Beispiel: "
                      "qwen3.5.9b__gpt-oss.20b")
    if len(teile) > MAX_VERGLEICH:
        return None, (f"{len(teile)} Modelle sind zu viele fuer einen Lauf "
                      f"(hoechstens {MAX_VERGLEICH}) - der Mac waere zu "
                      "lange belegt.")
    namen = []
    for teil in teile:
        name, meldung = modell_aufloesen(teil, installiert)
        if not name:
            return None, meldung
        if name not in namen:
            namen.append(name)
    if len(namen) < 2:
        return None, "Nach dem Aussortieren von Dubletten bleibt nur ein Modell."
    return namen, "Vergleich: " + ", ".join(namen)


def freier_ram_gb() -> float:
    try:
        import psutil
        return round(psutil.virtual_memory().available / 2**30, 1)
    except ImportError:
        return -1.0


def lauf_laeuft() -> dict | None:
    """Der eingetragene Lauf, falls sein Prozess noch lebt. Verwaiste
    Eintraege (Absturz, Neustart) werden dabei stillschweigend geraeumt."""
    if not LOCK.is_file():
        return None
    try:
        eintrag = json.loads(LOCK.read_text(encoding="utf-8"))
        pid = int(eintrag["pid"])
    except (ValueError, KeyError, OSError):
        LOCK.unlink(missing_ok=True)
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        LOCK.unlink(missing_ok=True)
        return None
    except PermissionError:
        pass
    return eintrag


def starten(argumente: list, beschreibung: str) -> int:
    stop = killswitch_aktiv()
    if stop:
        print(f"KILL-SWITCH aktiv ({stop}) - kein Benchmark-Start.")
        return 1
    laufend = lauf_laeuft()
    if laufend:
        print(f"Es laeuft schon ein Benchmark seit {laufend.get('start', '?')} "
              f"({laufend.get('beschreibung', '?')}). Erst abwarten - "
              f"zwei Laeufe zugleich verfaelschen beide Messungen. "
              f"Stand ansehen: Aktion modell_benchmark_status.")
        return 1

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stempel = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log = LOG_DIR / f"lauf_{stempel}.log"
    with open(log, "w", encoding="utf-8") as f:
        f.write(f"Benchmark-Lauf {stempel}: {beschreibung}\n")
        f.flush()
        prozess = subprocess.Popen(
            [sys.executable, str(BENCHMARK)] + argumente,
            stdout=f, stderr=subprocess.STDOUT,
            cwd=str(BENCHMARK.parent),
            start_new_session=True,  # ueberlebt das Ende des Job-Servers
        )
    LOCK.write_text(json.dumps({
        "pid": prozess.pid, "start": stempel, "log": str(log),
        "beschreibung": beschreibung}), encoding="utf-8")

    zeilen = [f"Benchmark gestartet ({beschreibung}).",
              f"Log: {log}",
              "Je Modell dauert das einige Minuten bis ueber eine Stunde. "
              "Stand ansehen: Aktion modell_benchmark_status. Der fertige "
              "Bericht erscheint unter Berichte (modell_benchmark_...)."]
    ram = freier_ram_gb()
    if 0 <= ram < RAM_WARNGRENZE_GB:
        zeilen.append(
            f"ACHTUNG: nur {ram} GB Speicher frei - laeuft Odysseus noch? "
            f"Erst 'odysseus_stoppen', sonst werden die Messwerte schlechter.")
    print("\n".join(zeilen))
    return 0


def status() -> int:
    laufend = lauf_laeuft()
    if laufend:
        print(f"Benchmark LAEUFT seit {laufend.get('start', '?')} "
              f"({laufend.get('beschreibung', '?')}).")
    else:
        print("Kein Benchmark am Laufen.")
    # Nach Aenderungszeit, nicht nach Name: die Logs von heute Nacht
    # ("lauf_20260823_...") liegen lexikografisch HINTER dem neuen Schema
    # ("lauf_2026-08-23_...") - nach Name gewaenne immer das alte.
    logs = (sorted(LOG_DIR.glob("lauf_*.log"), key=lambda p: p.stat().st_mtime)
            if LOG_DIR.is_dir() else [])
    if logs:
        letzte = logs[-1]
        zeilen = letzte.read_text(encoding="utf-8",
                                  errors="replace").splitlines()
        print(f"\nLetzte Zeilen aus {letzte.name}:")
        for zeile in zeilen[-25:]:
            print("  " + zeile)
    berichte = sorted(Path.home().glob(
        "Desktop/M1_DEPLOYMENT/berichte/modell_benchmark_*.md"))
    if berichte:
        print(f"\nJuengster Bericht: {berichte[-1].name} "
              "(lesbar ueber bericht_lesen)")
    return 0


def _selbsttest() -> int:
    global LOG_DIR, LOCK
    fehler = 0

    def pruefe(bedingung, text):
        nonlocal fehler
        if bedingung:
            print(f"  ok      {text}")
        else:
            print(f"  FEHLER  {text}")
            fehler += 1

    print("benchmark_starter Selbsttest:")

    installiert = ["qwen3.5:9b", "qwen3.6:35b-a3b", "qwen3-coder"]
    name, _ = modell_aufloesen("qwen3.5:9b", installiert)
    pruefe(name == "qwen3.5:9b", "exakter Name wird angenommen")
    name, _ = modell_aufloesen("qwen3.5.9b", installiert)
    pruefe(name == "qwen3.5:9b", "Punkt-Schreibweise wird aufgeloest "
                                 "(Chat-Riegel laesst ':' nicht durch)")
    name, meldung = modell_aufloesen("gibtsnicht:7b", installiert)
    pruefe(name is None and "nicht installiert" in meldung,
           "nicht installierte Modelle werden abgelehnt (Positivliste)")
    name, _ = modell_aufloesen("qwen3-coder", installiert)
    pruefe(name == "qwen3-coder", "Name ohne Tag wird angenommen")
    name, _ = modell_aufloesen(
        "saracen9.amoral-muse-glimmer-30b-abliterated.q3_k_m",
        installiert + ["saracen9/amoral-muse-glimmer-30b-abliterated:q3_k_m"])
    pruefe(name == "saracen9/amoral-muse-glimmer-30b-abliterated:q3_k_m",
           "auch '/' darf als Punkt geschrieben werden (Riegel der Zentrale)")
    name, meldung = modell_aufloesen("", installiert)
    pruefe(name is None, "leerer Name wird abgelehnt")
    # Mehrdeutigkeit: zwei installierte Modelle, die auf dieselbe
    # Punkt-Schreibweise fallen, duerfen nicht stumm geraten werden.
    name, meldung = modell_aufloesen("a.b", ["a:b", "a.b"])
    pruefe(name is None and "Mehrdeutig" in meldung,
           "mehrdeutige Punkt-Schreibweise wird gemeldet statt geraten")

    # Vergleich: Auswahl gegeneinander, EIN Lauf.
    namen, _ = vergleich_aufloesen("qwen3.5.9b__gpt-oss:20b",
                                   installiert + ["gpt-oss:20b"])
    pruefe(namen == ["qwen3.5:9b", "gpt-oss:20b"],
           "Vergleich loest beide Namen auf (Doppel-Unterstrich-Trenner)")
    namen, meldung = vergleich_aufloesen("qwen3.5.9b", installiert)
    pruefe(namen is None and "mindestens zwei" in meldung,
           "Vergleich mit nur einem Modell wird abgelehnt")
    namen, meldung = vergleich_aufloesen("qwen3.5.9b__gibtsnicht", installiert)
    pruefe(namen is None and "nicht installiert" in meldung,
           "ein Tippfehler lehnt den ganzen Vergleich ab")
    viele = "__".join(f"m{i}" for i in range(MAX_VERGLEICH + 1))
    namen, meldung = vergleich_aufloesen(
        viele, [f"m{i}" for i in range(MAX_VERGLEICH + 1)])
    pruefe(namen is None and "zu viele" in meldung,
           f"mehr als {MAX_VERGLEICH} Modelle werden abgelehnt")
    namen, meldung = vergleich_aufloesen("qwen3.5.9b__qwen3.5:9b", installiert)
    pruefe(namen is None and "nur ein Modell" in meldung,
           "Dubletten in der Auswahl fallen auf")

    # Lock-Verhalten mit Temp-Pfaden - die echten bleiben unangetastet.
    import shutil
    import tempfile
    echt_log_dir, echt_lock = LOG_DIR, LOCK
    tmp = Path(tempfile.mkdtemp(prefix="starter_probe_"))
    LOG_DIR, LOCK = tmp, tmp / "laeuft.json"
    try:
        pruefe(lauf_laeuft() is None, "ohne Lock-Datei laeuft nichts")
        LOCK.write_text(json.dumps({"pid": os.getpid(), "start": "jetzt",
                                    "beschreibung": "probe"}),
                        encoding="utf-8")
        laufend = lauf_laeuft()
        pruefe(laufend is not None and laufend["beschreibung"] == "probe",
               "lebendiger Prozess im Lock wird erkannt")
        LOCK.write_text(json.dumps({"pid": 99999999, "start": "alt",
                                    "beschreibung": "leiche"}),
                        encoding="utf-8")
        pruefe(lauf_laeuft() is None and not LOCK.exists(),
               "verwaistes Lock wird erkannt und geraeumt")
        LOCK.write_text("kein json", encoding="utf-8")
        pruefe(lauf_laeuft() is None and not LOCK.exists(),
               "kaputtes Lock wird geraeumt statt Absturz")

        # Doppelstart: mit lebendigem Lock darf starten() nicht starten.
        LOCK.write_text(json.dumps({"pid": os.getpid(), "start": "jetzt",
                                    "beschreibung": "probe"}),
                        encoding="utf-8")
        rc = starten(["--neue"], "probe")
        pruefe(rc == 1, "zweiter Start wird abgelehnt, solange einer laeuft")
        LOCK.unlink(missing_ok=True)

        # Status zeigt das juengste Log nach AENDERUNGSZEIT. Nach Namen
        # sortiert gewaenne immer das alte Schema von heute Nacht
        # ("lauf_20260823_..." liegt lexikografisch hinter "lauf_2026-...").
        alt = LOG_DIR / "lauf_20260823_0250.log"
        alt.write_text("altes schema\n", encoding="utf-8")
        neu = LOG_DIR / "lauf_2026-08-23_1200.log"
        neu.write_text("neues schema\n", encoding="utf-8")
        os.utime(alt, (time.time() - 3600, time.time() - 3600))
        import contextlib
        import io
        puffer = io.StringIO()
        with contextlib.redirect_stdout(puffer):
            status()
        pruefe("neues schema" in puffer.getvalue()
               and "altes schema" not in puffer.getvalue(),
               "Status zeigt das nach Aenderungszeit juengste Log")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        LOG_DIR, LOCK = echt_log_dir, echt_lock

    if fehler:
        print(f"\n{fehler} Fehler.")
    else:
        print("\nAlle Selbsttests bestanden.")
    return fehler


def main() -> int:
    args = sys.argv[1:]
    if "--selbsttest" in args:
        return _selbsttest()
    if "--status" in args:
        return status()
    if "--neue" in args:
        # Vorab pruefen statt blind starten: Sonst meldet der Knopf
        # "Benchmark gestartet", obwohl der Lauf eine Sekunde spaeter
        # mit "nichts zu tun" endet - genau so am 23.08.2026 passiert.
        if not installierte_modelle():
            print("Ollama nicht erreichbar oder keine Modelle installiert - "
                  "kein Start.")
            return 1
        offene = ungetestete_modelle()
        if not offene:
            print("Alle installierten Modelle sind bereits vermessen - "
                  "nichts gestartet. (Neues Modell erst mit 'ollama pull' "
                  "installieren, das macht Mexla selbst; danach findet dieser "
                  "Knopf es automatisch.)")
            return 0
        return starten(["--neue"],
                       "noch nie gemessen: " + ", ".join(offene[:6])
                       + ("..." if len(offene) > 6 else ""))
    if args and args[0] == "--modell":
        wunsch = args[1] if len(args) > 1 else ""
        name, meldung = modell_aufloesen(wunsch, installierte_modelle())
        if not name:
            print(meldung)
            return 1
        return starten([name], f"Einzeltest {name}")
    if args and args[0] == "--vergleich":
        wunsch = args[1] if len(args) > 1 else ""
        namen, meldung = vergleich_aufloesen(wunsch, installierte_modelle())
        if not namen:
            print(meldung)
            return 1
        return starten(namen, f"Vergleich {' vs '.join(namen)}")
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
