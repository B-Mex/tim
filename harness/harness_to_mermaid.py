#!/usr/bin/env python3
"""Erzeugt fuer jeden Ablauf in jobs/ ein Mermaid-Diagramm.

Abweichung von der urspruenglichen Doku-Skizze (Phase F3): dort sollte ein
LLM das Mermaid erzeugen. Hier passiert das deterministisch aus dem JSON -
zuverlaessiger und passt zum "Quality Gates ohne KI"-Prinzip des Harness
(ein Diagramm-Generator braucht kein Sprachmodell, nur eine Vorlage).
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from job_schema import pruefe_job

HARNESS_DIR = Path(__file__).parent
JOBS_DIR = HARNESS_DIR / "jobs"

# Nur diese Zeichen landen unveraendert im Diagramm. Alles andere wird
# ersetzt: Anfuehrungszeichen, eckige Klammern und Zeilenumbrueche wuerden
# die Mermaid-Syntax verlassen, und eine "click"-Anweisung machte aus einem
# Diagramm einen anklickbaren Link. Die Werte kommen aus einer JSON, die
# auch das lokale Modell geschrieben haben kann.
UNBEDENKLICH = re.compile(r"[^A-Za-z0-9 _./\-]")


def _zahm(text) -> str:
    """Macht einen Wert fuer die Mermaid-Ausgabe unbedenklich."""
    return UNBEDENKLICH.sub("_", " ".join(str(text).split()))[:60] or "_"


def crew_to_mermaid(definition: dict) -> str:
    zeilen = ["graph TD"]
    # .get statt [..]: eine unvollstaendige Definition darf hier nicht mit
    # KeyError abstuerzen. Dieses Script laeuft im Installer unter set -e -
    # ein Absturz haette dort die ganze Einrichtung abgebrochen.
    agent_rolle = {a.get("id"): a.get("rolle", "?")
                   for a in definition.get("agents", []) if isinstance(a, dict)}

    aufgaben = [t for t in definition.get("tasks", []) if isinstance(t, dict)]
    for task in aufgaben:
        agent = agent_rolle.get(task.get("agent"), task.get("agent", "?"))
        kennung = _zahm(task.get("id"))
        zeilen.append(f'    {kennung}["{kennung}<br/>{_zahm(agent)}"]')

    for task in aufgaben:
        eingaben = task.get("input", [])
        if not isinstance(eingaben, list):
            continue
        for eingabe in eingaben:
            zeilen.append(f"    {_zahm(eingabe)} --> {_zahm(task.get('id'))}")

    return "\n".join(zeilen)


def main():
    if not JOBS_DIR.exists():
        print(f"FEHLER: {JOBS_DIR} nicht gefunden.", file=sys.stderr)
        sys.exit(1)

    dateien = sorted(JOBS_DIR.glob("*.json"))
    if not dateien:
        print("Keine Ablaeufe in jobs/ gefunden.")
        return

    for jf in dateien:
        try:
            definition = json.loads(jf.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"{jf.name}: nicht lesbar ({e})", file=sys.stderr)
            continue
        # Auch hier die Sicherheitspruefung fahren. Vorher war das der
        # einzige Codepfad, der eine Ablauf-Definition OHNE pruefe_job
        # gelesen hat: eine untergeschobene JSON kam so ungeprueft in eine
        # .mmd-Datei, die in der Doku angezeigt wird.
        try:
            probleme = pruefe_job(definition)
        except Exception as e:
            probleme = [f"Pruefung fehlgeschlagen ({e})"]
        if probleme:
            print(f"{jf.name}: kein Diagramm - {probleme[0]}", file=sys.stderr)
            continue
        mermaid = crew_to_mermaid(definition)
        out_file = JOBS_DIR / f"{jf.stem}.mmd"
        out_file.write_text(mermaid, encoding="utf-8")
        print(f"--- {jf.stem} ---")
        print(mermaid)
        print(f"Gespeichert: {out_file}\n")


if __name__ == "__main__":
    if "--selbsttest" in sys.argv:
        # Frueher gab es hier keinen Selbsttest - 12_MAC_harness_setup.sh
        # rief das Script trotzdem unter der Ueberschrift "Selbsttest" auf.
        fehler = 0

        # 1. Unvollstaendige Definition darf nicht abstuerzen
        try:
            crew_to_mermaid({"agents": [{"id": "a"}], "tasks": [{"id": "t"}]})
            print("OK     unvollstaendige Definition stuerzt nicht ab")
        except Exception as e:
            print(f"FEHLER unvollstaendige Definition: {type(e).__name__}: {e}")
            fehler += 1

        # 2. Mermaid-Injection ueber eine Task-id.
        #    Entscheidend ist NICHT, ob der Text noch lesbar ist, sondern ob
        #    die Struktur haelt: aus einer id darf kein neues Anfuehrungs-
        #    zeichen, keine Klammer, kein Semikolon und keine neue Zeile in
        #    die Ausgabe gelangen - nur damit koennte man aus dem Knoten-
        #    Label ausbrechen und z.B. eine click-Anweisung anhaengen.
        angriffs_id = (chr(34) + '] ; click x ' + chr(34) + 'http:boese.example'
                       + chr(34) + chr(10) + 'click y')
        boese = crew_to_mermaid({
            "agents": [{"id": "a", "rolle": "r"}],
            "tasks": [{"id": angriffs_id, "agent": "a"}],
        })
        # Geruest der erzeugten Zeile abziehen, es enthaelt selbst " und [ ]
        eingefuegt = boese.split('graph TD')[1]
        for teil in ('["', '<br/>', '"]', chr(10), '    '):
            eingefuegt = eingefuegt.replace(teil, '')
        verboten = [z for z in eingefuegt if z in chr(34) + "[];" + chr(10) + chr(60)]
        if verboten:
            print(f"FEHLER Task-id bringt Sonderzeichen {verboten} ins Diagramm:"
                  f"{chr(10)}{boese}")
            fehler += 1
        elif boese.count(chr(10)) != 1:
            print(f"FEHLER Task-id erzeugt zusaetzliche Zeilen:{chr(10)}{boese}")
            fehler += 1
        else:
            print("OK     Task-id kann nicht aus dem Knoten-Label ausbrechen")

        # 3. Normale Definition ergibt weiterhin ein brauchbares Diagramm
        gut = crew_to_mermaid({
            "agents": [{"id": "a", "rolle": "Recherche-Agent"}],
            "tasks": [{"id": "scan", "agent": "a", "input": []},
                      {"id": "review", "agent": "a", "input": ["scan"]}],
        })
        if "scan --> review" in gut and "Recherche-Agent" in gut:
            print("OK     normales Diagramm bleibt lesbar")
        else:
            print(f"FEHLER normales Diagramm kaputt:{chr(10)}{gut}")
            fehler += 1

        print(f"{chr(10)}{3 - fehler}/3 Tests bestanden.")
        if fehler:
            raise SystemExit(1)
    else:
        main()
