#!/usr/bin/env python3
"""Passt dieses Modell auf diese Maschine? Einmal laden, einmal messen.

Warum es das gibt
-----------------
nemotron-3.5-lightning wurde installiert, bestand am 27.08.2026 das
Abitur - und machte Tim taub. Nicht weil es schlecht war, sondern weil
es nicht passt: Mit 25 GB steht der festgenagelte Speicher bei 27,7 GiB
gegen eine harte Grenze von 25,28 GiB. Ist die erreicht, scheitert
JEDES weitere mlock im System, auch der 80-KiB-Puffer, den CoreAudio
fuer einen Aufnahmestrom braucht. Der Fehler kommt dann als PortAudio
-9986 und sieht aus wie ein kaputtes Mikrofon.

Das haette man vor der Installation in zwei Minuten messen koennen.
Dieses Werkzeug ist diese zwei Minuten.

Was gemessen wird
-----------------
Der festgenagelte Speicher (vm_stat "Pages wired down") vor und
waehrend das Modell geladen ist - mit dem num_ctx, das im Betrieb
gelten soll. Der Aufschlag ist KEINE Konstante: Dieselbe laguna lag
einmal bei 21,31 und einmal bei 23,86 GiB, Unterschied war allein das
Kontextfenster. Deshalb wird mit dem echten Wert gemessen, nicht
gerechnet.

Aufruf:
    speicherprobe.py <modell> [num_ctx]
    speicherprobe.py --selbsttest
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
# Wieviel Reserve unter der Grenze bleiben muss, damit CoreAudio seinen
# Puffer noch bekommt und ein zweiter Strom (Sprachausgabe) auch. Der
# Puffer selbst ist winzig (80 KiB); die Reserve ist gegen die
# Schwankung des Modells waehrend einer langen Antwort.
RESERVE_GIB = 1.0


def wired_gib() -> float:
    """Festgenagelter Speicher in GiB - oder -1, wenn nicht lesbar."""
    try:
        aus = subprocess.run(["vm_stat"], capture_output=True, text=True,
                             timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return -1.0
    m = re.search(r"Pages wired down:\s+([\d.,]+)", aus)
    if not m:
        return -1.0
    return int(re.sub(r"[.,]", "", m.group(1))) * 16384 / 2 ** 30


def grenze_gib() -> float:
    """vm.global_user_wire_limit in GiB - oder -1."""
    try:
        aus = subprocess.run(["sysctl", "-n", "vm.global_user_wire_limit"],
                             capture_output=True, text=True, timeout=10).stdout
        return int(aus.strip()) / 2 ** 30
    except (OSError, ValueError, subprocess.SubprocessError):
        return -1.0


def urteilen(wired: float, grenze: float, reserve: float = RESERVE_GIB) -> dict:
    """Passt es? Reine Rechnung, damit sie ohne Modell pruefbar ist."""
    if wired < 0 or grenze < 0:
        return {"urteil": "UNKLAR", "passt": False,
                "grund": "Speicher oder Grenze nicht lesbar"}
    frei = grenze - wired
    if frei < 0:
        return {"urteil": "SPRENGT", "passt": False, "frei": round(frei, 2),
                "grund": ("festgenagelt %.2f GiB ueber der Grenze %.2f GiB - "
                          "Tim wird taub, sobald dieses Modell liegt"
                          % (wired, grenze))}
    if frei < reserve:
        return {"urteil": "KNAPP", "passt": False, "frei": round(frei, 2),
                "grund": ("nur %.2f GiB Reserve (noetig %.2f) - reicht fuer "
                          "den Aufnahmestrom, aber nicht fuer Schwankung"
                          % (frei, reserve))}
    return {"urteil": "PASST", "passt": True, "frei": round(frei, 2),
            "grund": "%.2f GiB Reserve unter der Grenze" % frei}


def _ollama(pfad: str, koerper: dict | None = None, geduld: int = 600):
    daten = json.dumps(koerper).encode("utf-8") if koerper is not None else None
    a = urllib.request.Request(OLLAMA + pfad, data=daten, method="POST"
                               if daten else "GET",
                               headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(a, timeout=geduld) as r:
        return json.loads(r.read().decode("utf-8"))


def messen(modell: str, num_ctx: int = 32768) -> dict:
    """Modell laden, messen, wieder entladen. Fragt EINE kurze Frage."""
    grenze = grenze_gib()
    vorher = wired_gib()
    print("Grenze (vm.global_user_wire_limit): %.2f GiB" % grenze)
    print("Festgenagelt vorher:                %.2f GiB" % vorher)
    print("Lade %s mit num_ctx=%d ..." % (modell, num_ctx))
    _ollama("/api/chat", {"model": modell, "stream": False,
                          "messages": [{"role": "user", "content": "Sag Hallo."}],
                          "options": {"num_ctx": num_ctx, "num_predict": 16},
                          "keep_alive": "5m"})
    spitzen = []
    for _ in range(6):
        spitzen.append(wired_gib())
        time.sleep(1.0)
    waehrend = max(spitzen)
    print("Festgenagelt mit Modell (Spitze):   %.2f GiB" % waehrend)
    print("Aufschlag durch das Modell:         %.2f GiB" % (waehrend - vorher))
    u = urteilen(waehrend, grenze)
    print()
    print("URTEIL: %s - %s" % (u["urteil"], u["grund"]))
    try:
        _ollama("/api/chat", {"model": modell, "messages": [],
                              "keep_alive": 0}, geduld=60)
        print("(Modell wieder entladen)")
    except Exception:
        print("(Entladen nicht bestaetigt - 'ollama stop %s' von Hand)" % modell)
    u.update(modell=modell, num_ctx=num_ctx, grenze=round(grenze, 2),
             vorher=round(vorher, 2), waehrend=round(waehrend, 2))
    return u


def selbsttest() -> int:
    fehler = []

    def pruefe(b, t, z=""):
        print("  %-7s %s%s" % ("ok" if b else "FEHLER", t,
                               "" if b else "  <- " + str(z)))
        if not b:
            fehler.append(t)

    print("speicherprobe Selbsttest (ohne Modell, ohne Ollama):")
    # Die echten Zahlen vom 31.08./01.09.2026 als Fixture.
    pruefe(urteilen(27.7, 25.28)["urteil"] == "SPRENGT",
           "nemotron (27,70 GiB) sprengt die Grenze - der echte Fall")
    pruefe(not urteilen(27.7, 25.28)["passt"], "und gilt als nicht tauglich")
    # laguna mit num_ctx 65536 liegt bei 23,86 GiB - 1,42 GiB Reserve.
    # Das gilt als PASST, und zwar nicht aus Nachsicht, sondern
    # gemessen: Mit genau dieser Lage hoerte Tim 32 Mal je Minute,
    # ohne einen einzigen -9986. Die Erwartung "KNAPP" stand hier
    # zuerst und war meine Vermutung, nicht die Messung.
    pruefe(urteilen(23.86, 25.28)["urteil"] == "PASST",
           "laguna mit grossem Fenster (23,86) passt - nachgemessen")
    pruefe(urteilen(24.8, 25.28)["urteil"] == "KNAPP",
           "0,48 GiB Reserve ist KNAPP - genug fuer den Puffer, nicht "
           "fuer Schwankung")
    pruefe(urteilen(19.03, 25.28)["urteil"] == "PASST",
           "muse-glimmer (19,03 GiB) passt")
    pruefe(urteilen(21.31, 25.28)["passt"] is True,
           "laguna mit kleinem Fenster (21,31) passt")
    pruefe(urteilen(-1, 25.28)["urteil"] == "UNKLAR",
           "unlesbarer Speicher ergibt UNKLAR, nicht PASST")
    pruefe(urteilen(24.5, 25.28)["passt"] is False,
           "im Zweifel gegen das Modell: 0,78 GiB Reserve reichen nicht")
    # Und die Messfunktionen selbst duerfen nicht abstuerzen.
    pruefe(isinstance(wired_gib(), float), "wired_gib liefert eine Zahl")
    pruefe(isinstance(grenze_gib(), float), "grenze_gib liefert eine Zahl")
    print("\n%s" % ("Alle Pruefungen gruen." if not fehler
                    else "%d FEHLER." % len(fehler)))
    return 1 if fehler else 0


def main(argumente: list) -> int:
    if "--selbsttest" in argumente:
        return selbsttest()
    rest = [a for a in argumente if not a.startswith("-")]
    if not rest:
        print(__doc__)
        return 0
    ctx = int(rest[1]) if len(rest) > 1 else 32768
    u = messen(rest[0], ctx)
    return 0 if u["passt"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
