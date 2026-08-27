#!/usr/bin/env python3
"""Graceful Degradation: waehlt je nach freiem RAM ein passendes Modell.

Siehe VOLLAUSBAU_SYSTEM.md Phase F3 ("Modell-Klassen fuer den Harness").
Modellnamen muessen zu denen passen, die 3_MAC_modelle_importieren.sh /
8_MAC_muse_glimmer.sh in Ollama anlegen.
"""

import json
import urllib.error
import urllib.request

try:
    import psutil
except ImportError:
    psutil = None

# Modell-Klassen. Stand 23.08.2026, gemessen mit modell_benchmark.py
# (14 Pruefungen; Bericht: berichte/modell_benchmark_2026-08-23_korrigiert.md).
# Klasse 1 und 2 sind bewusst dasselbe Modell: qwen3.6:35b-a3b (14/14,
# 42.6 Tok/s) schlaegt qwen3-coder (12/14) und qwen3-general (12/14) in
# Qualitaet UND Tempo - zwei getrennte grosse Modelle wuerden sich nur
# gegenseitig aus dem Speicher werfen.
KLASSE_1 = "qwen3.6:35b-a3b"  # stark: Recherche, Implementierung, Controller
KLASSE_2 = "qwen3.6:35b-a3b"  # mittel: komplexe Reviews, Planung
# ACHTUNG bei Klasse 3: Sie ist die SPEICHERSPARENDE Klasse, nicht die
# schnelle. Der Kommentar hier behauptete bis zum 24.08.2026 "klein/
# schnell" - gemessen ist das Gegenteil: qwen3.6:35b-a3b laeuft mit
# 42.6 Tok/s, qwen3.5:9b mit 30.5, bei gleicher Punktzahl (14/14). Das
# grosse Modell ist ein MoE mit wenigen aktiven Parametern und deshalb
# schneller, obwohl es groesser ist. Die Messung stand die ganze Zeit
# drei Zeilen weiter oben.
# Klein bleibt trotzdem richtig fuer Klasse 3: Das kleine Modell LAEDT
# schneller und passt, wenn der Speicher knapp ist. Fuer kurze
# Formatchecks und den Canary zaehlt die Ladezeit mehr als der
# Durchsatz.
#
# Stand 27.08.2026: laguna-xs-2.1. Die Zuordnung stammt aus dem ABITUR
# vom 26./27.08. (bestand als einziges kleines Modell alle
# Vorpruefungen, Injection 10 von 10 sauber; MoE mit wenigen aktiven
# Parametern), NICHT aus einem Benchmark-Vergleichslauf - der steht fuer
# laguna noch aus (Befund A2 der Gegenpruefung). Vorgeschichte: bis
# 21.08. "llama-fast" (erfand eine Zeilenzahl), dann gpt-oss:20b (leere
# Antworten nach langem Denken), dann qwen3.5:9b - am 26.08. geloescht,
# weil es im Kettentest den fuenften von fuenf Schritten erfand.
KLASSE_3 = "laguna-xs-2.1"    # speichersparend: Formatchecks, Canary, Verdichtung


def installierte_modelle(timeout: float = 5.0) -> list:
    """Alle bei Ollama INSTALLIERTEN Modellnamen (/api/tags).

    Nicht zu verwechseln mit modell_geladen (/api/ps, im Speicher):
    Ein geloeschtes Modell fiel bisher durch dasselbe Loch wie ein bloss
    entladenes - und die Verdichtung lief dann in einen 404-Notnagel,
    der die Gespraechsmitte zerstueckelte (Review-Befund vom 27.08.).
    """
    with urllib.request.urlopen("http://127.0.0.1:11434/api/tags",
                                timeout=timeout) as antwort:
        daten = json.loads(antwort.read().decode("utf-8"))
    return [m.get("name", "") for m in daten.get("models", [])]


def modelle_pruefen(namen: list) -> list:
    """Welche Klassenmodelle fehlen in der uebergebenen Bestandsliste?

    Bewusst mit uebergebener Liste statt Live-Abfrage: So bleibt die
    Funktion im Selbsttest ohne Betriebsdaten pruefbar. Den Live-Lauf
    macht der woechentliche __main__-Diagnoselauf.
    """
    fehlend = []
    for klasse, modell in (("Klasse 1", KLASSE_1), ("Klasse 2", KLASSE_2),
                           ("Klasse 3", KLASSE_3)):
        if not any(n.startswith(modell) for n in namen):
            fehlend.append("%s: %s" % (klasse, modell))
    return fehlend


