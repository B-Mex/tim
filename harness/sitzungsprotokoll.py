#!/usr/bin/env python3
"""Aus Claude-Sitzungen lesbare Protokolle machen - gegen das Vergessen.

Warum es das gibt (Mexla, 30.08.2026): "kannst du unseren chat auch
speichern? also inhaltlich in nem ordner am pc, das neue sitzungen und
du, vorallem nach der komprimierung nicht die haelfte vergessen haben?"

Die Rohdaten liegen laengst da - 47 Transkripte, 134 MB unter
~/.claude/projects/. Nur sind sie unbrauchbar: JSONL, zu drei Vierteln
Werkzeugaufrufe, und niemand liest 134 MB. Wenn eine Sitzung nach der
Kontext-Verdichtung weiterarbeitet, ist der Anfang weg; eine NEUE
Sitzung faengt ohnehin bei null an.

**Was hier herauskommt, ist bewusst kein Gespraechsmitschnitt.** Es sind
Mexlas eigene Nachrichten in ihrer Reihenfolge - denn dort stehen die
Entscheidungen, und die sind das, was verloren geht. "Nicht tot
optimieren", "die Latte darf keine Decke werden", "volle Shell": Solche
Saetze faellt niemand zweimal, und kein Werkzeugprotokoll gibt sie
wieder. Was Claude geantwortet hat, laesst sich rekonstruieren; was
Mexla wollte, nicht.

Dazu kommt, was sich hart belegen laesst: die Commits des Tages und die
Dateien, die angefasst wurden.

NUR LESEND. Schreibt ausschliesslich in den Zielordner, fasst weder
Transkripte noch das Repo an.

Aufrufe:
    sitzungsprotokoll.py                 alle neuen Sitzungen schreiben
    sitzungsprotokoll.py --alle          auch schon geschriebene neu
    sitzungsprotokoll.py --tage 3        nur die letzten 3 Tage
    sitzungsprotokoll.py --selbsttest    Pruefungen (nur Fixtures)
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

TRANSKRIPTE = Path.home() / ".claude" / "projects" / "-Users-mexla"
ZIEL = Path.home() / "Desktop" / "M1_DEPLOYMENT" / "sitzungen"
REPO = Path("/opt/ki-server")

# Nachrichten, die NICHT von Mexla stammen, obwohl sie als "user"
# gespeichert sind. Claude Code legt hier Systemmeldungen, Werkzeug-
# ergebnisse und Nachrichten anderer Sitzungen ab. Wer die mitnimmt,
# bekommt ein Protokoll voller Maschinentext und uebersieht darin die
# zwei Saetze, auf die es ankommt.
KEIN_MEXLA = (
    "<scheduled-task", "<command-name>", "<local-command-",
    "<cross-session-message", "<system-reminder", "<task-notification",
    "[Request interrupted", "<user-prompt-submit-hook",
    "Caveat: The messages below", "This is an automated",
)

# Kurze Bestaetigungen tragen nichts zum Gedaechtnis bei.
FUELLWOERTER = {"ok", "ja", "nein", "gut", "danke", "los", "weiter",
                "passt", "top", "jup", "jo", "mach", "gerne", "bitte"}


def ist_mexla(text: str) -> bool:
    """Stammt diese Nachricht wirklich von Mexla?"""
    if not text or not text.strip():
        return False
    kopf = text.lstrip()[:200]
    return not any(m in kopf for m in KEIN_MEXLA)


def ist_gehaltvoll(text: str) -> bool:
    """Traegt die Nachricht etwas bei, das man sich merken muesste?

    Kurze Zustimmungen nicht - aber ein kurzer Satz mit Inhalt schon.
    Die Grenze liegt bewusst niedrig: Lieber eine Zeile zu viel als
    eine Entscheidung zu wenig.
    """
    blank = text.strip().strip(".!?").lower()
    if blank in FUELLWOERTER:
        return False
    return len(blank) >= 4


def nachrichten_lesen(datei: Path) -> list:
    """(zeit, text) je echter Mexla-Nachricht, in Reihenfolge."""
    raus = []
    try:
        roh = datei.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return raus
    for zeile in roh.splitlines():
        zeile = zeile.strip()
        if not zeile:
            continue
        try:
            n = json.loads(zeile)
        except ValueError:
            continue
        if n.get("type") != "user" or n.get("isSidechain"):
            continue
        m = n.get("message") or {}
        inhalt = m.get("content")
        # Der Inhalt ist mal ein String, mal eine Liste von Bloecken.
        if isinstance(inhalt, list):
            teile = [t.get("text", "") for t in inhalt
                     if isinstance(t, dict) and t.get("type") == "text"]
            inhalt = "\n".join(teile)
        if not isinstance(inhalt, str):
            continue
        if not ist_mexla(inhalt) or not ist_gehaltvoll(inhalt):
            continue
        raus.append((str(n.get("timestamp") or "")[:19], inhalt.strip()))
    return raus


def commits_am_tag(tag: str) -> list:
    """Die Commits dieses Tages - hart belegbar, anders als Erinnerung."""
    try:
        lauf = subprocess.run(
            ["git", "-C", str(REPO), "log", "--all", "--no-merges",
             "--since=%s 00:00" % tag, "--until=%s 23:59" % tag,
             "--pretty=format:%h %s"],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return []
    return [z for z in (lauf.stdout or "").splitlines() if z.strip()]


def _tag_von(zeit: str) -> str:
    return zeit[:10] if len(zeit) >= 10 else "unbekannt"


def protokoll_bauen(datei: Path, nachrichten: list) -> str:
    """Der Text des Protokolls. Reine Funktion - deshalb pruefbar."""
    tage = sorted({_tag_von(z) for z, _ in nachrichten if z})
    von, bis = (tage[0], tage[-1]) if tage else ("unbekannt", "unbekannt")
    kopf = [
        "# Sitzung %s" % datei.stem[:8],
        "",
        "Quelle: `%s`" % datei.name,
        "Zeitraum: %s%s" % (von, "" if von == bis else " bis " + bis),
        "Nachrichten von Mexla: %d" % len(nachrichten),
        "",
        "> Dies sind **Mexlas eigene Nachrichten**, nicht das ganze",
        "> Gespraech. Dort stehen die Entscheidungen - und die sind das,",
        "> was nach einer Kontext-Verdichtung fehlt. Was Claude",
        "> geantwortet hat, steht in den Aenderungsdokus unter `docs/`.",
        "",
    ]
    for tag in tage:
        kopf.append("---")
        kopf.append("")
        kopf.append("## %s" % tag)
        kopf.append("")
        for zeit, text in nachrichten:
            if _tag_von(zeit) != tag:
                continue
            uhr = zeit[11:16] if len(zeit) >= 16 else "--:--"
            # Mehrzeiliges als Block, damit die Form erhalten bleibt.
            if "\n" in text:
                kopf.append("**%s**" % uhr)
                kopf.append("")
                for z in text.splitlines():
                    kopf.append("> " + z if z.strip() else ">")
                kopf.append("")
            else:
                kopf.append("**%s** — %s" % (uhr, text))
                kopf.append("")
        cm = commits_am_tag(tag)
        if cm:
            kopf.append("### Commits an diesem Tag (%d)" % len(cm))
            kopf.append("")
            for z in cm:
                kopf.append("- `%s`" % z)
            kopf.append("")
    return "\n".join(kopf).rstrip() + "\n"


def main(argumente: list) -> int:
    if "--selbsttest" in argumente:
        return selbsttest()

    alle = "--alle" in argumente
    grenze = None
    if "--tage" in argumente:
        try:
            n = int(argumente[argumente.index("--tage") + 1])
            grenze = datetime.now() - timedelta(days=n)
        except (IndexError, ValueError):
            print("FEHLER: --tage braucht eine Zahl.")
            return 1

    if not TRANSKRIPTE.is_dir():
        print("FEHLER: %s gibt es nicht." % TRANSKRIPTE)
        return 2
    try:
        ZIEL.mkdir(parents=True, exist_ok=True)
    except OSError as f:
        print("FEHLER: %s nicht anlegbar (%s)." % (ZIEL, f))
        return 2

    geschrieben = uebersprungen = leer = 0
    for datei in sorted(TRANSKRIPTE.glob("*.jsonl"),
                        key=lambda p: p.stat().st_mtime):
        if grenze and datetime.fromtimestamp(datei.stat().st_mtime) < grenze:
            continue
        nachrichten = nachrichten_lesen(datei)
        if not nachrichten:
            leer += 1
            continue
        tage = sorted({_tag_von(z) for z, _ in nachrichten if z})
        name = "%s_%s.md" % (tage[0] if tage else "unbekannt", datei.stem[:8])
        ziel = ZIEL / name
        if ziel.exists() and not alle:
            uebersprungen += 1
            continue
        try:
            ziel.write_text(protokoll_bauen(datei, nachrichten),
                            encoding="utf-8")
            geschrieben += 1
        except OSError as f:
            print("  WARNUNG: %s nicht schreibbar (%s)" % (ziel.name, f))

    print("Sitzungsprotokolle in %s" % ZIEL)
    print("  geschrieben:   %d" % geschrieben)
    print("  schon da:      %d" % uebersprungen)
    print("  ohne Inhalt:   %d (nur Maschinentext)" % leer)
    return 0


def selbsttest() -> int:
    fehler = 0

    def pruefe(bedingung, was, zusatz=""):
        nonlocal fehler
        if bedingung:
            print("  ok      %s" % was)
        else:
            print("  FEHLER  %s%s" % (was, "  [%s]" % zusatz if zusatz else ""))
            fehler += 1

    print("Sitzungsprotokoll Selbsttest (nur Fixtures):")

    # Fremdtext darf NICHT als Mexla-Nachricht durchgehen. Ohne diese
    # Trennung besteht das Protokoll zu drei Vierteln aus Maschinentext,
    # und die zwei Saetze, auf die es ankommt, gehen darin unter.
    for fremd in ('<scheduled-task name="x">Lauf</scheduled-task>',
                  "<command-name>/compact</command-name>",
                  '<cross-session-message from="y">Hallo</cross-session-message>',
                  "<system-reminder>Merke</system-reminder>",
                  "Caveat: The messages below were generated",
                  "This is an automated run of a scheduled task."):
        pruefe(not ist_mexla(fremd),
               "kein Mexla: %s" % fremd[:36])
    pruefe(ist_mexla("nicht tot optimieren, will kein AI optimation hole"),
           "eine echte Nachricht geht durch")
    # Gegenprobe, damit der Filter nicht zu weit greift: Ein Satz, der
    # ueber eine Systemmeldung SPRICHT, ist trotzdem Mexlas Satz.
    pruefe(ist_mexla("was bedeutet die system-reminder meldung da?"),
           "ein Satz UEBER eine Systemmeldung zaehlt weiter als Mexlas")

    pruefe(not ist_gehaltvoll("ok"), "'ok' traegt nichts bei")
    pruefe(not ist_gehaltvoll("Ja."), "'Ja.' traegt nichts bei")
    pruefe(ist_gehaltvoll("los, mach weiter mit dem plan"),
           "ein kurzer Satz mit Inhalt zaehlt")
    pruefe(ist_gehaltvoll("weg 1"), "auch eine knappe Entscheidung zaehlt")

    # Der Protokolltext selbst
    text = protokoll_bauen(Path("abcdef12-3456.jsonl"), [
        ("2026-08-29T07:11:50", "hast du jetzt Shell zugriff?"),
        ("2026-08-29T14:20:00", "mehrzeilig\nzweite Zeile"),
        ("2026-08-30T19:00:00", "der laeuft grad wieder"),
    ])
    pruefe("## 2026-08-29" in text and "## 2026-08-30" in text,
           "Tage werden getrennt ueberschrieben")
    pruefe("**07:11** — hast du jetzt Shell zugriff?" in text,
           "einzeilige Nachricht mit Uhrzeit")
    pruefe("> mehrzeilig" in text and "> zweite Zeile" in text,
           "mehrzeilige Nachricht bleibt als Block erhalten")
    pruefe("Nachrichten von Mexla: 3" in text, "die Anzahl stimmt")
    pruefe("nicht das ganze" in text,
           "der Kopf sagt ehrlich, dass es kein Mitschnitt ist")

    # Nur lesend: Die Zielpfade duerfen nirgends ins Repo oder in die
    # Transkripte zeigen.
    pruefe(str(ZIEL).endswith("M1_DEPLOYMENT/sitzungen"),
           "geschrieben wird nur in den Sitzungsordner", str(ZIEL))
    pruefe(not str(ZIEL).startswith(str(REPO)),
           "und NIEMALS ins Git-Repo - die Protokolle sind privat")
    pruefe(str(REPO) not in str(ZIEL) and str(TRANSKRIPTE) not in str(ZIEL),
           "und nicht in die Transkripte selbst")

    print()
    if fehler:
        print("%d Fehler." % fehler)
        return 1
    print("Alle Pruefungen gruen.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
