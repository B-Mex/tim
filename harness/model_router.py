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
KLASSE_3 = "qwen3.5:9b"       # klein/schnell: Reviews, Formatchecks, Canary
# Bis 21.08.2026 stand in Klasse 3 "llama-fast" (erfand eine Zeilenzahl),
# danach gpt-oss:20b. Der Benchmark vom 23.08. zeigte bei gpt-oss zweimal
# eine LEERE Antwort nach minutenlangem Denken - fuer Formatchecks und
# Canary untragbar, dort muss verlaesslich etwas zurueckkommen.
# qwen3.5:9b: 14/14, 30.5 Tok/s, 4.8 s Ladezeit, besteht die
# Ehrlichkeitsfalle (erfindet keine Zeilenzahlen).


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


if __name__ == "__main__":
    print(f"Freier RAM: {freier_ram_gb():.1f} GB (psutil {'gefunden' if psutil else 'FEHLT - pip3 install psutil'})")
    for klasse in (1, 2, 3):
        print(f"Job-Klasse {klasse} -> {get_model_for_job(klasse)}")