def freier_ram_gb() -> float:
    if psutil is None:
        return 99.0  # psutil fehlt -> optimistisch annehmen, kein erzwungenes Downgrade
    return psutil.virtual_memory().available / 1e9


def modell_geladen(name: str) -> bool:
    """Haelt Ollama das Modell schon im Speicher? Dann kostet es keinen
    neuen RAM - der Frei-RAM-Wert wuerde sonst faelschlich degradieren,
    denn das geladene Modell selbst drueckt ihn ja nach unten."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/ps",
                                    timeout=3) as antwort:
            daten = json.loads(antwort.read().decode("utf-8"))
        return any((m.get("name") or "").startswith(name)
                   for m in daten.get("models", []))
    except (urllib.error.URLError, OSError, ValueError):
        return False


def get_model_for_job(job_klasse: int) -> str:
    """job_klasse: 1 (stark) | 2 (mittel) | 3 (klein)."""
    ram_frei = freier_ram_gb()

    if ram_frei < 5 and not modell_geladen(KLASSE_1):
        return KLASSE_3  # Notfall: nur noch das kleine Modell passt sicher
    if job_klasse in (1, 2):
        # Das 23-GB-Modell braucht geladen rund 25 GB (GPU-wired). Ist es
        # schon im Speicher, kostet es nichts mehr; sonst nur laden, wenn
        # wirklich Platz ist.
        if modell_geladen(KLASSE_1) or ram_frei > 20:
            return KLASSE_1 if job_klasse == 1 else KLASSE_2
        return KLASSE_3
    return KLASSE_3


def selbsttest() -> int:
    fehler = []

    def pruefe(b, t, z=""):
        print("  %-7s %s%s" % ("ok" if b else "FEHLER", t,
                               "" if b else "  <- " + str(z)))
        if not b:
            fehler.append(t)

    print("model_router Selbsttest:")
    # Bestandsliste vom 27.08.2026 als Fixture - KEINE Live-Abfrage
    bestand = ["qwen3.6:35b-a3b", "laguna-xs-2.1:latest",
               "nemotron-3.5-lightning:latest", "muse-glimmer:latest"]
    pruefe(modelle_pruefen(bestand) == [],
           "alle Klassenmodelle stehen im Bestand vom 27.08.",
           str(modelle_pruefen(bestand)))
    pruefe(any("Klasse 3" in f for f in
               modelle_pruefen(["qwen3.6:35b-a3b"])),
           "ein fehlendes Klasse-3-Modell wird gemeldet")
    pruefe(modelle_pruefen([]) != [], "leerer Bestand meldet alle Klassen")
    pruefe(KLASSE_3 != "qwen3.5:9b",
           "Klasse 3 zeigt nicht mehr auf das geloeschte qwen3.5:9b")
    print("\n%s" % ("Alle Pruefungen bestanden." if not fehler
                    else "%d FEHLER." % len(fehler)))
    return 1 if fehler else 0


if __name__ == "__main__":
    import sys
    if "--selbsttest" in sys.argv:
        sys.exit(selbsttest())
    print(f"Freier RAM: {freier_ram_gb():.1f} GB (psutil {'gefunden' if psutil else 'FEHLT - pip3 install psutil'})")
    for klasse in (1, 2, 3):
        print(f"Job-Klasse {klasse} -> {get_model_for_job(klasse)}")
    # Diagnose (laeuft woechentlich ueber 14_MAC_selbsttests.sh): Sind
    # die Klassenmodelle ueberhaupt noch installiert? FEHLER ist hier
    # PROBLEM_MARKER der Montagsroutine; ein stummer Ollama ist dagegen
    # nur ein HINWEIS - nicht pruefbar ist kein Befund.
    try:
        namen = installierte_modelle()
    except (urllib.error.URLError, OSError, ValueError) as f:
        print("HINWEIS: Ollama nicht erreichbar (%s) - Modellbestand "
              "nicht pruefbar." % type(f).__name__)
        raise SystemExit(0)
    fehlend = modelle_pruefen(namen)
    if fehlend:
        print("FEHLER: Klassenmodell(e) nicht installiert: "
              + "; ".join(fehlend))
        raise SystemExit(1)
    print("Alle Klassenmodelle installiert.")
