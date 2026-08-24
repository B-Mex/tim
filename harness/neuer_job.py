#!/usr/bin/env python3
"""Legt einen neuen Harness-Ablauf an - aus einer Beschreibung in normalem Deutsch.

Damit kann die lokale KI (oder du) neue Ablaeufe selbst erstellen, ohne
Python zu schreiben. Das Ergebnis ist eine JSON-Datei in jobs/, die
crew_generic.py direkt ausfuehren kann.

Nutzung:
    python3 neuer_job.py "Pruefe jeden Montag die Home-Assistant-Logs auf Fehler"
    python3 neuer_job.py --vorlage recherche mein_neuer_job

Sicherheit: Der erzeugte Job wird IMMER erst validiert (Schema + Verweise)
und als Entwurf gespeichert. Ohne dein 'ja' wird er nicht in den Cron
eingetragen - ein Ablauf kann sich also nicht selbst scharfschalten.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from model_router import get_model_for_job
from job_schema import pruefe_job

JOBS_DIR = Path(__file__).parent / "jobs"
OLLAMA_URL = "http://localhost:11434/api/generate"

VORLAGEN = {
    "recherche": {
        "agents": [
            {"id": "researcher", "rolle": "Recherche-Agent", "ziel": "ZIEL_HIER",
             "hintergrund": "Sucht im Web nach belastbaren Quellen.",
             "modell_klasse": 1, "werkzeuge": ["searxng"]},
            {"id": "pruefer", "rolle": "Zwilling/Reviewer", "ziel": "Ergebnisse kritisch gegenpruefen",
             "hintergrund": "Skeptisch, prueft Plausibilitaet und Quellen.",
             "modell_klasse": 1, "werkzeuge": []},
        ],
        "tasks": [
            {"id": "recherche", "agent": "researcher", "beschreibung": "BESCHREIBUNG_HIER", "input": [],
             "erwartete_ausgabe": "Mexla, gefolgt von den Fundstellen als Liste."},
            {"id": "pruefung", "agent": "pruefer", "beschreibung": "Pruefe die Ergebnisse kritisch.",
             "input": ["recherche"],
             "erwartete_ausgabe": "Mexla, gefolgt von der geprueften Empfehlung."},
        ],
    },
    "dateicheck": {
        "agents": [
            {"id": "leser", "rolle": "Analyst", "ziel": "ZIEL_HIER",
             "hintergrund": "Liest die freigegebenen Projektdateien.",
             "modell_klasse": 1, "werkzeuge": ["dateien_lesen"]},
            {"id": "reviewer", "rolle": "Reviewer", "ziel": "Probleme und Verbesserungen benennen",
             "hintergrund": "Nennt konkrete Fundstellen statt allgemeiner Hinweise.",
             "modell_klasse": 1, "werkzeuge": []},
        ],
        "tasks": [
            {"id": "lesen", "agent": "leser", "beschreibung": "BESCHREIBUNG_HIER", "input": [],
             "erwartete_ausgabe": "Mexla, gefolgt von der Zusammenfassung."},
            {"id": "review", "agent": "reviewer", "beschreibung": "Bewerte das Gelesene und nenne konkrete Verbesserungen.",
             "input": ["lesen"],
             "erwartete_ausgabe": "Mexla, gefolgt von: [{fundstelle, problem, schwere, empfehlung}]"},
        ],
    },
}


def saubere_name(text: str) -> str:
    """Macht aus beliebigem Text einen sicheren Job-/Dateinamen.

    Wichtig: verhindert Pfad-Ausbruch. '../../evil' wuerde sonst eine
    Datei ausserhalb von jobs/ anlegen.
    """
    erlaubt = "".join(c if (c.isalnum() and c.isascii()) else "_" for c in text.lower())
    erlaubt = "_".join(teil for teil in erlaubt.split("_") if teil)[:40].strip("_")
    if not erlaubt or not erlaubt[0].isalnum():
        erlaubt = f"job_{erlaubt}".strip("_")
    return erlaubt or "neuer_job"


def aus_vorlage(vorlage: str, name: str, beschreibung: str, pfade=None) -> dict:
    if vorlage not in VORLAGEN:
        raise ValueError(f"Unbekannte Vorlage '{vorlage}'. Verfuegbar: {', '.join(VORLAGEN)}")
    roh = json.loads(json.dumps(VORLAGEN[vorlage]))  # tiefe Kopie
    for a in roh["agents"]:
        a["ziel"] = a["ziel"].replace("ZIEL_HIER", beschreibung)
    for t in roh["tasks"]:
        t["beschreibung"] = t["beschreibung"].replace("BESCHREIBUNG_HIER", beschreibung)
    return {
        "name": name,
        "beschreibung": beschreibung,
        "schedule_cron": None,
        "collection": f"job_{name}",
        "bericht_pfad": f"~/Desktop/M1_DEPLOYMENT/berichte/{name}.md",
        "benoetigt_freigabe": [],
        "erlaubte_pfade": list(pfade or []),
        **roh,
    }


def per_llm_entwerfen(beschreibung: str, name: str) -> dict:
    """Laesst das lokale Modell einen Job-Entwurf bauen. Faellt bei Problemen
    auf die Recherche-Vorlage zurueck - lieber ein simpler funktionierender
    Ablauf als ein kaputter."""
    import requests

    vorlage_beispiel = json.dumps(aus_vorlage("recherche", "beispiel", "Beispielziel"), indent=2, ensure_ascii=False)
    prompt = (
        "Du erstellst eine Ablauf-Definition (JSON) fuer einen lokalen Agenten-Harness.\n"
        f"Aufgabe des Nutzers: {beschreibung}\n\n"
        f"Halte dich exakt an dieses Format:\n{vorlage_beispiel}\n\n"
        "Regeln: 2-4 Agents, jeder Task braucht 'erwartete_ausgabe' die mit \"Mexla,\" beginnt. "
        "Werkzeuge nur 'searxng' oder 'dateien_lesen'. Bei 'dateien_lesen' MUSS 'erlaubte_pfade' gesetzt sein. "
        "Antworte NUR mit dem JSON, ohne Erklaerung und ohne Markdown-Zaun."
    )
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": get_model_for_job(1), "prompt": prompt, "stream": False, "format": "json"},
            timeout=180,
        )
        text = resp.json().get("response", "")
        job = json.loads(text)
        job["name"] = name
        job["schedule_cron"] = None  # ein Ablauf darf sich nie selbst einplanen
        probleme = pruefe_job(job, beim_anlegen=True)
        if probleme:
            print("Der Entwurf des Modells war fehlerhaft:")
            for p in probleme:
                print(f"  - {p}")
            print("-> Nutze stattdessen die gepruefte Vorlage 'recherche'.")
            return aus_vorlage("recherche", name, beschreibung)
        return job
    except Exception as e:
        print(f"Modell nicht erreichbar oder Antwort unbrauchbar ({e}).")
        print("-> Nutze stattdessen die gepruefte Vorlage 'recherche'.")
        return aus_vorlage("recherche", name, beschreibung)


def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        print(f"Vorlagen: {', '.join(VORLAGEN)}")
        return

    if args[0] == "--vorlage":
        if len(args) < 3:
            print("Nutzung: neuer_job.py --vorlage <vorlage> <jobname> [--pfad <ordner>] [beschreibung]")
            print(f"Vorlagen: {', '.join(VORLAGEN)}")
            print("Die Vorlage 'dateicheck' braucht --pfad (Ordner, den der Ablauf lesen darf).")
            return
        vorlage, name = args[1], saubere_name(args[2])
        if name != args[2]:
            print(f"Hinweis: Name '{args[2]}' bereinigt zu '{name}' (nur a-z, 0-9, _ erlaubt).")
        rest = args[3:]
        pfade = []
        while "--pfad" in rest:
            i = rest.index("--pfad")
            if i + 1 >= len(rest):
                print("FEHLER: --pfad ohne Ordnerangabe.")
                sys.exit(1)
            pfade.append(rest[i + 1])
            rest = rest[:i] + rest[i + 2:]
        beschreibung = " ".join(rest) or f"Ablauf '{name}'"
        job = aus_vorlage(vorlage, name, beschreibung, pfade)
    else:
        beschreibung = " ".join(args)
        name = saubere_name(beschreibung)
        print(f"Entwerfe Ablauf '{name}' mit dem lokalen Modell ...")
        job = per_llm_entwerfen(beschreibung, name)

    probleme = pruefe_job(job, beim_anlegen=True)
    if probleme:
        print("FEHLER - Entwurf ist ungueltig:")
        for p in probleme:
            print(f"  - {p}")
        sys.exit(1)

    JOBS_DIR.mkdir(parents=True, exist_ok=True)
    ziel = JOBS_DIR / f"{job['name']}.json"
    if ziel.exists():
        print(f"'{ziel}' existiert bereits - nichts ueberschrieben.")
        sys.exit(1)
    ziel.write_text(json.dumps(job, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\nAblauf gespeichert: {ziel}")
    print("Validierung bestanden. Er laeuft NICHT automatisch - erst testen:")
    print(f"    python3 crew_generic.py {job['name']}")
    print("Wenn er taugt, 'schedule_cron' in der JSON setzen (z.B. \"0 9 * * 1\").")


if __name__ == "__main__":
    main()
